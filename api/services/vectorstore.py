"""向量库薄封装：Chroma 可用就用 Chroma，不可用则降级到内存 + 字符相似度。

降级是刻意的：W1–W2 骨架必须能在没装 chromadb 的机器上跑通全链路，
W5 把 chromadb 装上后，同一套 upsert/query 接口无需改调用方。
"""

from __future__ import annotations

import difflib
import json
import threading
from pathlib import Path
from typing import Any

from config import settings

_lock = threading.Lock()
_client: Any = None
_backend: str | None = None


def backend() -> str:
    """'chroma' 或 'fallback'。"""
    _ensure()
    return _backend or "fallback"


def _ensure() -> None:
    global _client, _backend
    if _backend is not None:
        return
    with _lock:
        if _backend is not None:
            return
        try:
            import chromadb  # type: ignore

            _client = chromadb.PersistentClient(path=settings.chroma_path)
            _backend = "chroma"
        except Exception:  # noqa: BLE001 — 没装或初始化失败都降级
            _client = _FallbackClient(Path(settings.chroma_path) / "fallback.json")
            _backend = "fallback"


# --------------------------------------------------------------------------- #
# Fallback：JSON 落盘 + difflib 相似度。够 demo，不够 6000 张评测。
# --------------------------------------------------------------------------- #
class _FallbackCollection:
    def __init__(self, name: str, store: "_FallbackClient") -> None:
        self.name = name
        self._store = store
        self._data: dict[str, dict[str, Any]] = store.data.setdefault(name, {})

    def upsert(self, ids, documents, metadatas, **_: Any) -> None:
        for i, doc, meta in zip(ids, documents, metadatas):
            self._data[i] = {"document": doc, "metadata": meta}
        self._store.flush()

    def query(self, query_texts, n_results: int = 5, **_: Any):
        q = query_texts[0].lower()
        scored = sorted(
            (
                (difflib.SequenceMatcher(None, q, v["document"].lower()).ratio(), k, v)
                for k, v in self._data.items()
            ),
            key=lambda t: -t[0],
        )[:n_results]
        return {
            "ids": [[k for _, k, _ in scored]],
            "documents": [[v["document"] for _, _, v in scored]],
            "metadatas": [[v["metadata"] for _, _, v in scored]],
            "distances": [[1.0 - s for s, _, _ in scored]],
        }

    def count(self) -> int:
        return len(self._data)


class _FallbackClient:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data: dict[str, dict[str, Any]] = (
            json.loads(path.read_text()) if path.exists() else {}
        )

    def get_or_create_collection(self, name: str, **_: Any) -> _FallbackCollection:
        return _FallbackCollection(name, self)

    def flush(self) -> None:
        self.path.write_text(json.dumps(self.data, ensure_ascii=False))


def collection(name: str):
    """name ∈ {'products', 'memory'}（方案 §2 末尾）。"""
    _ensure()
    return _client.get_or_create_collection(name)
