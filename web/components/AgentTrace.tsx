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

/** 节点在时间线上的呈现。前三种是决议要求的三态，`skipped` 是缓存命中带来的第四种。 */
export type StepStatus = "running" | "done" | "fallback" | "skipped";

export interface TraceStep {
  node: string;
  status: StepStatus;
  logs: string[];
  summary?: string;
  ms?: number;
  fallback?: string | null;
  /** 结构化标记，来自后端 StepTrace.extra —— 不从 summary 文案里猜语义 */
  extra?: Record<string, unknown>;
}

const STYLE: Record<StepStatus, { box: string; dot: string; label: string }> = {
  running: { box: "bg-brand/5 border-brand/30", dot: "bg-brand animate-pulse", label: "进行中" },
  done: { box: "bg-slate-50 border-slate-200", dot: "bg-emerald-500", label: "完成" },
  // 兜底标黄：走到这条路径不算失败，但读轨迹的人必须一眼看到"这一步不是正常路径"
  fallback: { box: "bg-amber-50 border-amber-300", dot: "bg-amber-500", label: "兜底" },
  // 跳过用虚线 + 灰点：它和"完成"不是一回事，视觉上不该抢正常步骤的注意力
  skipped: {
    box: "border-dashed border-slate-300 bg-slate-50/60",
    dot: "bg-slate-300",
    label: "已跳过",
  },
};

/** 图里节点的固定顺序 —— 决定被跳过的步骤插在哪一节。 */
const NODE_ORDER = [
  "classify_initial",
  "cache_lookup",
  "web_search",
  "adjudicate_with_evidence",
  "human_review",
  "feedback_ingest",
  "output",
];

/**
 * SSE 事件流 → 按节点聚合的时间线。
 *
 * 从"一条事件一行"改成"一个节点一块"的理由：三态是**节点的状态**，
 * 而事件是状态的变化。按事件渲染表达不了"这个节点正在进行中"，
 * 只能表达"刚才发生了一件事"。
 */
export function toSteps(events: SSEEvent[]): TraceStep[] {
  const byNode = new Map<string, TraceStep>();
  const order: string[] = [];

  for (const e of events) {
    if (!("node" in e)) continue;
    let s = byNode.get(e.node);
    if (!s) {
      s = { node: e.node, status: "running", logs: [] };
      byNode.set(e.node, s);
      order.push(e.node);
    }
    if (e.type === "node_log") s.logs.push(e.msg);
    else if (e.type === "node_end") {
      s.summary = e.summary;
      s.ms = e.ms;
      s.fallback = e.fallback_reason;
      s.extra = e.extra ?? {};
      s.status =
        e.fallback_reason || e.status === "fallback" || e.status === "error"
          ? "fallback"
          : e.status === "skipped"
            ? "skipped"
            : "done";
    }
  }

  const steps = order.map((n) => byNode.get(n)!);

  // 缓存命中时 web_search 根本不会启动，SSE 里也就没有它的事件。
  // 但"这一步被跳过了"本身就是要给人看的信息（demo 的"哇时刻"正在这），
  // 所以补一个合成步骤，而不是让时间线上凭空少一节。
  const cache = byNode.get("cache_lookup");
  const hit = Boolean(cache?.extra?.cache_id) && !cache?.extra?.strict_rejected;
  if (hit && !byNode.has("web_search")) {
    steps.push({
      node: "web_search",
      status: "skipped",
      logs: [],
      summary: "缓存命中，免联网搜索",
      extra: { skipped_because: "cache_hit" },
    });
  }

  return steps.sort((a, b) => NODE_ORDER.indexOf(a.node) - NODE_ORDER.indexOf(b.node));
}

/** 缓存那一步的补充说明。全部读结构化字段，不解析文案。 */
function cacheNote(s: TraceStep): { text: string; tone: string } | null {
  if (s.node !== "cache_lookup") return null;
  const x = s.extra ?? {};
  if (x.strict_rejected) {
    return {
      text: `近名档案被 strict 否决（score ${x.score}）：${x.reject_reason}`,
      tone: "text-amber-700",
    };
  }
  if (x.cache_id) {
    const verified = x.provenance === "human_verified";
    return {
      text: verified
        ? `命中人工核验档案（score ${x.score}）— 免搜索`
        : `命中自动沉淀档案（score ${x.score}，未经人工核验）— 免搜索`,
      tone: verified ? "text-emerald-700" : "text-slate-600",
    };
  }
  return null;
}

export function AgentTrace({ events, live }: { events: SSEEvent[]; live: boolean }) {
  const steps = toSteps(events);
  const cacheHit = steps.some((s) => s.node === "web_search" && s.status === "skipped");
  const searched = steps.some((s) => s.node === "web_search" && s.status !== "skipped");

  return (
    <div className="card" data-testid="agent-trace">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-semibold">Agent 过程</h3>
        <div className="flex items-center gap-2">
          {cacheHit && (
            <span
              data-testid="timeline-mode-cache"
              className="rounded-full bg-emerald-50 px-2 py-0.5 text-xs text-emerald-700"
            >
              缓存命中 · 跳过搜索
            </span>
          )}
          {searched && (
            <span
              data-testid="timeline-mode-search"
              className="rounded-full bg-brand/10 px-2 py-0.5 text-xs text-brand"
            >
              联网搜索取证
            </span>
          )}
          {live && (
            <span className="flex items-center gap-1.5 text-xs text-muted">
              <span className="h-2 w-2 animate-pulse rounded-full bg-brand" />
              实时
            </span>
          )}
        </div>
      </div>

      <ol className="space-y-2">
        {steps.length === 0 && <li className="text-sm text-muted">等待 Agent 启动…</li>}
        {steps.map((s) => {
          const st = STYLE[s.status];
          const note = cacheNote(s);
          return (
            <li
              key={s.node}
              data-testid={`step-${s.node}`}
              data-status={s.status}
              className="flex gap-3 text-sm"
            >
              <div className="flex w-32 shrink-0 items-start gap-1.5 pt-1 text-xs text-muted">
                <span className={`mt-1 h-2 w-2 shrink-0 rounded-full ${st.dot}`} />
                {NODE_LABEL[s.node] ?? s.node}
              </div>
              <div className={`flex-1 rounded-lg border px-2 py-1 ${st.box}`}>
                <div className="flex items-baseline justify-between gap-2">
                  <span>{s.summary ?? s.logs[s.logs.length - 1] ?? "…"}</span>
                  <span className="shrink-0 text-xs text-muted">
                    {st.label}
                    {s.ms !== undefined && ` · ${s.ms}ms`}
                  </span>
                </div>
                {note && <div className={`mt-0.5 text-xs ${note.tone}`}>{note.text}</div>}
                {s.fallback && (
                  <div className="mt-0.5 text-xs text-amber-700">兜底原因：{s.fallback}</div>
                )}
                {s.status === "running" && s.logs.length > 0 && (
                  <div className="mt-0.5 text-xs text-muted">{s.logs[s.logs.length - 1]}</div>
                )}
              </div>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
