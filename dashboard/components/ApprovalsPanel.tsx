"use client";

import { api, type Approval } from "@/lib/api";
import { usePolling } from "@/lib/usePolling";

// Actions an approval-gated autopilot run is waiting on. Each card is one
// paused agent action: the run holds until you decide (a timeout in the
// backend auto-denies so nothing waits forever). Renders nothing when the
// queue is empty - it earns space only when a decision is actually needed.
export function ApprovalsPanel() {
  const { data: approvals, refresh } = usePolling(() => api.approvals(), 2000);
  const pending = approvals ?? [];
  if (pending.length === 0) return null;

  async function decide(id: string, approve: boolean) {
    try {
      await api.decideApproval(id, approve);
    } catch {
      /* already decided or expired - the refresh clears it */
    }
    refresh();
  }

  return (
    <section className="rounded-lg border border-amber-300 bg-amber-50 p-4">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-amber-800">
        Waiting for your approval ({pending.length})
      </h2>
      <div className="mt-3 space-y-2">
        {pending.map((a: Approval) => (
          <div
            key={a.id}
            className="flex flex-wrap items-center justify-between gap-3 rounded border border-amber-200 bg-white p-3"
          >
            <div className="min-w-0">
              <p className="text-sm">
                <span className="font-mono font-medium">{a.action}</span>{" "}
                <span className="break-all font-mono text-xs text-zinc-500">
                  {JSON.stringify(a.args)}
                </span>
              </p>
              {a.run_goal && (
                <p className="mt-0.5 truncate text-xs text-zinc-400">
                  run goal: {a.run_goal}
                </p>
              )}
            </div>
            <div className="flex shrink-0 gap-2">
              <button
                onClick={() => decide(a.id, true)}
                className="rounded bg-emerald-600 px-3 py-1 text-xs font-medium text-zinc-900 hover:bg-emerald-500"
              >
                Approve
              </button>
              <button
                onClick={() => decide(a.id, false)}
                className="rounded border border-red-300 px-3 py-1 text-xs font-medium text-red-700 hover:bg-red-50"
              >
                Deny
              </button>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
