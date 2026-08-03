"use client";

import { useEffect, useState } from "react";
import { api, imageUrl } from "@/lib/api";
import type {
  Classification,
  Evidence,
  GeneralCategory,
  SpecificCategory,
} from "@/lib/types";
import { AuditCard } from "./AuditCard";
import { EvidenceList } from "./EvidenceList";
import { ImageZoom } from "./ImageZoom";

/**
 * 双选项对比 —— 全链路闭环的核心交互（W6 红线）。
 *
 * 版面分工（一件事只说一遍）：
 *   ① 上方卡片 = 模型怎么想的：推理过程文字 + 大类/细类两级置信度；
 *   ② 下方卡片 = 模型给出的选项：带类别名与判定依据，点一下即裁定；
 *   ③ 最下操作区 = 采纳 original / 采纳 prediction / 33 类级联手动指定。
 * 候选编号只在 ② 出现，① 不再重复；prediction 缺席时不占一张空卡片，改一行说明。
 */
export function ReviewCompare({
  auditId,
  initial,
  revised,
  evidence = [],
  reason,
  imagePath,
  onDecided,
}: {
  auditId: string;
  initial: Classification | null;
  revised: Classification | null;
  evidence?: Evidence[];
  reason?: string;
  /** 广告原图路径。传了就在复核区顶部给一张可点开的大图。 */
  imagePath?: string | null;
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
  // 候选**带名字**才叫选项。原来卡片上只有 "[5] vs [19]" 两个裸编号，
  // 人要去别处查这两个码分别是什么，等于没给选项。
  const candidateList = specifics.filter((s) => candidates.has(s.code));
  const showOptions = !revised && candidateList.length > 0;

  async function decide(choice: "original" | "prediction" | "manual", code?: number) {
    setBusy(true);
    setErr(null);
    try {
      const picked = choice === "manual" ? code ?? manualCode ?? undefined : undefined;
      const res = await api.decide(auditId, choice, picked);
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

      {imagePath && (
        <div className="card flex items-center gap-4">
          <ImageZoom
            src={imageUrl(imagePath)}
            alt="广告原图"
            caption={initial?.product_name ?? undefined}
            className="h-32 w-32 rounded-lg border border-line bg-white object-contain p-1"
          />
          <div className="text-xs text-muted">
            <div className="font-medium text-slate-600">广告原图</div>
            <p className="mt-1">点击图片查看大图，按 Esc 或点击空白处关闭。</p>
          </div>
        </div>
      )}

      {/* ① 模型怎么想的：推理过程 + 两级置信度 */}
      <div className={revised ? "grid gap-4 md:grid-cols-2" : ""}>
        <AuditCard
          classification={initial}
          title="original · 初判"
          reasoningFirst
          hideCandidates={showOptions}
        />
        {revised && (
          <AuditCard
            classification={revised}
            title="prediction · 搜索后重裁决"
            verified
            reasoningFirst
            hideCandidates={showOptions}
          />
        )}
      </div>

      {!revised && (
        <p className="px-1 text-xs text-muted">
          本次没有 prediction —— {reason ?? "取证未产出结论"}。Agent 判不了就交出来，
          不会拿一个凑数的答案顶上；请在下方选项里裁定，或用"手动指定"。
        </p>
      )}

      {evidence.length > 0 && <EvidenceList evidence={evidence} />}

      {/* ② 模型给出的选项 */}
      {showOptions && (
        <div className="card space-y-3 border-amber-200 bg-amber-50/50">
          <div>
            <h3 className="text-sm font-semibold text-amber-900">
              模型给出的{candidateList.length === 2 ? "两个" : `${candidateList.length} 个`}选项
            </h3>
            <p className="mt-1 text-xs text-amber-800">
              取证没拿到营养数据，所以这几个之间模型分不出来。下面写了各自的判定依据，
              点一下即可裁定（记为手动裁定，不计入"预测采纳率"）。
            </p>
          </div>
          <div className="grid gap-2 md:grid-cols-2">
            {candidateList.map((s) => (
              <button
                key={s.code}
                disabled={busy}
                onClick={() => decide("manual", s.code)}
                className="rounded-lg border border-amber-300 bg-white p-3 text-left hover:bg-amber-100 disabled:opacity-50"
              >
                <div className="text-sm font-medium">
                  [{s.code}] {s.name_zh}
                </div>
                <div className="mt-1 text-xs text-muted">{s.description_zh}</div>
                {s.key_dimensions?.length > 0 && (
                  <div className="mt-1 text-xs text-amber-700">
                    判定依据：{s.key_dimensions.join(" / ")}
                  </div>
                )}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* ③ 操作区 */}
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
