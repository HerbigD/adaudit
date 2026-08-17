"""向量库薄封装：Chroma 可用就用 Chroma，不可用则降级到 JSON + difflib 字符相似度。

降级是刻意的：W1–W2 骨架必须能在没装 chromadb 的机器上跑通全链路，
装上 chromadb 后同一套 upsert/query 接口无需改调用方。

## 降级必须响（Day8）

之前降级是**静默**的。而缓存命中的得分构成是
`0.55 品牌 + 0.20 名称重叠 + 0.25 语义`，阈值 0.82 ——
**语义分是每一次命中的必要条件**。所以降级不是"检索差一点"，是"缓存直接失效"，
表现却像"这批广告恰好没命中过"。

现在：降级一律打 `logger.warning`，`backend()` 如实返回当前后端，
且这个值会进 trace 与 stats_json。任何一份缓存命中率数字都必须能答出
"这是在哪个 backend 上测的"。

## Chroma 的隐藏网络依赖（Day8 实测）

`chromadb` 装上不等于能用：默认 embedding function（ONNXMiniLM_L6_V2）
**首次使用时要联网下载 ~80MB 模型**。沙箱里直接 `ProxyError: 403`。
而且它不是在建 client 时炸，是在第一次 upsert/query 时炸 ——
只在 `_ensure()` 里 try/except 拦不住。

所以这里包了一层 `_Guarded`：任何一次 chroma 操作抛错就**永久降级**到 fallback、
打一条醒目 warning、并把该操作在 fallback 上重放。宁可慢，不要静默失效。
"""

from __future__ import annotations

import difflib
import json
import logging
import threading
from pathlib import Path
from typing import Any

from config import settings

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_client: Any = None
_backend: str | None = None
_degrade_reason: str | None = None

# 一次会话里只吼一次，避免每条广告刷屏
_warned = False


def backend() -> str:
    """`chroma` | `difflib`。**这个值必须跟着每一份缓存指标走。**"""
    _ensure()
    return _backend or "difflib"


def degrade_reason() -> str | None:
    """降级到 difflib 的原因；用 chroma 时为 None。"""
    _ensure()
    return _degrade_reason


def _warn_degraded(reason: str) -> None:
    global _warned, _degrade_reason
    _degrade_reason = reason
    if _warned:
        return
    _warned = True
    logger.warning(
        "\n"
        "==================== 向量库已降级到 difflib ====================\n"
        " 原因：%s\n"
        " 影响：缓存命中得分 = 0.55 品牌 + 0.20 名称重叠 + 0.25 语义，阈值 0.82。\n"
        "       difflib 只是字符相似度，语义分不可靠 —— 命中率会明显偏低，\n"
        "       且**这不是缓存写入的问题**，排查方向别走偏。\n"
        " 处置：pip install -e '.[vector]'；chromadb 首次使用需联网下载 embedding 模型。\n"
        " 提醒：任何缓存指标都必须注明 cache_backend，否则数字之间不可比。\n"
        "===============================================================",
        reason,
    )


def _make_fallback() -> Any:
    return _FallbackClient(Path(settings.chroma_path) / "fallback.json")


def _ensure() -> None:
    global _client, _backend
    if _backend is not None:
        return
    with _lock:
        if _backend is not None:
            return
        try:
            import chromadb  # type: ignore

            _client = _Guarded(chromadb.PersistentClient(path=settings.chroma_path))
            _backend = "chroma"
        except ImportError as exc:
            _client, _backend = _make_fallback(), "difflib"
            _warn_degraded(f"chromadb 未安装（{exc}）")
        except Exception as exc:  # noqa: BLE001 — 初始化失败也降级
            _client, _backend = _make_fallback(), "difflib"
            _warn_degraded(f"chromadb 初始化失败（{type(exc).__name__}: {exc}）")


def _demote(reason: str) -> Any:
    """把后端**永久**降到 fallback，并返回新的 client。

    永久而不是本次重试：embedding 模型下不下来是环境问题，
    每条广告重试一遍只会把每次缓存查询都拖上一个网络超时。
    """
    global _client, _backend
    with _lock:
        if _backend != "chroma":
            return _client
        _client, _backend = _make_fallback(), "difflib"
    _warn_degraded(reason)
    return _client


class VectorStoreUsageError(ValueError):
    """我们自己把参数传错了 —— 不是环境问题，不该降级。"""


def _validate_upsert(ids, documents, metadatas) -> None:
    """显式校验我们这边的参数。**两个后端的 upsert 入口都要调它。**

    存在的意义是把"我们的 bug"和"环境的 bug"在源头分开，
    这样后端那边就可以简单粗暴：出错一律降级。

    第一版只挂在 `_GuardedCollection.upsert` 上，而那个 wrapper **只在 chroma
    路径上被构造** —— 没装 chromadb、或已经降级之后，`collection()` 返回的是裸的
    `_FallbackCollection`，一点都不校验。结论正好翻了过来：校验只存在于装了
    chroma 的机器上，而它想防的恰恰是"bug 只在装了 chroma 的机器上出现"。
    所以现在 `_FallbackCollection.upsert` 里也调一次。
    """
    if not ids or not documents:
        raise VectorStoreUsageError("ids 与 documents 都不能为空")
    if len(ids) != len(documents):
        raise VectorStoreUsageError(f"ids({len(ids)}) 与 documents({len(documents)}) 长度不一致")
    if metadatas is not None:
        if len(metadatas) != len(ids):
            raise VectorStoreUsageError(
                f"metadatas({len(metadatas)}) 与 ids({len(ids)}) 长度不一致"
            )
        for i, m in enumerate(metadatas):
            if not m:
                raise VectorStoreUsageError(
                    f"metadatas[{i}] 为空 —— chroma 不接受空 metadata，"
                    f"fallback 接受，两边行为不一致会让 bug 只在装了 chroma 的机器上出现"
                )


