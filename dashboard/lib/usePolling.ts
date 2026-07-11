"use client";

import { useCallback, useEffect, useRef, useState } from "react";

// Poll an async loader on an interval. Boring by design: the backend is
// local, payloads are tiny, and polling keeps the dashboard state honest
// without a websocket layer. `refresh()` forces an immediate reload.
export function usePolling<T>(
  load: () => Promise<T>,
  intervalMs: number,
): {
  data: T | null;
  error: string;
  /** true when data exists but the LATEST poll failed: what's on screen is
      a snapshot from lastSuccess, not live state. Render it as such. */
  stale: boolean;
  lastSuccess: Date | null;
  refresh: () => void;
} {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [lastSuccess, setLastSuccess] = useState<Date | null>(null);
  const loadRef = useRef(load);
  loadRef.current = load;

  const tick = useCallback(async () => {
    try {
      setData(await loadRef.current());
      setError("");
      setLastSuccess(new Date());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    tick();
    const id = setInterval(tick, intervalMs);
    return () => clearInterval(id);
  }, [tick, intervalMs]);

  return { data, error, stale: !!(error && data), lastSuccess, refresh: tick };
}
