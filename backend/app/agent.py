"""Autopilot: an agent loop driven by a model served ON an instance.

The brain is any OpenAI-compatible model server Manifold itself launched
(vllm-serve). Each turn, the loop sends the conversation to the brain over
the managed SSH connection, expects EXACTLY ONE JSON action back, executes
it against Manifold's own guarded operations, and feeds the observation to
the next turn. GPU A literally manages GPU B.

Safety model — same philosophy as the MCP bridge, one level deeper:
- The action surface is a fixed allowlist below. There is no shell action,
  no arbitrary HTTP, no self-modification.
- Every launch goes through orchestrator.request_launch: budget,
  concurrency, and region guards bind the autopilot exactly as they bind
  the dashboard. A rejection comes back as an observation the model can
  read and adapt to.
- Hard step cap per run (config: autopilot.max_steps_cap), a wait cap so
  the loop cannot sleep forever, and a per-turn chat timeout.
- Every step is persisted (agent_steps) and audited (actor "autopilot"),
  so the dashboard shows the loop as it happens. Runs are cancellable.
- A protocol that small open-weight models can actually follow: one JSON
  object per turn, errors returned as data. Malformed output is bounced
  back with a correction hint; three consecutive failures end the run.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from .config import Settings
from .db import Database, utcnow
from .dispatcher import ParameterError, coerce_parameters
from .lambda_api import LambdaAPIError
from .model_client import ModelClientError
from .orchestrator import LaunchRejected, Orchestrator, TerminationBlocked
from .task_queue import TaskQueue
from .templates import JobTemplate

logger = logging.getLogger("manifold.autopilot")

MAX_CONSECUTIVE_FAILURES = 3
MAX_HISTORY_MESSAGES = 40      # system prompt + trailing window


def find_serving_task(queue: TaskQueue, templates: dict[str, JobTemplate],
                      instance_id: str) -> dict | None:
    """The running task on this instance whose template publishes a port —
    i.e. a live model server. Single source of truth for 'is a model
    being served here', shared by the chat endpoints and the autopilot."""
    for task in queue.list():
        if task["status"] != "running" or task["instance_id"] != instance_id:
            continue
        template = templates.get(task["template"])
        if template is None or not template.ports:
            continue
        return {
            **task,
            "port": template.ports[0].host,
            "model_id": task["parameters"].get("model_id") or task["template"],
        }
    return None


def parse_action(text: str) -> tuple[dict | None, str | None]:
    """Extract the first JSON object with an "action" key from model text.

    Tolerates code fences and prose around the JSON — small models add
    both. Returns (parsed, None) or (None, error_for_the_model)."""
    cleaned = text.replace("```json", "```").replace("```", " ")
    decoder = json.JSONDecoder()
    idx = cleaned.find("{")
    while idx != -1:
        try:
            obj, _ = decoder.raw_decode(cleaned, idx)
        except json.JSONDecodeError:
            idx = cleaned.find("{", idx + 1)
            continue
        if isinstance(obj, dict) and isinstance(obj.get("action"), str):
            args = obj.get("args")
            if args is None:
                obj["args"] = {}
            elif not isinstance(args, dict):
                return None, '"args" must be a JSON object'
            return obj, None
        idx = cleaned.find("{", idx + 1)
    return None, (
        "Your reply contained no JSON action. Respond with exactly one "
        'JSON object: {"thought": "...", "action": "<name>", "args": {...}}'
    )


