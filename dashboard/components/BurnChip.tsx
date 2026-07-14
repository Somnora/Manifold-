"use client";

import Link from "next/link";
import { api } from "@/lib/api";
import { usePolling } from "@/lib/usePolling";
import { formatMoney } from "@/lib/format";

// The one number that matters, on every page: what running instances cost
// per hour RIGHT NOW. It used to live only on the Instances page, which
// meant a user deep in the Jobs or Autopilot page had no ambient signal
// that two H100s were burning. Click-through lands on Instances, where the
// terminate buttons are.
export function BurnChip() {
  const { data: instances } = usePolling(() => api.instances(), 10000);
  // Backend unreachable or still loading: show nothing rather than a
  // reassuring-but-unknown $0.
  if (!instances) return null;

  const burn = instances.reduce((sum, i) => sum + i.hourly_rate_usd, 0);
  const count = instances.length;
  const burning = burn > 0;

  return (
    <Link
      href="/"
      title={
        burning
          ? `${count} instance${count === 1 ? "" : "s"} running - click to manage`
          : "No instances running"
      }
      className={`flex h-8 items-center gap-1.5 rounded border px-2.5 font-mono text-xs transition-colors ${
        burning
          ? "border-amber-300/60 bg-amber-50 text-amber-800 hover:border-amber-400"
          : "border-zinc-200 text-zinc-400 hover:border-zinc-300 hover:text-zinc-600"
      }`}
    >
      <span
        className={`h-1.5 w-1.5 rounded-full ${
          burning ? "animate-pulse bg-amber-500" : "bg-emerald-500"
        }`}
      />
      {formatMoney(burn)}/hr
    </Link>
  );
}
