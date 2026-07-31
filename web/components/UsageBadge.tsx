"use client";

import { useEffect, useState } from "react";

interface Usage {
  calls: number;
  total_tokens: number;
  budget: number;
  remaining: number;
  used_ratio: number;
  exceeded: boolean;
  cost: number;
  currency: string;
}

/**
 * 顶栏的成本熔断指示器（Day6 任务 0 的"UI 可见"）。
 * 预算是给人看的约束，不是藏在 config 里的数字 —— 烧到 80% 就得让人注意到。
 */
export function UsageBadge() {
  const [u, setU] = useState<Usage | null>(null);

  useEffect(() => {
    const load = () =>
      fetch("/api/usage", { cache: "no-store" })
        .then((r) => r.json())
        .then(setU)
        .catch(() => void 0);
    load();
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, []);

  if (!u || u.total_tokens === 0) return null;

  const pct = Math.min(100, Math.round(u.used_ratio * 100));
  const tone = u.exceeded
    ? "bg-red-100 text-red-700"
    : pct >= 80
      ? "bg-amber-100 text-amber-800"
      : "bg-slate-100 text-slate-600";

  return (
    <span
      className={`ml-auto rounded-full px-2.5 py-1 text-xs font-medium ${tone}`}
      title={`${u.calls} 次真实调用 · ${u.total_tokens}/${u.budget} tokens · 估算 ${u.cost.toFixed(4)} ${u.currency}`}
    >
      {u.exceeded ? "⛔ 预算已用尽" : `预算 ${pct}%`}
      <span className="ml-1.5 font-normal opacity-70">
        {u.total_tokens.toLocaleString()} tok · {u.cost.toFixed(3)} {u.currency}
      </span>
    </span>
  );
}
