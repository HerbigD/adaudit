"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { Classification, Evidence, SpecificCategory } from "@/lib/types";
import { AuditCard } from "./AuditCard";

/**
 * 双选项对比 —— 全链路闭环的核心交互（W6 红线）。
 * 左右并列 original(初判) vs prediction(搜索后重裁决)，各自类别/置信度/推理/证据引用；
 * 下方三个操作：采纳 original / 采纳 prediction / 手动指定（33 类级联选择器）。
 */
export function ReviewCompare({
  auditId,
  initial,
  revised,
  evidence = [],
  reason,
  onDecided,
}: {
  auditId: string;
  initial: Classification | null;
  revised: Classification | null;
  evidence?: Evidence[];
  reason?: string;
  onDecided?: (result: { status: string; ingested: string[] }) => void;
}) {
  const [specifics, setSpecifics] = useState<SpecificCategory[]>([]);
  const [general, setGeneral] = useState<string>("");
  const [manualCode, setManualCode] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [ingested, setIngested] = useState<string[] | null>(null);

  useEffect(() => {
    api.taxonomy().then((t) => setSpecifics(t.specifics)).catch(() => void 0);
  }, []);

  const generals = Array.from(new Set(specifics.map((s) => s.general)));
  const leafOptions = specifics.filter((s) => s.general === general);

  async function decide(choice: "original" | "prediction" | "manual") {
    setBusy(true);
    setErr(null);
    try {
      const res = await api.decide(auditId, choice, choice === "manual" ? manualCode ?? undefined : undefined);
      setIngested(res.ingested);
      onDecided?.(res);
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  if (ingested) {
    return (
      <div className="card border-emerald-200 bg-emerald-50 text-sm text-emerald-800">
        已裁定并回流：{ingested.join(" + ")}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {reason && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800">
          卡点原因：{reason}
        </div>
      )}

      <div className="grid gap-4 md:grid-cols-2">
        <AuditCard classification={initial} title="original · 初判" />
        <AuditCard classification={revised} title="prediction · 搜索后重裁决" verified={!!revised} />
      </div>

      {evidence.length > 0 && (
        <div className="card">
          <h3 className="mb-2 text-sm font-semibold">营养证据</h3>
          <ul className="space-y-2 text-xs">
            {evidence.map((e, i) => (
              <li key={i} className="rounded-lg bg-slate-50 p-2">
                <div className="mb-1 flex items-center gap-2">
                  <span className="rounded bg-white px-1.5 py-0.5">#{i}</span>
                  <span className="rounded bg-white px-1.5 py-0.5">{e.source}</span>
                  {e.url && (
                    <a className="truncate text-brand hover:underline" href={e.url} target="_blank" rel="noreferrer">
                      {e.title ?? e.url}
                    </a>
                  )}
                </div>
                <div className="text-slate-600">
                  {[
                    e.sugar_g != null && `糖 ${e.sugar_g}g`,
                    e.fat_g != null && `脂肪 ${e.fat_g}g`,
                    e.fibre_g != null && `纤维 ${e.fibre_g}g`,
                    e.salt_g != null && `盐 ${e.salt_g}g`,
                  ]
                    .filter(Boolean)
                    .join(" · ") || "未抽取到结构化营养数据"}
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="card space-y-3">
        <div className="flex flex-wrap gap-2">
          <button className="btn-primary" disabled={busy || !initial} onClick={() => decide("original")}>
            采纳 original
          </button>
          <button className="btn-primary" disabled={busy || !revised} onClick={() => decide("prediction")}>
            采纳 prediction
          </button>
        </div>

        <div className="border-t border-line pt-3">
          <div className="label mb-2">手动指定（33 类级联）</div>
          <div className="flex flex-wrap items-center gap-2">
            <select
              className="rounded-lg border border-line px-2 py-1.5 text-sm"
              value={general}
              onChange={(e) => {
                setGeneral(e.target.value);
                setManualCode(null);
              }}
            >
              <option value="">选择大类…</option>
              {generals.map((g) => (
                <option key={g} value={g}>
                  {g}
                </option>
              ))}
            </select>

            <select
              className="min-w-64 rounded-lg border border-line px-2 py-1.5 text-sm"
              value={manualCode ?? ""}
              disabled={!general}
              onChange={(e) => setManualCode(Number(e.target.value))}
            >
              <option value="">选择细类…</option>
              {leafOptions.map((s) => (
                <option key={s.code} value={s.code}>
                  [{s.code}] {s.name.slice(0, 48)}
                </option>
              ))}
            </select>

            <button
              className="btn-ghost"
              disabled={busy || manualCode === null}
              onClick={() => decide("manual")}
            >
              提交手动裁定
            </button>
          </div>
        </div>

        {err && <p className="text-xs text-bad">{err}</p>}
      </div>
    </div>
  );
}
