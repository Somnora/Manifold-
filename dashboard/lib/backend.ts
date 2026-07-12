// Where the backend lives - THE single source of truth for API and WS URLs.
//
// Three cases:
// - NEXT_PUBLIC_API_URL set: explicit override, used verbatim.
// - Served BY the backend (packaged desktop app, or the static export
//   mounted on :8000): same-origin, so the base is "".
// - Next dev server on :3000: the backend is on localhost:8000.
function detectApiBase(): string {
  if (process.env.NEXT_PUBLIC_API_URL) return process.env.NEXT_PUBLIC_API_URL;
  if (typeof window !== "undefined" && window.location.port !== "3000") {
    return "";
  }
  return "http://localhost:8000";
}

export const API_BASE = detectApiBase();

// WebSocket base, derived the same way. Called inside effects, so window
// is always available.
export function wsBase(): string {
  const base = detectApiBase();
  if (base === "") {
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    return `${proto}://${window.location.host}`;
  }
  return base.replace(/^http/, "ws");
}