SYSTEM_PROMPT = """You are Manifold Autopilot, an agent operating a Lambda Cloud GPU \
orchestrator to accomplish the user's goal.

Respond with EXACTLY ONE JSON object per turn, nothing else:
{"thought": "<brief reasoning>", "action": "<name>", "args": {...}}

Actions:
- list_instance_types {} -> GPU types with $/hr and regions that have capacity
- list_instances {} -> running instances with connection state
- launch_gpu {"instance_type": str, "region": str, "filesystem": str} -> start a GPU (async; poll get_launch_status)
- get_launch_status {"launch_id": str} -> launching|retrying|booting|active|failed
- list_templates {} -> runnable job templates and their parameters
- run_job {"template": str, "parameters": {...}} -> queue a job on the connected instance
- get_job_status {"task_id": str} -> queued|running|succeeded|failed + outputs
- get_job_logs {"task_id": str, "tail": int} -> recent log lines
- sync_outputs {"instance_id": str} -> save ephemeral scratch to persistent storage
- terminate_instance {"instance_id": str, "force": bool} -> stop billing; force=false is blocked if unsaved files exist (then sync_outputs and retry)
- wait {"seconds": number} -> pause before polling again
- done {"summary": str} -> finish the run; ALWAYS end with this

Rules:
- Results arrive as JSON in the next user message. An "error" key means the
  action was refused (budget, concurrency, region guards) - read it and adapt;
  do not repeat a refused action unchanged.
- GPUs cost real money. Prefer the cheapest type that fits. Terminate
  instances you started once the work is finished (sync first if needed).
- One action per turn. Be decisive; you have a limited number of steps.

Goal: {goal}"""


