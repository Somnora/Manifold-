"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { usePolling } from "@/lib/usePolling";

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

type Message = { role: "user" | "assistant"; content: string };

// Chat with the model served on THIS instance (a running vllm-serve job).
// Tokens stream: backend relays vLLM's OpenAI-style SSE over the managed
// SSH connection; we parse the chunks and append deltas as they arrive.
export function ChatPanel({ instanceId }: { instanceId: string }) {
  const load = useCallback(() => api.modelStatus(instanceId), [instanceId]);
  const { data: model } = usePolling(load, 5000);

  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState("");
  const scrollRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages]);

  useEffect(() => () => abortRef.current?.abort(), []);

  async function send(e: React.FormEvent) {
    e.preventDefault();
    const content = input.trim();
    if (!content || streaming) return;
    setInput("");
    setError("");
    const history: Message[] = [...messages, { role: "user", content }];
    // Add the user message plus an empty assistant message to stream into.
    setMessages([...history, { role: "assistant", content: "" }]);
    setStreaming(true);

    const abort = new AbortController();
    abortRef.current = abort;
    try {
      const resp = await fetch(`${API_BASE}/instances/${instanceId}/chat`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ messages: history }),
        signal: abort.signal,
      });
      if (!resp.ok || !resp.body) {
        const body = await resp.json().catch(() => ({}));
        throw new Error(body.detail ?? `HTTP ${resp.status}`);
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        // SSE events are separated by blank lines; keep the tail partial.
        const events = buffer.split("\n\n");
        buffer = events.pop() ?? "";
        for (const event of events) {
          const data = event
            .split("\n")
            .filter((l) => l.startsWith("data: "))
            .map((l) => l.slice(6))
            .join("");
          if (!data || data === "[DONE]") continue;
          try {
            const chunk = JSON.parse(data);
            if (chunk.error) throw new Error(chunk.error);
            const delta: string =
              chunk.choices?.[0]?.delta?.content ?? "";
            if (delta) {
              setMessages((m) => {
                const out = [...m];
                out[out.length - 1] = {
                  role: "assistant",
                  content: out[out.length - 1].content + delta,
                };
                return out;
              });
            }
          } catch (err) {
            if (err instanceof Error && !(err instanceof SyntaxError)) {
              throw err;
            }
            // Ignore unparseable keep-alive lines.
          }
        }
      }
    } catch (err) {
      if (!(err instanceof DOMException && err.name === "AbortError")) {
        setError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setStreaming(false);
      abortRef.current = null;
    }
  }

  if (!model?.serving) {
    return (
      <div className="mt-3 rounded border border-zinc-100 bg-zinc-50 p-4 text-sm text-zinc-500">
        No model is being served here yet. Queue a{" "}
        <span className="font-mono text-xs">vllm-serve</span> job on the Jobs
        page with the HuggingFace model you want; once it is ready, the chat
        opens automatically. (First run downloads the weights to persistent
        storage — subsequent serves start much faster.)
      </div>
    );
  }

  if (!model.ready) {
    return (
      <div className="mt-3 flex items-start gap-3 rounded border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
        <span className="mt-1.5 h-2 w-2 shrink-0 animate-pulse rounded-full bg-amber-500" />
        <span>
          <span className="font-mono">{model.model_id}</span> is loading on
          this instance: downloading the weights (a 7B model is ~15 GB on
          first serve) and warming up the GPU. Chat opens automatically when
          it is ready — this can take several minutes. Watch real progress in
          the job&apos;s <span className="font-medium">Logs</span> on the Jobs
          page (look for the model shards downloading).
          {model.status_detail ? (
            <span className="mt-1 block text-xs text-amber-700">
              Not answering yet — normal while it loads (probe:{" "}
              {model.status_detail}).
            </span>
          ) : null}
        </span>
      </div>
    );
  }

  return (
    <div className="mt-3 overflow-hidden rounded border border-zinc-200">
      <div className="flex items-center justify-between border-b border-zinc-200 bg-zinc-50 px-3 py-1.5">
        <span className="text-xs text-zinc-500">
          Chatting with{" "}
          <span className="font-mono text-zinc-700">{model.model_id}</span> on
          this instance
        </span>
        {streaming && (
          <button
            onClick={() => abortRef.current?.abort()}
            className="rounded border border-zinc-300 px-2 py-0.5 text-xs hover:bg-zinc-100"
          >
            Stop
          </button>
        )}
      </div>

      <div ref={scrollRef} className="max-h-96 space-y-3 overflow-y-auto p-3">
        {messages.length === 0 && (
          <p className="text-sm text-zinc-400">
            Say something — tokens are generated on this GPU and streamed
            back over the managed SSH connection.
          </p>
        )}
        {messages.map((m, i) => (
          <div
            key={i}
            className={m.role === "user" ? "flex justify-end" : "flex"}
          >
            <div
              className={
                m.role === "user"
                  ? "max-w-[80%] rounded-lg bg-zinc-900 px-3 py-2 text-sm text-white"
                  : "max-w-[80%] whitespace-pre-wrap rounded-lg bg-zinc-100 px-3 py-2 text-sm text-zinc-800"
              }
            >
              {m.content ||
                (streaming && i === messages.length - 1 ? "…" : "")}
            </div>
          </div>
        ))}
      </div>

      {error && (
        <p className="border-t border-red-100 bg-red-50 px-3 py-2 text-xs text-red-700">
          {error}
        </p>
      )}

      <form onSubmit={send} className="flex gap-2 border-t border-zinc-200 p-2">
        <input
          className="flex-1 rounded border border-zinc-300 px-2.5 py-1.5 text-sm"
          placeholder={`Message ${model.model_id}...`}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          disabled={streaming}
        />
        <button
          type="submit"
          disabled={streaming || !input.trim()}
          className="rounded bg-zinc-900 px-4 py-1.5 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-50"
        >
          Send
        </button>
      </form>
    </div>
  );
}
