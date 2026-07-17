"use client";

import { useEffect, useState } from "react";
import {
  api,
  ApiError,
  type Filesystem,
  type Region,
  type StoredFile,
} from "@/lib/api";
import { formatBytes, formatDate } from "@/lib/format";

// Browse and delete files on a persistent filesystem via the backend's S3
// adapter endpoints. Works with no instance running: that is the point.
export default function StoragePage() {
  const [filesystems, setFilesystems] = useState<Filesystem[]>([]);
  const [selected, setSelected] = useState("");
  // Create a filebase without leaving for the Lambda console: pick a
  // region (e.g. where a GPU has capacity but no storage exists yet),
  // name it, done. Creation is free; storage bills by GB-month used.
  const [regions, setRegions] = useState<Region[]>([]);
  const [newName, setNewName] = useState("");
  const [newRegion, setNewRegion] = useState("");
  const [creating, setCreating] = useState(false);
  const [createNote, setCreateNote] = useState("");
  const [prefix, setPrefix] = useState("");
  const [files, setFiles] = useState<StoredFile[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  // Unreachable is UNKNOWN, not empty: only trust the file list (and the
  // "no files" / count copy) after a read that actually succeeded.
  const [readOk, setReadOk] = useState(false);
  const [confirmKey, setConfirmKey] = useState<string | null>(null);

  useEffect(() => {
    api
      .filesystems()
      .then((fs) => {
        setFilesystems(fs);
        if (fs.length > 0) setSelected((v) => v || fs[0].name);
      })
      .catch((e) => setError(e.message));
    api
      .regions()
      .then((rs) => {
        setRegions(rs);
        setNewRegion((v) => v || rs[0]?.code || "");
      })
      .catch(() => {});
  }, []);

  async function createFilebase(e: React.FormEvent) {
    e.preventDefault();
    if (!newName.trim() || !newRegion) return;
    setCreating(true);
    setCreateNote("");
    try {
      const fs = await api.createFilesystem(newName.trim(), newRegion);
      setFilesystems((prev) => [...prev, fs]);
      setSelected(fs.name);
      setNewName("");
      setCreateNote(
        `Created ${fs.name} in ${fs.region}. It is ready to mount on the ` +
          `next launch there.`,
      );
    } catch (err) {
      setCreateNote(err instanceof ApiError ? err.message : String(err));
    } finally {
      setCreating(false);
    }
  }

  useEffect(() => {
    if (!selected) return;
    let cancelled = false;
    setLoading(true);
    api
      .storageFiles(selected, prefix)
      .then((f) => {
        if (!cancelled) {
          setFiles(f);
          setError("");
          setReadOk(true);
        }
      })
      .catch((e) => {
        if (!cancelled) {
          setError(e.message);
          setReadOk(false); // read failed: the list on screen is not truth
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selected, prefix]);

  async function deleteFile(key: string) {
    try {
      await api.deleteFile(selected, key);
      setFiles((prev) => prev.filter((f) => f.key !== key));
      setConfirmKey(null);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }

  const fs = filesystems.find((f) => f.name === selected);
  const totalBytes = files.reduce((sum, f) => sum + f.size_bytes, 0);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <label className="block text-xs font-medium text-zinc-600">
          Filesystem
          <select
            className="mt-1 block rounded border border-zinc-300 bg-white px-2.5 py-1.5 text-sm"
            value={selected}
            onChange={(e) => setSelected(e.target.value)}
          >
            {filesystems.map((f) => (
              <option key={f.name} value={f.name}>
                {f.name} ({f.region})
              </option>
            ))}
          </select>
        </label>
        <label className="block flex-1 text-xs font-medium text-zinc-600">
          Prefix filter
          <input
            className="mt-1 block w-full max-w-sm rounded border border-zinc-300 bg-white px-2.5 py-1.5 font-mono text-sm"
            placeholder="e.g. models/"
            value={prefix}
            onChange={(e) => setPrefix(e.target.value)}
          />
        </label>
        {fs && (
          <p className="pb-1.5 text-xs text-zinc-500">
            mounted at {fs.mount_point} on instances
          </p>
        )}
      </div>

      <form
        onSubmit={createFilebase}
        className="flex flex-wrap items-end gap-3 rounded-lg border border-zinc-200 bg-white p-4"
      >
        <label className="block text-xs font-medium text-zinc-600">
          New filebase
          <input
            className="mt-1 block rounded border border-zinc-300 bg-white px-2.5 py-1.5 font-mono text-sm"
            placeholder="e.g. Somnora-Texas"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
          />
        </label>
        <label className="block text-xs font-medium text-zinc-600">
          Region
          <select
            className="mt-1 block rounded border border-zinc-300 bg-white px-2.5 py-1.5 text-sm"
            value={newRegion}
            onChange={(e) => setNewRegion(e.target.value)}
          >
            {regions.map((r) => (
              <option key={r.code} value={r.code}>
                {r.name} ({r.code})
              </option>
            ))}
          </select>
        </label>
        <button
          type="submit"
          disabled={creating || !newName.trim()}
          className="rounded bg-zinc-900 px-4 py-1.5 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-50"
        >
          {creating ? "Creating..." : "Create"}
        </button>
        <p className="pb-1.5 text-xs text-zinc-500">
          Creation is free; storage bills by the GB-month actually used.
          Filesystems are region-locked: create one where you plan to
          launch.
        </p>
        {createNote && (
          <p className="w-full text-xs text-zinc-600">{createNote}</p>
        )}
      </form>

      {error && (
        <p className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">
          {error}
        </p>
      )}

      <div
        className={`overflow-hidden rounded-lg border border-zinc-200 bg-white ${
          !readOk && !loading ? "opacity-40" : ""
        }`}
      >
        <table className="w-full text-sm">
          <thead className="bg-zinc-50 text-left text-xs uppercase tracking-wide text-zinc-500">
            <tr>
              <th className="px-4 py-2 font-medium">Key</th>
              <th className="px-4 py-2 font-medium">Size</th>
              <th className="px-4 py-2 font-medium">Modified</th>
              <th className="px-4 py-2" />
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-100">
            {files.map((f) => (
              <tr key={f.key}>
                <td className="px-4 py-2 font-mono text-xs">{f.key}</td>
                <td className="px-4 py-2 whitespace-nowrap text-zinc-600">
                  {formatBytes(f.size_bytes)}
                </td>
                <td className="px-4 py-2 whitespace-nowrap text-zinc-600">
                  {formatDate(f.last_modified)}
                </td>
                <td className="px-4 py-2 text-right">
                  {confirmKey === f.key ? (
                    <span className="inline-flex items-center gap-2">
                      <span className="text-xs text-zinc-500">Delete?</span>
                      <button
                        onClick={() => deleteFile(f.key)}
                        className="rounded bg-red-600 px-2 py-0.5 text-xs font-medium text-zinc-900 hover:bg-red-500"
                      >
                        Yes
                      </button>
                      <button
                        onClick={() => setConfirmKey(null)}
                        className="rounded border border-zinc-300 px-2 py-0.5 text-xs hover:bg-zinc-50"
                      >
                        No
                      </button>
                    </span>
                  ) : (
                    <button
                      onClick={() => setConfirmKey(f.key)}
                      className="rounded border border-red-200 px-2 py-0.5 text-xs text-red-700 hover:bg-red-50"
                    >
                      Delete
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {files.length === 0 && !loading && (
              <tr>
                <td
                  colSpan={4}
                  className="px-4 py-8 text-center text-sm text-zinc-500"
                >
                  {!readOk
                    ? "Can't read files right now: the backend or storage is unreachable, so this list is unknown (not necessarily empty)."
                    : selected
                      ? "No files match."
                      : "No filesystems available."}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {/* Count is a claim of fact: only make it after a successful read. */}
      {readOk ? (
        <p className="text-xs text-zinc-500">
          {files.length} file{files.length === 1 ? "" : "s"},{" "}
          {formatBytes(totalBytes)} total
        </p>
      ) : (
        <p className="text-xs text-zinc-400">File count unavailable.</p>
      )}
    </div>
  );
}
