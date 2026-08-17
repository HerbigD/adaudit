"use client";

import { useEffect, useState } from "react";

/**
 * 点击放大原图 —— 人工复核时缩略图看不清糖/纤维标注，等于没给依据。
 * 缩略图任何尺寸都可用；点开为全屏遮罩，Esc 或点击空白关闭。
 * 注意：onClick 里 stopPropagation，避免把外层"展开复核"的点击一并触发。
 */
export function ImageZoom({
  src,
  alt = "",
  className = "",
  style,
  caption,
}: {
  src: string;
  alt?: string;
  className?: string;
  style?: React.CSSProperties;
  /** 大图下方的说明文字（可选） */
  caption?: string;
}) {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [open]);

  return (
    <>
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={src}
        alt={alt}
        title="点击查看大图"
        className={`cursor-zoom-in transition hover:opacity-90 ${className}`}
        style={style}
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          setOpen(true);
        }}
      />

      {open && (
        <div
          role="dialog"
          aria-modal="true"
          className="fixed inset-0 z-50 flex flex-col items-center justify-center gap-3 bg-black/80 p-6"
          onClick={(e) => {
            e.stopPropagation();
            setOpen(false);
          }}
        >
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={src}
            alt={alt}
            className="max-h-[85vh] max-w-[92vw] cursor-zoom-out rounded-lg bg-white object-contain shadow-2xl"
            onClick={(e) => {
              e.stopPropagation();
              setOpen(false);
            }}
          />
          {caption && <p className="max-w-[92vw] text-center text-xs text-white/80">{caption}</p>}
          <button
            type="button"
            className="absolute right-4 top-4 rounded-full bg-white/90 px-3 py-1 text-sm font-medium text-slate-700 hover:bg-white"
            onClick={(e) => {
              e.stopPropagation();
              setOpen(false);
            }}
          >
            关闭 ✕
          </button>
        </div>
      )}
    </>
  );
}
