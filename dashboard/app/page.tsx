"use client";

import { useState } from "react";
import { api, type Launch } from "@/lib/api";
import { usePolling } from "@/lib/usePolling";
import { LaunchForm } from "@/components/LaunchForm";
import { InstanceCard } from "@/components/InstanceCard";
import { StatusBadge } from "@/components/Badge";
import { formatMoney, launchCost } from "@/lib/format";

const IN_FLIGHT = ["launching", "retrying", "booting"];
const RECENT_FAILURE_WINDOW_MS = 15 * 60 * 1000;

export default function InstancesPage() {
  const [dismissed, setDismissed] = useState<Set<string>>(new Set());

  const { data, error, refresh } = usePolling(async () => {
    const [instances, launches] = await Promise.all([
      api.instances(),
      api.launches(),
    ]);
    return { instances, launches };
  }, 2000);

  const instances = data?.instances ?? [];
  const launches = data?.launches ?? [];

  // Launches still working their way toward an instance card.
  const inFlight = launches.filter((l) => IN_FLIGHT.includes(l.status));
  // Recent failures stay visible until dismissed: never fail silently.
  const failed = launches.filter(
    (l) =>
      l.status === "failed" &&
      !dismissed.has(l.id) &&
      Date.now() - new Date(l.created_at).getTime() < RECENT_FAILURE_WINDOW_MS,
  );

  // Live cost picture: what running instances burn per hour, and what every
  // launch in the ledger has cost so far (running ones keep ticking).
  const hourlyBurn = instances.reduce((sum, i) => sum + i.hourly_rate_usd, 0);
  const totalSpend = launches.reduce(
    (sum, l) => sum + (launchCost(l)?.usd ?? 0),
    0,
  );

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-end gap-6 text-sm">
        <span className="text-zinc-500">
          Current burn:{" "}
          <span className="font-medium text-zinc-900">
            {formatMoney(hourlyBurn)}/hr
          </span>
        </span>
        <span className="text-zinc-500">
          Total spend:{" "}
          <span className="font-medium text-zinc-900">
            {formatMoney(totalSpend)}
          </span>
        </span>
      </div>

      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-zinc-500">
          Launch an instance
        </h2>
        <LaunchForm onLaunched={refresh} />
      </section>

      {error && (
        <p className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">
          {error}
        </p>
      )}

      {(inFlight.length > 0 || failed.length > 0) && (
        <section className="space-y-3">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">
            Pending launches
          </h2>
          {inFlight.map((l) => (
            <PendingLaunchCard key={l.id} launch={l} />
          ))}
          {failed.map((l) => (
            <FailedLaunchCard
              key={l.id}
              launch={l}
              onDismiss={() => setDismissed((prev) => new Set(prev).add(l.id))}
            />
          ))}
        </section>
      )}

      <section className="space-y-3">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">
          Running instances
        </h2>
        {instances.length === 0 ? (
          <p className="rounded-lg border border-dashed border-zinc-300 p-6 text-center text-sm text-zinc-500">
            No instances running. Nothing is billing.
          </p>
        ) : (
          instances.map((i) => (
            <InstanceCard key={i.id} instance={i} onChanged={refresh} />
          ))
        )}
      </section>
    </div>
  );
}

function PendingLaunchCard({ launch }: { launch: Launch }) {
  return (
    <div className="rounded-lg border border-amber-200 bg-amber-50 p-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <StatusBadge status={launch.status} />
          <span className="text-sm font-medium">
            {launch.requested_type} in {launch.region}
          </span>
        </div>
        <span className="text-xs text-zinc-500">attempt {launch.attempts}</span>
      </div>
      {launch.error && (
        <p className="mt-2 text-xs text-amber-800">{launch.error}</p>
      )}
    </div>
  );
}

function FailedLaunchCard({
  launch,
  onDismiss,
}: {
  launch: Launch;
  onDismiss: () => void;
}) {
  return (
    <div className="rounded-lg border border-red-200 bg-red-50 p-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <StatusBadge status="failed" />
          <span className="text-sm font-medium">
            {launch.requested_type} in {launch.region}
          </span>
          <span className="text-xs text-zinc-500">
            after {launch.attempts} attempt{launch.attempts === 1 ? "" : "s"}
          </span>
        </div>
        <button
          onClick={onDismiss}
          className="rounded border border-zinc-300 px-2 py-0.5 text-xs text-zinc-600 hover:bg-white"
        >
          Dismiss
        </button>
      </div>
      {launch.error && (
        <p className="mt-2 text-xs text-red-800">{launch.error}</p>
      )}
    </div>
  );
}
