"use client";

import { useState } from "react";
import { api, ApiError, type Instance } from "@/lib/api";
import { StatusBadge } from "@/components/Badge";
import { formatMoney } from "@/lib/format";

export function InstanceCard({
  instance,
  onChanged,
}: {
  instance: Instance;
  onChanged: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [terminating, setTerminating] = useState(false);
  const [error, setError] = useState("");

  async function terminate() {
    setTerminating(true);
    setError("");
    try {
      await api.terminate(instance.id);
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
      setTerminating(false);
      setConfirming(false);
    }
  }

  return (
    <div className="rounded-lg border border-zinc-200 bg-white p-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <h3 className="font-medium">{instance.name || instance.id}</h3>
            <StatusBadge status={instance.status} />
          </div>
          <p className="mt-1 text-sm text-zinc-500">
            {instance.instance_type} in {instance.region}
            {" at "}
            {formatMoney(instance.hourly_rate_usd)}/hr
          </p>
        </div>
        <div className="text-right">
          {confirming ? (
            <div className="flex items-center gap-2">
              <span className="text-xs text-zinc-500">Terminate?</span>
              <button
                onClick={terminate}
                disabled={terminating}
                className="rounded bg-red-600 px-3 py-1 text-xs font-medium text-white hover:bg-red-500 disabled:opacity-50"
              >
                {terminating ? "Terminating..." : "Yes, terminate"}
              </button>
              <button
                onClick={() => setConfirming(false)}
                disabled={terminating}
                className="rounded border border-zinc-300 px-3 py-1 text-xs hover:bg-zinc-50"
              >
                Cancel
              </button>
            </div>
          ) : (
            <button
              onClick={() => setConfirming(true)}
              className="rounded border border-red-200 px-3 py-1 text-xs font-medium text-red-700 hover:bg-red-50"
            >
              Terminate
            </button>
          )}
        </div>
      </div>

      <dl className="mt-3 grid grid-cols-2 gap-x-6 gap-y-1 text-sm md:grid-cols-4">
        <div>
          <dt className="text-xs text-zinc-400">SSH connection</dt>
          <dd className="mt-0.5">
            <StatusBadge status={instance.connection_state} />
          </dd>
        </div>
        <div>
          <dt className="text-xs text-zinc-400">Mode</dt>
          <dd className="mt-0.5 text-zinc-700">
            {instance.connection_mode ?? "unknown"}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-zinc-400">IP</dt>
          <dd className="mt-0.5 font-mono text-xs text-zinc-700">
            {instance.ip ?? "assigning..."}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-zinc-400">Filesystem</dt>
          <dd className="mt-0.5 text-zinc-700">
            {instance.filesystems.join(", ") || "none"}
          </dd>
        </div>
      </dl>

      {instance.connection_error && (
        <p className="mt-2 text-xs text-amber-700">
          Connection: {instance.connection_error}
        </p>
      )}
      {error && <p className="mt-2 text-xs text-red-700">{error}</p>}
    </div>
  );
}
