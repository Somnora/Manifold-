"use client";

import { useEffect, useMemo, useState } from "react";
import {
  api,
  ApiError,
  type Filesystem,
  type InstanceTypeInfo,
} from "@/lib/api";
import { formatMoney } from "@/lib/format";

// The form only collects input; every rule (region match, budget,
// concurrency) is enforced by the backend, and its rejection message is
// shown verbatim so the user sees the same truth every client sees.
export function LaunchForm({ onLaunched }: { onLaunched: () => void }) {
  const [types, setTypes] = useState<Record<string, InstanceTypeInfo>>({});
  const [filesystems, setFilesystems] = useState<Filesystem[]>([]);
  const [instanceType, setInstanceType] = useState("");
  const [region, setRegion] = useState("");
  const [filesystem, setFilesystem] = useState("");
  const [mode, setMode] = useState("direct-ssh");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [loadError, setLoadError] = useState("");

  useEffect(() => {
    Promise.all([api.instanceTypes(), api.filesystems()])
      .then(([t, fs]) => {
        setTypes(t);
        setFilesystems(fs);
        const firstType = Object.keys(t)[0] ?? "";
        setInstanceType((v) => v || firstType);
        if (fs.length > 0) {
          setFilesystem((v) => v || fs[0].name);
          // Filesystems are region-locked, so the filesystem's region is
          // the sensible default. The select stays editable; a mismatch is
          // the backend's job to reject.
          setRegion((v) => v || fs[0].region);
        } else {
          setRegion((v) => v || t[firstType]?.regions_with_capacity[0] || "");
        }
      })
      .catch((e) => setLoadError(e.message));
  }, []);

  const regionOptions = useMemo(() => {
    const fromType = types[instanceType]?.regions_with_capacity ?? [];
    const fromFilesystems = filesystems.map((f) => f.region);
    return [...new Set([...fromType, ...fromFilesystems])].sort();
  }, [types, instanceType, filesystems]);

  const selectedType = types[instanceType];

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError("");
    try {
      await api.launch({
        instance_type: instanceType,
        region,
        filesystem,
        connection_mode: mode,
      });
      onLaunched();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  }

  const field =
    "w-full rounded border border-zinc-300 bg-white px-2.5 py-1.5 text-sm";

  if (loadError) {
    return (
      <div className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-800">
        {loadError}
      </div>
    );
  }

  return (
    <form
      onSubmit={submit}
      className="rounded-lg border border-zinc-200 bg-white p-4"
    >
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <label className="block text-xs font-medium text-zinc-600">
          Instance type
          <select
            className={`${field} mt-1`}
            value={instanceType}
            onChange={(e) => setInstanceType(e.target.value)}
          >
            {Object.entries(types).map(([name, t]) => (
              <option key={name} value={name}>
                {name} ({formatMoney(t.price_usd_per_hour)}/hr)
              </option>
            ))}
          </select>
        </label>
        <label className="block text-xs font-medium text-zinc-600">
          Region
          <select
            className={`${field} mt-1`}
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
        <label className="block text-xs font-medium text-zinc-600">
          Filesystem
          <select
            className={`${field} mt-1`}
            value={filesystem}
            onChange={(e) => {
              const name = e.target.value;
              setFilesystem(name);
              const fs = filesystems.find((f) => f.name === name);
              if (fs) setRegion(fs.region);
            }}
          >
            {filesystems.map((f) => (
              <option key={f.name} value={f.name}>
                {f.name} ({f.region})
              </option>
            ))}
          </select>
        </label>
        <label className="block text-xs font-medium text-zinc-600">
          Connection
          <select
            className={`${field} mt-1`}
            value={mode}
            onChange={(e) => setMode(e.target.value)}
          >
            <option value="direct-ssh">direct-ssh</option>
            <option value="tailscale">tailscale</option>
          </select>
        </label>
      </div>

      <div className="mt-3 flex items-center justify-between gap-4">
        <p className="text-xs text-zinc-500">
          {selectedType
            ? `${selectedType.description}: ${selectedType.specs.gpus} GPU, ` +
              `${selectedType.specs.vcpus} vCPU, ${selectedType.specs.memory_gib} GiB RAM`
            : ""}
        </p>
        <button
          type="submit"
          disabled={submitting || !instanceType || !filesystem}
          className="rounded bg-zinc-900 px-4 py-1.5 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-50"
        >
          {submitting ? "Launching..." : "Launch"}
        </button>
      </div>

      {error && (
        <p className="mt-3 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">
          {error}
        </p>
      )}
    </form>
  );
}
