"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

// The Hub merged into Autopilot (Phase 38): brains and approvals live where
// runs start, and the local terminal became the drawer in the header (the
// ">_" button, available on every page). This stub keeps old links working.
export default function HubRedirect() {
  const router = useRouter();
  useEffect(() => {
    router.replace("/autopilot");
  }, [router]);
  return (
    <p className="text-sm text-zinc-500">
      The Hub moved into Autopilot. Taking you there...
    </p>
  );
}
