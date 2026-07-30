"use client";

import { useCallback, useEffect, useState } from "react";
import { api, imageUrl } from "@/lib/api";
import type { Audit } from "@/lib/types";
import { ReviewCompare } from "@/components/ReviewCompare";

/** ③ 人工复核队列页。队列来源：audits.status='pending_human'（interrupt 挂起的图实例）。 */
export default function ReviewPage() {
  const [queue, setQueue] = useState<Audit[]>([]);
  const [openId, setOpenId] = useState<string | null>(null);
  const [detail, setDetail] = useState<Audit | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    setLoading(true);
    const q = await api.queue().catch(() => []);
    setQueue(q);
    setLoading(false);
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    if (!openId) return setDetail(null);
    api.reviewDetail(openId).then(setDetail).catch(() => setDetail(null));
  }, [openId]);

  return (
    <div className="space-y-4">
      <div className="flex items-baseline justify-between">
        <div>
          <h1 className="text-lg font-semibold">人工复核队列</h1>
          <p className="text-sm text-muted">
            Agent 给出 original 与 prediction 两个选项，人工裁定后回流 eval 集 + 记忆库 + 缓存库。
          </p>
        </div>
        <button className="btn-ghost" onClick={refresh}>
          刷新
        </button>
      </div>

      {loading && <p className="text-sm text-muted">加载中…</p>}
      {!loading && queue.length === 0 && (
        <div className="card text-sm text-muted">队列为空 —— 所有广告都已直出或已裁定。</div>
      )}

      <ul className="space-y-2">
        {queue.map((a) => (
          <li key={a.id} className="space-y-3">
            <button
              className="card flex w-full items-center gap-3 text-left hover:bg-slate-50"
              onClick={() => setOpenId(openId === a.id ? null : a.id)}
            >
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={imageUrl(a.image_path)}
                alt=""
                className="h-12 w-12 rounded border border-line object-cover"
              />
              <div className="flex-1">
                <div className="text-sm font-medium">
                  {a.initial?.product_name ?? "未识别产品"}
                  {a.initial?.brand && <span className="ml-2 text-xs text-muted">{a.initial.brand}</span>}
                </div>
                <div className="text-xs text-muted">
                  卡点：{a.reason} · 路由 {a.route_1}
                  {a.route_2 ? ` → ${a.route_2}` : ""}
                </div>
              </div>
              <span className="text-xs text-brand">{openId === a.id ? "收起" : "复核"}</span>
            </button>

            {openId === a.id && detail && (
              <ReviewCompare
                auditId={a.id}
                initial={detail.initial}
                revised={detail.revised}
                evidence={detail.evidence}
                reason={detail.reason}
                onDecided={() => {
                  setOpenId(null);
                  refresh();
                }}
              />
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
