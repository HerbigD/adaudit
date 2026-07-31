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

    @property
    def _data(self) -> dict[str, dict[str, Any]]:
        """每次访问都过一遍 `reload_if_changed` —— 不缓存 dict 引用。

        Day7 踩到的坑：原来在 `__init__` 里把 `store.data[name]` 存成实例属性，
        于是这个 collection 永远看的是**构造那一刻**的快照。
        表现是 API 进程启动后，别的进程（脚本、eval runner）写进 fallback.json 的档案
        对它完全不可见 —— 缓存命中得分停在 0.75（只有品牌+名称重叠，没有语义分），
        刚好卡在 0.82 阈值下面，看起来就像"缓存没命中"，而不是"索引没刷新"。
        """
        self._store.reload_if_changed()
        return self._store.data.setdefault(self.name, {})

    def upsert(self, ids, documents, metadatas, **_: Any) -> None:
        data = self._data
        for i, doc, meta in zip(ids, documents, metadatas):
            data[i] = {"document": doc, "metadata": meta}
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
    """JSON 落盘的极简向量库。**按 mtime 惰性重载**，这样多进程之间不会互相看不见。

    不上文件锁：这是 demo 级降级实现，并发写的最坏结果是后写覆盖先写，
    而真实并发写只会来自单个 API 进程内的顺序调用。要真正的并发安全就装 chromadb。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data: dict[str, dict[str, Any]] = {}
        self._mtime: float = -1.0
        self.reload_if_changed()

    def reload_if_changed(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            return
        if mtime == self._mtime:
            return
        try:
            self.data = json.loads(self.path.read_text() or "{}")
        except json.JSONDecodeError:      # 半截文件（另一进程正在写）：下次再读
            return
        self._mtime = mtime

    def get_or_create_collection(self, name: str, **_: Any) -> _FallbackCollection:
        return _FallbackCollection(name, self)

    def flush(self) -> None:
        self.path.write_text(json.dumps(self.data, ensure_ascii=False))
        try:
            self._mtime = self.path.stat().st_mtime   # 自己写的不必再读回来
        except FileNotFoundError:
            pass


def collection(name: str):
    """name ∈ {'products', 'memory'}（方案 §2 末尾）。"""
    _ensure()
    return _client.get_or_create_collection(name)


def reset() -> None:
    """丢弃单例，下次访问按当前 `settings.chroma_path` 重建。

    测试用：向量库是进程级单例，不重置的话上一个测试文件写进去的档案会漂到下一个，
    命中得分因此变得依赖测试执行顺序（实测能让缓存命中分从 1.00 掉到 0.75）。
    """
    global _client, _backend
    _client, _backend = None, None
