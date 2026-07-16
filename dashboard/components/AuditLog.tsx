"use client";

import { useMemo, useState } from "react";
import { api } from "@/lib/api";
import { usePolling } from "@/lib/usePolling";
import { Badge } from "@/components/Badge";
import { formatDate } from "@/lib/format";

type Entry = {
  id: number;
  at: string;
  actor: string;
  action: string;
  detail: string;
};

// An entry is an "agent action" if an agent (or autopilot) caused it, or if
// it is a job the dispatcher pushed on an agent's behalf. Everything else the
// BACKEND itself did on its own schedule (reconnect, reconcile, idle) is
// "system". User/API actions (chat, file ops) are neither, so they appear
// only under "All".
function isAgent(e: Entry): boolean {
  return (
    e.actor === "mcp" ||
    e.actor === "autopilot" ||
    e.action === "task_dispatch"
  );
}
function isSystem(e: Entry): boolean {
  return e.actor === "backend" && e.action !== "task_dispatch";
}

type Filter = "all" | "agent" | "system";

// A run of adjacent entries that share (actor, action) collapses into one row
// with a count and a time range — so N honest backend restarts read as
// "reconnect_on_startup ×N, 9:34–9:42" instead of N near-identical lines.
type Group = { first: Entry; last: Entry; count: number; members: Entry[] };

function groupConsecutive(entries: Entry[]): Group[] {
  const groups: Group[] = [];
  for (const e of entries) {
    const tail = groups[groups.length - 1];
    if (tail && tail.first.actor === e.actor && tail.first.action === e.action) {
      tail.count += 1;
      tail.last = e; // entries are newest-first, so `last` is the oldest
      tail.members.push(e);
    } else {
      groups.push({ first: e, last: e, count: 1, members: [e] });
    }
  }
  return groups;
}

// Everything agents (and the backend itself) have done, newest first.
// MCP entries carry {args, note, result} as JSON in detail.
export function AuditLog() {
  const { data, error } = usePolling(() => api.audit(), 3000);
  const [filter, setFilter] = useState<Filter>("all");

  const entries = useMemo(() => (data ?? []) as Entry[], [data]);
  const filtered = useMemo(() => {
    if (filter === "agent") return entries.filter(isAgent);
    if (filter === "system") return entries.filter(isSystem);
    return entries;
  }, [entries, filter]);
  const groups = useMemo(() => groupConsecutive(filtered), [filtered]);

  const tabs: { key: Filter; label: string }[] = [
    { key: "all", label: "All" },
    { key: "agent", label: "Agent actions" },
    { key: "system", label: "System" },
  ];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-zinc-500">
          Every MCP tool call and backend safety action, newest first. Agents
          cannot act outside this log: all tool calls flow through the same
          guarded backend as the dashboard.
        </p>
        <div className="flex overflow-hidden rounded border border-zinc-300 text-xs">
          {tabs.map((t) => (
            <button
              key={t.key}
              onClick={() => setFilter(t.key)}
              className={`px-3 py-1 ${
                filter === t.key
                  ? "bg-zinc-900 text-white"
                  : "text-zinc-600 hover:bg-zinc-50"
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
      </div>

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
            {groups.map((g) =>
              g.count === 1 ? (
                <AuditRow key={g.first.id} entry={g.first} />
              ) : (
                <CollapsedRow key={g.first.id} group={g} />
              ),
            )}
            {groups.length === 0 && (
              <tr>
                <td
                  colSpan={4}
                  className="px-4 py-8 text-center text-sm text-zinc-500"
                >
                  {entries.length === 0
                    ? "No activity yet. Register the MCP server (docs/mcp-setup.md) and let an agent loose."
                    : "No entries match this filter."}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function CollapsedRow({ group }: { group: Group }) {
  const [expanded, setExpanded] = useState(false);
  // members are newest-first; show the range oldest → newest.
  const start = formatDate(group.last.at);
  const end = formatDate(group.first.at);
  return (
    <>
      <tr className="bg-zinc-50/50">
        <td className="whitespace-nowrap px-4 py-2 text-zinc-600">
          {start} – {end}
        </td>
        <td className="px-4 py-2">
          <Badge label={group.first.actor} tone="zinc" />
        </td>
        <td className="whitespace-nowrap px-4 py-2 font-mono text-xs">
          {group.first.action}
          <span className="ml-1.5 rounded bg-zinc-200 px-1.5 py-0.5 font-sans text-[11px] font-medium text-zinc-600">
            ×{group.count}
          </span>
        </td>
        <td className="px-4 py-2 text-xs text-zinc-500">
          {group.count} identical events collapsed
          <button
            onClick={() => setExpanded((v) => !v)}
            className="ml-2 rounded border border-zinc-300 px-1.5 py-0.5 text-[11px] hover:bg-white"
          >
            {expanded ? "Hide" : "Show each"}
          </button>
        </td>
      </tr>
      {expanded &&
        group.members.map((m) => <AuditRow key={m.id} entry={m} nested />)}
    </>
  );
}

function AuditRow({ entry, nested }: { entry: Entry; nested?: boolean }) {
  let parsed:
    | { args?: Record<string, unknown>; note?: string; result?: string }
    | null = null;
  if (entry.actor === "mcp") {
    try {
      parsed = JSON.parse(entry.detail);
    } catch {
      parsed = null;
    }
  }
  const rejected = parsed?.result?.startsWith("rejected");

  return (
    <tr className={nested ? "bg-zinc-50" : undefined}>
      <td className="whitespace-nowrap px-4 py-2 text-zinc-600">
        <span className={nested ? "pl-4 text-zinc-400" : ""}>
          {formatDate(entry.at)}
        </span>
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
