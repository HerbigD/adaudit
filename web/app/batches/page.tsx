"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import type { Batch } from "@/lib/types";

/** 批次列表（④ 的入口）。 */
export default function BatchesPage() {
  const [batches, setBatches] = useState<Batch[]>([]);

  useEffect(() => {
    api.batches().then(setBatches).catch(() => void 0);
  }, []);

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">批次</h1>
      {batches.length === 0 && <p className="text-sm text-muted">暂无批次 —— 先在上传页传一批广告。</p>}
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
  );
}
