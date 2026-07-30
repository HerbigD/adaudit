"use client";

export function ConfidenceBadge({
  value,
  label = "置信度",
}: {
  value: number;
  label?: string;
}) {
  const pct = Math.round(value * 100);
  const tone =
    value >= 0.85 ? "bg-emerald-100 text-emerald-700" : value >= 0.7 ? "bg-amber-100 text-amber-700" : "bg-red-100 text-red-700";
  return (
    <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${tone}`}>
      {label} {pct}%
    </span>
  );
}

export function ConfidenceBar({ value }: { value: number }) {
  const pct = Math.round(value * 100);
  const tone = value >= 0.85 ? "bg-ok" : value >= 0.7 ? "bg-warn" : "bg-bad";
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-slate-100">
      <div className={`h-full ${tone}`} style={{ width: `${pct}%` }} />
    </div>
  );
}
