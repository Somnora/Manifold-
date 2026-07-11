"use client";

import { api } from "@/lib/api";
import { usePolling } from "@/lib/usePolling";
import { StatusBadge } from "@/components/Badge";
import {
  formatDate,
  formatDuration,
  formatMoney,
  launchCost,
} from "@/lib/format";

// Launch history straight from SQLite, with cost = rate x billable runtime.
// Billing runs from launch acceptance to termination; running launches show
// a live, still-growing cost.
export default function HistoryPage() {
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
            {rows.map((l) => {
              const cost = launchCost(l);
              const fellBack =
                l.launched_type && l.launched_type !== l.requested_type;
              return (
                <tr key={l.id} title={l.error ?? undefined}>
                  <td className="px-4 py-2 whitespace-nowrap text-zinc-600">
                    {formatDate(l.created_at)}
                  </td>
                  <td className="px-4 py-2">
                    {l.requested_type}
                    {fellBack && (
                      <span className="text-zinc-500">
                        {" "}
                        (launched {l.launched_type})
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-2 text-zinc-600">{l.region}</td>
                  <td className="px-4 py-2 text-zinc-600">
                    {l.connection_mode}
                  </td>
                  <td className="px-4 py-2">
                    <StatusBadge status={l.status} />
                  </td>
                  <td className="px-4 py-2 text-right text-zinc-600">
                    {l.attempts}
                  </td>
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
              );
            })}
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
