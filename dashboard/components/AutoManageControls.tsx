"use client";

import { useEffect, useMemo, useState } from "react";
import {
  api,
  type Filesystem,
  type InstanceTypeInfo,
  type Region,
} from "@/lib/api";
import { formatMoney } from "@/lib/format";

export type AutoManageState = {
  enabled: boolean;
  gpu_type: string;
  region: string;
  filesystem: string;
};

// "Auto-manage instance": rent a GPU just for this job. When on, Manifold
// launches a dedicated instance (through the same guarded path as the launch
// form), runs the job, syncs outputs, and terminates it — so nothing bills
// while there is no work. The GPU/region/filesystem cascade mirrors the
// launch form: available GPUs first, regions with capacity for that GPU, then
// the region-locked filesystems.
export function AutoManageControls({
  value,
  onChange,
}: {
  value: AutoManageState;
  onChange: (next: AutoManageState) => void;
}) {
  const [types, setTypes] = useState<Record<string, InstanceTypeInfo>>({});
  const [regions, setRegions] = useState<Region[]>([]);
  const [filesystems, setFilesystems] = useState<Filesystem[]>([]);
  const [loadError, setLoadError] = useState("");

  useEffect(() => {
    Promise.all([api.instanceTypes(), api.regions(), api.filesystems()])
      .then(([t, r, fs]) => {
        setTypes(t);
        setRegions(r);
        setFilesystems(fs);
      })
      .catch((e) => setLoadError(e.message));
  }, []);

  const set = (patch: Partial<AutoManageState>) =>
    onChange({ ...value, ...patch });

  const typeOptions = useMemo(
    () =>
      Object.entries(types)
        .map(([name, t]) => ({
          name,
          t,
          available: t.regions_with_capacity.length > 0,
        }))
        .sort((a, b) => {
          if (a.available !== b.available) return a.available ? -1 : 1;
          return a.t.price_usd_per_hour - b.t.price_usd_per_hour;
        }),
    [types],
  );

  const selectedType = types[value.gpu_type];
  const fsRegions = useMemo(
    () => new Set(filesystems.map((f) => f.region)),
    [filesystems],
  );
  const regionOptions = useMemo(() => {
    const avail = new Set(selectedType?.regions_with_capacity ?? []);
    return regions
      .map((r) => ({
        ...r,
        available: avail.has(r.code),
        hasFs: fsRegions.has(r.code),
      }))
      .sort((a, b) => {
        if (a.available !== b.available) return a.available ? -1 : 1;
        if (a.available && a.hasFs !== b.hasFs) return a.hasFs ? -1 : 1;
        return a.name.localeCompare(b.name);
      });
  }, [regions, selectedType, fsRegions]);
  const fsInRegion = useMemo(
    () => filesystems.filter((f) => f.region === value.region),
    [filesystems, value.region],
  );

  // Seed a sensible default GPU (cheapest with capacity) once types load and
  // the user turns auto-manage on without a choice yet.
  useEffect(() => {
    if (!value.enabled || value.gpu_type || typeOptions.length === 0) return;
    const firstAvail = typeOptions.find((o) => o.available) ?? typeOptions[0];
    set({ gpu_type: firstAvail.name });
  }, [value.enabled, value.gpu_type, typeOptions]); // eslint-disable-line react-hooks/exhaustive-deps

  // Keep region valid for the GPU (prefer one that already has a filesystem).
  useEffect(() => {
    const avail = selectedType?.regions_with_capacity ?? [];
    if (avail.length === 0) return;
    if (!avail.includes(value.region)) {
      set({ region: avail.find((r) => fsRegions.has(r)) ?? avail[0] });
    }
  }, [value.gpu_type, selectedType, fsRegions]); // eslint-disable-line react-hooks/exhaustive-deps

  // Keep the filesystem inside the chosen region.
  useEffect(() => {
    if (fsInRegion.length > 0 && !fsInRegion.some((f) => f.name === value.filesystem)) {
      set({ filesystem: fsInRegion[0].name });
    }
  }, [value.region, fsInRegion]); // eslint-disable-line react-hooks/exhaustive-deps

  const field =
    "w-full min-w-0 max-w-full rounded border border-zinc-300 bg-white px-2 py-1 text-xs";
  const outOfCapacity =
    !!selectedType && selectedType.regions_with_capacity.length === 0;
  const noFs = value.region !== "" && fsInRegion.length === 0;

  return (
    <div className="rounded border border-sky-200 bg-sky-50/60 p-3">
      <label className="flex cursor-pointer items-start gap-2">
        <input
          type="checkbox"
          className="mt-0.5"
          checked={value.enabled}
          onChange={(e) => set({ enabled: e.target.checked })}
        />
        <span className="text-xs">
          <span className="font-medium text-sky-900">
            Auto-manage the instance for this job
          </span>
          <span className="mt-0.5 block text-sky-800/80">
            On: rent a NEW GPU just for this job - Manifold launches it, runs
            the job, syncs outputs, then terminates it. Off: the job queues
            onto an instance you already have running.
          </span>
        </span>
      </label>

      {value.enabled && (
        <>
          {loadError && (
            <p className="mt-2 text-xs text-red-700">{loadError}</p>
          )}
          <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-3">
            <label className="block min-w-0 text-[11px] font-medium text-sky-900">
              GPU
              <select
                className={`${field} mt-0.5`}
                value={value.gpu_type}
                onChange={(e) => set({ gpu_type: e.target.value })}
              >
                {typeOptions.map(({ name, t, available }) => (
                  <option key={name} value={name} disabled={!available}>
                    {formatMoney(t.price_usd_per_hour)}/hr ·{" "}
                    {t.gpu_description || t.description}
                    {available ? "" : " · out of capacity"}
                  </option>
                ))}
              </select>
            </label>
            <label className="block min-w-0 text-[11px] font-medium text-sky-900">
              Region
              <select
                className={`${field} mt-0.5`}
                value={value.region}
                onChange={(e) => set({ region: e.target.value })}
              >
                {regionOptions.map((r) => (
                  <option key={r.code} value={r.code} disabled={!r.available}>
                    {r.name} ({r.code})
                    {r.available
                      ? r.hasFs
                        ? " · has filesystem"
                        : ""
                      : " — n/a for this GPU"}
                  </option>
                ))}
              </select>
            </label>
            <label className="block min-w-0 text-[11px] font-medium text-sky-900">
              Filesystem
              <select
                className={`${field} mt-0.5`}
                value={value.filesystem}
                onChange={(e) => set({ filesystem: e.target.value })}
                disabled={fsInRegion.length === 0}
              >
                {fsInRegion.length === 0 && (
                  <option value="">none in this region</option>
                )}
                {fsInRegion.map((f) => (
                  <option key={f.name} value={f.name}>
                    {f.name}
                  </option>
                ))}
              </select>
            </label>
          </div>
          {(outOfCapacity || noFs) && (
            <p className="mt-2 text-xs text-amber-700">
              {outOfCapacity
                ? "That GPU is out of capacity everywhere right now. Manifold will keep waiting for a slot, or pick another GPU."
                : "No filesystem in this region. Pick a region where you have one."}
            </p>
          )}
        </>
      )}
    </div>
  );
}
