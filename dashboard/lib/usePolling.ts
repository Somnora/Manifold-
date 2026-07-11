"use client";

import { useCallback, useEffect, useRef, useState } from "react";

// Poll an async loader on an interval. Boring by design: the backend is
// local, payloads are tiny, and polling keeps the dashboard state honest
// without a websocket layer. `refresh()` forces an immediate reload.
export function usePolling<T>(
  load: () => Promise<T>,
  intervalMs: number,
): { data: T | null; error: string; refresh: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const loadRef = useRef(load);
  loadRef.current = load;

  const tick = useCallback(async () => {
    try {
      setData(await loadRef.current());
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    tick();
    const id = setInterval(tick, intervalMs);
    return () => clearInterval(id);
  }, [tick, intervalMs]);

  return { data, error, refresh: tick };
}
