"use client";

import type { Classification } from "@/lib/types";
import { ConfidenceBadge, ConfidenceBar } from "./ConfidenceBadge";

/**
 * 粒度自适应展示（方案 §5 ②）：
 * 后端已经把判定做完了（`leaf_vs_parent`），前端只负责用样式区分
 * "确定层级"（实心）与"待定层级"（虚线 + 灰）——阈值口径只有一处，不在前端重算。
 */
export function AuditCard({
  classification,
  title = "分类结果",
  verified = false,
  compact = false,
}: {
  classification: Classification | null;
  title?: string;
  verified?: boolean;
  compact?: boolean;
}) {
  if (!classification) {
    return <div className="card text-sm text-muted">{title}：等待中…</div>;
  }
  const c = classification;
  const pending = c.leaf_vs_parent === "parent";

  return (
    <div className="card space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold">{title}</h3>
        <div className="flex flex-wrap items-center gap-2">
          {verified && (
            <span className="rounded-full bg-blue-100 px-2 py-0.5 text-xs font-medium text-blue-700">
              经搜索验证
            </span>
          )}
          {c.conflict && (
            <span className="rounded-full bg-red-100 px-2 py-0.5 text-xs font-medium text-red-700">
              证据冲突
            </span>
          )}
          {c.adapter && (c.adapter.startsWith("mock") || c.adapter === "rule-fallback") && (
            <span className="rounded-full bg-slate-200 px-2 py-0.5 text-xs font-medium text-slate-700">
              {c.adapter}
            </span>
          )}
        </div>
      </div>

      {!compact && (
        <div className="text-sm">
          <span className="text-muted">产品：</span>
          {c.product_name ?? <span className="text-muted">未识别</span>}
          {c.brand && <span className="ml-2 rounded bg-slate-100 px-1.5 py-0.5 text-xs">{c.brand}</span>}
        </div>
      )}

      {/* 父类：粒度自适应时这一层是"确定层级" */}
      <div>
        <div className="label">大类{pending && <span className="ml-1 text-ok">· 已确定</span>}</div>
        <div className="flex items-center justify-between gap-3">
          <span className="text-sm font-medium">{c.general_category}</span>
          <ConfidenceBadge value={c.general_confidence} label="" />
        </div>
        <ConfidenceBar value={c.general_confidence} />
      </div>

      {/* 子类：待定时虚线框 + 灰字 */}
      <div className={pending ? "rounded-lg border border-dashed border-line p-2 opacity-80" : ""}>
        <div className="label">
          细类 {pending && <span className="text-warn">· 待定</span>}
        </div>
        <div className="flex items-center justify-between gap-3">
          <span className="text-sm font-medium">
            {c.specific_code !== null ? `[${c.specific_code}]` : "—"}
          </span>
          <ConfidenceBadge value={c.specific_confidence} label="" />
        </div>
        <ConfidenceBar value={c.specific_confidence} />
        {pending && c.candidate_codes.length > 0 && (
          <p className="mt-1 text-xs text-muted">
            候选：{c.candidate_codes.map((x) => `[${x}]`).join(" vs ")} —— 需营养证据裁定
          </p>
        )}
      </div>

      {!compact && c.reasoning && (
        <p className="rounded-lg bg-slate-50 p-2 text-xs leading-relaxed text-slate-600">
          {c.reasoning}
        </p>
      )}

      <div className="flex flex-wrap gap-2 text-[11px] text-muted">
        <span>来源 {c.source}</span>
        {c.model && <span>· {c.model}</span>}
        {c.ad_language && <span>· 语言 {c.ad_language}</span>}
        {c.country && <span>· {c.country}</span>}
        {c.evidence_refs.length > 0 && <span>· 引用 {c.evidence_refs.join(", ")}</span>}
      </div>
    </div>
  );
}
