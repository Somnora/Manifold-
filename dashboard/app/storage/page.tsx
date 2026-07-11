"use client";

import { useEffect, useState } from "react";
import { api, ApiError, type Filesystem, type StoredFile } from "@/lib/api";
import { formatBytes, formatDate } from "@/lib/format";

// Browse and delete files on a persistent filesystem via the backend's S3
// adapter endpoints. Works with no instance running: that is the point.
export default function StoragePage() {
  const [filesystems, setFilesystems] = useState<Filesystem[]>([]);
  const [selected, setSelected] = useState("");
  const [prefix, setPrefix] = useState("");
  const [files, setFiles] = useState<StoredFile[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [confirmKey, setConfirmKey] = useState<string | null>(null);

  useEffect(() => {
    api
      .filesystems()
      .then((fs) => {
        setFilesystems(fs);
        if (fs.length > 0) setSelected((v) => v || fs[0].name);
      })
      .catch((e) => setError(e.message));
  }, []);

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
        }
      })
      .catch((e) => {
        if (!cancelled) setError(e.message);
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

      {error && (
        <p className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">
          {error}
        </p>
      )}

      <div className="overflow-hidden rounded-lg border border-zinc-200 bg-white">
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
                        className="rounded bg-red-600 px-2 py-0.5 text-xs font-medium text-white hover:bg-red-500"
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
                  {selected
                    ? "No files match."
                    : "No filesystems available."}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <p className="text-xs text-zinc-500">
        {files.length} file{files.length === 1 ? "" : "s"},{" "}
        {formatBytes(totalBytes)} total
      </p>
    </div>
  );
}
