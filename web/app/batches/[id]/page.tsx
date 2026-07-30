"use client";

import { use, useEffect, useState } from "react";
import Link from "next/link";
import { api, imageUrl } from "@/lib/api";
import type { Batch, TrendPoint } from "@/lib/types";
import { CategoryChart, Histogram, TrendChart } from "@/components/CategoryChart";
import { ReportView } from "@/components/ReportView";

/** ④ 批次报告与看板页 —— demo 录屏的结尾画面。 */
export default function BatchPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [batch, setBatch] = useState<Batch | null>(null);
  const [trend, setTrend] = useState<TrendPoint[]>([]);
  const [report, setReport] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const load = () =>
      api.batch(id).then((b) => {
        setBatch(b);
        setReport(b.report_md);
      });
    load();
    api.trend(id).then((t) => setTrend(t.points)).catch(() => void 0);
    // 批次处理中时轮询（单张页用 SSE，批次页用轮询就够）
    const timer = setInterval(load, 3000);
    return () => clearInterval(timer);
  }, [id]);

  async function generate() {
    setBusy(true);
    try {
      const r = await api.report(id);
      setReport(r.report_md);
    } finally {
      setBusy(false);
    }
  }

  if (!batch) return <p className="text-sm text-muted">加载中…</p>;
  const s = batch.stats;
  const codeToGeneral: Record<string, string> = {};
  batch.audits.forEach((a) => {
    if (a.final) codeToGeneral[String(a.final.specific_code)] = a.final.general_category;
  });

  return (
    <div className="space-y-5">
      <div className="flex items-baseline justify-between">
        <div>
          <h1 className="text-lg font-semibold">{batch.name}</h1>
          <p className="text-sm text-muted">
            {s.completed}/{s.total} 已完成 · 待复核 {batch.pending_human} · 状态 {batch.status}
          </p>
        </div>
        <div className="flex gap-2">
          {batch.pending_human > 0 && (
            <Link href="/review" className="btn-ghost">
              去复核 ({batch.pending_human})
            </Link>
          )}
          <button className="btn-primary" disabled={busy} onClick={generate}>
            {busy ? "生成中…" : "生成报告"}
          </button>
        </div>
      </div>

      {/* 统计层：全部来自 stats_json，数字 grounded */}
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label="搜索触发率" value={pct(s.search_trigger_rate)} />
        <Stat label="人工修正率" value={pct(s.human_review_rate)} />
        <Stat label="HFSS 品类占比" value={pct(s.hfss_share)} />
        <Stat
          label="缓存命中"
          value={`${s.cache.total_hits} 次 / ${s.cache.products} 档案`}
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <CategoryChart
          general={s.general_distribution}
          specific={s.specific_distribution}
          codeToGeneral={codeToGeneral}
        />
        <Histogram data={s.confidence_histogram} title="置信度分布" />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <div className="card">
          <h3 className="mb-3 text-sm font-semibold">original vs prediction 采纳率</h3>
          <div className="space-y-2 text-sm">
            <Row label="采纳 original" value={pct(s.original_adopted_rate)} />
            <Row label="采纳 prediction" value={pct(s.prediction_adopted_rate)} />
            <Row label="路由分布" value={JSON.stringify(s.route_distribution)} />
          </div>
        </div>
        <TrendChart points={trend} />
      </div>

      {/* 洞察层 */}
      <ReportView md={report} />

      <div className="card">
        <h3 className="mb-3 text-sm font-semibold">本批次广告</h3>
        <div className="grid grid-cols-3 gap-3 sm:grid-cols-6">
          {batch.audits.map((a) => (
            <Link key={a.id} href={`/audits/${a.id}`} className="space-y-1">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={imageUrl(a.image_path)}
                alt=""
                className="aspect-square w-full rounded border border-line object-cover"
              />
              <div className="truncate text-[11px] text-muted">
                {a.final ? `[${a.final.specific_code}]` : a.status}
              </div>
            </Link>
          ))}
        </div>
      </div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="card">
      <div className="label">{label}</div>
      <div className="mt-1 text-xl font-semibold">{value}</div>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-muted">{label}</span>
      <span className="font-medium">{value}</span>
    </div>
  );
}

function pct(v: number): string {
  return `${(v * 100).toFixed(1)}%`;
}
