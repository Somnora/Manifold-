"use client";

import { useEffect, useRef, useState } from "react";
import { api, ApiError, type SidecarDiagnosis } from "@/lib/api";

type GpuSample = {
  name: string;
  vram_used_mib: number;
  vram_total_mib: number;
  utilization_pct: number;
  temperature_c: number;
};

const WS_BASE = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000")
  .replace(/^http/, "ws");
const HISTORY = 60; // samples kept per series

// Live GPU telemetry over the backend's relay WebSocket
// (pynvml -> sidecar -> SSH forward -> backend -> here).
export function TelemetryChart({ instanceId }: { instanceId: string }) {
  const [latest, setLatest] = useState<GpuSample | null>(null);
  const [history, setHistory] = useState<{ util: number[]; vram: number[] }>({
    util: [],
    vram: [],
  });
  const [state, setState] = useState<"connecting" | "live" | "unavailable">(
    "connecting",
  );
  const wsRef = useRef<WebSocket | null>(null);
  const [diag, setDiag] = useState<SidecarDiagnosis | null>(null);
  const [diagBusy, setDiagBusy] = useState(false);
  const [diagErr, setDiagErr] = useState("");

  async function runDiagnose() {
    setDiagBusy(true);
    setDiagErr("");
    try {
      setDiag(await api.diagnoseSidecar(instanceId));
    } catch (e) {
      setDiagErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      setDiagBusy(false);
    }
  }

  useEffect(() => {
    let closed = false;
    const ws = new WebSocket(`${WS_BASE}/instances/${instanceId}/metrics/stream`);
    wsRef.current = ws;

    ws.onmessage = (event) => {
      const payload = JSON.parse(event.data);
      if (!payload.available || !payload.gpus?.length) {
        setState("unavailable");
        return;
      }
      const gpu: GpuSample = payload.gpus[0];
      setLatest(gpu);
      setState("live");
      setHistory((h) => ({
        util: [...h.util, gpu.utilization_pct].slice(-HISTORY),
        vram: [
          ...h.vram,
          (gpu.vram_used_mib / gpu.vram_total_mib) * 100,
        ].slice(-HISTORY),
      }));
    };
    ws.onerror = () => {
      if (!closed) setState("unavailable");
    };
    ws.onclose = () => {
      if (!closed) setState("unavailable");
    };
    return () => {
      closed = true;
      ws.close();
    };
  }, [instanceId]);

  if (state === "unavailable") {
    return (
      <div className="mt-3 text-xs text-zinc-400">
        <div className="flex items-center gap-2">
          <span>Telemetry unavailable (sidecar not reachable yet).</span>
          <button
            onClick={runDiagnose}
            disabled={diagBusy}
            className="rounded border border-zinc-300 px-2 py-0.5 text-zinc-600 hover:bg-zinc-50 disabled:opacity-50"
          >
            {diagBusy ? "Diagnosing..." : "Diagnose"}
          </button>
        </div>
        {diagErr && <p className="mt-2 text-red-600">{diagErr}</p>}
        {diag && (
          <div className="mt-2 rounded border border-zinc-200 bg-zinc-50 p-3 text-zinc-700">
            <p className="font-medium text-zinc-800">
              {diag.summary}
            </p>
            <p className="mt-1 font-mono text-[11px] text-zinc-400">
              cause: {diag.cause}
            </p>
            <div className="mt-2 space-y-2">
              {diag.checks.map((c) => (
                <div key={c.label}>
                  <p className="font-medium text-zinc-600">{c.label}</p>
                  <pre className="mt-0.5 overflow-x-auto whitespace-pre-wrap rounded bg-zinc-950 p-2 text-[11px] leading-relaxed text-zinc-800">
                    {c.output || "(no output)"}
                  </pre>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="mt-3 rounded border border-zinc-100 bg-zinc-50 p-3">
      <div className="flex items-center justify-between text-xs text-zinc-500">
        <span>
          {latest ? latest.name : "GPU"} telemetry{" "}
          {state === "connecting" && "(connecting...)"}
        </span>
        {latest && (
          <span className="flex gap-4 font-mono">
            <span>{latest.utilization_pct}% util</span>
            <span>
              {(latest.vram_used_mib / 1024).toFixed(1)}/
              {(latest.vram_total_mib / 1024).toFixed(0)} GiB VRAM
            </span>
            <span>{latest.temperature_c}°C</span>
          </span>
        )}
      </div>
      <div className="mt-2 flex gap-4">
        <Sparkline label="Utilization" values={history.util} color="#38bdf8" />
        <Sparkline label="VRAM" values={history.vram} color="#a78bfa" />
      </div>
    </div>
  );
}

// Tiny dependency-free sparkline: values are percentages (0-100).
function Sparkline({
  label,
  values,
  color,
}: {
  label: string;
  values: number[];
  color: string;
}) {
  const w = 220;
  const h = 40;
  const points = values
    .map((v, i) => {
      const x = (i / Math.max(values.length - 1, 1)) * w;
      const y = h - (Math.min(v, 100) / 100) * h;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  return (
    <div className="flex-1">
      <svg
        viewBox={`0 0 ${w} ${h}`}
        className="h-10 w-full"
        preserveAspectRatio="none"
        role="img"
        aria-label={`${label} history`}
      >
        <line x1="0" y1={h} x2={w} y2={h} stroke="#27272f" strokeWidth="1" />
        {values.length > 1 && (
          <polyline
            points={points}
            fill="none"
            stroke={color}
            strokeWidth="1.5"
          />
        )}
      </svg>
      <p className="mt-0.5 text-[10px] uppercase tracking-wide text-zinc-400">
        {label}
      </p>
    </div>
  );
}
