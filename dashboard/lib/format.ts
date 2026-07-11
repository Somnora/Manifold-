export function formatBytes(n: number): string {
  if (n === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(Math.floor(Math.log2(n) / 10), units.length - 1);
  const value = n / 2 ** (10 * i);
  return `${value >= 100 ? value.toFixed(0) : value.toFixed(1)} ${units[i]}`;
}

export function formatMoney(usd: number): string {
  return `$${usd.toFixed(2)}`;
}

export function formatDate(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const h = Math.floor(seconds / 3600);
  const m = Math.round((seconds % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

// Cost of a launch: hourly rate times billable runtime. Billing starts when
// Lambda accepts the launch (launched_at) and ends at termination or now.
export function launchCost(launch: {
  hourly_rate_cents: number | null;
  launched_at: string | null;
  terminated_at: string | null;
}): { seconds: number; usd: number } | null {
  if (!launch.launched_at || launch.hourly_rate_cents == null) return null;
  const start = new Date(launch.launched_at).getTime();
  const end = launch.terminated_at
    ? new Date(launch.terminated_at).getTime()
    : Date.now();
  const seconds = Math.max(0, (end - start) / 1000);
  return {
    seconds,
    usd: (launch.hourly_rate_cents / 100) * (seconds / 3600),
  };
}
