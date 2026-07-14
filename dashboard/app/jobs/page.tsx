"use client";

import { useEffect, useState } from "react";
import {
  api,
  ApiError,
  type Instance,
  type ModelPreset,
  type Task,
  type Template,
} from "@/lib/api";
import { usePolling } from "@/lib/usePolling";
import { StatusBadge } from "@/components/Badge";
import { ParameterForm } from "@/components/ParameterForm";
import { EstimateWidget } from "@/components/EstimateWidget";
import {
  AutoManageControls,
  type AutoManageState,
} from "@/components/AutoManageControls";
import { LifecyclePipeline } from "@/components/LifecyclePipeline";
import { formatDate } from "@/lib/format";

// Accept a pasted HuggingFace URL or a bare id, and trim stray whitespace /
// trailing punctuation (a trailing ";" once caused a serve failure).
function normalizeModelId(raw: string): string {
  let v = raw.trim();
  const m = v.match(/huggingface\.co\/([^/\s]+\/[^/\s?#]+)/i);
  if (m) v = m[1];
  return v.replace(/[;,\s/]+$/g, "");
}

// A job is still "active" while its auto-managed lifecycle is in flight, even
// after the container itself has exited (it is still syncing/terminating).
const TERMINAL_LIFECYCLE = ["done", "failed", "cancelled"];
function isActiveJob(t: Task): boolean {
  if (t.status === "queued" || t.status === "running") return true;
  return (
    t.auto_manage &&
    !!t.lifecycle &&
    !TERMINAL_LIFECYCLE.includes(t.lifecycle)
  );
}

export default function JobsPage() {
  const [templates, setTemplates] = useState<Template[]>([]);
  const [templateErrors, setTemplateErrors] = useState<Record<string, string>>({});
  const [presets, setPresets] = useState<ModelPreset[]>([]);
  const [selected, setSelected] = useState("");
  const [seed, setSeed] = useState<{ model_id: string; parameters?: Record<string, unknown> } | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [auto, setAuto] = useState<AutoManageState>({
    enabled: false,
    gpu_type: "",
    region: "",
    filesystem: "",
  });

  const { data: tasks, refresh } = usePolling(() => api.tasks(), 2000);
  // Connected instances, for the "Run on" picker (manual jobs, multi-GPU).
  const { data: instances } = usePolling(() => api.instances(), 5000);
  const connected = (instances ?? []).filter(
    (i: Instance) => i.connection_state === "connected",
  );
  const [targetInstance, setTargetInstance] = useState("");

  useEffect(() => {
    api
      .templates()
      .then((r) => {
        setTemplates(r.templates);
        setTemplateErrors(r.errors);
        if (r.templates.length > 0) setSelected((v) => v || r.templates[0].name);
      })
      .catch((e) => setError(e.message));
    api.modelPresets().then(setPresets).catch(() => {});
  }, []);

  const template = templates.find((t) => t.name === selected);
  const isVllm = selected === "vllm-serve";

  async function enqueue(values: Record<string, unknown>) {
    setSubmitting(true);
    setError("");
    setNotice("");
    try {
      if (isVllm && typeof values.model_id === "string") {
        values = { ...values, model_id: normalizeModelId(values.model_id) };
      }
      const autoConfig =
        auto.enabled && auto.gpu_type && auto.region && auto.filesystem
          ? {
              gpu_type: auto.gpu_type,
              region: auto.region,
              filesystem: auto.filesystem,
            }
          : undefined;
      if (auto.enabled && !autoConfig) {
        setError("Auto-manage needs a GPU, region, and filesystem.");
        setSubmitting(false);
        return;
      }
      const task = await api.enqueueTask(
        selected,
        values,
        autoConfig,
        !autoConfig && targetInstance ? targetInstance : undefined,
      );
      setNotice(
        autoConfig
          ? `Queued ${task.id} (${task.template}) — Manifold will rent a ${autoConfig.gpu_type} for it`
          : `Queued ${task.id} (${task.template})`,
      );
      refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  }

  async function clearHistory() {
    setClearing(true);
    setError("");
    try {
      const { cleared } = await api.clearFinishedTasks();
      setNotice(`Cleared ${cleared} finished job(s)`);
      refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setClearing(false);
    }
  }

  async function removeTask(id: string) {
    setError("");
    try {
      await api.deleteTask(id);
      refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }

  async function cancelTask(id: string) {
    setError("");
    try {
      await api.cancelTask(id);
      refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }

  const active = (tasks ?? []).filter(isActiveJob);
  const history = (tasks ?? []).filter((t) => !isActiveJob(t));

  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(360px,460px)_1fr]">
      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-zinc-500">
          Queue a job
        </h2>
        <div className="space-y-4 rounded-lg border border-zinc-200 bg-white p-5">
          <label className="block text-xs font-medium text-zinc-600">
            Template
            <select
              className="mt-1 w-full rounded border border-zinc-300 bg-white px-2.5 py-1.5 text-sm"
              value={selected}
              onChange={(e) => {
                setSelected(e.target.value);
                setSeed(null);
              }}
            >
              {templates.map((t) => (
                <option key={t.name} value={t.name}>
                  {t.name}
                </option>
              ))}
            </select>
          </label>
          {template && (
            <>
              {/* The launch decision is made here: what this job does and
                  what GPU it needs, in a callout right under the picker. */}
              <div className="rounded border border-zinc-200 bg-zinc-50 p-3 text-xs text-zinc-600">
                <p>{template.description}</p>
                {template.gpu?.min_vram_gib ? (
                  <p className="mt-1 font-medium text-zinc-700">
                    Needs a GPU with ≥{template.gpu.min_vram_gib} GiB VRAM.
                  </p>
                ) : null}
              </div>

              {/* Rent a GPU just for this job (launch -> run -> sync ->
                  terminate), or leave off to run on a connected instance. */}
              <AutoManageControls value={auto} onChange={setAuto} />

              {/* Manual jobs: which running instance takes this job. With
                  one instance this is informational; with several it is the
                  multi-GPU router. */}
              {!auto.enabled && connected.length > 0 && (
                <label className="block text-xs font-medium text-zinc-600">
                  Run on
                  <select
                    className="mt-1 w-full min-w-0 rounded border border-zinc-300 bg-white px-2.5 py-1.5 text-sm"
                    value={targetInstance}
                    onChange={(e) => setTargetInstance(e.target.value)}
                  >
                    <option value="">
                      first free instance ({connected.length} connected)
                    </option>
                    {connected.map((i: Instance) => (
                      <option key={i.id} value={i.id}>
                        {i.name} · {i.gpu_description || i.instance_type} ·{" "}
                        {i.region}
                      </option>
                    ))}
                  </select>
                </label>
              )}
              {!auto.enabled && connected.length === 0 && (
                <p className="rounded border border-zinc-200 bg-zinc-100 px-3 py-2 text-xs text-zinc-500">
                  No instance is connected: this job will wait in the queue
                  until one is running (launch one on Instances), or turn on
                  auto-manage above to rent a GPU just for it.
                </p>
              )}

              {/* Advisory pre-launch estimate: what a run of this template is
                  likely to cost. When auto-manage is on it follows that GPU. */}
              <EstimateWidget
                template={template.name}
                instanceType={
                  auto.enabled && auto.gpu_type ? auto.gpu_type : undefined
                }
              />

              {isVllm && presets.length > 0 && (
                <div className="mb-4 rounded border border-zinc-100 bg-zinc-50 p-2.5">
                  <p className="mb-2 text-xs font-medium text-zinc-600">
                    Presets (click to fill · ungated, no token needed)
                  </p>
                  <div className="flex flex-wrap gap-1.5">
                    {presets.map((p) => (
                      <button
                        key={p.model_id}
                        type="button"
                        title={`${p.model_id} — ${p.note}`}
                        onClick={() => setSeed({ model_id: p.model_id, parameters: p.parameters })}
                        className={`rounded border px-2 py-1 text-left text-xs hover:bg-white ${
                          seed?.model_id === p.model_id
                            ? "border-zinc-900 bg-white"
                            : "border-zinc-300 bg-zinc-50"
                        }`}
                      >
                        <span className="font-medium text-zinc-800">
                          {p.label}
                        </span>
                        <span className="ml-1.5 text-zinc-400">{p.tier}</span>
                      </button>
                    ))}
                  </div>
                </div>
              )}

              <ParameterForm
                key={`${template.name}:${seed?.model_id ?? ""}`}
                template={template}
                onSubmit={enqueue}
                submitting={submitting}
                initialValues={
                  isVllm && seed
                    ? { model_id: seed.model_id, ...seed.parameters }
                    : undefined
                }
              />
            </>
          )}
          {notice && <p className="mt-3 text-xs text-emerald-700">{notice}</p>}
          {error && <p className="mt-3 text-xs text-red-700">{error}</p>}
          {Object.entries(templateErrors).map(([file, message]) => (
            <p key={file} className="mt-3 text-xs text-amber-700">
              {file}: {message}
            </p>
          ))}
        </div>
      </section>

      <section className="space-y-6">
        <div>
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-zinc-500">
            Active {active.length > 0 && `(${active.length})`}
          </h2>
          <div className="space-y-3">
            {active.map((t) => (
              <TaskCard
                key={t.id}
                task={t}
                onRemove={removeTask}
                onCancel={cancelTask}
              />
            ))}
            {active.length === 0 && (
              <p className="rounded-lg border border-dashed border-zinc-300 p-6 text-center text-sm text-zinc-500">
                No active jobs.
              </p>
            )}
          </div>
        </div>

        <div>
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">
              History {history.length > 0 && `(${history.length})`}
            </h2>
            {history.length > 0 && (
              <button
                onClick={clearHistory}
                disabled={clearing}
                className="rounded border border-zinc-300 px-2 py-1 text-xs text-zinc-600 hover:bg-zinc-50 disabled:opacity-50"
              >
                {clearing ? "Clearing..." : "Clear history"}
              </button>
            )}
          </div>
          <div className="space-y-3">
            {history.map((t) => (
              <TaskCard
                key={t.id}
                task={t}
                onRemove={removeTask}
                onCancel={cancelTask}
              />
            ))}
            {history.length === 0 && (
              <p className="rounded-lg border border-dashed border-zinc-300 p-6 text-center text-sm text-zinc-500">
                No finished jobs yet.
              </p>
            )}
          </div>
        </div>
      </section>
    </div>
  );
}

const CANCELLABLE = ["queued", "waiting", "launching", "ready"];

function TaskCard({
  task,
  onRemove,
  onCancel,
}: {
  task: Task;
  onRemove: (id: string) => void;
  onCancel: (id: string) => void;
}) {
  const [showLogs, setShowLogs] = useState(false);
  const [lines, setLines] = useState<string[]>([]);
  const [failTail, setFailTail] = useState<string[] | null>(null);

  const auto = task.auto_manage;
  const lc = task.lifecycle;
  // In-flight auto-managed jobs must not be removed (their instance is still
  // being managed); they can be cancelled instead while pre-run.
  const inFlightAuto = auto && !!lc && !["done", "failed", "cancelled"].includes(lc);
  const canCancel = auto && !!lc && CANCELLABLE.includes(lc);

  // A failed job must show WHY inline, not just "exit -1": pull the last 10
  // lines of its retained log automatically. The full "Logs" button still
  // shows everything.
  useEffect(() => {
    if (task.status !== "failed") return;
    let cancelled = false;
    api
      .taskLogs(task.id, 10)
      .then((l) => {
        if (!cancelled) setFailTail(l.map((x) => x.line));
      })
      .catch(() => {
        if (!cancelled) setFailTail([]);
      });
    return () => {
      cancelled = true;
    };
  }, [task.id, task.status]);

  useEffect(() => {
    if (!showLogs) return;
    let cancelled = false;
    // Tail the last 400 lines: a served-model job emits tens of thousands,
    // and this refetches every 1.5s while running.
    const load = () =>
      api
        .taskLogs(task.id, 400)
        .then((l) => {
          if (!cancelled) setLines(l.map((x) => x.line));
        })
        .catch(() => {});
    load();
    const id =
      task.status === "running" || task.status === "queued"
        ? setInterval(load, 1500)
        : undefined;
    return () => {
      cancelled = true;
      if (id) clearInterval(id);
    };
  }, [showLogs, task.id, task.status]);

  const finished = task.status !== "running" && task.status !== "queued";

  return (
    <div className="rounded-lg border border-zinc-200 bg-white p-4">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <StatusBadge status={task.status} />
          {auto && (
            <span
              className="rounded bg-sky-100 px-1.5 py-0.5 text-[11px] font-medium text-sky-800"
              title="Manifold rents and tears down a GPU just for this job"
            >
              auto-manage
            </span>
          )}
          <span className="text-sm font-medium">{task.template}</span>
          <span className="font-mono text-xs text-zinc-400">{task.id}</span>
        </div>
        <div className="flex items-center gap-3 text-xs text-zinc-500">
          {task.exit_code !== null && finished && (
            <span
              className={`font-mono ${
                task.exit_code === 0 ? "text-zinc-400" : "text-red-600"
              }`}
              title="Container exit code (the honest signal; see Logs)"
            >
              exit {task.exit_code}
            </span>
          )}
          <span>{formatDate(task.created_at)}</span>
          <button
            onClick={() => setShowLogs((s) => !s)}
            className="rounded border border-zinc-300 px-2 py-0.5 hover:bg-zinc-50"
          >
            {showLogs ? "Hide logs" : "Logs"}
          </button>
          {canCancel && (
            <button
              onClick={() => onCancel(task.id)}
              title="Cancel and tear down any instance it launched"
              className="rounded border border-amber-200 px-2 py-0.5 text-amber-700 hover:bg-amber-50"
            >
              Cancel
            </button>
          )}
          {task.status !== "running" && !inFlightAuto && (
            <button
              onClick={() => onRemove(task.id)}
              title="Remove from history"
              className="rounded border border-zinc-200 px-2 py-0.5 text-zinc-400 hover:bg-red-50 hover:text-red-600"
            >
              Remove
            </button>
          )}
        </div>
      </div>

      {auto && <LifecyclePipeline task={task} />}

      {Object.keys(task.parameters).length > 0 && (
        <p className="mt-1 font-mono text-xs text-zinc-500">
          {Object.entries(task.parameters)
            .map(([k, v]) => `${k}=${v}`)
            .join("  ")}
        </p>
      )}
      {task.error && <p className="mt-2 text-xs text-red-700">{task.error}</p>}
      {task.status === "failed" && failTail !== null && (
        <div className="mt-2">
          <p className="mb-1 text-[11px] font-medium uppercase tracking-wide text-zinc-400">
            Last log lines
          </p>
          {failTail.length > 0 ? (
            <pre className="max-h-40 overflow-y-auto whitespace-pre-wrap break-words rounded bg-zinc-950 p-2.5 font-mono text-[11px] leading-relaxed text-zinc-800">
              {failTail.join("\n")}
            </pre>
          ) : (
            <p className="text-xs text-zinc-500">
              No log output was captured for this job.
            </p>
          )}
        </div>
      )}
      {task.status === "succeeded" && task.output_paths.length > 0 && (
        <p className="mt-2 text-xs text-zinc-500">
          Outputs:{" "}
          <span className="font-mono">{task.output_paths.join(", ")}</span>
        </p>
      )}

      {showLogs && (
        /* pre-wrap + break-words: long docker/pip lines wrap instead of
           forcing the whole card into horizontal scroll; height stays capped. */
        <pre className="mt-3 max-h-72 overflow-y-auto whitespace-pre-wrap break-words rounded bg-zinc-950 p-3 font-mono text-xs leading-relaxed text-zinc-800">
          {lines.length > 0 ? lines.join("\n") : "(no output yet)"}
        </pre>
      )}
    </div>
  );
}
