/** EventSource 封装。前端只认这 6 种事件（方案 §4）。 */

import type { Classification } from "./types";

export type SSEEvent =
  | { type: "node_start"; node: string }
  | { type: "node_log"; node: string; msg: string }
  | {
      type: "node_end";
      node: string;
      ms: number;
      status: string;
      summary: string;
      fallback_reason: string | null;
    }
  | { type: "classified"; initial: Classification | null; route_1?: string }
  | {
      type: "need_human";
      initial: Classification | null;
      revised: Classification | null;
      reason: string;
    }
  | { type: "done"; final: Classification | null; route: string | null; human_choice: string | null };

const EVENTS = ["node_start", "node_log", "node_end", "classified", "need_human", "done"] as const;

export function subscribeAudit(
  auditId: string,
  onEvent: (e: SSEEvent) => void,
  onError?: (err: Event) => void
): () => void {
  const es = new EventSource(`/api/audits/${auditId}/stream`);

  const handlers = EVENTS.map((name) => {
    const h = (ev: MessageEvent) => {
      try {
        onEvent({ type: name, ...JSON.parse(ev.data) } as SSEEvent);
      } catch {
        /* 忽略心跳等非 JSON 负载 */
      }
    };
    es.addEventListener(name, h as EventListener);
    return [name, h] as const;
  });

  es.onerror = (err) => {
    onError?.(err);
    // 流结束（图跑完）时浏览器会自动重连 —— done 之后主动关闭避免空转
  };

  return () => {
    handlers.forEach(([name, h]) => es.removeEventListener(name, h as EventListener));
    es.close();
  };
}
