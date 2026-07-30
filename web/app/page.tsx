"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { api } from "@/lib/api";
import type { Batch } from "@/lib/types";

/** ① 上传页：拖拽/点选多图 + 可选批次名；上传后单张跳②、多张跳④。 */
export default function UploadPage() {
  const router = useRouter();
  const [files, setFiles] = useState<File[]>([]);
  const [batchName, setBatchName] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [batches, setBatches] = useState<Batch[]>([]);

  useEffect(() => {
    api.batches().then(setBatches).catch(() => void 0);
  }, []);

  async function submit() {
    if (files.length === 0) return;
    setBusy(true);
    setErr(null);
    try {
      const res = await api.upload(files, batchName || undefined);
      router.push(res.redirect);
    } catch (e) {
      setErr(String(e));
      setBusy(false);
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-lg font-semibold">上传广告图片</h1>
        <p className="text-sm text-muted">
          单张 → 审计卡片页看实时 Agent 过程；多张 → 建批次并生成品类结构报告。
        </p>
      </div>

      <div
        className="card flex min-h-48 flex-col items-center justify-center gap-3 border-2 border-dashed"
        onDragOver={(e) => e.preventDefault()}
        onDrop={(e) => {
          e.preventDefault();
          setFiles(Array.from(e.dataTransfer.files));
        }}
      >
        <p className="text-sm text-muted">把广告图拖到这里，或</p>
        <label className="btn-ghost cursor-pointer">
          选择文件
          <input
            type="file"
            multiple
            accept="image/*"
            className="hidden"
            onChange={(e) => setFiles(Array.from(e.target.files ?? []))}
          />
        </label>
        {files.length > 0 && (
          <p className="text-sm">
            已选 {files.length} 张：
            <span className="text-muted"> {files.map((f) => f.name).join("、").slice(0, 120)}</span>
          </p>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <input
          className="rounded-lg border border-line px-3 py-2 text-sm"
          placeholder="批次名（可选）"
          value={batchName}
          onChange={(e) => setBatchName(e.target.value)}
        />
        <button className="btn-primary" disabled={busy || files.length === 0} onClick={submit}>
          {busy ? "上传中…" : "开始审计"}
        </button>
        {err && <span className="text-xs text-bad">{err}</span>}
      </div>

      <div>
        <h2 className="mb-2 text-sm font-semibold">历史批次</h2>
        {batches.length === 0 && <p className="text-sm text-muted">暂无批次</p>}
        <ul className="space-y-2">
          {batches.map((b) => (
            <li key={b.id}>
              <Link href={`/batches/${b.id}`} className="card flex items-center justify-between hover:bg-slate-50">
                <span className="text-sm font-medium">{b.name}</span>
                <span className="text-xs text-muted">
                  {b.status} · {b.created_at}
                </span>
              </Link>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
