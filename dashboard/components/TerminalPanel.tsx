"use client";

import { useEffect, useRef, useState } from "react";
import "@xterm/xterm/css/xterm.css";
import { wsBase } from "@/lib/backend";


// A real shell in the dashboard: xterm.js <-> backend WS. Two flavors of
// the same wire protocol: an instance shell (SSH session over the managed
// connection) or, with wsPath="/local/terminal", a shell on THIS machine
// (the local half of the hub). Closing the panel closes the shell.
export function TerminalPanel({
  instanceId,
  wsPath,
  label,
}: {
  instanceId?: string;
  wsPath?: string;
  label?: string;
}) {
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
        scrollback: 5000,
      });
      const fit = new FitAddon();
      term.loadAddon(fit);
      term.open(el);

      // Fit to the host element's real box, then keep the prompt in view.
      // The host has NO padding of its own (padding lives on the wrapper),
      // so FitAddon's row math is exact and the last row never clips.
      const doFit = () => {
        try {
          fit.fit();
        } catch {
          return;
        }
        term.scrollToBottom();
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(
            JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }),
          );
        }
      };

      const path = wsPath ?? `/instances/${instanceId}/terminal`;
      const ws = new WebSocket(`${wsBase()}${path}`);

      ws.onopen = () => {
        setStatus("open");
        // Fit once the socket is up so the initial resize reaches the PTY.
        requestAnimationFrame(doFit);
        term.focus();
      };
      ws.onmessage = (event) => {
        term.write(event.data as string, () => term.scrollToBottom());
      };
      ws.onclose = () => setStatus("closed");
      ws.onerror = () => setStatus("closed");

      const dataSub = term.onData((data) => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: "input", data }));
        }
      });

      // Refit whenever the panel actually changes size (open animation,
      // window resize, sidebar toggles) — more reliable than a one-shot timer.
      const observer = new ResizeObserver(() => doFit());
      observer.observe(el);
      window.addEventListener("resize", doFit);
      requestAnimationFrame(doFit);

      cleanup = () => {
        observer.disconnect();
        window.removeEventListener("resize", doFit);
        dataSub.dispose();
        ws.close();
        term.dispose();
      };
    })();

    return () => {
      disposed = true;
      cleanup?.();
    };
  }, [instanceId, wsPath]);

  return (
    <div className="mt-3 overflow-hidden rounded border border-zinc-300 bg-[#09090b]">
      <div className="flex items-center justify-between border-b border-zinc-200 px-3 py-1.5">
        <span className="text-xs text-zinc-400">
          {label ?? "Terminal (SSH via the managed connection)"} · drag the
          bottom-right corner to resize
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
      {/* Padding is on THIS wrapper; the xterm host below fills it exactly
          so FitAddon measures a clean, padding-free box. resize-y gives a
          native drag handle; the ResizeObserver refits rows on every drag. */}
      <div className="h-80 min-h-40 max-h-[85vh] resize-y overflow-hidden p-2">
        <div ref={containerRef} className="h-full w-full" />
      </div>
    </div>
  );
}
