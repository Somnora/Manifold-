import type { Metadata } from "next";
import { JetBrains_Mono, Space_Grotesk } from "next/font/google";
import "./globals.css";
import { Nav } from "@/components/Nav";

// The type pairing: Space Grotesk for UI (geometric, a little posh),
// JetBrains Mono for anything terminal-adjacent. Self-hosted at build time
// by next/font; wired into Tailwind via the variables in globals.css.
const grotesk = Space_Grotesk({
  subsets: ["latin"],
  variable: "--font-grotesk",
});
const mono = JetBrains_Mono({
  subsets: ["latin"],
  variable: "--font-jbmono",
});

export const metadata: Metadata = {
  title: "Manifold",
  description: "Lambda Cloud GPU orchestrator",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className={`${grotesk.variable} ${mono.variable}`}>
      <body className="min-h-screen bg-zinc-50 text-zinc-900 antialiased">
        <header className="sticky top-0 z-40 border-b border-zinc-200 bg-zinc-50/80 backdrop-blur">
          <div className="mx-auto flex max-w-6xl items-center gap-8 px-6 py-3">
            <span className="flex items-center gap-2">
              {/* Monogram: the real Menlo Bold "M" glyph, outlined from the
                  system font so it is exact and font-independent - the same
                  mark as the favicon and app icon. */}
              <svg
                width="18"
                height="18"
                viewBox="0 0 64 64"
                fill="none"
                aria-hidden="true"
              >
                <g transform="translate(15.483 52) scale(0.02679 -0.02679)">
                  <path
                    d="M86 1493H438L616 838L793 1493H1147V0H893V1196L735 543H500L340 1196V0H86Z"
                    fill="#2dd4bf"
                  />
                </g>
              </svg>
              <span className="flex items-baseline gap-0.5 font-mono text-sm font-semibold tracking-tight">
                manifold
                <span className="cursor-blink text-teal-400">▌</span>
              </span>
            </span>
            <Nav />
          </div>
        </header>
        <main className="mx-auto max-w-6xl px-6 py-8">{children}</main>
      </body>
    </html>
  );
}
