"use client";

import { useEffect, useState } from "react";
import { api, ApiError, type Task, type Template } from "@/lib/api";
import { usePolling } from "@/lib/usePolling";
import { StatusBadge } from "@/components/Badge";
import { ParameterForm } from "@/components/ParameterForm";
import { formatDate } from "@/lib/format";

export default function JobsPage() {
  const [templates, setTemplates] = useState<Template[]>([]);
  const [templateErrors, setTemplateErrors] = useState<Record<string, string>>({});
  const [selected, setSelected] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const { data: tasks, refresh } = usePolling(() => api.tasks(), 2000);

  useEffect(() => {
    api
      .templates()
      .then((r) => {
        setTemplates(r.templates);
        setTemplateErrors(r.errors);
        if (r.templates.length > 0) setSelected((v) => v || r.templates[0].name);
      })
      .catch((e) => setError(e.message));
  }, []);

  const template = templates.find((t) => t.name === selected);

  async function enqueue(values: Record<string, unknown>) {
    setSubmitting(true);
    setError("");
    setNotice("");
    try {
      const task = await api.enqueueTask(selected, values);
      setNotice(`Queued ${task.id} (${task.template})`);
      refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="grid gap-6 md:grid-cols-[minmax(280px,360px)_1fr]">
      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-zinc-500">
          Queue a job
        </h2>
        <div className="rounded-lg border border-zinc-200 bg-white p-4">
          <label className="block text-xs font-medium text-zinc-600">
            Template
            <select
              className="mt-1 w-full rounded border border-zinc-300 bg-white px-2.5 py-1.5 text-sm"
              value={selected}
              onChange={(e) => setSelected(e.target.value)}
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
              <p className="mt-2 mb-4 text-xs text-zinc-500">
                {template.description}
                {template.gpu?.min_vram_gib
                  ? ` · needs ≥${template.gpu.min_vram_gib} GiB VRAM`
                  : ""}
              </p>
              <ParameterForm
                key={template.name}
                template={template}
                onSubmit={enqueue}
                submitting={submitting}
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

      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-zinc-500">
          Queue
        </h2>
        <div className="space-y-3">
          {(tasks ?? []).map((t) => (
            <TaskCard key={t.id} task={t} />
          ))}
          {(tasks ?? []).length === 0 && (
            <p className="rounded-lg border border-dashed border-zinc-300 p-6 text-center text-sm text-zinc-500">
              No jobs yet.
            </p>
          )}
        </div>
      </section>
    </div>
  );
}

function TaskCard({ task }: { task: Task }) {
  const [showLogs, setShowLogs] = useState(false);
  const [lines, setLines] = useState<string[]>([]);

  useEffect(() => {
    if (!showLogs) return;
    let cancelled = false;
    const load = () =>
      api
        .taskLogs(task.id)
        .then((l) => {
          if (!cancelled) setLines(l.map((x) => x.line));
        })
        .catch(() => {});
    load();
    // Live-tail while the task is active.
    const id =
      task.status === "running" || task.status === "queued"
        ? setInterval(load, 1500)
        : undefined;
    return () => {
      cancelled = true;
      if (id) clearInterval(id);
    };
  }, [showLogs, task.id, task.status]);

  return (
    <div className="rounded-lg border border-zinc-200 bg-white p-4">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <StatusBadge status={task.status} />
          <span className="text-sm font-medium">{task.template}</span>
          <span className="font-mono text-xs text-zinc-400">{task.id}</span>
        </div>
        <div className="flex items-center gap-3 text-xs text-zinc-500">
          {task.exit_code !== null &&
            task.status !== "running" &&
            task.status !== "queued" && (
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
        </div>
      </div>

      {Object.keys(task.parameters).length > 0 && (
        <p className="mt-1 font-mono text-xs text-zinc-500">
          {Object.entries(task.parameters)
            .map(([k, v]) => `${k}=${v}`)
            .join("  ")}
        </p>
      )}
      {task.error && (
        <p className="mt-2 text-xs text-red-700">{task.error}</p>
      )}
      {task.status === "succeeded" && task.output_paths.length > 0 && (
        <p className="mt-2 text-xs text-zinc-500">
          Outputs:{" "}
          <span className="font-mono">{task.output_paths.join(", ")}</span>
        </p>
      )}

      {showLogs && (
        <pre className="mt-3 max-h-72 overflow-auto rounded bg-zinc-950 p-3 text-xs leading-relaxed text-zinc-100">
          {lines.length > 0 ? lines.join("\n") : "(no output yet)"}
        </pre>
      )}
    </div>
  );
}
