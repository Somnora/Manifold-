"use client";

import { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { TerminalPanel } from "@/components/TerminalPanel";

const MIN_HEIGHT = 180;
const DEFAULT_HEIGHT = 320;

// A shell on THIS machine, one keystroke from every page. The button lives
// in the header; the drawer rises from the BOTTOM of the viewport.
//
// Two invariants, both learned the hard way:
//
// - The panel is rendered through a portal to document.body. It must NOT be
//   a DOM child of the header: the header's backdrop-blur creates a CSS
//   containing block, which re-anchors position:fixed descendants to the
//   header instead of the viewport - the drawer then appears at the TOP of
//   the page, covers the nav, and clips the terminal off-screen.
// - While open, the drawer's exact height is reserved as bottom padding on
//   the page body, so the drawer never hides content: whatever its size,
//   the page still scrolls all the way to its true bottom.
//
// Persistence: once opened, the panel stays MOUNTED and is only hidden with
// CSS, so closing the drawer does not kill the shell. Navigate anywhere,
// reopen, and the session (history, cwd, running command) is where you left
// it; type `exit` to end it. Loopback-only and origin-checked on the
// backend; hub.local_terminal: false removes the endpoint entirely.
export function TerminalDrawer() {
  const [open, setOpen] = useState(false);
  // Latches true on first open and never resets - this keeps the shell
  // alive behind a closed drawer.
  const [everOpened, setEverOpened] = useState(false);
  const [height, setHeight] = useState(DEFAULT_HEIGHT);
  // Portals need document; render the drawer only after mount.
  const [onClient, setOnClient] = useState(false);
  useEffect(() => setOnClient(true), []);

  // Reserve the drawer's footprint at the end of the page (see above).
  useEffect(() => {
    document.body.style.paddingBottom = open ? `${height}px` : "";
    return () => {
      document.body.style.paddingBottom = "";
    };
  }, [open, height]);

  // Drag the top edge to resize: the drawer is anchored at the bottom, so
  // height = viewport bottom minus the pointer. Clamped so it can neither
  // collapse into uselessness nor swallow the whole window.
  const startDrag = useCallback((e: React.PointerEvent) => {
    e.preventDefault();
    const move = (ev: PointerEvent) => {
      const h = window.innerHeight - ev.clientY;
      setHeight(
        Math.min(Math.max(h, MIN_HEIGHT), Math.round(window.innerHeight * 0.8)),
      );
    };
    const stop = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", stop);
      document.body.style.userSelect = "";
    };
    document.body.style.userSelect = "none";
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop);
  }, []);

  return (
    <>
      <button
        onClick={() => {
          setOpen((s) => !s);
          setEverOpened(true);
        }}
        aria-label={open ? "Hide local terminal" : "Open local terminal"}
        title="Terminal on this machine"
        className={`flex h-8 w-8 items-center justify-center rounded border font-mono text-[11px] font-semibold transition-colors ${
          open
            ? "border-zinc-900 bg-zinc-900 text-white"
            : "border-zinc-200 text-zinc-500 hover:border-zinc-300 hover:text-zinc-800"
        }`}
      >
        {">_"}
      </button>

      {onClient &&
        everOpened &&
        createPortal(
          <div
            className={`fixed inset-x-0 bottom-0 z-40 flex flex-col border-t border-zinc-300 bg-zinc-50 shadow-[0_-8px_30px_rgba(0,0,0,0.35)] ${
              open ? "" : "hidden"
            }`}
            style={{ height }}
          >
            {/* The resize handle: grab anywhere on this strip. */}
            <div
              onPointerDown={startDrag}
              title="Drag to resize"
              className="group flex h-3 shrink-0 cursor-ns-resize items-center justify-center"
            >
              <span className="h-1 w-12 rounded-full bg-zinc-300 transition-colors group-hover:bg-teal-400" />
            </div>

            <div className="mx-auto flex h-full min-h-0 w-full max-w-6xl flex-col px-6 pb-3">
              <div className="flex shrink-0 items-center justify-between pb-1">
                <span className="text-xs font-semibold uppercase tracking-wide text-zinc-500">
                  Local terminal
                </span>
                <div className="flex items-center gap-3">
                  <span className="text-[11px] text-zinc-400">
                    keeps running while hidden - type{" "}
                    <span className="font-mono">exit</span> to end the shell
                  </span>
                  <button
                    onClick={() => setOpen(false)}
                    className="rounded border border-zinc-300 px-2 py-0.5 text-xs text-zinc-600 hover:bg-zinc-100"
                  >
                    Hide
                  </button>
                </div>
              </div>
              <div className="min-h-0 flex-1">
                <TerminalPanel
                  fill
                  wsPath="/local/terminal"
                  label="Shell on this machine (loopback-only)"
                />
              </div>
            </div>
          </div>,
          document.body,
        )}
    </>
  );
}
