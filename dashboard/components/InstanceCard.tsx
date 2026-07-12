"use client";

import { useEffect, useState } from "react";
import { api, ApiError, type Instance, type UnpersistedFile } from "@/lib/api";
import { StatusBadge } from "@/components/Badge";
import { TelemetryChart } from "@/components/TelemetryChart";
import { TerminalPanel } from "@/components/TerminalPanel";
import { RecentFiles } from "@/components/RecentFiles";
import { FileNavigator } from "@/components/FileNavigator";
import { ChatPanel } from "@/components/ChatPanel";
import { formatBytes, formatMoney } from "@/lib/format";

export function InstanceCard({
  instance,
  onChanged,
}: {
  instance: Instance;
  onChanged: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [showTerminal, setShowTerminal] = useState(false);
  const [showFiles, setShowFiles] = useState(false);
  const [showBrowse, setShowBrowse] = useState(false);
  const [showChat, setShowChat] = useState(false);
  const [busy, setBusy] = useState<"" | "terminating" | "syncing">("");
  const [blockedFiles, setBlockedFiles] = useState<UnpersistedFile[] | null>(
    null,
  );
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  // Latch "has been connected". The SSH supervisor can briefly flip to
  // reconnecting when the box is saturated (e.g. downloading a 15GB model),
  // and gating the action buttons/panels on the LIVE state made the whole
  // UI — terminal included — disappear and reappear on every blip. Once an
  // instance has connected, keep the controls mounted; each panel shows its
  // own connection status. The card leaves entirely when the instance is
  // terminated (it drops out of the list), so nothing lingers.
  const connected = instance.connection_state === "connected";
  const [everConnected, setEverConnected] = useState(false);
  useEffect(() => {
    if (connected) setEverConnected(true);
  }, [connected]);

  async function terminate(force = false) {
    setBusy("terminating");
    setError("");
    try {
      await api.terminate(instance.id, force);
      onChanged();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409 && err.body?.blocked) {
        // The safety hook fired: show the evidence and the two ways out.
        setBlockedFiles(err.body.unpersisted_files as UnpersistedFile[]);
        setConfirming(false);
      } else {
        setError(err instanceof ApiError ? err.message : String(err));
        setConfirming(false);
      }
    } finally {
      setBusy("");
    }
  }

  async function toggleKeepAlive() {
    setError("");
    try {
      await api.setKeepAlive(instance.id, !instance.idle?.keep_alive);
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }

  async function syncThenTerminate() {
    setBusy("syncing");
    setError("");
    try {
      const result = await api.syncEphemeral(instance.id);
      setNotice(`Synced to ${result.synced_to}`);
      setBlockedFiles(null);
      // Re-attempt the normal (guarded) terminate; if new files appeared
      // since the sync, the hook will fire again — correctly.
      await terminate(false);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
      setBusy("");
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
            {instance.gpu_description || instance.instance_type} in{" "}
            {instance.region} at {formatMoney(instance.hourly_rate_usd)}/hr
          </p>
        </div>
        <div className="flex items-center gap-2 text-right">
          {everConnected && (
            <>
              <button
                onClick={() => setShowTerminal((s) => !s)}
                className={`rounded border px-3 py-1 text-xs font-medium ${
                  showTerminal
                    ? "border-zinc-900 bg-zinc-900 text-white"
                    : "border-zinc-300 text-zinc-700 hover:bg-zinc-50"
                }`}
              >
                {showTerminal ? "Close Terminal" : "Open Terminal"}
              </button>
              <button
                onClick={() => setShowFiles((s) => !s)}
                className={`rounded border px-3 py-1 text-xs font-medium ${
                  showFiles
                    ? "border-zinc-900 bg-zinc-900 text-white"
                    : "border-zinc-300 text-zinc-700 hover:bg-zinc-50"
                }`}
              >
                Files
              </button>
              <button
                onClick={() => setShowBrowse((s) => !s)}
                className={`rounded border px-3 py-1 text-xs font-medium ${
                  showBrowse
                    ? "border-zinc-900 bg-zinc-900 text-white"
                    : "border-zinc-300 text-zinc-700 hover:bg-zinc-50"
                }`}
              >
                Browse
              </button>
              <button
                onClick={() => setShowChat((s) => !s)}
                className={`rounded border px-3 py-1 text-xs font-medium ${
                  showChat
                    ? "border-zinc-900 bg-zinc-900 text-white"
                    : "border-zinc-300 text-zinc-700 hover:bg-zinc-50"
                }`}
              >
                Chat
              </button>
            </>
          )}
          {confirming ? (
            <div className="flex items-center gap-2">
              <span className="text-xs text-zinc-500">Terminate?</span>
              <button
                onClick={() => terminate(false)}
                disabled={busy !== ""}
                className="rounded bg-red-600 px-3 py-1 text-xs font-medium text-white hover:bg-red-500 disabled:opacity-50"
              >
                {busy === "terminating" ? "Terminating..." : "Yes, terminate"}
              </button>
              <button
                onClick={() => setConfirming(false)}
                disabled={busy !== ""}
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

      {instance.idle && instance.connection_state === "connected" && (
        <div className="mt-2 flex items-center gap-2 text-xs">
          {instance.idle.keep_alive ? (
            <span className="text-emerald-700">
              Idle auto-termination is off; this instance runs until you
              terminate it.
            </span>
          ) : (
            <span
              className={
                instance.idle.timeout_seconds - instance.idle.idle_seconds <
                300
                  ? "font-medium text-amber-700"
                  : "text-zinc-500"
              }
            >
              Idle {Math.floor(instance.idle.idle_seconds / 60)}m; auto
              terminates after{" "}
              {Math.round(instance.idle.timeout_seconds / 60)}m idle (
              {Math.max(
                0,
                Math.ceil(
                  (instance.idle.timeout_seconds -
                    instance.idle.idle_seconds) /
                    60,
                ),
              )}
              m left)
            </span>
          )}
          <button
            onClick={toggleKeepAlive}
            className="rounded border border-zinc-300 px-2 py-0.5 text-xs text-zinc-700 hover:bg-zinc-50"
          >
            {instance.idle.keep_alive ? "Resume auto-off" : "Keep alive"}
          </button>
        </div>
      )}

      {everConnected && <TelemetryChart instanceId={instance.id} />}

      {/* Panels stay mounted through transient reconnects; each surfaces its
          own connection state rather than being torn down (which flapped). */}
      {showTerminal && everConnected && (
        <TerminalPanel instanceId={instance.id} />
      )}
      {showFiles && everConnected && (
        <RecentFiles instanceId={instance.id} />
      )}
      {showBrowse && everConnected && (
        <FileNavigator instanceId={instance.id} />
      )}
      {showChat && everConnected && <ChatPanel instanceId={instance.id} />}

      {blockedFiles && (
        <div className="mt-3 rounded border border-amber-300 bg-amber-50 p-3">
          <p className="text-sm font-medium text-amber-900">
            Unsaved work on this instance
          </p>
          <p className="mt-1 text-xs text-amber-800">
            These files exist only in ephemeral scratch and will be destroyed
            by termination:
          </p>
          <ul className="mt-2 max-h-40 overflow-y-auto font-mono text-xs text-amber-900">
            {blockedFiles.map((f) => (
              <li key={f.path} className="flex justify-between gap-4 py-0.5">
                <span className="truncate">{f.path}</span>
                <span className="shrink-0 text-amber-700">
                  {formatBytes(f.size_bytes)}
                </span>
              </li>
            ))}
          </ul>
          <div className="mt-3 flex gap-2">
            <button
              onClick={syncThenTerminate}
              disabled={busy !== ""}
              className="rounded bg-zinc-900 px-3 py-1 text-xs font-medium text-white hover:bg-zinc-700 disabled:opacity-50"
            >
              {busy === "syncing"
                ? "Syncing..."
                : "Sync to persistent, then terminate"}
            </button>
            <button
              onClick={() => terminate(true)}
              disabled={busy !== ""}
              className="rounded border border-red-300 px-3 py-1 text-xs font-medium text-red-700 hover:bg-red-50 disabled:opacity-50"
            >
              Terminate anyway (lose files)
            </button>
            <button
              onClick={() => setBlockedFiles(null)}
              disabled={busy !== ""}
              className="rounded border border-zinc-300 px-3 py-1 text-xs hover:bg-zinc-50"
            >
              Keep running
            </button>
          </div>
        </div>
      )}

      {notice && <p className="mt-2 text-xs text-emerald-700">{notice}</p>}
      {instance.connection_error && (
        <p className="mt-2 text-xs text-amber-700">
          Connection: {instance.connection_error}
        </p>
      )}
      {error && <p className="mt-2 text-xs text-red-700">{error}</p>}
    </div>
  );
}
