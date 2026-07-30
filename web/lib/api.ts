/** REST 封装。前端一律走同源 /api/*（next.config.mjs 里 rewrite 到 FastAPI）。 */

import type { Audit, Batch, TaxonomyCascade, TrendPoint } from "./types";

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, { cache: "no-store", ...init });
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText} — ${detail.slice(0, 200)}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  upload(files: File[], batchName?: string) {
    const fd = new FormData();
    files.forEach((f) => fd.append("files", f));
    if (batchName) fd.append("batch_name", batchName);
    return req<{ batch_id: string | null; audits: { audit_id: string }[]; redirect: string }>(
      "/api/audits",
      { method: "POST", body: fd }
    );
  },

  audit: (id: string) => req<Audit>(`/api/audits/${id}`),
  audits: (q: { status?: string; batch_id?: string } = {}) =>
    req<Audit[]>(`/api/audits?${new URLSearchParams(q as Record<string, string>)}`),

  queue: () => req<Audit[]>("/api/review/queue"),
  reviewDetail: (id: string) => req<Audit>(`/api/review/${id}`),
  taxonomy: () => req<TaxonomyCascade>("/api/taxonomy"),
  decide: (id: string, choice: string, manual_code?: number) =>
    req<{ status: string; ingested: string[] }>(`/api/review/${id}/decide`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ choice, manual_code: manual_code ?? null }),
    }),

  batches: () => req<Batch[]>("/api/batches"),
  batch: (id: string) => req<Batch>(`/api/batches/${id}`),
  report: (id: string) =>
    req<{ report_md: string }>(`/api/batches/${id}/report`, { method: "POST" }),
  trend: (id: string) => req<{ points: TrendPoint[] }>(`/api/batches/${id}/trend`),

  health: () => req<Record<string, unknown>>("/api/health"),
};

/** 图片路径 → 可访问 URL（后端把 data/uploads 挂在 /static/uploads）。 */
export function imageUrl(imagePath: string): string {
  return `/static/uploads/${imagePath.split("/").pop()}`;
}
