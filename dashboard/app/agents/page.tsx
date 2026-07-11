"use client";

import { api } from "@/lib/api";
import { usePolling } from "@/lib/usePolling";
import { Badge } from "@/components/Badge";
import { formatDate } from "@/lib/format";

// Everything agents (and the backend itself) have done, newest first.
// MCP entries carry {args, note, result} as JSON in detail.
export default function AgentActivityPage() {
  const { data, error } = usePolling(() => api.audit(), 3000);
  const entries = data ?? [];

  return (
    <div className="space-y-4">
      <p className="text-sm text-zinc-500">
        Every MCP tool call and backend safety action, newest first. Agents
        cannot act outside this log: all tool calls flow through the same
        guarded backend as the dashboard.
      </p>

      {error && (
        <p className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">
          {error}
        </p>
      )}

      <div className="overflow-hidden rounded-lg border border-zinc-200 bg-white">
        <table className="w-full text-sm">
          <thead className="bg-zinc-50 text-left text-xs uppercase tracking-wide text-zinc-500">
            <tr>
              <th className="px-4 py-2 font-medium">When</th>
              <th className="px-4 py-2 font-medium">Actor</th>
              <th className="px-4 py-2 font-medium">Action</th>
              <th className="px-4 py-2 font-medium">Detail</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-100">
            {entries.map((e) => (
              <AuditRow key={e.id} entry={e} />
            ))}
            {entries.length === 0 && (
              <tr>
                <td
                  colSpan={4}
                  className="px-4 py-8 text-center text-sm text-zinc-500"
                >
                  No activity yet. Register the MCP server (docs/mcp-setup.md)
                  and let an agent loose.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

type Entry = { id: number; at: string; actor: string; action: string; detail: string };

function AuditRow({ entry }: { entry: Entry }) {
  let parsed: { args?: Record<string, unknown>; note?: string; result?: string } | null =
    null;
  if (entry.actor === "mcp") {
    try {
      parsed = JSON.parse(entry.detail);
    } catch {
      parsed = null;
    }
  }
  const rejected = parsed?.result?.startsWith("rejected");

  return (
    <tr>
      <td className="whitespace-nowrap px-4 py-2 text-zinc-600">
        {formatDate(entry.at)}
      </td>
      <td className="px-4 py-2">
        <Badge
          label={entry.actor}
          tone={entry.actor === "mcp" ? "green" : "zinc"}
        />
      </td>
      <td className="whitespace-nowrap px-4 py-2 font-mono text-xs">
        {entry.action}
      </td>
      <td className="px-4 py-2 text-xs text-zinc-600">
        {parsed ? (
          <div className="space-y-0.5">
            {parsed.note && (
              <p className="italic text-zinc-500">&ldquo;{parsed.note}&rdquo;</p>
            )}
            {parsed.args && Object.keys(parsed.args).length > 0 && (
              <p className="break-all font-mono">
                {Object.entries(parsed.args)
                  .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
                  .join(" ")}
              </p>
            )}
            {parsed.result && (
              <p className={rejected ? "font-medium text-red-700" : "text-zinc-500"}>
                {parsed.result}
              </p>
            )}
          </div>
        ) : (
          <span className="break-all">{entry.detail}</span>
        )}
      </td>
    </tr>
  );
}
