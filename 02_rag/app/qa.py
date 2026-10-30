"""问答编排：检索 → 拼 prompt → 调 LLMClient → 归一化引用。

分层原因：`retriever` 只管"找到什么"，`llm` 只管"怎么说"，
中间这一层负责"证据怎么交给模型"和"模型说的话怎么挂回证据"。
页面拿到的是带 `citations` 的结构化结果，前端不用解析自然语言里的 [1][2]。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

from .llm import LLMClient, LLMResponse, build_prompt
from .store import Retriever


@dataclass
class Citation:
    index: int
    chunk_id: str
    doc_id: str
    doc_title: str
    doc_no: str | None
    issuer: str | None
    publish_date: str | None
    effective_from: str | None
    effective_to: str | None
    url: str
    quote: str
    score: float
    heading_path: str | None = None
    page_no: int | None = None


@dataclass
class QAResult:
    question: str
    answer: str
    citations: list[Citation]
    mode: str
    badge: str | None
    model: str | None = None
    error: str | None = None
    latency_ms: int = 0
    used_chunks: int = 0
    contexts: list[dict] = field(default_factory=list)

    @property
    def used_model(self) -> bool:
        return self.mode == "live"


class QAService:
    def __init__(self, db, retriever: Retriever | None = None,
                 llm: LLMClient | None = None, top_k: int = 5):
        self.db = db
        self.retriever = retriever or Retriever(db)
        self.llm = llm or LLMClient()
        self.top_k = top_k

    # ------------------------------------------------------------------ 主流程
    def ask(self, question: str, *, top_k: int | None = None) -> QAResult:
        question = (question or "").strip()
        if not question:
            return QAResult(question="", answer="请输入问题。", citations=[],
                            mode=self.llm.mode, badge=self.llm.status()["badge"])

        k = top_k or self.top_k
        contexts = self.retriever.search(question, top_k=k)
        prompt = build_prompt(question, contexts)
        resp: LLMResponse = self.llm.complete(prompt, context=contexts)

        citations = self._build_citations(contexts)
        # 把回答里出现的 [n] 映射回真实来源；模型编的编号（超出范围）要剔掉
        answer = self._sanitize_refs(resp.text, len(citations))

        return QAResult(
            question=question,
            answer=answer,
            citations=citations,
            mode=resp.mode,
            badge=resp.badge,
            model=resp.model,
            error=resp.error,
            latency_ms=resp.latency_ms,
            used_chunks=len(contexts),
            contexts=contexts,
        )

    # ------------------------------------------------------------------ 引用
    @staticmethod
    def _build_citations(contexts: Sequence[dict]) -> list[Citation]:
        out: list[Citation] = []
        for i, c in enumerate(contexts, 1):
            out.append(Citation(
                index=i,
                chunk_id=c["chunk_id"],
                doc_id=c["doc_id"],
                doc_title=c.get("doc_title") or "",
                doc_no=c.get("doc_no"),
                issuer=c.get("issuer"),
                publish_date=c.get("publish_date"),
                effective_from=c.get("effective_from"),
                effective_to=c.get("effective_to"),
                url=c.get("url") or "",
                quote=(c.get("text") or "")[:200],
                score=c.get("score") or 0.0,
                heading_path=c.get("heading_path"),
                page_no=c.get("page_no"),
            ))
        return out

    @staticmethod
    def _sanitize_refs(text: str, valid: int) -> str:
        """移除越界引用编号，保留合法编号。防止模型引用不存在的资料。"""
        if not text:
            return text

        def repl(m: re.Match) -> str:
            try:
                n = int(m.group(1))
            except (TypeError, ValueError):
                return ""
            return m.group(0) if 1 <= n <= valid else ""

        cleaned = re.sub(r"\[(\d{1,2})\]", repl, text)
        # 清掉因剔除产生的空括号与多余空格
        cleaned = re.sub(r"[（(]\s*[）)]", "", cleaned)
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
        return cleaned.strip()


# ====================================================================== 景区问答辅助
FIELD_LABELS = {
    # 名录类公告只产出 category 与 level：原文只写了「这家是 A 级景区还是
    # 省级度假区、几 A」，没写票价开放时间。硬凑出来的其它字段等于编造，
    # 所以这两个标签排在最前，其余字段按单篇公告有没有真的写出来再展示。
    "category": "类别",
    "level": "质量等级",
    "opening_hours": "开放时间",
    "ticket_price": "门票价格",
    "policy_validity": "政策有效期",
    "address": "地址",
    "booking_method": "预约方式",
    "phone": "咨询电话",
}

STATUS_CLASS = {"已核验": "ok", "待核验": "warn", "冲突": "conflict"}


def group_fields(items: Sequence[dict]) -> list[dict]:
    """把同一景区的证据按字段聚合，供页面渲染（含冲突并排）。

    输出结构：
        [{"field": "ticket_price", "label": "门票价格",
          "status": "冲突", "current": 值, "values": [{值, 证据…}, ...]}]
    """
    by_field: dict[str, list[dict]] = {}
    for it in items:
        by_field.setdefault(it["field_name"], []).append(it)

    out = []
    for field, entries in by_field.items():
        # 已核验优先作为 current；否则取最新抓取的
        verified = [e for e in entries if e["status"] == "已核验"]
        pool = verified or entries
        pool_sorted = sorted(pool, key=lambda e: (e.get("fetched_at") or ""), reverse=True)
        values = []
        seen: set[str] = set()
        for e in entries:
            v = e["field_value"]
            if v in seen:
                continue
            seen.add(v)
            values.append({
                "value": v,
                "status": e["status"],
                "status_class": STATUS_CLASS.get(e["status"], "warn"),
                "source_doc_id": e["source_doc_id"],
                "source_url": e["source_url"],
                "doc_title": e["doc_title"],
                "issuer": e.get("issuer"),
                "publish_date": e.get("publish_date"),
                "effective_from": e.get("effective_from"),
                "effective_to": e.get("effective_to"),
                "quote": e.get("quote"),
                "fetched_at": e.get("fetched_at"),
                "conflict_group": e.get("conflict_group"),
            })
        statuses = {e["status"] for e in entries}
        status = "冲突" if "冲突" in statuses else (
            "已核验" if statuses == {"已核验"} else "待核验")
        out.append({
            "field": field,
            "label": FIELD_LABELS.get(field, field),
            "status": status,
            "status_class": STATUS_CLASS.get(status, "warn"),
            "current": pool_sorted[0]["field_value"] if pool_sorted else values[0]["value"],
            "values": values,
            "is_conflict": len(values) > 1,
        })
    order = list(FIELD_LABELS.keys())
    out.sort(key=lambda x: order.index(x["field"]) if x["field"] in order else 99)
    return out
