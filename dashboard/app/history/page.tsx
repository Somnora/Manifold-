"use client";

import { useEffect, useState } from "react";
import { api, type Launch, type Utilization } from "@/lib/api";
import { usePolling } from "@/lib/usePolling";
import { StatusBadge } from "@/components/Badge";
import { AuditLog } from "@/components/AuditLog";
import {
  formatDate,
  formatDuration,
  formatMoney,
  launchCost,
} from "@/lib/format";

// Activity: everything that happened, in two ledgers. "Spend" is the launch
// history with cost = rate x billable runtime; "Audit" is the full action
// trail (agents, MCP, backend safety actions). One place to answer both
// "what did this cost me" and "who did what" - they used to be two nav
// destinations, which made each feel thinner than it is.
type Tab = "spend" | "audit";

export default function ActivityPage() {
  const [tab, setTab] = useState<Tab>("spend");

  // Deep link: /history?tab=audit (used by the old /agents URL's redirect).
  // Read on the client only - the static export prerenders without a query.
  useEffect(() => {
    if (new URLSearchParams(window.location.search).get("tab") === "audit") {
      setTab("audit");
    }
  }, []);

  return (
    <div className="space-y-4">
      <div className="flex overflow-hidden rounded border border-zinc-300 text-xs self-start w-fit">
        {(
          [
            ["spend", "Spend"],
            ["audit", "Audit trail"],
          ] as [Tab, string][]
        ).map(([key, label]) => (
          <button
            key={key}
            onClick={() => setTab(key)}
            className={`px-4 py-1.5 ${
              tab === key
                ? "bg-zinc-900 text-white"
                : "text-zinc-600 hover:bg-zinc-50"
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {tab === "spend" ? <SpendLedger /> : <AuditLog />}
    </div>
  );
}

// Launch history straight from SQLite, with cost = rate x billable runtime.
// Billing runs from launch acceptance to termination; running launches show
// a live, still-growing cost. Each row expands to a post-run utilization
// verdict (peak VRAM, avg util) with an advisory right-size hint.
function SpendLedger() {
  const { data: launches, error } = usePolling(() => api.launches(), 5000);

  const rows = launches ?? [];
  const totalUsd = rows.reduce(
    (sum, l) => sum + (launchCost(l)?.usd ?? 0),
    0,
  );

  return (
    <div className="space-y-4">
      {error && (
        <p className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">
          {error}
        </p>
      )}

      <div className="overflow-x-auto rounded-lg border border-zinc-200 bg-white">
        <table className="w-full text-sm">
          <thead className="bg-zinc-50 text-left text-xs uppercase tracking-wide text-zinc-500">
            <tr>
              <th className="px-4 py-2 font-medium">Created</th>
              <th className="px-4 py-2 font-medium">Type</th>
              <th className="px-4 py-2 font-medium">Region</th>
              <th className="px-4 py-2 font-medium">Mode</th>
              <th className="px-4 py-2 font-medium">Status</th>
              <th className="px-4 py-2 font-medium text-right">Attempts</th>
              <th className="px-4 py-2 font-medium text-right">Rate</th>
              <th className="px-4 py-2 font-medium text-right">Runtime</th>
              <th className="px-4 py-2 font-medium text-right">Cost</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-100">
            {rows.map((l) => (
              <LaunchRow key={l.id} launch={l} />
            ))}
            {rows.length === 0 && (
              <tr>
                <td
                  colSpan={9}
                  className="px-4 py-8 text-center text-sm text-zinc-500"
                >
                  No launches yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <p className="text-right text-sm text-zinc-600">
        Total spend shown: <span className="font-medium">{formatMoney(totalUsd)}</span>
      </p>
    </div>
  );
}

function LaunchRow({ launch: l }: { launch: Launch }) {
  const [open, setOpen] = useState(false);
  const [util, setUtil] = useState<Utilization | null>(null);
  const [loading, setLoading] = useState(false);

  const cost = launchCost(l);
  const fellBack = l.launched_type && l.launched_type !== l.requested_type;

  function toggle() {
    const next = !open;
    setOpen(next);
    // Fetch once, on first expand. Utilization only exists once a launch has
    // recorded telemetry samples, so this is cheap and idempotent.
    if (next && util === null && !loading) {
      setLoading(true);
      api
        .launchUtilization(l.id)
        .then(setUtil)
        .catch(() => setUtil({ available: false, reason: "unavailable" }))
        .finally(() => setLoading(false));
    }
  }

  return (
    <>
      <tr
        onClick={toggle}
        title={l.error ?? "Click for utilization"}
        className="cursor-pointer hover:bg-zinc-50"
      >
        <td className="px-4 py-2 whitespace-nowrap text-zinc-600">
          <span className="mr-1.5 inline-block w-2 text-zinc-400">
            {open ? "▾" : "▸"}
          </span>
          {formatDate(l.created_at)}
        </td>
        <td className="px-4 py-2">
          {l.requested_type}
          {fellBack && (
            <span className="text-zinc-500"> (launched {l.launched_type})</span>
          )}
        </td>
        <td className="px-4 py-2 text-zinc-600">{l.region}</td>
        <td className="px-4 py-2 text-zinc-600">{l.connection_mode}</td>
        <td className="px-4 py-2">
          <StatusBadge status={l.status} />
        </td>
        <td className="px-4 py-2 text-right text-zinc-600">{l.attempts}</td>
        <td className="px-4 py-2 text-right text-zinc-600">
          {l.hourly_rate_cents != null
            ? `${formatMoney(l.hourly_rate_cents / 100)}/hr`
            : "-"}
        </td>
        <td className="px-4 py-2 text-right text-zinc-600">
          {cost ? formatDuration(cost.seconds) : "-"}
        </td>
        <td className="px-4 py-2 text-right font-medium">
          {cost ? formatMoney(cost.usd) : "-"}
        </td>
      </tr>
      {open && (
        <tr className="bg-zinc-50/60">
          <td colSpan={9} className="px-4 py-3">
            <UtilizationDetail loading={loading} util={util} />
          </td>
        </tr>
      )}
    </>
  );
}

function UtilizationDetail({
  loading,
  util,
}: {
  loading: boolean;
  util: Utilization | null;
}) {
  if (loading || util === null) {
    return <p className="text-xs text-zinc-400">Loading utilization…</p>;
  }
  if (!util.available) {
    return (
      <p className="text-xs text-zinc-500">
        No GPU telemetry recorded for this launch.
      </p>
    );
  }
  return (
    <div className="space-y-1.5 text-xs">
      <p className="font-medium text-zinc-700">{util.verdict}</p>
      {util.hint && (
        <p
          className={
            util.right_size_hint
              ? "rounded bg-amber-50 px-2 py-1 text-amber-800"
              : "text-zinc-500"
          }
        >
          {util.right_size_hint ? "Right-size hint: " : ""}
          {util.hint}
        </p>
      )}
      <p className="text-[11px] text-zinc-400">
        Advisory only, from {util.sample_count} telemetry sample(s). Manifold
        never changes your GPU choice.
      </p>
    </div>
  );
}
