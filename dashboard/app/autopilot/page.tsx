"use client";

import { useEffect, useState } from "react";
import {
  api,
  ApiError,
  type AgentRun,
  type AgentStep,
  type Brain,
} from "@/lib/api";
import { ApprovalsPanel } from "@/components/ApprovalsPanel";
import { usePolling } from "@/lib/usePolling";
import { StatusBadge } from "@/components/Badge";
import { formatDate } from "@/lib/format";

// Autopilot: a model served on one of YOUR instances (vllm-serve) drives
// Manifold's guarded operations toward a goal — GPU A managing GPU B.
// Every step is recorded below and on the Agent Activity page; budget and
// concurrency guards bind the autopilot exactly as they bind you.
export default function AutopilotPage() {
  const [brain, setBrain] = useState("");
  const [goal, setGoal] = useState("");
  const [maxSteps, setMaxSteps] = useState(20);
  const [requireApproval, setRequireApproval] = useState(true);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState("");

  const { data: runs, refresh } = usePolling(() => api.autopilotRuns(), 2000);
  // Every model that can drive Manifold: served on a GPU instance, running
  // locally (Ollama/LM Studio), or a frontier API with a key in Settings.
  const { data: brainList } = usePolling(() => api.brains(), 5000);
  const brains: Brain[] = brainList ?? [];

  useEffect(() => {
    if (brains.length > 0 && !brain) setBrain(brains[0].ref);
  }, [brains, brain]);

  async function start(e: React.FormEvent) {
    e.preventDefault();
    setStarting(true);
    setError("");
    try {
      await api.startAutopilot({
        goal: goal.trim(),
        brain,
        max_steps: maxSteps,
        require_approval: requireApproval,
      });
      setGoal("");
      refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setStarting(false);
    }
  }

  return (
    <div className="space-y-6">
      <section className="rounded-lg border border-zinc-200 bg-white p-4">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">
          Start a run
        </h2>
        {brains.length === 0 ? (
          <p className="mt-3 text-sm text-zinc-500">
            No brain available. A brain can be: a model served on a running
            instance (queue{" "}
            <span className="font-mono text-xs">vllm-serve</span> on the Jobs
            page), a local model server on this machine (start Ollama or LM
            Studio and it appears here automatically), or a frontier API
            (add an Anthropic/OpenAI/Gemini key in Settings).
          </p>
        ) : (
          <form onSubmit={start} className="mt-3 space-y-3">
            <div className="flex flex-wrap gap-3">
              <label className="block min-w-0 text-xs font-medium text-zinc-600">
                Brain
                <select
                  className="mt-1 block w-full min-w-0 max-w-full rounded border border-zinc-300 bg-white px-2.5 py-1.5 text-sm"
                  value={brain}
                  onChange={(e) => setBrain(e.target.value)}
                >
                  {brains.map((b) => (
                    <option key={b.ref} value={b.ref}>
                      {b.label}
                    </option>
                  ))}
                </select>
              </label>
              <label className="block text-xs font-medium text-zinc-600">
                Step limit
                <input
                  type="number"
                  min={1}
                  max={50}
                  className="mt-1 block w-24 rounded border border-zinc-300 bg-white px-2.5 py-1.5 text-sm"
                  value={maxSteps}
                  onChange={(e) => setMaxSteps(Number(e.target.value))}
                />
              </label>
            </div>
            <textarea
              className="w-full rounded border border-zinc-300 px-2.5 py-1.5 text-sm"
              rows={2}
              placeholder='e.g. "Run the gpu-smoke job on this instance and report whether the GPU is healthy, then stop."'
              value={goal}
              onChange={(e) => setGoal(e.target.value)}
              required
              minLength={4}
            />
            <label
              className="flex cursor-pointer items-start gap-2 text-xs text-zinc-600"
              title="launch_gpu, run_job, and terminate_instance pause as a pending approval until you decide; everything else runs freely"
            >
              <input
                type="checkbox"
                className="mt-0.5"
                checked={requireApproval}
                onChange={(e) => setRequireApproval(e.target.checked)}
              />
              <span>
                <span className="font-medium">Require my approval</span> before
                the agent launches GPUs, queues jobs, or terminates instances
                (each pauses as a pending card here until you approve or deny).
              </span>
            </label>
            <div className="flex items-center justify-between gap-4">
              <p className="text-xs text-zinc-400">
                The agent can launch GPUs (your budget and concurrency guards
                apply), run jobs, read logs, sync outputs, and terminate
                instances. Every step is audited.
              </p>
              <button
                type="submit"
                disabled={starting || !goal.trim()}
                className="rounded bg-zinc-900 px-4 py-1.5 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-50"
              >
                {starting ? "Starting..." : "Start run"}
              </button>
            </div>
          </form>
        )}
        {error && <p className="mt-2 text-xs text-red-700">{error}</p>}
      </section>

      <ApprovalsPanel />

      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-zinc-500">
          Runs
        </h2>
        <div className="space-y-3">
          {(runs ?? []).map((run) => (
            <RunCard key={run.id} run={run} onChanged={refresh} />
          ))}
          {(runs ?? []).length === 0 && (
            <p className="rounded-lg border border-dashed border-zinc-300 p-6 text-center text-sm text-zinc-500">
              No runs yet.
            </p>
          )}
        </div>
      </section>
    </div>
  );
}

