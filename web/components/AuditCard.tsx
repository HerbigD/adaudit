"use client";

import type { Classification } from "@/lib/types";
import { ConfidenceBadge, ConfidenceBar } from "./ConfidenceBadge";

const DIRECT_THRESHOLD = 0.85;
const GENERAL_FALLBACK = 0.8;

/**
 * 粒度自适应展示（方案 §5 ②）：
 * 子类置信低但父类置信高时按父类粒度展示 —— "确定是谷物，糖分高低待定"。
 * UI 用样式区分"确定层级"（实心）与"待定层级"（虚线 + 灰）。
 */
export function displayLevel(c: Classification): "specific" | "general" {
  return c.specific_confidence < DIRECT_THRESHOLD && c.general_confidence >= GENERAL_FALLBACK
    ? "general"
    : "specific";
}

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
  const level = displayLevel(c);

  return (
    <div className="card space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold">{title}</h3>
        <div className="flex items-center gap-2">
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
        <div className="label">大类</div>
        <div className="flex items-center justify-between gap-3">
          <span className="text-sm font-medium">{c.general_category}</span>
          <ConfidenceBadge value={c.general_confidence} label="" />
        </div>
        <ConfidenceBar value={c.general_confidence} />
      </div>

      {/* 子类：待定时虚线框 + 灰字 */}
      <div className={level === "general" ? "rounded-lg border border-dashed border-line p-2 opacity-70" : ""}>
        <div className="label">
          细类 {level === "general" && <span className="text-warn">· 待定</span>}
        </div>
        <div className="flex items-center justify-between gap-3">
          <span className="text-sm font-medium">[{c.specific_code}]</span>
          <ConfidenceBadge value={c.specific_confidence} label="" />
        </div>
        <ConfidenceBar value={c.specific_confidence} />
        {level === "general" && c.alternative_code !== null && (
          <p className="mt-1 text-xs text-muted">
            候选：[{c.specific_code}] vs [{c.alternative_code}] —— 需营养证据裁定
          </p>
        )}
      </div>

      {!compact && c.reasoning && (
        <p className="rounded-lg bg-slate-50 p-2 text-xs leading-relaxed text-slate-600">
          {c.reasoning}
        </p>
      )}

      <div className="flex gap-2 text-[11px] text-muted">
        <span>来源 {c.source}</span>
        {c.model && <span>· {c.model}</span>}
        {c.evidence_refs.length > 0 && <span>· 引用证据 #{c.evidence_refs.join(", #")}</span>}
      </div>
    </div>
  );
}
