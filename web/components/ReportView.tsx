"use client";

/**
 * LLM 报告渲染。极简 Markdown（标题 / 列表 / 引用 / 段落）——
 * 骨架阶段不引 markdown 库，避免为 4 种语法拖一整个依赖树。
 */
export function ReportView({ md }: { md: string | null }) {
  if (!md) {
    return <div className="card text-sm text-muted">尚未生成报告。全部裁定完成后点击「生成报告」。</div>;
  }

  const blocks = md.split("\n").map((line, i) => {
    if (line.startsWith("## ")) return <h2 key={i} className="mt-4 text-base font-semibold">{line.slice(3)}</h2>;
    if (line.startsWith("# ")) return <h1 key={i} className="text-lg font-bold">{line.slice(2)}</h1>;
    if (line.startsWith("> "))
      return <blockquote key={i} className="border-l-2 border-line pl-3 text-xs text-muted">{line.slice(2)}</blockquote>;
    if (line.startsWith("- ")) return <li key={i} className="ml-4 list-disc text-sm">{line.slice(2)}</li>;
    if (!line.trim()) return <div key={i} className="h-2" />;
    return <p key={i} className="text-sm leading-relaxed">{line}</p>;
  });

  return <div className="card space-y-1">{blocks}</div>;
}
