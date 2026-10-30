"""向量库：LangChain `FAISS` + 本地持久化，元数据回写 tourism.db。

为什么用 LangChain 的 FAISS 而不是自己包 `faiss.IndexFlatIP`
------------------------------------------------------------
自己包只有 add/search 两个方法，检索命中后还得手动回 SQLite 拿正文。LangChain 的
`FAISS` 把「向量 → Document(带 metadata)」的映射一起管，`similarity_search_with_score`
直接返回可读片段与来源字段，上层的 prompt 拼装、引用溯源都省一层转换。
持久化用 `save_local`/`load_local`（落 `<index>.faiss` + `<index>.pkl`），
比裸 faiss 多一个 metadata 序列化文件，换来开箱即用的 metadata。

同时保留一条「faiss_row → chunk_id」的映射写回 `chunks.faiss_row`，
这样即使 pkl 丢了，也能靠 SQLite 复原语料，不至于两份数据不同步。
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from . import settings_store
from .embeddings import as_langchain_embeddings, get_embedder

RAG_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = RAG_ROOT.parent
DEFAULT_DB = PROJECT_ROOT / "01_crawler" / "data" / "tourism.db"
DB_ENV = "TOURISM_DB_PATH"
INDEX_NAME = "fujian_tourism"


def resolve_db_path() -> Path:
    return Path(os.environ.get(DB_ENV) or DEFAULT_DB).resolve()


def index_dir() -> Path:
    return Path(os.environ.get("RAG_INDEX_DIR") or (RAG_ROOT / "data" / "faiss")).resolve()


@dataclass
class DocRow:
    doc_id: str
    title: str
    doc_type: str
    issuer: str | None
    doc_no: str | None
    publish_date: str | None
    effective_from: str | None
    effective_to: str | None
    url: str
    content_hash: str | None
    current_version: int


@dataclass
class ChunkRow:
    chunk_id: str
    doc_id: str
    chunk_index: int
    text: str
    heading_path: str | None
    page_no: int | None
    token_estimate: float | None


class TourismDB:
    """读 01_crawler 的库；只允许写索引元数据（faiss_index_meta / chunks.faiss_row）。"""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else resolve_db_path()
        if not self.path.exists():
            raise FileNotFoundError(
                f"未找到数据库 {self.path}\n"
                f"先跑采集：cd ../01_crawler && python cli.py pipeline --all\n"
                f"或用环境变量 {DB_ENV} 指向别的库"
            )
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")

    # ------------------------------------------------------------------ 文档
    def documents(self) -> list[DocRow]:
        rows = self.conn.execute(
            """SELECT doc_id, title, doc_type, issuer, doc_no, publish_date,
                      effective_from, effective_to, url, content_hash, current_version
               FROM documents WHERE fetch_status='ok'
               ORDER BY COALESCE(publish_date,'') DESC, doc_id"""
        ).fetchall()
        return [DocRow(**dict(r)) for r in rows]

    def doc(self, doc_id: str) -> DocRow | None:
        r = self.conn.execute(
            """SELECT doc_id, title, doc_type, issuer, doc_no, publish_date,
                      effective_from, effective_to, url, content_hash, current_version
               FROM documents WHERE doc_id=?""", (doc_id,)).fetchone()
        return DocRow(**dict(r)) if r else None

    def doc_versions(self, doc_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT version_no, diff_summary, fetched_at FROM doc_versions "
            "WHERE doc_id=? ORDER BY version_no DESC", (doc_id,)).fetchall()
        return [dict(r) for r in rows]

    def chunks(self, doc_id: str | None = None) -> list[ChunkRow]:
        sql = ("SELECT chunk_id, doc_id, chunk_index, text, heading_path, page_no, "
               "token_estimate FROM chunks ")
        args: tuple = ()
        if doc_id:
            sql += "WHERE doc_id=? ORDER BY chunk_index"
            args = (doc_id,)
        else:
            sql += "ORDER BY doc_id, chunk_index"
        return [ChunkRow(**dict(r)) for r in self.conn.execute(sql, args)]

    def chunk(self, chunk_id: str) -> ChunkRow | None:
        r = self.conn.execute(
            """SELECT chunk_id, doc_id, chunk_index, text, heading_path, page_no,
                      token_estimate FROM chunks WHERE chunk_id=?""", (chunk_id,)).fetchone()
        return ChunkRow(**dict(r)) if r else None

    def corpus_hash(self) -> str:
        """全部 chunk 的稳定指纹，用来判断索引是否还新鲜。"""
        import hashlib
        h = hashlib.sha256()
        for r in self.conn.execute(
            """SELECT c.chunk_id, c.text, d.content_hash
               FROM chunks c JOIN documents d ON d.doc_id=c.doc_id
               ORDER BY c.chunk_id"""
        ):
            h.update(f"{r['chunk_id']}|{r['content_hash']}|{len(r['text'] or '')}".encode())
        return h.hexdigest()

    # ------------------------------------------------------------------ 景区与证据
    def spots(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM scenic_spots ORDER BY spot_id")]

    def spot(self, spot_id: str) -> dict | None:
        r = self.conn.execute(
            "SELECT * FROM scenic_spots WHERE spot_id=?", (spot_id,)).fetchone()
        return dict(r) if r else None

    def spot_by_name(self, name: str) -> dict | None:
        r = self.conn.execute(
            "SELECT * FROM scenic_spots WHERE name=? OR alias=? LIMIT 1", (name, name)).fetchone()
        return dict(r) if r else None

    def fields(self, spot_id: str, include_non_current: bool = True) -> list[dict]:
        sql = ("SELECT * FROM field_provenance WHERE spot_id=?"
               + ("" if include_non_current else " AND is_current=1")
               + " ORDER BY field_name, prov_id")
        return [dict(r) for r in self.conn.execute(sql, (spot_id,))]

    def conflicts(self, spot_id: str | None = None) -> list[dict]:
        sql = ("SELECT * FROM field_provenance WHERE conflict_group IS NOT NULL"
               + (" AND spot_id=?" if spot_id else "")
               + " ORDER BY field_name, prov_id")
        args = (spot_id,) if spot_id else ()
        groups: dict[str, list[dict]] = {}
        for r in self.conn.execute(sql, args):
            row = dict(r)
            groups.setdefault(row["conflict_group"], []).append(row)
        out = []
        for gid, items in groups.items():
            values = sorted({i["field_value"] for i in items})
            if len(values) < 2:
                continue
            out.append({
                "conflict_group": gid,
                "spot_id": items[0]["spot_id"],
                "field_name": items[0]["field_name"],
                "values": values,
                "items": items,
            })
        return out

    def related_docs(self, spot_id: str) -> list[dict]:
        rows = self.conn.execute(
            """SELECT l.doc_id, l.relation, l.confidence, l.evidence,
                      d.title, d.url, d.publish_date, d.issuer, d.doc_no
               FROM spot_policy_link l JOIN documents d ON d.doc_id=l.doc_id
               WHERE l.spot_id=? ORDER BY l.confidence DESC, d.publish_date DESC""",
            (spot_id,)).fetchall()
        return [dict(r) for r in rows]

    def review_queue(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            """SELECT q.item_id, q.prov_id, q.doc_id, q.reason, q.severity,
                      q.created_at, q.resolved_at,
                      p.spot_id, p.field_name, p.field_value, p.status
               FROM review_queue q
               LEFT JOIN field_provenance p ON p.prov_id = q.prov_id
               WHERE q.resolved_at IS NULL
               ORDER BY CASE q.severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                        q.item_id DESC LIMIT ?""", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        def one(sql: str) -> int:
            r = self.conn.execute(sql).fetchone()
            return int(r[0]) if r else 0
        return {
            "documents": one("SELECT COUNT(*) FROM documents WHERE fetch_status='ok'"),
            "chunks": one("SELECT COUNT(*) FROM chunks"),
            "spots": one("SELECT COUNT(*) FROM scenic_spots"),
            "provenance": one("SELECT COUNT(*) FROM field_provenance"),
            "verified": one("SELECT COUNT(*) FROM field_provenance WHERE status='已核验'"),
            "pending": one("SELECT COUNT(*) FROM field_provenance WHERE status='待核验'"),
            "conflicts": one("SELECT COUNT(*) FROM field_provenance WHERE status='冲突'"),
            "open_review": one("SELECT COUNT(*) FROM review_queue WHERE resolved_at IS NULL"),
        }

    # ------------------------------------------------------------------ 索引元数据（可写）
    def save_index_meta(self, *, embedding_model: str, dim: int, total_vectors: int,
                        index_path: str, corpus_hash: str, metric: str = "ip",
                        nlist: int = 0) -> None:
        self.conn.execute(
            """INSERT INTO faiss_index_meta(index_name, embedding_model, dim, metric,
                 total_vectors, nlist, index_path, corpus_hash, built_at)
               VALUES (?,?,?,?,?,?,?,?,datetime('now','localtime'))
               ON CONFLICT(index_name) DO UPDATE SET
                 embedding_model=excluded.embedding_model, dim=excluded.dim,
                 metric=excluded.metric, total_vectors=excluded.total_vectors,
                 nlist=excluded.nlist, index_path=excluded.index_path,
                 corpus_hash=excluded.corpus_hash, built_at=excluded.built_at""",
            (INDEX_NAME, embedding_model, dim, metric, total_vectors,
             nlist, index_path, corpus_hash))
        self.conn.commit()

    def load_index_meta(self) -> dict | None:
        r = self.conn.execute(
            "SELECT * FROM faiss_index_meta WHERE index_name=?", (INDEX_NAME,)).fetchone()
        return dict(r) if r else None

    def set_faiss_rows(self, mapping) -> None:
        """回填 chunks.faiss_row：mapping = [(row_no, chunk_id), ...]。"""
        self.conn.executemany(
            "UPDATE chunks SET faiss_row=? WHERE chunk_id=?", mapping)
        self.conn.commit()

    def clear_faiss_rows(self) -> None:
        self.conn.execute("UPDATE chunks SET faiss_row=NULL")
        self.conn.commit()

    def chunk_by_faiss_row(self, row_no: int) -> ChunkRow | None:
        r = self.conn.execute(
            """SELECT chunk_id, doc_id, chunk_index, text, heading_path, page_no,
                      token_estimate FROM chunks WHERE faiss_row=?""", (row_no,)).fetchone()
        return ChunkRow(**dict(r)) if r else None

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass


# ====================================================================== 向量库
class VectorStore:
    """LangChain FAISS 的持有者：建库、落盘、加载、检索。"""

    def __init__(self, db: TourismDB, embedder=None):
        self.db = db
        self.embedder = embedder or get_embedder()
        self._vs = None
        self._lc_emb = None

    # ------------------------------------------------------------------ 加载
    @property
    def lc_embeddings(self):
        if self._lc_emb is None:
            self._lc_emb = as_langchain_embeddings(self.embedder)
        return self._lc_emb

    def index_path(self) -> Path:
        return index_dir() / INDEX_NAME

    def exists(self) -> bool:
        p = self.index_path()
        return p.with_suffix(".faiss").exists() or Path(str(p) + ".faiss").exists()

    def load(self) -> bool:
        """从磁盘加载索引。元数据里的模型名和当前 embedder 不一致就拒绝加载 ——
        不同模型的向量空间不能混算，加载了也只是给出无意义的分数。"""
        from langchain_community.vectorstores import FAISS

        path = self.index_path()
        faiss_file = Path(str(path) + ".faiss")
        if not faiss_file.exists():
            return False

        meta = self.db.load_index_meta()
        if meta and meta.get("embedding_model") != self.embedder.name:
            raise RuntimeError(
                f"索引是用 {meta.get('embedding_model')} 建的，当前后端是 "
                f"{self.embedder.name}；请重建索引")

        self._vs = FAISS.load_local(
            str(index_dir()), self.lc_embeddings,
            index_name=INDEX_NAME, allow_dangerous_deserialization=True,
        )
        return True

    def ensure(self) -> bool:
        if self._vs is not None:
            return True
        return self.load()

    # ------------------------------------------------------------------ 建库
    def build(self, force: bool = False) -> "BuildReport":
        import time
        from langchain_core.documents import Document
        from langchain_community.vectorstores import FAISS

        t0 = time.perf_counter()
        chunks = self.db.chunks()
        if not chunks:
            return BuildReport("empty", 0, self.embedder.dim, self.embedder.name,
                               "", "", 0, "库内没有切片，先跑清洗")

        corpus_hash = self.db.corpus_hash()
        meta = self.db.load_index_meta()
        if (not force and meta and self.exists()
                and meta.get("embedding_model") == self.embedder.name
                and meta.get("corpus_hash") == corpus_hash
                and int(meta.get("total_vectors") or 0) == len(chunks)):
            return BuildReport("skipped", len(chunks), self.embedder.dim,
                               self.embedder.name, str(self.index_path()), corpus_hash,
                               int((time.perf_counter() - t0) * 1000),
                               "语料与模型都没变，复用已有索引")

        docs: list[Document] = []
        for c in chunks:
            doc = self.db.doc(c.doc_id)
            docs.append(Document(
                page_content=c.text,
                metadata={
                    "chunk_id": c.chunk_id,
                    "doc_id": c.doc_id,
                    "chunk_index": c.chunk_index,
                    "heading_path": c.heading_path or "",
                    "page_no": c.page_no or 0,
                    "doc_title": doc.title if doc else "",
                    "issuer": doc.issuer if doc else "",
                    "doc_no": doc.doc_no if doc else "",
                    "publish_date": doc.publish_date if doc else "",
                    "effective_from": doc.effective_from if doc else "",
                    "effective_to": doc.effective_to if doc else "",
                    "url": doc.url if doc else "",
                },
            ))

        self._vs = FAISS.from_documents(docs, self.lc_embeddings)
        index_dir().mkdir(parents=True, exist_ok=True)
        self._vs.save_local(str(index_dir()), index_name=INDEX_NAME)

        # 回填 faiss_row：Document 的顺序与加入索引的顺序一致
        self.db.clear_faiss_rows()
        self.db.set_faiss_rows([(i, d.metadata["chunk_id"]) for i, d in enumerate(docs)])
        self.db.save_index_meta(
            embedding_model=self.embedder.name, dim=self.embedder.dim,
            total_vectors=len(docs), index_path=str(self.index_path()),
            corpus_hash=corpus_hash, metric="ip",
        )
        return BuildReport("built", len(docs), self.embedder.dim, self.embedder.name,
                           str(self.index_path()), corpus_hash,
                           int((time.perf_counter() - t0) * 1000), None)

    # ------------------------------------------------------------------ 检索
    def search(self, query: str, top_k: int = 5) -> list[tuple[Any, float]]:
        if not self.ensure():
            return []
        return self._vs.similarity_search_with_score(query, k=top_k)
    @property
    def total(self) -> int:
        return int(self._vs.index.ntotal) if self._vs is not None else 0


@dataclass
class BuildReport:
    status: str            # built | skipped | empty
    chunks: int
    dim: int
    model: str
    index_path: str
    corpus_hash: str
    elapsed_ms: int
    note: str | None

    def as_dict(self) -> dict:
        return {
            "status": self.status, "chunks": self.chunks, "dim": self.dim,
            "model": self.model, "index_path": self.index_path,
            "corpus_hash": self.corpus_hash, "elapsed_ms": self.elapsed_ms,
            "note": self.note,
        }


class Retriever:
    """查询入口：向量召回 + 字面加权，返回带完整溯源字段的结果。"""

    def __init__(self, db: TourismDB, store: VectorStore | None = None):
        self.db = db
        self.store = store or VectorStore(db)

    def search(self, query: str, top_k: int = 5, *,
               keyword_boost: float = 0.25) -> list[dict]:
        """`keyword_boost` 是字面兜底。

        纯向量检索在专有名词（景区全名、政策文号）上偶发漏召回，而用户问
        「福州高峰谷景区门票多少」时，字面命中该名的块必须优先。加权幅度小，
        不改变排序主体，只把同分候选里的字面命中顶上来。
        """
        hits = self.store.search(query, top_k=max(top_k * 4, 12))
        terms = [t for t in _query_terms(query) if len(t) >= 2]

        results: list[dict] = []
        for doc, score in hits:
            m = doc.metadata or {}
            kw = 0.0
            if terms:
                low = (doc.page_content or "").lower()
                kw = sum(1 for t in terms if t.lower() in low) / len(terms)
            # LangChain 的 FAISS 用 L2 距离（越小越近）；转成可比的相关度
            vec = 1.0 / (1.0 + float(score))
            results.append({
                "chunk_id": m.get("chunk_id", ""),
                "doc_id": m.get("doc_id", ""),
                "text": doc.page_content,
                "heading_path": m.get("heading_path") or None,
                "page_no": m.get("page_no") or None,
                "score": round(vec + keyword_boost * kw, 4),
                "vec_score": round(vec, 4),
                "kw_score": round(kw, 4),
                "distance": round(float(score), 4),
                "doc_title": m.get("doc_title", ""),
                "issuer": m.get("issuer") or None,
                "doc_no": m.get("doc_no") or None,
                "publish_date": m.get("publish_date") or None,
                "effective_from": m.get("effective_from") or None,
                "effective_to": m.get("effective_to") or None,
                "url": m.get("url", ""),
            })

        results.sort(key=lambda r: r["score"], reverse=True)
        return results[:top_k]


def _query_terms(query: str) -> list[str]:
    """从问句里挑可能指代实体的词：去疑问词与虚词。"""
    import re
    q = re.sub(r"[？?。，,！!、；;：:\s]+", " ", query or "")
    stop = {"多少", "什么", "怎么", "如何", "哪些", "哪个", "是否", "可以", "请问",
            "告诉", "我", "的", "了", "吗", "呢", "呀", "有", "是", "在", "和", "与",
            "现在", "最近", "今天", "目前", "情况", "介绍", "一下", "请问一下"}
    return [w for w in q.split() if w not in stop and len(w) >= 2]


def _json_default(o: Any):
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    raise TypeError(type(o))


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=_json_default)
