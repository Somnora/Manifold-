"use client";

import { useCallback } from "react";
import { api } from "@/lib/api";
import { usePolling } from "@/lib/usePolling";
import { Badge } from "@/components/Badge";
import { formatBytes, formatDate } from "@/lib/format";

// Live view of what the instance is producing: recently changed files on
// both volumes, newest first, relayed from the sidecar.
export function RecentFiles({ instanceId }: { instanceId: string }) {
  const load = useCallback(
    () => api.recentFiles(instanceId),
    [instanceId],
  );
  const { data, error } = usePolling(load, 5000);

  if (error) {
    return <p className="mt-3 text-xs text-amber-700">Files: {error}</p>;
  }
  const files = data?.files ?? [];

  return (
    <div className="mt-3 rounded border border-zinc-100 bg-zinc-50 p-3">
      <p className="text-xs text-zinc-500">
        Recent files (last {data?.hours ?? 24}h)
        {data?.truncated && " — large tree, list truncated"}
      </p>
      {files.length === 0 ? (
        <p className="mt-2 text-xs text-zinc-400">Nothing written yet.</p>
      ) : (
        <ul className="mt-2 max-h-48 overflow-y-auto">
          {files.map((f) => (
            <li
              key={`${f.root}:${f.path}`}
              className="flex items-center justify-between gap-3 py-0.5 font-mono text-xs text-zinc-700"
            >
              <span className="flex min-w-0 items-center gap-2">
                <Badge
                  label={f.root}
                  tone={f.root === "persistent" ? "green" : "amber"}
                />
                <span className="truncate">{f.path}</span>
              </span>
              <span className="shrink-0 text-zinc-400">
                {formatBytes(f.size_bytes)} · {formatDate(f.modified)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
