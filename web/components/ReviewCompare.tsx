"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type {
  Classification,
  Evidence,
  GeneralCategory,
  SpecificCategory,
} from "@/lib/types";
import { AuditCard } from "./AuditCard";
import { EvidenceList } from "./EvidenceList";

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
  const [generals, setGenerals] = useState<GeneralCategory[]>([]);
  const [generalId, setGeneralId] = useState<number | "">("");
  const [manualCode, setManualCode] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [ingested, setIngested] = useState<string[] | null>(null);

  useEffect(() => {
    api
      .taxonomy()
      .then((t) => {
        setSpecifics(t.specifics);
        setGenerals(t.generals);
      })
      .catch(() => void 0);
  }, []);

  const leafOptions = specifics.filter((s) => s.parent_id === generalId);
  // 叶子待定时，把候选类别顶到列表最前面 —— 人工最可能就在这两个里选
  const candidates = new Set([
    ...(initial?.candidate_codes ?? []),
    ...(revised?.candidate_codes ?? []),
  ]);

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

      {evidence.length > 0 && <EvidenceList evidence={evidence} />}

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
              value={generalId}
              onChange={(e) => {
                setGeneralId(e.target.value ? Number(e.target.value) : "");
                setManualCode(null);
              }}
            >
              <option value="">选择大类…</option>
              {generals.map((g) => (
                <option key={g.id} value={g.id}>
                  {g.label} / {g.name_zh}
                </option>
              ))}
            </select>

            <select
              className="min-w-64 rounded-lg border border-line px-2 py-1.5 text-sm"
              value={manualCode ?? ""}
              disabled={generalId === ""}
              onChange={(e) => setManualCode(Number(e.target.value))}
            >
              <option value="">选择细类…</option>
              {leafOptions.map((s) => (
                <option key={s.code} value={s.code}>
                  {candidates.has(s.code) ? "★ " : ""}[{s.code}] {s.name_zh}
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
