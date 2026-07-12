"use client";

import { useEffect, useMemo, useState } from "react";
import { api, type Estimate, type InstanceTypeInfo } from "@/lib/api";
import { formatMoney } from "@/lib/format";

// Pre-launch cost/runtime estimate for a template on a chosen GPU. Advisory:
// it never changes anything, it just tells you what a run is likely to cost.
// The estimate sharpens as run history accumulates (measured vs rough).
// When `instanceType` is supplied (auto-manage picks the GPU), the widget
// follows that choice and hides its own picker so there is one GPU control.
export function EstimateWidget({
  template,
  instanceType,
}: {
  template: string;
  instanceType?: string;
}) {
  const [types, setTypes] = useState<Record<string, InstanceTypeInfo>>({});
  const [pickedGpu, setPickedGpu] = useState("");
  const [est, setEst] = useState<Estimate | null>(null);
  const [loading, setLoading] = useState(false);
  const gpu = instanceType || pickedGpu;
  const controlled = !!instanceType;

  useEffect(() => {
    api
      .instanceTypes()
      .then((t) => {
        setTypes(t);
        const withCap = Object.entries(t)
          .filter(([, i]) => i.regions_with_capacity.length > 0)
          .sort((a, b) => a[1].price_usd_per_hour - b[1].price_usd_per_hour);
        setPickedGpu(
          (v) => v || withCap[0]?.[0] || Object.keys(t)[0] || "gpu_1x_a10",
        );
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (!template || !gpu) return;
    let cancelled = false;
    setLoading(true);
    api
      .estimate(template, gpu)
      .then((e) => !cancelled && setEst(e))
      .catch(() => !cancelled && setEst(null))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [template, gpu]);

  const options = useMemo(
    () =>
      Object.entries(types).sort(
        (a, b) => a[1].price_usd_per_hour - b[1].price_usd_per_hour,
      ),
    [types],
  );
  const rate = types[gpu]?.price_usd_per_hour;

  return (
    <div className="rounded border border-indigo-100 bg-indigo-50/50 p-3 text-xs">
      <div className="flex min-w-0 items-center justify-between gap-2">
        <span className="font-medium text-indigo-900">Estimated cost</span>
        {controlled && (
          <span className="min-w-0 truncate text-indigo-800">
            {gpu}
            {rate != null ? ` (${formatMoney(rate)}/hr)` : ""}
          </span>
        )}
      </div>
      {/* The picker sits on its own full-width row BENEATH the label: a
          closed <select> sizes itself to its widest option and would burst
          out of the plate if it shared the header row. w-full + max-w-full
          pins the closed control inside the plate; the opened dropdown and
          the estimate line still show the full text. */}
      {!controlled && (
        <select
          className="mt-1.5 block w-full min-w-0 max-w-full rounded border border-indigo-200 bg-white px-1.5 py-0.5 text-xs"
          value={gpu}
          onChange={(e) => setPickedGpu(e.target.value)}
        >
          {options.map(([name, t]) => (
            <option key={name} value={name}>
              {name} ({formatMoney(t.price_usd_per_hour)}/hr)
            </option>
          ))}
        </select>
      )}

      <div className="mt-1.5 text-indigo-900" title={est?.basis}>
        {loading || !est ? (
          <span className="text-indigo-400">estimating…</span>
        ) : est.confidence === "none" ? (
          <span>
            Runs until you stop it —{" "}
            <span className="font-medium">
              {rate != null ? `${formatMoney(rate)}/hr while running` : "cost depends on runtime"}
            </span>
          </span>
        ) : (
          <span>
            ≈ <span className="font-medium">{fmtMinutes(est.minutes)}</span>
            {est.cost_usd != null && (
              <>
                {" · "}
                <span className="font-medium">~{formatMoney(est.cost_usd)}</span>
              </>
            )}{" "}
            <ConfidenceTag est={est} />
          </span>
        )}
      </div>
    </div>
  );
}

function fmtMinutes(m: number | null): string {
  if (m == null) return "unknown";
  if (m < 90) return `${Math.round(m)} min`;
  return `${(m / 60).toFixed(1)} hr`;
}

function ConfidenceTag({ est }: { est: Estimate }) {
  if (est.confidence === "measured") {
    return (
      <span className="rounded bg-emerald-100 px-1.5 py-0.5 text-[11px] font-medium text-emerald-800">
        measured · {est.sample_size} runs
      </span>
    );
  }
  return (
    <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[11px] font-medium text-amber-800">
      rough{est.sample_size > 0 ? ` · ${est.sample_size} run(s)` : " · no history yet"}
    </span>
  );
}
