"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

// Agent Activity merged into the Activity page (Phase 38): the audit trail
// now lives beside the spend ledger. This stub keeps old links working.
export default function AgentsRedirect() {
  const router = useRouter();
  useEffect(() => {
    router.replace("/history?tab=audit");
  }, [router]);
  return (
    <p className="text-sm text-zinc-500">
      The audit trail moved to Activity. Taking you there...
    </p>
  );
}
