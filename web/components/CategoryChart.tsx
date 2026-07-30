"use client";

import { useState } from "react";

/**
 * 品类分布图（两级可下钻）。刻意不引图表库：
 * 骨架阶段用纯 CSS 条形图，W7 要做交互再换 Recharts，接口不变。
 */
export function CategoryChart({
  general,
  specific,
  codeToGeneral,
}: {
  general: Record<string, number>;
  specific: Record<string, number>;
  codeToGeneral?: Record<string, string>;
}) {
  const [drill, setDrill] = useState<string | null>(null);

  const rows = drill
    ? Object.entries(specific).filter(([code]) => !codeToGeneral || codeToGeneral[code] === drill)
    : Object.entries(general);
  const max = Math.max(1, ...rows.map(([, v]) => v));

  return (
    <div className="card">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-semibold">
          品类分布 {drill ? <span className="text-muted">· {drill}</span> : <span className="text-muted">· 大类</span>}
        </h3>
        {drill && (
          <button className="btn-ghost !py-1 text-xs" onClick={() => setDrill(null)}>
            返回大类
          </button>
        )}
      </div>

      {rows.length === 0 && <p className="text-sm text-muted">暂无数据</p>}
      <ul className="space-y-1.5">
        {rows
          .sort((a, b) => b[1] - a[1])
          .map(([k, v]) => (
            <li key={k}>
              <button
                className="flex w-full items-center gap-2 text-left"
                onClick={() => !drill && setDrill(k)}
                disabled={!!drill}
              >
                <span className="w-56 shrink-0 truncate text-xs">{drill ? `[${k}]` : k}</span>
                <span className="h-4 rounded bg-brand/80" style={{ width: `${(v / max) * 60}%` }} />
                <span className="text-xs text-muted">{v}</span>
              </button>
            </li>
          ))}
      </ul>
      {!drill && <p className="mt-2 text-[11px] text-muted">点击大类可下钻到细类</p>}
    </div>
  );
}

export function Histogram({ data, title }: { data: Record<string, number>; title: string }) {
  const max = Math.max(1, ...Object.values(data));
  return (
    <div className="card">
      <h3 className="mb-3 text-sm font-semibold">{title}</h3>
      <div className="flex items-end gap-3" style={{ height: 120 }}>
        {Object.entries(data).map(([k, v]) => (
          <div key={k} className="flex flex-1 flex-col items-center gap-1">
            <span className="text-xs text-muted">{v}</span>
            <div className="w-full rounded-t bg-brand/70" style={{ height: `${(v / max) * 90}px` }} />
            <span className="text-[10px] text-muted">{k}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

/** 跨批次看板曲线：人工复核率下降 + 缓存命中率上升 —— "记忆机制生效"的证据。 */
export function TrendChart({
  points,
}: {
  points: { name: string; human_review_rate: number; cache_hits: number }[];
}) {
  if (points.length === 0) return null;
  const w = 520;
  const h = 140;
  const step = points.length > 1 ? w / (points.length - 1) : w;
  const maxHits = Math.max(1, ...points.map((p) => p.cache_hits));

  const line = (get: (p: (typeof points)[number]) => number, scale: number) =>
    points.map((p, i) => `${i * step},${h - get(p) * scale * h}`).join(" ");

  return (
    <div className="card">
      <h3 className="mb-3 text-sm font-semibold">看板曲线（跨批次）</h3>
      <svg viewBox={`0 0 ${w} ${h}`} className="w-full">
        <polyline points={line((p) => p.human_review_rate, 1)} fill="none" stroke="#dc2626" strokeWidth="2" />
        <polyline points={line((p) => p.cache_hits / maxHits, 1)} fill="none" stroke="#059669" strokeWidth="2" />
        {/* 单批次时只有一个点，画不出折线 —— 补圆点标记 */}
        {points.map((p, i) => (
          <g key={i}>
            <circle cx={i * step} cy={h - p.human_review_rate * h} r="3" fill="#dc2626" />
            <circle cx={i * step} cy={h - (p.cache_hits / maxHits) * h} r="3" fill="#059669" />
          </g>
        ))}
      </svg>
      <div className="mt-2 flex gap-4 text-xs text-muted">
        <span><span className="mr-1 inline-block h-2 w-3 bg-bad" />人工复核率</span>
        <span><span className="mr-1 inline-block h-2 w-3 bg-ok" />缓存命中（归一化）</span>
      </div>
    </div>
  );
}
