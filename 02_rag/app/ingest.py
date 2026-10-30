"""建索引：读 SQLite 里的切片 → 向量化 → 落 FAISS。

幂等设计
--------
`build()` 先比对三件事：embedding 模型名、语料指纹（`corpus_hash`）、切片总数。
三者都没变就跳过，直接复用磁盘上的索引 —— 重启服务不该触发一次全量重算。

为什么不增量
------------
万级切片在 CPU 上跑 bge-small-zh 大约十几秒，全量重建比维护增量状态更省心，
也不会出现「删掉的切片还留在索引里」这类脏数据。真到十万级再换 IVF + 增量。
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from .embeddings import get_embedder
from .store import BuildReport, TourismDB, VectorStore


def build_index(db: TourismDB, embedder=None, *, force: bool = False) -> BuildReport:
    emb = embedder or get_embedder()
    store = VectorStore(db, emb)
    return store.build(force=force)


def ensure_index(db: TourismDB, *, embedder=None) -> BuildReport:
    """服务启动时调用：没有索引就建，有就复用。"""
    return build_index(db, embedder, force=False)


def rebuild_on_backend_change(db: TourismDB) -> BuildReport | None:
    """后端从 hash 换成语义模型（或反之）时，旧索引必然失效。

    这里不主动删文件，只返回 None 表示「无需处理」或一个新建的 report；
    `VectorStore.load()` 在模型名不匹配时会抛错，避免拿错索引算相似度。
    """
    meta = db.load_index_meta()
    if not meta:
        return None
    emb = get_embedder()
    if meta.get("embedding_model") == emb.name:
        return None
    return build_index(db, emb, force=True)


@dataclass
class IndexStatus:
    ready: bool
    total: int
    model: str
    dim: int
    built_at: str | None
    note: str | None = None

    def as_dict(self) -> dict:
        return {"ready": self.ready, "total": self.total, "model": self.model,
                "dim": self.dim, "built_at": self.built_at, "note": self.note}


def index_status(db: TourismDB) -> IndexStatus:
    meta = db.load_index_meta() or {}
    emb = get_embedder()
    ready = bool(meta) and meta.get("embedding_model") == emb.name
    note = None
    if meta and not ready:
        note = (f"索引由 {meta.get('embedding_model')} 建立，当前后端是 {emb.name}，"
                f"需要重建")
    elif not meta:
        note = "尚未建索引"
    return IndexStatus(
        ready=ready,
        total=int(meta.get("total_vectors") or 0),
        model=meta.get("embedding_model") or emb.name,
        dim=int(meta.get("dim") or emb.dim),
        built_at=meta.get("built_at"),
        note=note,
    )