class Autopilot:
    """Owns agent runs: starts the loop task, executes actions, records
    every step, and enforces the caps."""

    def __init__(self, settings: Settings, orchestrator: Orchestrator,
                 queue: TaskQueue, templates: dict[str, JobTemplate],
                 db: Database, *, sleep=asyncio.sleep):
        self.settings = settings
        self.orchestrator = orchestrator
        self.queue = queue
        self.templates = templates
        self.db = db
        self._sleep = sleep
        self.tasks: dict[str, asyncio.Task] = {}

    # -- lifecycle ------------------------------------------------------------------

    def start_run(self, *, goal: str, brain_instance_id: str,
                  brain_model: str, brain_port: int, max_steps: int) -> str:
        run_id = self.db.create_agent_run(
            goal=goal, brain_instance_id=brain_instance_id,
            brain_model=brain_model, max_steps=max_steps,
        )
        self.db.record_audit(
            "autopilot", "run_start",
            f"{run_id}: goal={goal[:120]!r} brain={brain_model} "
            f"on {brain_instance_id} (max {max_steps} steps)",
        )
        self.tasks[run_id] = asyncio.create_task(
            self._run_loop(run_id, goal, brain_instance_id, brain_model,
                           brain_port, max_steps)
        )
        return run_id

    def cancel_run(self, run_id: str) -> bool:
        task = self.tasks.get(run_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def stop(self) -> None:
        for task in self.tasks.values():
            task.cancel()
        for task in self.tasks.values():
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.tasks.clear()

    # -- the loop ---------------------------------------------------------------------

    async def _run_loop(self, run_id: str, goal: str, brain_instance_id: str,
                        brain_model: str, brain_port: int,
                        max_steps: int) -> None:
        messages: list[dict] = [
            # .replace, not .format: the prompt is full of literal JSON braces.
            {"role": "system", "content": SYSTEM_PROMPT.replace("{goal}", goal)},
            {"role": "user", "content": "Begin. What is your first action?"},
        ]
        failures = 0
        try:
            for seq in range(1, max_steps + 1):
                try:
                    reply = await asyncio.wait_for(
                        self._chat(brain_instance_id, brain_model,
                                   brain_port, messages),
                        timeout=self.settings.autopilot.chat_timeout_seconds,
                    )
                except (ModelClientError, asyncio.TimeoutError) as exc:
                    failures += 1
                    self._record(run_id, seq, thought="",
                                 action="__brain_error__", args={},
                                 result={"error": str(exc)})
                    if failures >= MAX_CONSECUTIVE_FAILURES:
                        self._finish(run_id, seq, "failed",
                                     error=f"brain unreachable: {exc}")
                        return
                    await self._sleep(2)
                    continue

                messages.append({"role": "assistant", "content": reply})
                parsed, parse_err = parse_action(reply)
                if parsed is None:
                    failures += 1
                    observation = {"error": parse_err}
                    self._record(run_id, seq, thought=reply[:300],
                                 action="__invalid__", args={},
                                 result=observation)
                    if failures >= MAX_CONSECUTIVE_FAILURES:
                        self._finish(run_id, seq, "failed",
                                     error="brain kept producing unparseable "
                                           "output")
                        return
                    messages.append({"role": "user",
                                     "content": json.dumps(observation)})
                    continue

                failures = 0
                thought = str(parsed.get("thought", ""))[:500]
                action, args = parsed["action"], parsed["args"]

                if action == "done":
                    summary = str(args.get("summary", ""))[:2000]
                    self._record(run_id, seq, thought=thought, action="done",
                                 args=args, result={"ok": True})
                    self._finish(run_id, seq, "succeeded", summary=summary)
                    return

                observation = await self._execute(action, args)
                self._record(run_id, seq, thought=thought, action=action,
                             args=args, result=observation)
                messages.append({"role": "user",
                                 "content": json.dumps(observation)})
                messages = self._trim(messages)

            self._finish(run_id, max_steps, "exhausted",
                         error=f"step limit ({max_steps}) reached before done")
        except asyncio.CancelledError:
            self.db.update_agent_run(
                run_id, status="cancelled", finished_at=utcnow(),
                error="cancelled by user",
            )
            self.db.record_audit("autopilot", "run_cancelled", run_id)
            raise
        except Exception as exc:   # never leave a run stuck 'running'
            logger.exception("autopilot run %s crashed", run_id)
            self.db.update_agent_run(
                run_id, status="failed", finished_at=utcnow(),
                error=f"internal error: {exc}",
            )

    @staticmethod
    def _trim(messages: list[dict]) -> list[dict]:
        if len(messages) <= MAX_HISTORY_MESSAGES:
            return messages
        # Keep the system prompt and the most recent window.
        return [messages[0]] + messages[-(MAX_HISTORY_MESSAGES - 1):]

    def _record(self, run_id: str, seq: int, *, thought: str, action: str,
                args: dict, result: dict) -> None:
        self.db.add_agent_step(run_id, seq, thought=thought, action=action,
                               args=args, result=result)
        self.db.update_agent_run(run_id, steps_taken=seq)
        outcome = "error" if result.get("error") else "ok"
        self.db.record_audit(
            "autopilot", action,
            f"run {run_id} step {seq}: {json.dumps(args)[:150]} -> {outcome}",
        )

    def _finish(self, run_id: str, steps: int, status: str, *,
                summary: str = "", error: str = "") -> None:
        self.db.update_agent_run(
            run_id, status=status, steps_taken=steps, finished_at=utcnow(),
            summary=summary or None, error=error or None,
        )
        self.db.record_audit(
            "autopilot", f"run_{status}",
            f"{run_id} after {steps} step(s)"
            + (f": {summary[:150]}" if summary else "")
            + (f": {error[:150]}" if error else ""),
        )

    # -- talking to the brain ------------------------------------------------------------

    async def _chat(self, instance_id: str, model: str, port: int,
                    messages: list[dict]) -> str:
        client = self.orchestrator.model_client_for(instance_id)
        if client is None:
            raise ModelClientError(
                f"no managed connection to brain instance {instance_id}"
            )
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": 2048,
            "temperature": 0.0,   # agent turns want determinism, not flair
        }
        parts: list[str] = []
        async for line in client.chat_stream(port, payload):
            line = line.strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            try:
                chunk = json.loads(line[len("data: "):])
            except json.JSONDecodeError:
                continue
            if chunk.get("error"):
                raise ModelClientError(str(chunk["error"]))
            delta = (chunk.get("choices") or [{}])[0].get("delta", {})
            parts.append(delta.get("content") or "")
        return "".join(parts)

    # -- the action surface ---------------------------------------------------------------

    async def _execute(self, action: str, args: dict) -> dict:
        """Run one action; ALL failures come back as {"error": ...} data so
        the model can read them. Nothing raises across this boundary except
        cancellation."""
        try:
            handler = getattr(self, f"_act_{action}", None)
            if handler is None:
                return {"error": f"unknown action '{action}'. Valid: "
                                 "list_instance_types, list_instances, "
                                 "launch_gpu, get_launch_status, "
                                 "list_templates, run_job, get_job_status, "
                                 "get_job_logs, sync_outputs, "
                                 "terminate_instance, wait, done"}
            return await handler(args)
        except asyncio.CancelledError:
            raise
        except LaunchRejected as exc:
            return {"error": exc.detail}
        except TerminationBlocked as exc:
            return {"blocked": True, "error": str(exc),
                    "unpersisted_files": exc.files}
        except LambdaAPIError as exc:
            return {"error": exc.message}
        except (KeyError, TypeError, ValueError) as exc:
            return {"error": f"bad arguments for {action}: {exc}"}
        except Exception as exc:
            logger.exception("autopilot action %s failed", action)
            return {"error": f"{action} failed internally: {exc}"}

    async def _act_list_instance_types(self, args: dict) -> dict:
        types = await self.orchestrator.client.list_instance_types()
        return {"instance_types": {
            name: {
                "usd_per_hour": t.price_cents_per_hour / 100,
                "regions_with_capacity": t.regions_with_capacity,
            }
            for name, t in sorted(types.items())
        }}

    async def _act_list_instances(self, args: dict) -> dict:
        instances = await self.orchestrator.instances_with_state()
        return {"instances": [
            {k: i[k] for k in ("id", "name", "status", "region",
                               "instance_type", "hourly_rate_usd",
                               "connection_state")}
            for i in instances
        ]}

    async def _act_launch_gpu(self, args: dict) -> dict:
        launch = await self.orchestrator.request_launch(
            instance_type=str(args["instance_type"]),
            region=str(args["region"]),
            filesystem=str(args["filesystem"]),
        )
        return {"launch": {k: launch[k] for k in ("id", "status")}}

    async def _act_get_launch_status(self, args: dict) -> dict:
        launch = self.db.get_launch(str(args["launch_id"]))
        if launch is None:
            return {"error": f"launch {args['launch_id']} not found"}
        return {k: launch[k] for k in ("id", "status", "lambda_instance_id",
                                       "launched_type", "attempts", "error")}

    async def _act_list_templates(self, args: dict) -> dict:
        return {"templates": [
            {
                "name": t.name,
                "description": t.description,
                "parameters": [
                    {"name": p.name, "type": p.type,
                     "required": p.required, "default": p.default}
                    for p in t.parameters
                ],
            }
            for t in self.templates.values()
        ]}

    async def _act_run_job(self, args: dict) -> dict:
        name = str(args["template"])
        parameters = args.get("parameters") or {}
        template = self.templates.get(name)
        if template is None:
            return {"error": f"unknown template '{name}'. Available: "
                             f"{', '.join(sorted(self.templates))}"}
        try:
            coerce_parameters(template, parameters)
        except ParameterError as exc:
            return {"error": str(exc)}
        task_id = self.queue.enqueue(template=name, parameters=parameters)
        return {"task": {"id": task_id, "status": "queued"}}

    async def _act_get_job_status(self, args: dict) -> dict:
        task = self.queue.get(str(args["task_id"]))
        if task is None:
            return {"error": f"task {args['task_id']} not found"}
        return {k: task[k] for k in ("id", "status", "exit_code", "error",
                                     "output_paths", "instance_id")}

    async def _act_get_job_logs(self, args: dict) -> dict:
        task_id = str(args["task_id"])
        if self.queue.get(task_id) is None:
            return {"error": f"task {task_id} not found"}
        tail = min(int(args.get("tail", 30)), 200)
        lines = self.queue.get_logs(task_id, tail=tail)
        return {"lines": [l["line"] for l in lines]}

    async def _act_sync_outputs(self, args: dict) -> dict:
        return await self.orchestrator.sync_ephemeral(str(args["instance_id"]))

    async def _act_terminate_instance(self, args: dict) -> dict:
        return await self.orchestrator.terminate(
            str(args["instance_id"]), force=bool(args.get("force", False))
        )

    async def _act_wait(self, args: dict) -> dict:
        seconds = min(float(args.get("seconds", 5)),
                      self.settings.autopilot.wait_cap_seconds)
        seconds = max(seconds, 0.0)
        await self._sleep(seconds)
        return {"waited_seconds": seconds}
