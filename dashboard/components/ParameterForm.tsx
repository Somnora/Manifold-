"use client";

import { useState } from "react";
import type { Template, TemplateParameter } from "@/lib/api";

// The whole point of this component: it renders ANY template's parameter
// schema. Adding a new template YAML requires zero frontend changes.
export function ParameterForm({
  template,
  onSubmit,
  submitting,
}: {
  template: Template;
  onSubmit: (values: Record<string, unknown>) => void;
  submitting: boolean;
}) {
  const [values, setValues] = useState<Record<string, string>>({});

  function currentValue(p: TemplateParameter): string {
    if (p.name in values) return values[p.name];
    return p.default != null ? String(p.default) : "";
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const payload: Record<string, unknown> = {};
    for (const p of template.parameters) {
      const raw = currentValue(p);
      if (raw === "") continue; // let backend apply defaults / flag missing
      payload[p.name] = raw;    // backend coerces types from the schema
    }
    onSubmit(payload);
  }

  const field =
    "w-full rounded border border-zinc-300 bg-white px-2.5 py-1.5 text-sm";

  return (
    <form onSubmit={handleSubmit} className="space-y-3">
      {template.parameters.map((p) => (
        <label key={p.name} className="block text-xs font-medium text-zinc-600">
          <span className="flex items-baseline justify-between">
            <span>
              {p.name}
              {p.required && <span className="ml-1 text-red-500">*</span>}
            </span>
            <span className="font-normal text-zinc-400">{p.type}</span>
          </span>
          {p.type === "boolean" ? (
            <select
              className={`${field} mt-1`}
              value={currentValue(p) || "false"}
              onChange={(e) =>
                setValues((v) => ({ ...v, [p.name]: e.target.value }))
              }
            >
              <option value="true">true</option>
              <option value="false">false</option>
            </select>
          ) : (
            <input
              className={`${field} mt-1`}
              type={p.type === "string" ? "text" : "number"}
              step={p.type === "number" ? "any" : undefined}
              value={currentValue(p)}
              placeholder={p.default != null ? String(p.default) : ""}
              required={p.required}
              onChange={(e) =>
                setValues((v) => ({ ...v, [p.name]: e.target.value }))
              }
            />
          )}
          <span className="mt-0.5 block font-normal text-zinc-400">
            {p.description}
          </span>
        </label>
      ))}
      <button
        type="submit"
        disabled={submitting}
        className="rounded bg-zinc-900 px-4 py-1.5 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-50"
      >
        {submitting ? "Queueing..." : "Queue job"}
      </button>
    </form>
  );
}
