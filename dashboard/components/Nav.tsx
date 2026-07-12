"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

// Grouped by frequency of use: everyday driving · agent surfaces ·
// occasional reference. A thin divider separates each cluster.
const groups = [
  [
    { href: "/", label: "Instances" },
    { href: "/jobs", label: "Jobs" },
    { href: "/storage", label: "Storage" },
  ],
  [
    { href: "/autopilot", label: "Autopilot" },
    { href: "/agents", label: "Agent Activity" },
  ],
  [
    { href: "/history", label: "History" },
    { href: "/settings", label: "Settings" },
  ],
];

export function Nav() {
  const pathname = usePathname();
  return (
    <nav className="flex items-center gap-1 text-sm">
      {groups.map((group, gi) => (
        <div key={gi} className="flex items-center gap-1">
          {gi > 0 && <span className="mx-1.5 h-4 w-px bg-zinc-300" />}
          {group.map(({ href, label }) => {
            const active = pathname === href;
            return (
              <Link
                key={href}
                href={href}
                className={`rounded px-3 py-1.5 ${
                  active
                    ? "bg-zinc-900 text-white"
                    : "text-zinc-600 hover:bg-zinc-100 hover:text-zinc-900"
                }`}
              >
                {label}
              </Link>
            );
          })}
        </div>
      ))}
    </nav>
  );
}
