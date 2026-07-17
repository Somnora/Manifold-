"use client";

import { useEffect, useMemo, useState } from "react";
import {
  api,
  ApiError,
  type Filesystem,
  type InstanceTypeInfo,
  type Region,
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
  const [regions, setRegions] = useState<Region[]>([]);
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
    Promise.all([api.instanceTypes(), api.filesystems(), api.regions()])
      .then(([t, fs, rs]) => {
        setTypes(t);
        setFilesystems(fs);
        setRegions(rs);
        setInstanceType((v) => v || Object.keys(t)[0] || "");
        if (fs.length > 0) setFilesystem((v) => v || fs[0].name);
        setRegion((v) => v || rs[0]?.code || "");
      })
      .catch((e) => setError(e.message));
  }, []);

  // A watch is usually for a region where the type has NO capacity right
  // now, so the picker offers EVERY known region (the old list only showed
  // regions with current capacity, which hid most of the map). Each option
  // says whether the chosen GPU has capacity there at this moment; Lambda
  // publishes no per-type region roster beyond that, so a watch in a region
  // that never carries the type will simply never fire.
  const hasCapacityNow = useMemo(
    () => new Set(types[instanceType]?.regions_with_capacity ?? []),
    [types, instanceType],
  );

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
            {regions.map((r) => (
              <option key={r.code} value={r.code}>
                {r.name} ({r.code})
                {hasCapacityNow.has(r.code)
                  ? " · has capacity now"
                  : ""}
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