class _GuardedCollection:
    """包住 chroma 的 collection：任何一次操作抛错就降级并在 fallback 上重放。

    为什么不让它抛出去：调用方（cache_store）本来就把向量查询包在 try/except 里
    「不阻断主链路」，于是 chroma 一坏，语义分静默变 0、缓存永远命中不了，
    而日志里什么都看不到。这一层的存在就是为了让那件事**响一声**。
    """

    def __init__(self, inner: Any, name: str) -> None:
        self._inner, self._name = inner, name

    def _live(self) -> Any:
        """每次操作前解析当前后端。

        不这么做会有个很隐蔽的问题：降级发生后，**已经拿在手里的 collection 句柄**
        仍然指着那个坏掉的 chroma collection。对会抛错的操作（upsert/query）没关系
        —— 它们会走重试路径；但对**不抛错**的操作（`count()` 不需要 embedding）
        就会安静地返回 chroma 里的空数据。实测：数据明明写进了 fallback，
        旧句柄的 `count()` 仍然报 0，而新句柄报 1。
        """
        if _backend == "chroma":
            return self._inner
        return _client.get_or_create_collection(self._name)

    def _retry_on_fallback(self, op: str, exc: Exception, *args, **kw):
        """chroma 出的任何错都降级；**调用方自己的参数错误在进来之前就拦掉了**。

        第一版这里按异常类型分流（`ValueError`/`TypeError` 视为调用方 bug 原样抛出）。
        那个判据是错的：chroma 把 embedding 模型下载失败也包成 `ValueError`
        从 `_validate_and_prepare_upsert_request` 抛出 —— 于是最该降级的那种失败
        被当成了"我们传错参数"，原样抛给了调用方，再被 `cache_store` 的
        `except Exception: pass` 吞掉。没降级、没 warning、什么都没写。

        教训与 HFSS 正则那次同类：**不要从表象（异常类型 / 名称字符串）反推语义。**
        现在改成：我们自己的参数由 `_validate_upsert` 显式校验并抛出，
        剩下从 chroma 出来的一律当环境失败处理。
        """
        client = _demote(f"chroma {op} 失败（{type(exc).__name__}: {str(exc)[:80]}）")
        return getattr(client.get_or_create_collection(self._name), op)(*args, **kw)

    def upsert(self, *a, **kw):
        _validate_upsert(kw.get("ids", a[0] if a else None),
                         kw.get("documents", a[1] if len(a) > 1 else None),
                         kw.get("metadatas", a[2] if len(a) > 2 else None))
        try:
            return self._live().upsert(*a, **kw)
        except Exception as exc:  # noqa: BLE001
            return self._retry_on_fallback("upsert", exc, *a, **kw)

    def query(self, *a, **kw):
        try:
            return self._live().query(*a, **kw)
        except Exception as exc:  # noqa: BLE001
            return self._retry_on_fallback("query", exc, *a, **kw)

    def count(self) -> int:
        try:
            return self._live().count()
        except Exception as exc:  # noqa: BLE001
            return self._retry_on_fallback("count", exc)


class _Guarded:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def get_or_create_collection(self, name: str, **kw: Any):
        """**建 collection 这一步本身就会炸**，所以它也要被守住。

        chroma 在 `get_or_create_collection` 时就会构造默认 embedding function，
        而那一步要联网下载 ~80MB 的 ONNX 模型。第一版守卫只包了 upsert/query，
        于是异常从这里漏出去、被调用方 `cache_store` 的
        `except Exception: pass`（"向量库不可用不应阻断主链路"）吞掉 ——
        结果是：没有降级、没有 warning、什么都没写进去，`backend()` 仍报 chroma。
        缓存命中率因此静默塌到 0.75 以下，而日志里一片安静。
        """
        try:
            return _GuardedCollection(self._inner.get_or_create_collection(name, **kw), name)
        except Exception as exc:  # noqa: BLE001
            client = _demote(
                f"chroma 建 collection 失败（{type(exc).__name__}: {str(exc)[:80]}）"
            )
            return client.get_or_create_collection(name)


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
        # 与 chroma 路径同一把尺子：fallback 本身能接受空 metadata，
        # 放过去就等于把 bug 攒到"装了 chromadb 之后"才爆。
        _validate_upsert(ids, documents, metadatas)
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


def force_fallback(reason: str = "调用方显式指定") -> None:
    """显式切到 difflib 后端。

    对比实验要能**主动选后端**，不能只看"环境里碰巧装没装 chromadb"——
    那样两次跑的差异里会混进环境差异，比出来的东西不知道是什么。
    """
    global _client, _backend
    with _lock:
        _client, _backend = _make_fallback(), "difflib"
    _warn_degraded(reason)


def reset() -> None:
    """丢弃单例，下次访问按当前 `settings.chroma_path` 重建。

    测试用：向量库是进程级单例，不重置的话上一个测试文件写进去的档案会漂到下一个，
    命中得分因此变得依赖测试执行顺序（实测能让缓存命中分从 1.00 掉到 0.75）。
    """
    global _client, _backend, _degrade_reason, _warned
    _client, _backend, _degrade_reason, _warned = None, None, None, False
