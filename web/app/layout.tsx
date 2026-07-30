import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "AdAudit v2 · 食品广告品类审计 Agent",
  description: "置信度感知 + 联网取证 + 人工复核闭环的广告品类审计系统",
};

const NAV = [
  { href: "/", label: "上传" },
  { href: "/review", label: "人工复核" },
  { href: "/batches", label: "批次报告" },
];

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>
        <header className="border-b border-line bg-white">
          <div className="mx-auto flex max-w-6xl items-center gap-6 px-6 py-3">
            <Link href="/" className="text-sm font-semibold">
              AdAudit <span className="text-muted">v2</span>
            </Link>
            <nav className="flex gap-4 text-sm text-muted">
              {NAV.map((n) => (
                <Link key={n.href} href={n.href} className="hover:text-ink">
                  {n.label}
                </Link>
              ))}
            </nav>
          </div>
        </header>
        <main className="mx-auto max-w-6xl px-6 py-6">{children}</main>
      </body>
    </html>
  );
}
