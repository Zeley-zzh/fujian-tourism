"""数据库访问层：建库、写入、查询、日志。

所有写入都是幂等的（INSERT OR REPLACE / 先查后插），
所以在同一天重复跑采集不会产生重复数据，也不会毁掉已有版本历史。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from ..utils import content_hash, now_iso, url_hash

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class Database:
    def __init__(self, db_path: Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")

    # ---------------- 生命周期 ----------------
    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self._migrate()
        self.conn.commit()

    # 后来加列时补的迁移。CREATE TABLE IF NOT EXISTS 不会给已有表加列，
    # 老库直接跑新代码会报 no such column，这里补一句就好。
    _ADDED_COLUMNS = {
        "scenic_spots": {"category": "TEXT"},
    }

    def _migrate(self) -> None:
        for table, cols in self._ADDED_COLUMNS.items():
            have = {r["name"] for r in
                    self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for col, ddl in cols.items():
                if col not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")

    def close(self) -> None:
        try:
            self.conn.commit()
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---------------- 任务与日志 ----------------
    def start_job(self, job_name: str, mode: str, site_key: str) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO crawl_jobs(job_name, mode, site_key, started_at, status) "
                "VALUES (?,?,?,?,'running')",
                (job_name, mode, site_key, now_iso()),
            )
            return int(cur.lastrowid)

    def finish_job(self, job_id: int, *, status: str = "done", pages: int = 0,
                   new: int = 0, changed: int = 0, failed: int = 0, note: str = "") -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE crawl_jobs SET finished_at=?, status=?, pages_fetched=?, "
                "docs_new=?, docs_changed=?, docs_failed=?, note=? WHERE job_id=?",
                (now_iso(), status, pages, new, changed, failed, note, job_id),
            )

    def log(self, job_id: int | None, level: str, event: str, *,
            url: str | None = None, detail: str = "", elapsed_ms: int | None = None) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO crawl_logs(job_id, ts, level, url, event, detail, elapsed_ms) "
                "VALUES (?,?,?,?,?,?,?)",
                (job_id, now_iso(), level, url, event, detail[:2000], elapsed_ms),
            )

    # ---------------- 文档 ----------------
    def next_doc_id(self) -> str:
        row = self.conn.execute(
            "SELECT doc_id FROM documents ORDER BY doc_id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return "d0001"
        try:
            n = int(row["doc_id"][1:]) + 1
        except ValueError:
            n = self.conn.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"] + 1
        return f"d{n:04d}"

    def find_doc_by_url(self, url: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM documents WHERE url_hash = ?", (url_hash(url),)
        ).fetchone()

    def upsert_document(self, rec, *, raw_path: str | None = None) -> tuple[str, str]:
        """写入或更新一条文档。返回 (doc_id, action)，action ∈ new|unchanged|updated。

        版本化策略：正文 hash 变化 → 新增 doc_versions 行 + 更新 documents 当前值，
        历史版本永不删除，页面据此提供「变更对比」。
        """
        url = rec.url
        c_hash = content_hash(rec.content_text)
        existing = self.find_doc_by_url(url)
        ts = now_iso()

        if existing is None:
            doc_id = self.next_doc_id()
            with self.tx() as c:
                c.execute(
                    """INSERT INTO documents(doc_id,url,url_hash,doc_type,title,issuer,doc_no,
                       publish_date,effective_from,effective_to,content_html,content_text,
                       content_hash,raw_path,fetch_status,first_seen_at,last_fetched_at,current_version)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                    (doc_id, url, url_hash(url), rec.doc_type, rec.title, rec.issuer, rec.doc_no,
                     rec.publish_date, rec.effective_from, rec.effective_to,
                     rec.content_html, rec.content_text, c_hash, raw_path, "ok", ts, ts),
                )
                c.execute(
                    """INSERT INTO doc_versions(doc_id,version_no,content_hash,content_text,
                       content_html,title,publish_date,fetched_at,diff_summary)
                       VALUES (?,1,?,?,?,?,?,?,?)""",
                    (doc_id, c_hash, rec.content_text, rec.content_html, rec.title,
                     rec.publish_date, ts, "首次采集"),
                )
            return doc_id, "new"

        doc_id = existing["doc_id"]
        if existing["content_hash"] == c_hash:
            with self.tx() as c:
                c.execute("UPDATE documents SET last_fetched_at=?, fetch_status='ok' WHERE doc_id=?",
                          (ts, doc_id))
            return doc_id, "unchanged"

        # 内容变更 → 新版本
        new_ver = int(existing["current_version"]) + 1
        diff = self._diff_summary(existing["content_text"], rec.content_text)
        with self.tx() as c:
            c.execute(
                """UPDATE documents SET title=?, issuer=?, doc_no=?, publish_date=?,
                   effective_from=?, effective_to=?, content_html=?, content_text=?,
                   content_hash=?, raw_path=COALESCE(?, raw_path), fetch_status='ok',
                   last_fetched_at=?, current_version=? WHERE doc_id=?""",
                (rec.title, rec.issuer, rec.doc_no, rec.publish_date, rec.effective_from,
                 rec.effective_to, rec.content_html, rec.content_text, c_hash, raw_path,
                 ts, new_ver, doc_id),
            )
            c.execute(
                """INSERT INTO doc_versions(doc_id,version_no,content_hash,content_text,
                   content_html,title,publish_date,fetched_at,diff_summary)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (doc_id, new_ver, c_hash, rec.content_text, rec.content_html, rec.title,
                 rec.publish_date, ts, diff),
            )
        return doc_id, "updated"

    @staticmethod
    def _diff_summary(old: str, new: str, limit: int = 300) -> str:
        """粗粒度差异摘要：按句切分，找新增/消失的句子。足够人工判断「改了什么」。"""
        import re
        split = lambda t: [s.strip() for s in re.split(r"[。\n]", t or "") if s.strip()]
        old_s, new_s = set(split(old)), set(split(new))
        added = [s for s in new_s - old_s][:3]
        removed = [s for s in old_s - new_s][:3]
        parts = []
        if added:
            parts.append("新增: " + " / ".join(added))
        if removed:
            parts.append("删除: " + " / ".join(removed))
        return ("；".join(parts) or "正文排版变化，语义无明显差异")[:limit]

    # ---------------- 景区 ----------------
    def upsert_spot(self, *, name: str, **fields) -> str:
        row = self.conn.execute("SELECT spot_id FROM scenic_spots WHERE name = ?", (name,)).fetchone()
        ts = now_iso()
        if row:
            spot_id = row["spot_id"]
            sets = ", ".join(f"{k}=?" for k in fields) if fields else ""
            with self.tx() as c:
                if sets:
                    c.execute(f"UPDATE scenic_spots SET {sets}, updated_at=? WHERE spot_id=?",
                              (*fields.values(), ts, spot_id))
                else:
                    c.execute("UPDATE scenic_spots SET updated_at=? WHERE spot_id=?", (ts, spot_id))
            return spot_id

        n = self.conn.execute("SELECT COUNT(*) c FROM scenic_spots").fetchone()["c"] + 1
        spot_id = f"s{n:04d}"
        cols = ["spot_id", "name", "created_at", "updated_at", *fields.keys()]
        vals = [spot_id, name, ts, ts, *fields.values()]
        with self.tx() as c:
            c.execute(f"INSERT INTO scenic_spots({','.join(cols)}) VALUES ({','.join('?'*len(cols))})",
                      vals)
        return spot_id

    # ---------------- 字段溯源 ----------------
    def add_provenance(self, *, spot_id: str, field_name: str, field_value: str,
                       source_doc_id: str, source_url: str, doc_title: str,
                       issuer: str | None, publish_date: str | None,
                       effective_from: str | None, effective_to: str | None,
                       quote: str | None, status: str,
                       conflict_group: str | None = None) -> int:
        """写入一条字段证据。同一 (spot, field, value) 不重复插；值不同则旧的降级。"""
        dup = self.conn.execute(
            """SELECT prov_id FROM field_provenance
               WHERE spot_id=? AND field_name=? AND field_value=? AND source_doc_id=?""",
            (spot_id, field_name, field_value, source_doc_id),
        ).fetchone()
        if dup:
            return int(dup["prov_id"])

        same_field = self.conn.execute(
            "SELECT prov_id, field_value FROM field_provenance WHERE spot_id=? AND field_name=? AND is_current=1",
            (spot_id, field_name),
        ).fetchall()

        with self.tx() as c:
            cur = c.execute(
                """INSERT INTO field_provenance(spot_id,field_name,field_value,source_doc_id,
                   source_url,doc_title,issuer,publish_date,effective_from,effective_to,quote,
                   fetched_at,status,is_current,conflict_group)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)""",
                (spot_id, field_name, field_value, source_doc_id, source_url, doc_title,
                 issuer, publish_date, effective_from, effective_to, quote, now_iso(),
                 status, conflict_group),
            )
            prov_id = int(cur.lastrowid)

            # 值不同 → 不静默丢弃，改成「冲突」并保留双方
            diff_vals = {r["field_value"] for r in same_field if r["field_value"] != field_value}
            if diff_vals:
                group = f"{spot_id}:{field_name}:c{prov_id}"
                c.execute(
                    "UPDATE field_provenance SET conflict_group=?, status='冲突' WHERE prov_id=?",
                    (group, prov_id),
                )
                c.execute(
                    f"UPDATE field_provenance SET conflict_group=?, status='冲突' "
                    f"WHERE spot_id=? AND field_name=? AND field_value IN ({','.join('?'*len(diff_vals))})",
                    (group, spot_id, field_name, *diff_vals),
                )
                c.execute(
                    "INSERT INTO review_queue(prov_id, doc_id, reason, severity, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (prov_id, source_doc_id, "conflict", "high", now_iso()),
                )
        return prov_id

    # ---------------- 关联 / 切片 / 索引元数据 ----------------
    def link_spot_policy(self, spot_id: str, doc_id: str, relation: str,
                         confidence: float, evidence: str) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT OR IGNORE INTO spot_policy_link(spot_id,doc_id,relation,confidence,
                   evidence,created_at) VALUES (?,?,?,?,?,?)""",
                (spot_id, doc_id, relation, confidence, evidence[:500], now_iso()),
            )

    def replace_chunks(self, doc_id: str, chunks: list[dict]) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
            for ch in chunks:
                c.execute(
                    """INSERT INTO chunks(chunk_id,doc_id,chunk_index,text,char_len,heading_path,
                       page_no,faiss_row,token_estimate,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (ch["chunk_id"], doc_id, ch["chunk_index"], ch["text"], len(ch["text"]),
                     ch.get("heading_path"), ch.get("page_no"), ch.get("faiss_row"),
                     ch.get("token_estimate"), now_iso()),
                )

    def save_index_meta(self, **kw) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT OR REPLACE INTO faiss_index_meta(index_name,embedding_model,dim,metric,
                   total_vectors,nlist,index_path,corpus_hash,built_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (kw["index_name"], kw["embedding_model"], kw["dim"], kw.get("metric", "IP"),
                 kw["total_vectors"], kw.get("nlist"), kw["index_path"],
                 kw["corpus_hash"], now_iso()),
            )

    # ---------------- 查询 ----------------
    def documents_for_export(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM documents WHERE fetch_status='ok' ORDER BY publish_date DESC"
        ).fetchall()

    def stats(self) -> dict:
        def one(sql: str, *a) -> Any:
            r = self.conn.execute(sql, a).fetchone()
            return r[0] if r else 0
        return {
            "documents": one("SELECT COUNT(*) FROM documents"),
            "doc_versions": one("SELECT COUNT(*) FROM doc_versions"),
            "spots": one("SELECT COUNT(*) FROM scenic_spots"),
            "provenance": one("SELECT COUNT(*) FROM field_provenance"),
            "verified": one("SELECT COUNT(*) FROM field_provenance WHERE status='已核验'"),
            "pending": one("SELECT COUNT(*) FROM field_provenance WHERE status='待核验'"),
            "conflicts": one("SELECT COUNT(*) FROM field_provenance WHERE status='冲突'"),
            "chunks": one("SELECT COUNT(*) FROM chunks"),
            "links": one("SELECT COUNT(*) FROM spot_policy_link"),
            "open_review": one("SELECT COUNT(*) FROM review_queue WHERE resolved_at IS NULL"),
        }

    def dump_json(self, path: Path) -> None:
        path.write_text(json.dumps(self.stats(), ensure_ascii=False, indent=2), encoding="utf-8")


def open_db(db_path: Path, *, init: bool = True) -> Database:
    db = Database(db_path)
    if init:
        db.init_schema()
    return db