function RunCard({ run, onChanged }: { run: AgentRun; onChanged: () => void }) {
  const [open, setOpen] = useState(false);
  const [steps, setSteps] = useState<AgentStep[]>([]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    const load = () =>
      api
        .autopilotRun(run.id)
        .then((r) => {
          if (!cancelled) setSteps(r.steps);
        })
        .catch(() => {});
    load();
    const id = run.status === "running" ? setInterval(load, 1500) : undefined;
    return () => {
      cancelled = true;
      if (id) clearInterval(id);
    };
  }, [open, run.id, run.status]);

  async function cancel() {
    try {
      await api.cancelAutopilot(run.id);
      onChanged();
    } catch {
      /* already finished */
    }
  }

  return (
    <div className="rounded-lg border border-zinc-200 bg-white p-4">
      <div className="flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2">
          <StatusBadge status={run.status} />
          <span className="truncate text-sm font-medium">{run.goal}</span>
        </div>
        <div className="flex shrink-0 items-center gap-3 text-xs text-zinc-500">
          <span>
            {run.steps_taken}/{run.max_steps} steps
          </span>
          <span>{formatDate(run.created_at)}</span>
          {run.status === "running" && (
            <button
              onClick={cancel}
              className="rounded border border-red-200 px-2 py-0.5 font-medium text-red-700 hover:bg-red-50"
            >
              Cancel
            </button>
          )}
          <button
            onClick={() => setOpen((s) => !s)}
            className="rounded border border-zinc-300 px-2 py-0.5 hover:bg-zinc-50"
          >
            {open ? "Hide steps" : "Steps"}
          </button>
        </div>
      </div>

      <p className="mt-1 text-xs text-zinc-400">
        brain: <span className="font-mono">{run.brain_model}</span>{" "}
        <span className="text-zinc-300">·</span>{" "}
        <span className="font-mono">{run.brain_instance_id}</span>
      </p>
      {run.summary && (
        <p className="mt-2 rounded bg-emerald-50 px-3 py-2 text-sm text-emerald-900">
          {run.summary}
        </p>
      )}
      {run.error && (
        <p className="mt-2 rounded bg-red-50 px-3 py-2 text-xs text-red-800">
          {run.error}
        </p>
      )}

      {open && (
        <ol className="mt-3 space-y-2">
          {steps.map((s) => (
            <li
              key={s.seq}
              className="rounded border border-zinc-100 bg-zinc-50 p-2 text-xs"
            >
              <div className="flex items-center gap-2">
                <span className="font-mono text-zinc-400">#{s.seq}</span>
                <span className="font-mono font-medium text-zinc-800">
                  {s.action}
                </span>
                {"error" in s.result && (
                  <span className="rounded bg-red-100 px-1.5 text-red-800">
                    refused
                  </span>
                )}
              </div>
              {s.thought && (
                <p className="mt-1 italic text-zinc-500">{s.thought}</p>
              )}
              {Object.keys(s.args).length > 0 && (
                <p className="mt-1 break-all font-mono text-zinc-600">
                  {JSON.stringify(s.args)}
                </p>
              )}
              <p className="mt-1 max-h-24 overflow-y-auto break-all font-mono text-zinc-500">
                → {JSON.stringify(s.result).slice(0, 600)}
              </p>
            </li>
          ))}
          {steps.length === 0 && (
            <p className="text-xs text-zinc-400">No steps yet.</p>
          )}
        </ol>
      )}
    </div>
  );
}
