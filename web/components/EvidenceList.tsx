"use client";

import type { Evidence, Nutrient } from "@/lib/types";

const NUTRIENT_LABEL: Record<Nutrient, string> = {
  sugar: "糖",
  fat: "脂肪",
  fiber: "纤维",
  sodium: "钠",
  protein: "蛋白",
};

const SOURCE_LABEL: Record<string, string> = {
  official: "品牌官网",
  nutrition_db: "营养数据库",
  ecommerce: "电商页面",
  cache: "缓存档案",
  other: "其他",
};

const SOURCE_TONE: Record<string, string> = {
  official: "bg-emerald-100 text-emerald-700",
  nutrition_db: "bg-blue-100 text-blue-700",
  ecommerce: "bg-slate-200 text-slate-700",
  cache: "bg-violet-100 text-violet-700",
  other: "bg-slate-100 text-slate-600",
};

/**
 * 证据卡。展示的是**结构化读数**而不是原文段落 ——
 * 裁决结论引用的是这里的 ev_id，人工复核时要能一眼核到。
 */
export function EvidenceList({ evidence }: { evidence: Evidence[] }) {
  return (
    <div className="card">
      <h3 className="mb-2 text-sm font-semibold">
        营养证据 <span className="font-normal text-muted">（{evidence.length} 条）</span>
      </h3>
      <ul className="space-y-2 text-xs">
        {evidence.map((e) => {
          const degraded = e.nutrients.length === 0;
          return (
            <li key={e.id} className="rounded-lg bg-slate-50 p-2">
              <div className="mb-1 flex flex-wrap items-center gap-1.5">
                <span className="rounded bg-white px-1.5 py-0.5 font-mono">{e.id}</span>
                <span className={`rounded px-1.5 py-0.5 ${SOURCE_TONE[e.source_type] ?? ""}`}>
                  {SOURCE_LABEL[e.source_type] ?? e.source_type}
                </span>
                {e.query_tier === 3 && (
                  <span className="rounded bg-amber-100 px-1.5 py-0.5 text-amber-800">
                    去品牌查询 · 已降权
                  </span>
                )}
                {degraded && (
                  <span className="rounded bg-amber-100 px-1.5 py-0.5 text-amber-800">
                    降级证据 · 无营养面板
                  </span>
                )}
                {e.cache_provenance && (
                  <span
                    className={`rounded px-1.5 py-0.5 ${
                      e.cache_provenance === "human_verified"
                        ? "bg-emerald-100 text-emerald-700"
                        : "bg-amber-100 text-amber-800"
                    }`}
                  >
                    {e.cache_provenance === "human_verified" ? "人工核验" : "未经人工核验"}
                  </span>
                )}
                {e.source_url && (
                  <a
                    className="truncate text-brand hover:underline"
                    href={e.source_url}
                    target="_blank"
                    rel="noreferrer"
                  >
                    {e.source_title || e.source_url}
                  </a>
                )}
              </div>

              {degraded ? (
                <p className="text-slate-600">{e.conclusion_hint ?? "无结构化营养数据"}</p>
              ) : (
                <div className="flex flex-wrap gap-2 text-slate-700">
                  {e.nutrients.map((nv, i) => (
                    <span key={i} className="rounded bg-white px-1.5 py-0.5">
                      {NUTRIENT_LABEL[nv.nutrient]}{" "}
                      {nv.normalized !== null ? (
                        <b>{nv.normalized}g/100</b>
                      ) : (
                        <span className="text-warn" title="缺份量信息，无法换算">
                          {nv.value}
                          {nv.unit} · 未换算
                        </span>
                      )}
                    </span>
                  ))}
                </div>
              )}

              {e.product_query && (
                <p className="mt-1 text-[11px] text-muted">查询词：{e.product_query}</p>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
