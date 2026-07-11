"use client";

import { useEffect, useMemo, useState } from "react";
import {
  api,
  ApiError,
  type Filesystem,
  type InstanceTypeInfo,
  type Watch,
} from "@/lib/api";
import { usePolling } from "@/lib/usePolling";
import { Badge, StatusBadge } from "@/components/Badge";
import { formatDate } from "@/lib/format";

// Capacity watches: "tell me when 1x H100 shows up in us-east-3."
// The backend polls the catalog; a watch flips to `available` (and, if
// enabled in config.yaml AND requested on the watch, auto-launches through
// the same guarded pipeline as every other launch).
export function WatchPanel() {
  const [types, setTypes] = useState<Record<string, InstanceTypeInfo>>({});
  const [filesystems, setFilesystems] = useState<Filesystem[]>([]);
  const [instanceType, setInstanceType] = useState("");
  const [region, setRegion] = useState("");
  const [autoLaunch, setAutoLaunch] = useState(false);
  const [filesystem, setFilesystem] = useState("");
  const [error, setError] = useState("");

  const { data, refresh } = usePolling(() => api.watches(), 5000);
  const watches = (data?.watches ?? []).filter(
    (w) => w.status !== "cancelled",
  );

  useEffect(() => {
    Promise.all([api.instanceTypes(), api.filesystems()])
      .then(([t, fs]) => {
        setTypes(t);
        setFilesystems(fs);
        setInstanceType((v) => v || Object.keys(t)[0] || "");
        if (fs.length > 0) setFilesystem((v) => v || fs[0].name);
      })
      .catch((e) => setError(e.message));
  }, []);

  // For a watch you usually want a region where the type is NOT currently
  // available — offer all known regions.
  const regionOptions = useMemo(() => {
    const all = new Set<string>();
    for (const t of Object.values(types)) {
      for (const r of t.regions_with_capacity) all.add(r);
    }
    for (const f of filesystems) all.add(f.region);
    return [...all].sort();
  }, [types, filesystems]);

  useEffect(() => {
    setRegion((v) => v || regionOptions[0] || "");
  }, [regionOptions]);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    try {
      await api.createWatch({
        instance_type: instanceType,
        region,
        filesystem: autoLaunch ? filesystem : undefined,
        auto_launch: autoLaunch,
      });
      refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }

  async function cancel(id: string) {
    try {
      await api.cancelWatch(id);
      refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }

  const field =
    "rounded border border-zinc-300 bg-white px-2.5 py-1.5 text-sm";

  return (
    <div className="rounded-lg border border-zinc-200 bg-white p-4">
      <form onSubmit={create} className="flex flex-wrap items-end gap-3">
        <label className="block text-xs font-medium text-zinc-600">
          GPU to watch for
          <select
            className={`${field} mt-1 block`}
            value={instanceType}
            onChange={(e) => setInstanceType(e.target.value)}
          >
            {Object.entries(types).map(([name, t]) => (
              <option key={name} value={name}>
                {t.description}
              </option>
            ))}
          </select>
        </label>
        <label className="block text-xs font-medium text-zinc-600">
          Region
          <select
            className={`${field} mt-1 block`}
            value={region}
            onChange={(e) => setRegion(e.target.value)}
          >
            {regionOptions.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-1.5 pb-2 text-xs font-medium text-zinc-600">
          <input
            type="checkbox"
            checked={autoLaunch}
            onChange={(e) => setAutoLaunch(e.target.checked)}
          />
          auto-launch
        </label>
        {autoLaunch && (
          <label className="block text-xs font-medium text-zinc-600">
            Filesystem
            <select
              className={`${field} mt-1 block`}
              value={filesystem}
              onChange={(e) => setFilesystem(e.target.value)}
            >
              {filesystems.map((f) => (
                <option key={f.name} value={f.name}>
                  {f.name} ({f.region})
                </option>
              ))}
            </select>
          </label>
        )}
        <button
          type="submit"
          className="rounded bg-zinc-900 px-4 py-1.5 text-sm font-medium text-white hover:bg-zinc-700"
        >
          Watch
        </button>
      </form>
      {autoLaunch && data && !data.auto_launch_enabled && (
        <p className="mt-2 text-xs text-amber-700">
          Note: auto-launch is disabled in config.yaml
          (watches.auto_launch_enabled) — this watch will only notify.
        </p>
      )}
      {error && <p className="mt-2 text-xs text-red-700">{error}</p>}

      {watches.length > 0 && (
        <ul className="mt-4 divide-y divide-zinc-100">
          {watches.map((w) => (
            <li key={w.id} className="flex items-center justify-between py-2">
              <div className="flex items-center gap-2 text-sm">
                <StatusBadge status={w.status} />
                <span>
                  {types[w.instance_type]?.description ?? w.instance_type} in{" "}
                  {w.region}
                </span>
                {w.auto_launch === 1 && (
                  <Badge label="auto-launch" tone="zinc" />
                )}
              </div>
              <div className="flex items-center gap-3 text-xs text-zinc-500">
                {w.status === "available" && w.triggered_at && (
                  <span className="font-medium text-emerald-700">
                    capacity since {formatDate(w.triggered_at)}
                  </span>
                )}
                {w.status === "watching" && (
                  <button
                    onClick={() => cancel(w.id)}
                    className="rounded border border-zinc-300 px-2 py-0.5 hover:bg-zinc-50"
                  >
                    Cancel
                  </button>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
