"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { TerminalPanel } from "@/components/TerminalPanel";

// The terminal dock: one shared surface for every shell - the local machine
// and any docked instance terminals - snappable to the BOTTOM or the RIGHT
// side of the viewport, with two arrangements:
//
//   tabs   one shell visible, the rest mounted-but-hidden behind tabs
//   split  all shells visible: side by side when the dock is at the bottom,
//          stacked when it is on the right (each pane keeps a usable width)
//
// Invariants carried over from the single-terminal drawer (learned the hard
// way in Phase 38):
//
// - Rendered through a portal to document.body. The header's backdrop-blur
//   creates a CSS containing block, which re-anchors position:fixed
//   descendants to the header instead of the viewport.
// - While open, the dock's exact footprint is reserved as padding on the
//   page body (bottom or right to match the snap), so the dock never hides
//   content: the page always scrolls fully clear of it.
// - Sessions stay MOUNTED while hidden (behind a tab, or with the dock
//   closed), so a shell - and anything running in it, codex included -
//   survives tab switches, dock hides, and page navigation. A session ends
//   only when its tab's x is clicked or the shell exits.

type Session = {
  id: string;
  kind: "local" | "instance";
  instanceId?: string;
  label: string;
};

type Position = "bottom" | "right";
type Arrangement = "tabs" | "split";

type DockState = {
  open: boolean;
  sessions: Session[];
  toggleLocal: () => void;
  dockInstance: (instanceId: string, name: string) => void;
};

const DockContext = createContext<DockState | null>(null);

export function useTerminalDock(): DockState {
  const ctx = useContext(DockContext);
  if (!ctx) throw new Error("useTerminalDock outside TerminalDockProvider");
  return ctx;
}

const LOCAL: Session = {
  id: "local",
  kind: "local",
  label: "This machine",
};

const MIN_HEIGHT = 180;
const MIN_WIDTH = 360;
const DEFAULT_HEIGHT = 320;
const DEFAULT_WIDTH = 520;
// The right-snapped dock starts below the sticky header (py-3 + h-8 content
// + 1px border) so the nav, burn chip, and bell stay reachable.
const HEADER_PX = 57;

