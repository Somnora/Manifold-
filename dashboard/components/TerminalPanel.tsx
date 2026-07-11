"use client";

import { useEffect, useRef, useState } from "react";
import "@xterm/xterm/css/xterm.css";

const WS_BASE = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000")
  .replace(/^http/, "ws");

// A real shell on the instance: xterm.js <-> backend WS <-> SSH session on
// the managed connection. Nothing runs on the instance except sshd; closing
// the panel closes the shell.
export function TerminalPanel({ instanceId }: { instanceId: string }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [status, setStatus] = useState<"connecting" | "open" | "closed">(
    "connecting",
  );

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    let disposed = false;
    let cleanup: (() => void) | undefined;

    // xterm touches the DOM at import time in some builds; load it client-side.
    (async () => {
      const [{ Terminal }, { FitAddon }] = await Promise.all([
        import("@xterm/xterm"),
        import("@xterm/addon-fit"),
      ]);
      if (disposed) return;

      const term = new Terminal({
        cursorBlink: true,
        fontSize: 13,
        fontFamily:
          "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
        theme: { background: "#09090b", foreground: "#e4e4e7" },
      });
      const fit = new FitAddon();
      term.loadAddon(fit);
      term.open(el);
      fit.fit();

      const ws = new WebSocket(`${WS_BASE}/instances/${instanceId}/terminal`);

      ws.onopen = () => {
        setStatus("open");
        ws.send(
          JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }),
        );
        term.focus();
      };
      ws.onmessage = (event) => term.write(event.data as string);
      ws.onclose = () => setStatus("closed");
      ws.onerror = () => setStatus("closed");

      const dataSub = term.onData((data) => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: "input", data }));
        }
      });

      const onResize = () => {
        fit.fit();
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(
            JSON.stringify({
              type: "resize",
              cols: term.cols,
              rows: term.rows,
            }),
          );
        }
      };
      window.addEventListener("resize", onResize);
      // Refit once the panel has its final size.
      const refit = setTimeout(onResize, 50);

      cleanup = () => {
        clearTimeout(refit);
        window.removeEventListener("resize", onResize);
        dataSub.dispose();
        ws.close();
        term.dispose();
      };
    })();

    return () => {
      disposed = true;
      cleanup?.();
    };
  }, [instanceId]);

  return (
    <div className="mt-3 overflow-hidden rounded border border-zinc-700 bg-[#09090b]">
      <div className="flex items-center justify-between border-b border-zinc-800 px-3 py-1.5">
        <span className="text-xs text-zinc-400">
          Terminal — SSH via backend (managed connection)
        </span>
        <span
          className={`text-xs ${
            status === "open"
              ? "text-emerald-400"
              : status === "closed"
                ? "text-red-400"
                : "text-zinc-400"
          }`}
        >
          {status}
        </span>
      </div>
      <div ref={containerRef} className="h-80 p-2" />
    </div>
  );
}
