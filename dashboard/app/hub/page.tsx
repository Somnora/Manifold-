"use client";

import { useState } from "react";
import { api, type Brain } from "@/lib/api";
import { usePolling } from "@/lib/usePolling";
import { TerminalPanel } from "@/components/TerminalPanel";
import { ApprovalsPanel } from "@/components/ApprovalsPanel";

const KIND_LABEL: Record<Brain["kind"], string> = {
  instance: "GPU instance",
  local: "this machine",
  api: "frontier API",
  cli: "your login",
};

const KIND_TONE: Record<Brain["kind"], string> = {
  instance: "bg-emerald-100 text-emerald-800",
  local: "bg-sky-100 text-sky-800",
  api: "bg-indigo-100 text-indigo-800",
  cli: "bg-amber-100 text-amber-800",
};

// The Hub: where the local machine and the cloud meet. A terminal on THIS
// machine, every brain Manifold can put in charge (GPU-served, local
// Ollama/LM Studio, frontier APIs), and any approvals waiting on you.
export default function HubPage() {
  const { data: brains } = usePolling(() => api.brains(), 5000);
  const [showTerminal, setShowTerminal] = useState(true);

  return (
    <div className="space-y-6">
      <ApprovalsPanel />

      <section>
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">
            Local terminal
          </h2>
          <button
            onClick={() => setShowTerminal((s) => !s)}
            className="rounded border border-zinc-300 px-2 py-1 text-xs text-zinc-600 hover:bg-zinc-50"
          >
            {showTerminal ? "Hide" : "Show"}
          </button>
        </div>
        {showTerminal ? (
          <div className="rounded-lg border border-zinc-200 bg-white p-1">
            <TerminalPanel wsPath="/local/terminal" />
          </div>
        ) : (
          <p className="rounded-lg border border-dashed border-zinc-300 p-4 text-center text-xs text-zinc-500">
            Terminal hidden. The shell closes when the panel closes.
          </p>
        )}
        <p className="mt-2 text-xs text-zinc-400">
          A login shell on this machine - the same terminal the instances get,
          pointed at your own box. Loopback-only, origin-checked, and it can
          be switched off with{" "}
          <span className="font-mono">hub.local_terminal: false</span> in
          config.yaml.
        </p>
      </section>

      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-zinc-500">
          Brains
        </h2>
        <div className="space-y-2">
          {(brains ?? []).map((b: Brain) => (
            <div
              key={b.ref}
              className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-zinc-200 bg-white p-3"
            >
              <div className="flex min-w-0 items-center gap-2.5">
                <span
                  className={`shrink-0 rounded-full px-2 py-0.5 text-[11px] font-medium ${KIND_TONE[b.kind]}`}
                >
                  {KIND_LABEL[b.kind]}
                </span>
                <span className="truncate text-sm font-medium">{b.model}</span>
              </div>
              <span className="truncate font-mono text-xs text-zinc-400">
                {b.detail || b.ref}
              </span>
            </div>
          ))}
          {(brains ?? []).length === 0 && (
            <div className="rounded-lg border border-dashed border-zinc-300 p-6 text-sm text-zinc-500">
              <p className="font-medium text-zinc-600">No brains found yet.</p>
              <ul className="mt-2 list-inside list-disc space-y-1 text-xs">
                <li>
                  Serve a model on a GPU instance (Jobs page,{" "}
                  <span className="font-mono">vllm-serve</span>) - it appears
                  here once running.
                </li>
                <li>
                  Start Ollama or LM Studio on this machine - detected
                  automatically within seconds.
                </li>
                <li>
                  Log into a frontier CLI once (claude, codex, or gemini) -
                  it appears here via your own subscription, no API key
                  needed.
                </li>
                <li>
                  Or add an Anthropic / OpenAI / Gemini API key to .env
                  (ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY) - the
                  frontier brain appears on the next refresh.
                </li>
              </ul>
            </div>
          )}
        </div>
        <p className="mt-2 text-xs text-zinc-400">
          Any brain here can drive an Autopilot run or be the reasoning end of
          a pipeline: a local model orchestrating cloud GPUs, a frontier model
          reviewing a fine-tune, one instance directing another. Pick it on
          the Autopilot page.
        </p>
      </section>
    </div>
  );
}