export function TerminalDockProvider({
  children,
}: {
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [active, setActive] = useState<string>("local");
  const [position, setPosition] = useState<Position>("bottom");
  const [arrangement, setArrangement] = useState<Arrangement>("tabs");
  const [height, setHeight] = useState(DEFAULT_HEIGHT);
  const [width, setWidth] = useState(DEFAULT_WIDTH);
  // Portals need document; render only after mount.
  const [onClient, setOnClient] = useState(false);
  useEffect(() => setOnClient(true), []);

  const toggleLocal = useCallback(() => {
    setSessions((s) => (s.some((x) => x.id === "local") ? s : [LOCAL, ...s]));
    setOpen((o) => {
      // If the dock is open but local is buried behind another tab, the
      // button surfaces it instead of closing the dock.
      if (o && active !== "local" && arrangement === "tabs") {
        setActive("local");
        return true;
      }
      return !o;
    });
    if (!open) setActive("local");
  }, [open, active, arrangement]);

  const dockInstance = useCallback((instanceId: string, name: string) => {
    const id = `instance:${instanceId}`;
    setSessions((s) =>
      s.some((x) => x.id === id)
        ? s
        : [
            ...s,
            {
              id,
              kind: "instance",
              instanceId,
              label: name || instanceId.slice(0, 12),
            },
          ],
    );
    setActive(id);
    setOpen(true);
  }, []);

  const closeSession = useCallback(
    (id: string) => {
      setSessions((s) => {
        const next = s.filter((x) => x.id !== id);
        if (next.length === 0) setOpen(false);
        else if (active === id) setActive(next[next.length - 1].id);
        return next;
      });
    },
    [active],
  );

  // Reserve the dock's footprint at the matching page edge (see above).
  useEffect(() => {
    document.body.style.paddingBottom =
      open && position === "bottom" ? `${height}px` : "";
    document.body.style.paddingRight =
      open && position === "right" ? `${width}px` : "";
    return () => {
      document.body.style.paddingBottom = "";
      document.body.style.paddingRight = "";
    };
  }, [open, position, height, width]);

  // Drag the dock's inner edge to resize: top edge when snapped to the
  // bottom, left edge when snapped to the right. Clamped so it can neither
  // collapse nor swallow the window.
  const startDrag = useCallback(
    (e: React.PointerEvent) => {
      e.preventDefault();
      const move = (ev: PointerEvent) => {
        if (position === "bottom") {
          const h = window.innerHeight - ev.clientY;
          setHeight(
            Math.min(
              Math.max(h, MIN_HEIGHT),
              Math.round(window.innerHeight * 0.8),
            ),
          );
        } else {
          const w = window.innerWidth - ev.clientX;
          setWidth(
            Math.min(
              Math.max(w, MIN_WIDTH),
              Math.round(window.innerWidth * 0.7),
            ),
          );
        }
      };
      const stop = () => {
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", stop);
        document.body.style.userSelect = "";
      };
      document.body.style.userSelect = "none";
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", stop);
    },
    [position],
  );

  const bottom = position === "bottom";
  const split = arrangement === "split" && sessions.length > 1;

  return (
    <DockContext.Provider
      value={{ open, sessions, toggleLocal, dockInstance }}
    >
      {children}

      {onClient &&
        sessions.length > 0 &&
        createPortal(
          <div
            className={`fixed z-40 flex bg-zinc-50 ${
              bottom
                ? "inset-x-0 bottom-0 flex-col border-t border-zinc-300 shadow-[0_-8px_30px_rgba(0,0,0,0.35)]"
                : "bottom-0 right-0 flex-row border-l border-zinc-300 shadow-[-8px_0_30px_rgba(0,0,0,0.35)]"
            } ${open ? "" : "hidden"}`}
            style={
              bottom ? { height } : { width, top: HEADER_PX }
            }
          >
            {/* Resize handle on the inner edge. */}
            <div
              onPointerDown={startDrag}
              title="Drag to resize"
              className={`group flex shrink-0 items-center justify-center ${
                bottom ? "h-3 cursor-ns-resize" : "w-3 cursor-ew-resize"
              }`}
            >
              <span
                className={`rounded-full bg-zinc-300 transition-colors group-hover:bg-teal-400 ${
                  bottom ? "h-1 w-12" : "h-12 w-1"
                }`}
              />
            </div>

            <div
              className={`flex h-full min-h-0 w-full min-w-0 flex-col ${
                bottom ? "mx-auto max-w-6xl px-6" : "pr-3"
              } pb-3`}
            >
              {/* Tab strip + dock controls. */}
              <div className="flex shrink-0 flex-wrap items-center gap-1 pb-1.5 pt-0.5">
                {sessions.map((s) => (
                  <span
                    key={s.id}
                    className={`flex items-center overflow-hidden rounded border text-xs ${
                      !split && active === s.id
                        ? "border-zinc-900 bg-zinc-900 text-white"
                        : "border-zinc-300 bg-white text-zinc-600"
                    }`}
                  >
                    <button
                      onClick={() => setActive(s.id)}
                      className="px-2.5 py-1"
                      title={
                        s.kind === "local"
                          ? "Shell on this machine"
                          : `SSH shell on ${s.label}`
                      }
                    >
                      <span
                        className={`mr-1.5 font-mono text-[10px] ${
                          s.kind === "local"
                            ? "text-teal-400"
                            : "text-emerald-500"
                        }`}
                      >
                        {s.kind === "local" ? ">_" : "gpu"}
                      </span>
                      {s.label}
                    </button>
                    <button
                      onClick={() => closeSession(s.id)}
                      title="Close this shell (ends the session)"
                      className="px-1.5 py-1 opacity-60 hover:opacity-100"
                    >
                      ×
                    </button>
                  </span>
                ))}

                <span className="ml-auto flex items-center gap-1">
                  {sessions.length > 1 && (
                    <DockButton
                      onClick={() =>
                        setArrangement(split ? "tabs" : "split")
                      }
                      title={
                        split
                          ? "One shell at a time, behind tabs"
                          : bottom
                            ? "Show all shells side by side"
                            : "Show all shells stacked"
                      }
                      label={split ? "Tabs" : "Split"}
                    />
                  )}
                  <DockButton
                    onClick={() => setPosition(bottom ? "right" : "bottom")}
                    title={
                      bottom
                        ? "Snap the dock to the right side"
                        : "Snap the dock to the bottom"
                    }
                    label={bottom ? "Snap right" : "Snap bottom"}
                  />
                  <DockButton
                    onClick={() => setOpen(false)}
                    title="Hide the dock - every shell keeps running"
                    label="Hide"
                  />
                </span>
              </div>

              {/* Sessions. All stay mounted; visibility is CSS only. In
                  split, panes run across a bottom dock and down a right
                  dock, so each keeps a usable shape. */}
              <div
                className={`min-h-0 min-w-0 flex-1 gap-2 ${
                  split ? (bottom ? "flex flex-row" : "flex flex-col") : ""
                }`}
              >
                {sessions.map((s) => (
                  <div
                    key={s.id}
                    className={
                      split
                        ? "min-h-0 min-w-0 flex-1"
                        : `h-full min-h-0 ${active === s.id ? "" : "hidden"}`
                    }
                  >
                    <TerminalPanel
                      fill
                      instanceId={s.instanceId}
                      wsPath={s.kind === "local" ? "/local/terminal" : undefined}
                      label={
                        s.kind === "local"
                          ? "Shell on this machine (loopback-only)"
                          : `${s.label} (SSH via the managed connection)`
                      }
                    />
                  </div>
                ))}
              </div>
            </div>
          </div>,
          document.body,
        )}
    </DockContext.Provider>
  );
}

function DockButton({
  onClick,
  title,
  label,
}: {
  onClick: () => void;
  title: string;
  label: string;
}) {
  return (
    <button
      onClick={onClick}
      title={title}
      className="rounded border border-zinc-300 px-2 py-0.5 text-xs text-zinc-600 hover:bg-zinc-100"
    >
      {label}
    </button>
  );
}

// The header button: opens the dock with the local shell (or surfaces the
// local tab if the dock is already open on another shell).
export function TerminalDockToggle() {
  const { open, toggleLocal } = useTerminalDock();
  return (
    <button
      onClick={toggleLocal}
      aria-label={open ? "Hide terminal dock" : "Open local terminal"}
      title="Terminal on this machine"
      className={`flex h-8 w-8 items-center justify-center rounded border font-mono text-[11px] font-semibold transition-colors ${
        open
          ? "border-zinc-900 bg-zinc-900 text-white"
          : "border-zinc-200 text-zinc-500 hover:border-zinc-300 hover:text-zinc-800"
      }`}
    >
      {">_"}
    </button>
  );
}
