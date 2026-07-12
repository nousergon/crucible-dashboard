import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "Crucible — experiment results",
  description:
    "The Reference Rate experiment, honestly measured: performance, measurement integrity, and the grader's verdicts. Paper-traded, illustrative only.",
};

const NAV = [
  { href: "/", label: "Performance" },
  { href: "/risk", label: "Risk & Attribution" },
  { href: "/integrity", label: "Integrity" },
  { href: "/about", label: "About" },
];

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <div className="mx-auto max-w-6xl px-5 pb-16">
          <header className="flex flex-wrap items-baseline justify-between gap-3 border-b border-line py-5">
            <div>
              <div className="text-xs font-semibold uppercase tracking-widest text-muted">
                Crucible · experiment results
              </div>
              <h1 className="font-mono text-xl font-semibold">reference-rate</h1>
            </div>
            <nav className="flex gap-1 text-sm">
              {NAV.map((item) => (
                <Link
                  key={item.href}
                  href={item.href}
                  className="rounded px-3 py-1.5 text-muted transition-colors hover:bg-surface hover:text-ink"
                >
                  {item.label}
                </Link>
              ))}
            </nav>
          </header>
          <main className="pt-6">{children}</main>
          <footer className="mt-14 border-t border-line pt-4 text-xs leading-relaxed text-muted">
            Paper-traded and illustrative only — not investment advice, not an offer of any
            security or advisory service. Every figure derives from versioned artifacts; the
            grader that produced them is itself validated (see Integrity).
          </footer>
        </div>
      </body>
    </html>
  );
}
