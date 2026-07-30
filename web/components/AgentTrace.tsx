"use client";

import type { SSEEvent } from "@/lib/sse";

const NODE_LABEL: Record<string, string> = {
  classify_initial: "① 感知与初分类",
  cache_lookup: "② 缓存取证",
  web_search: "③ 联网搜索",
  adjudicate_with_evidence: "④ 证据重裁决",
  human_review: "⑤ 人工复核",
  feedback_ingest: "⑥ 数据回流",
  output: "出口",
  graph: "图",
};

export interface TraceItem {
  node: string;
  kind: "start" | "log" | "end";
  text: string;
  ms?: number;
  fallback?: string | null;
}

/** SSE 事件 → 时间线条目。node_start/node_log/node_end 三种事件驱动整条时间线生长。 */
export function toTraceItems(events: SSEEvent[]): TraceItem[] {
  const out: TraceItem[] = [];
  for (const e of events) {
    if (e.type === "node_start") out.push({ node: e.node, kind: "start", text: "开始" });
    else if (e.type === "node_log") out.push({ node: e.node, kind: "log", text: e.msg });
    else if (e.type === "node_end")
      out.push({ node: e.node, kind: "end", text: e.summary, ms: e.ms, fallback: e.fallback_reason });
  }
  return out;
}

export function AgentTrace({ events, live }: { events: SSEEvent[]; live: boolean }) {
  const items = toTraceItems(events);
  return (
    <div className="card">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-semibold">Agent 过程</h3>
        {live && (
          <span className="flex items-center gap-1.5 text-xs text-muted">
            <span className="h-2 w-2 animate-pulse rounded-full bg-brand" />
            实时
          </span>
        )}
      </div>

      <ol className="space-y-2">
        {items.length === 0 && <li className="text-sm text-muted">等待 Agent 启动…</li>}
        {items.map((it, i) => (
          <li key={i} className="flex gap-3 text-sm">
            <div className="w-32 shrink-0 text-xs text-muted">{NODE_LABEL[it.node] ?? it.node}</div>
            <div
              className={`flex-1 rounded-lg px-2 py-1 ${
                it.fallback
                  ? "bg-amber-50 text-amber-800"   // 兜底路径在时间线上标黄
                  : it.kind === "end"
                    ? "bg-slate-50"
                    : ""
              }`}
            >
              {it.text}
              {it.ms !== undefined && <span className="ml-2 text-xs text-muted">{it.ms}ms</span>}
              {it.fallback && <span className="ml-2 text-xs">（兜底：{it.fallback}）</span>}
            </div>
          </li>
        ))}
      </ol>
    </div>
  );
}
