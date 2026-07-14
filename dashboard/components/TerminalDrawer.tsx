"use client";

import { useState } from "react";
import { TerminalPanel } from "@/components/TerminalPanel";

// A shell on THIS machine, one keystroke from every page. It used to be a
// section on the Hub page - but a terminal is a tool, not a destination, so
// it became a bottom drawer toggled from the header.
//
// Persistence is the point: once opened, the panel stays MOUNTED and is only
// hidden with CSS, so closing the drawer does not kill your shell. Navigate
// anywhere, reopen, and your session (history, cwd, running command) is
// exactly where you left it. The shell ends when you type `exit` or quit the
// app. Loopback-only and origin-checked on the backend; switch it off
// entirely with hub.local_terminal: false in config.yaml.
export function TerminalDrawer() {
  const [open, setOpen] = useState(false);
  // Latches true on first open and never resets - this is what keeps the
  // shell alive behind a closed drawer.
  const [everOpened, setEverOpened] = useState(false);

  function toggle() {
    setOpen((s) => !s);
    setEverOpened(true);
  }

  return (
    <>
      <button
        onClick={toggle}
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

      {everOpened && (
        <div
          className={`fixed inset-x-0 bottom-0 z-50 border-t border-zinc-300 bg-zinc-50 shadow-[0_-8px_30px_rgba(0,0,0,0.35)] ${
            open ? "" : "hidden"
          }`}
        >
          <div className="mx-auto max-w-6xl px-6 pb-3">
            <div className="flex items-center justify-between pt-2">
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
            <TerminalPanel
              wsPath="/local/terminal"
              label="Shell on this machine (loopback-only)"
            />
          </div>
        </div>
      )}
    </>
  );
}
