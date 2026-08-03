"use client";

import { use, useEffect, useRef, useState } from "react";
import { api, imageUrl } from "@/lib/api";
import { subscribeAudit, type SSEEvent } from "@/lib/sse";
import type { Audit, Classification } from "@/lib/types";
import { AgentTrace } from "@/components/AgentTrace";
import { AuditCard } from "@/components/AuditCard";
import { ReviewCompare } from "@/components/ReviewCompare";
import { ImageZoom } from "@/components/ImageZoom";

/** ② 单张审计卡片页 —— demo 核心页。左图右 trace，SSE 实时生长。 */
export default function AuditPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);

  const [audit, setAudit] = useState<Audit | null>(null);
  const [events, setEvents] = useState<SSEEvent[]>([]);
  const [initial, setInitial] = useState<Classification | null>(null);
  const [revised, setRevised] = useState<Classification | null>(null);
  const [final, setFinal] = useState<Classification | null>(null);
  const [needHuman, setNeedHuman] = useState<{ reason: string } | null>(null);
  const [live, setLive] = useState(true);
  const closeRef = useRef<null | (() => void)>(null);

  useEffect(() => {
    api.audit(id).then((a) => {
      setAudit(a);
      setInitial(a.initial);
      setRevised(a.revised);
      setFinal(a.final);
      if (a.status === "pending_human") setNeedHuman({ reason: a.reason ?? "置信度不足" });
    });

    closeRef.current = subscribeAudit(id, (e) => {
      setEvents((prev) => [...prev, e]);
      if (e.type === "classified") setInitial(e.initial);
      if (e.type === "need_human") {
        setInitial(e.initial);
        setRevised(e.revised);
        setNeedHuman({ reason: e.reason });
        setLive(false);
      }
      if (e.type === "done") {
        setFinal(e.final);
        setLive(false);
        closeRef.current?.();
      }
    });
    return () => closeRef.current?.();
  }, [id]);

  const verified = audit?.route_2 === "direct_verified" || !!revised;

  return (
    <div className="space-y-4">
      <div className="flex items-baseline justify-between">
        <h1 className="text-lg font-semibold">单张审计</h1>
        <span className="text-xs text-muted">audit {id.slice(0, 8)}</span>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        {/* 左：原图 + 结果卡片 */}
        <div className="space-y-4">
          {audit && (
            <ImageZoom
              src={imageUrl(audit.image_path)}
              alt="广告原图"
              caption={initial?.product_name ?? undefined}
              className="w-full rounded-xl border border-line bg-white object-contain p-2"
              style={{ maxHeight: 320 }}
            />
          )}
          <AuditCard
            classification={final ?? revised ?? initial}
            title={final ? "最终结果" : revised ? "重裁决结果" : "初分类"}
            verified={verified}
          />
        </div>

        {/* 右：Agent 流式时间线 */}
        <AgentTrace events={events} live={live} />
      </div>

      {/* 收到 need_human：页面内嵌双选项 UI，不打断，允许当场裁定 */}
      {needHuman && !final && (
        <div className="space-y-3">
          <h2 className="text-sm font-semibold">需要人工裁定</h2>
          <ReviewCompare
            auditId={id}
            initial={initial}
            revised={revised}
            reason={needHuman.reason}
            imagePath={audit?.image_path}
            onDecided={() => api.audit(id).then((a) => setFinal(a.final))}
          />
        </div>
      )}
    </div>
  );
}
