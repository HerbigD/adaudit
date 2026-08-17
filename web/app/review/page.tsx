"use client";

import { useCallback, useEffect, useState } from "react";
import { api, imageUrl } from "@/lib/api";
import type { Audit } from "@/lib/types";
import { ReviewCompare } from "@/components/ReviewCompare";
import { ImageZoom } from "@/components/ImageZoom";

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
            {/* 缩略图独立于展开按钮之外：点图看大图，点其余区域展开复核 */}
            <div className="card flex w-full items-center gap-3 hover:bg-slate-50">
              <ImageZoom
                src={imageUrl(a.image_path)}
                alt={a.initial?.product_name ?? "广告原图"}
                caption={a.initial?.product_name ?? undefined}
                className="h-12 w-12 shrink-0 rounded border border-line object-cover"
              />
              <button
                type="button"
                className="flex flex-1 items-center gap-3 text-left"
                onClick={() => setOpenId(openId === a.id ? null : a.id)}
              >
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
            </div>

            {openId === a.id && detail && (
              <ReviewCompare
                auditId={a.id}
                initial={detail.initial}
                revised={detail.revised}
                evidence={detail.evidence}
                reason={detail.reason}
                imagePath={detail.image_path ?? a.image_path}
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
