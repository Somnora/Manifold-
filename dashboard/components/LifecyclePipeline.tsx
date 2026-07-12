"use client";

import type { Lifecycle, Task } from "@/lib/api";

// The full auto-managed lifecycle, shown honestly on the job card:
//   launching -> ready -> running -> syncing -> terminating -> done
// with waiting/queued as a pre-launch state and failed/cancelled as terminal.
const STAGES: { key: Lifecycle; label: string }[] = [
  { key: "launching", label: "Launching" },
  { key: "ready", label: "Ready" },
  { key: "running", label: "Running" },
  { key: "syncing", label: "Syncing" },
  { key: "terminating", label: "Terminating" },
  { key: "done", label: "Done" },
];

const TERMINAL_BAD: Record<string, { label: string; klass: string }> = {
  failed: { label: "Failed", klass: "bg-red-100 text-red-800 border-red-200" },
  cancelled: {
    label: "Cancelled",
    klass: "bg-zinc-200 text-zinc-600 border-zinc-300",
  },
};

export function LifecyclePipeline({ task }: { task: Task }) {
  const lc = task.lifecycle;
  if (!lc) return null;

  const events = task.lifecycle_events || {};
  const currentIndex = STAGES.findIndex((s) => s.key === lc);
  const bad = TERMINAL_BAD[lc];
  const ready = task.launch_to_ready_seconds;

  return (
    <div className="mt-2 rounded border border-sky-100 bg-sky-50/50 p-2.5">
      <div className="flex items-center gap-1 overflow-x-auto">
        {(lc === "queued" || lc === "waiting") && (
          <StagePill
            state="current"
            label={lc === "waiting" ? "Waiting for a slot" : "Queued"}
          />
        )}
        {STAGES.map((stage, i) => {
          const reached = stage.key in events || (currentIndex >= 0 && i < currentIndex);
          const isCurrent = stage.key === lc;
          const state = isCurrent ? "current" : reached ? "done" : "todo";
          return (
            <div key={stage.key} className="flex items-center gap-1">
              {i > 0 && <span className="text-zinc-300">·</span>}
              <StagePill state={state} label={stage.label} />
            </div>
          );
        })}
        {bad && (
          <>
            <span className="text-zinc-300">·</span>
            <span
              className={`whitespace-nowrap rounded border px-1.5 py-0.5 text-[11px] font-medium ${bad.klass}`}
            >
              {bad.label}
            </span>
          </>
        )}
      </div>

      {task.lifecycle_detail && (
        <p className="mt-1.5 text-[11px] text-sky-900/80">
          {task.lifecycle_detail}
        </p>
      )}
      <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-zinc-500">
        {task.instance_id && (
          <span className="font-mono">instance {task.instance_id}</span>
        )}
        {ready != null && ready >= 0 && (
          <span>GPU ready in {formatSeconds(ready)}</span>
        )}
      </div>
    </div>
  );
}

function StagePill({
  state,
  label,
}: {
  state: "done" | "current" | "todo";
  label: string;
}) {
  const klass =
    state === "current"
      ? "bg-sky-600 text-white border-sky-600"
      : state === "done"
        ? "bg-sky-100 text-sky-800 border-sky-200"
        : "bg-white text-zinc-400 border-zinc-200";
  return (
    <span
      className={`whitespace-nowrap rounded border px-1.5 py-0.5 text-[11px] font-medium ${klass}`}
    >
      {label}
    </span>
  );
}

function formatSeconds(s: number): string {
  if (s < 90) return `${Math.round(s)}s`;
  return `${(s / 60).toFixed(1)} min`;
}
