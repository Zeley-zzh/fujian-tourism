"""导出层：把库里的东西变成 02_rag 能直接吃的文件。

三份产物（与 CONTRACT.md 一致）：
- data/tourism.db          : SQLite 主库（02 直接读，字段带溯源）
- data/corpus/documents.jsonl : 每行一篇文档 + 其切片，给 RAG 建索引用
- data/reports/quality_report.json : 质量报告（三态分布、冲突清单、待核验清单、覆盖率）

额外导出 data/corpus/spots.jsonl，方便脱离 SQLite 单独检查字段证据。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings
from ..db import Database
from ..utils import get_logger, now_iso


@dataclass
class ExportReport:
    documents: int = 0
    chunks: int = 0
    spots: int = 0
    fields: int = 0
    corpus_path: str = ""
    spots_path: str = ""
    report_path: str = ""


class Exporter:
    def __init__(self, settings: Settings, db: Database):
        self.s = settings
        self.db = db
        self.log = get_logger("exporter", settings.p("log_dir"))

    def run(self) -> ExportReport:
        rep = ExportReport()
        corpus_dir = self.s.p("corpus_dir")
        corpus_dir.mkdir(parents=True, exist_ok=True)

        docs = self.db.conn.execute(
            "SELECT * FROM documents ORDER BY doc_id"
        ).fetchall()

        corpus_path = corpus_dir / "documents.jsonl"
        with corpus_path.open("w", encoding="utf-8") as f:
            for d in docs:
                chunks = self.db.conn.execute(
                    "SELECT chunk_id, chunk_index, text, heading_path, page_no, token_estimate "
                    "FROM chunks WHERE doc_id=? ORDER BY chunk_index",
                    (d["doc_id"],),
                ).fetchall()
                if not chunks:
                    continue
                rec = {
                    "doc_id": d["doc_id"],
                    "title": d["title"],
                    "doc_type": d["doc_type"],
                    "issuer": d["issuer"],
                    "doc_no": d["doc_no"],
                    "publish_date": d["publish_date"],
                    "effective_from": d["effective_from"],
                    "effective_to": d["effective_to"],
                    "url": d["url"],
                    "content_hash": d["content_hash"],
                    "current_version": d["current_version"],
                    "fetched_at": d["last_fetched_at"],
                    "chunks": [
                        {
                            "chunk_id": c["chunk_id"],
                            "index": c["chunk_index"],
                            "text": c["text"],
                            "heading_path": c["heading_path"],
                            "page_no": c["page_no"],
                            "token_estimate": c["token_estimate"],
                        }
                        for c in chunks
                    ],
                }
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                rep.documents += 1
                rep.chunks += len(chunks)

        # 景区 + 字段证据（独立一份，便于核对）
        spots_path = corpus_dir / "spots.jsonl"
        spots = self.db.conn.execute("SELECT * FROM scenic_spots ORDER BY spot_id").fetchall()
        with spots_path.open("w", encoding="utf-8") as f:
            for sp in spots:
                provs = self.db.conn.execute(
                    "SELECT * FROM field_provenance WHERE spot_id=? ORDER BY field_name, prov_id",
                    (sp["spot_id"],),
                ).fetchall()
                rec = {
                    "spot_id": sp["spot_id"],
                    "name": sp["name"],
                    "level": sp["level"],
                    "city": sp["city"],
                    "county": sp["county"],
                    "address": sp["address"],
                    "lng": sp["lng"],
                    "lat": sp["lat"],
                    "intro": sp["intro"],
                    "fields": [
                        {
                            "field_name": p["field_name"],
                            "field_value": p["field_value"],
                            "status": p["status"],
                            "source_doc_id": p["source_doc_id"],
                            "source_url": p["source_url"],
                            "doc_title": p["doc_title"],
                            "issuer": p["issuer"],
                            "publish_date": p["publish_date"],
                            "effective_from": p["effective_from"],
                            "effective_to": p["effective_to"],
                            "quote": p["quote"],
                            "fetched_at": p["fetched_at"],
                            "conflict_group": p["conflict_group"],
                        }
                        for p in provs
                    ],
                }
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                rep.spots += 1
                rep.fields += len(provs)

        # 质量报告
        report_dir = self.s.p("report_dir")
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / "quality_report.json"
        report_path.write_text(
            json.dumps(self._build_report(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

        rep.corpus_path = str(corpus_path)
        rep.spots_path = str(spots_path)
        rep.report_path = str(report_path)
        self.log.info("导出完成: 文档 %d / 切片 %d / 景区 %d / 字段 %d",
                      rep.documents, rep.chunks, rep.spots, rep.fields)
        return rep

    def _build_report(self) -> dict:
        db = self.db
        stats = db.stats()

        # 三态按字段统计
        by_field = db.conn.execute(
            """SELECT field_name, status, COUNT(*) c FROM field_provenance
               WHERE is_current=1 GROUP BY field_name, status ORDER BY field_name"""
        ).fetchall()
        matrix: dict[str, dict[str, int]] = {}
        for r in by_field:
            matrix.setdefault(r["field_name"], {})[r["status"]] = r["c"]

        # 冲突清单（同组多值）
        conflicts = db.conn.execute(
            """SELECT spot_id, field_name, conflict_group, group_concat(field_value, ' | ') vals,
                      group_concat(source_url, ' | ') urls, COUNT(*) n
               FROM field_provenance WHERE conflict_group IS NOT NULL
               GROUP BY conflict_group HAVING n > 1 ORDER BY spot_id"""
        ).fetchall()

        pending = db.conn.execute(
            """SELECT prov_id, spot_id, field_name, field_value, source_doc_id, status
               FROM field_provenance WHERE status='待核验' AND is_current=1
               ORDER BY spot_id, field_name LIMIT 200"""
        ).fetchall()

        review = db.conn.execute(
            """SELECT reason, severity, COUNT(*) c FROM review_queue
               WHERE resolved_at IS NULL GROUP BY reason, severity"""
        ).fetchall()

        jobs = db.conn.execute(
            "SELECT job_name, mode, site_key, status, pages_fetched, docs_new, docs_changed, "
            "docs_failed, started_at, finished_at FROM crawl_jobs ORDER BY job_id DESC LIMIT 20"
        ).fetchall()

        # 覆盖率：有字段证据的景区 / 全部景区
        total_spots = stats["spots"] or 1
        covered = db.conn.execute(
            "SELECT COUNT(DISTINCT spot_id) c FROM field_provenance WHERE is_current=1"
        ).fetchone()["c"]

        # 字段完整度：每个景区的 6 个易变字段是否都有值
        field_names = list((self.s.scenic_fields or {}).keys())
        completeness = []
        for sp in db.conn.execute("SELECT spot_id, name FROM scenic_spots").fetchall():
            have = {r["field_name"] for r in db.conn.execute(
                "SELECT DISTINCT field_name FROM field_provenance WHERE spot_id=? AND is_current=1",
                (sp["spot_id"],)).fetchall()}
            completeness.append({
                "spot_id": sp["spot_id"],
                "name": sp["name"],
                "covered": len(have),
                "total": len(field_names),
                "missing": [f for f in field_names if f not in have],
            })
        completeness.sort(key=lambda x: x["covered"])

        return {
            "generated_at": now_iso(),
            "summary": stats,
            "status_matrix": matrix,
            "coverage": {
                "spots_with_fields": covered,
                "total_spots": stats["spots"],
                "ratio": round(covered / total_spots, 4) if stats["spots"] else 0.0,
            },
            "field_completeness": completeness[:50],
            "conflicts": [
                {
                    "spot_id": c["spot_id"],
                    "field_name": c["field_name"],
                    "conflict_group": c["conflict_group"],
                    "values": c["vals"].split(" | "),
                    "sources": c["urls"].split(" | "),
                }
                for c in conflicts
            ],
            "pending_review": [dict(p) for p in pending],
            "review_queue": [
                {"reason": r["reason"], "severity": r["severity"], "count": r["c"]} for r in review
            ],
            "recent_jobs": [dict(j) for j in jobs],
            "notes": [
                "所有 quote 均为原文摘录，未做改写；超 200 字会被裁剪但仍保留语义边界。",
                "「冲突」表示同一景区同一字段存在多来源不同取值，双方均保留，页面提供对比。",
                "「待核验」包含：缺发布日期 / 缺原文摘录 / 超出有效期 / 语义校验不通过。",
            ],
        }
