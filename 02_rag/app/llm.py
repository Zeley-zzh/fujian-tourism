"""模型调用：全项目唯一的出站口，LangChain `ChatOpenAI` 驱动。

一条收口原则
------------
任何要问模型的地方都必须走 `LLMClient.complete()`。页面、API、脚本都不能自己
拼 HTTP 请求 —— 否则换模型、加超时、改重试要满项目找散落的调用点。

两种运行状态
------------
- `live`      后台配了 Key 且调用成功，回答来自模型。
- `fallback`  没配 Key，或配了但调用失败。此时用 `local_answer()` 从检索到的
              原文里摘句拼装，答案仍可交付，但必须在 UI 上明确标注来源方式，
              不能让访客误以为是模型生成。

配置从 `settings_store` 读，后台配置页保存后调用 `reload()` 立即生效，不必重启。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from . import settings_store

SYSTEM_PROMPT = """你是福建省文化和旅游厅公开文件的知识库问答助手。

规则：
1. 只根据提供的资料片段作答，不得引入片段之外的知识。
2. 每句结论后用 [n] 标注它来自第几个片段，n 必须是片段编号。
3. 资料中没有的内容，直接回答「资料中未收录」，不要推测。
4. 票价、开放时间、政策有效期属于易变信息，回答时必须说明对应的资料发布日期，
   并提示以官方最新公告为准。"""

FALLBACK_NOTE = "本条回答由本地检索结果直接摘录拼装，未经模型改写。"
NO_RESULT_TEXT = "资料中未收录与该问题相关的内容。"


@dataclass
class LLMResponse:
    text: str
    mode: str                       # live | fallback
    model: str | None = None
    usage: dict = field(default_factory=dict)
    error: str | None = None
    latency_ms: int = 0

    @property
    def badge(self) -> str | None:
        """给 UI 的角标。live 时无角标；fallback 必须让访客看清当前状态。"""
        if self.mode == "live":
            return None
        return "未接入模型·本地摘录"


class LLMClient:
    """`reload()` 会重新读配置；`complete()` 是唯一调用点。"""

    def __init__(self) -> None:
        self.reload()

    # ------------------------------------------------------------------ 配置
    def reload(self) -> None:
        cfg = settings_store.load()
        self.base_url = cfg["base_url"]
        self.model = cfg["model"]
        self.temperature = float(cfg["temperature"])
        self.max_tokens = int(cfg["max_tokens"])
        self._api_key = cfg["api_key"]
        self._llm = None

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    @property
    def mode(self) -> str:
        return "live" if self.configured else "fallback"

    def status(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "mode": self.mode,
            "model": self.model,
            "base_url": self.base_url,
            "api_key_mask": settings_store.mask_key(self._api_key),
            "badge": None if self.configured else "未接入模型·本地摘录",
            "hint": None if self.configured else "在「模型配置」页填入 API Key 后即可用模型作答",
        }

    # ------------------------------------------------------------------ 调用
    def _client(self):
        """惰性构造 `ChatOpenAI`，供 OpenAI 兼容端点使用（DeepSeek、通义、Kimi 都兼容）。"""
        if self._llm is None:
            from langchain_openai import ChatOpenAI
            self._llm = ChatOpenAI(
                model=self.model,
                api_key=self._api_key,
                base_url=self.base_url,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                timeout=60,
                max_retries=2,
            )
        return self._llm

    def complete(self, prompt: str, *, system: str | None = None,
                 context: Sequence[dict] | None = None) -> LLMResponse:
        """调用模型；任何异常都降级为本地摘录，并把原因带回给 UI。"""
        t0 = time.perf_counter()

        if not self.configured:
            return LLMResponse(
                text=local_answer(prompt, context or []),
                mode="fallback",
                latency_ms=int((time.perf_counter() - t0) * 1000),
            )

        try:
            from langchain_core.messages import HumanMessage, SystemMessage
            msgs = [SystemMessage(content=system or SYSTEM_PROMPT),
                    HumanMessage(content=prompt)]
            resp = self._client().invoke(msgs)
            text = (resp.content or "").strip()
            if not text:
                raise RuntimeError("模型返回空内容")
            usage = dict(getattr(resp, "usage_metadata", None) or {})
            return LLMResponse(
                text=text, mode="live", model=self.model, usage=usage,
                latency_ms=int((time.perf_counter() - t0) * 1000),
            )
        except Exception as e:
            return LLMResponse(
                text=local_answer(prompt, context or []),
                mode="fallback",
                model=self.model,
                error=f"{type(e).__name__}: {e}",
                latency_ms=int((time.perf_counter() - t0) * 1000),
            )


# ====================================================================== 提示词
def build_prompt(question: str, contexts: Sequence[dict]) -> str:
    """把检索结果拼成带编号的资料块。编号是引用回溯源头的唯一锚。"""
    if not contexts:
        return f"资料片段：\n（无检索结果）\n\n问题：{question}"
    parts = []
    for i, c in enumerate(contexts, 1):
        head = f"[{i}] 《{c.get('doc_title', '')}》"
        meta = []
        if c.get("issuer"):
            meta.append(f"发文机关：{c['issuer']}")
        if c.get("doc_no"):
            meta.append(f"文号：{c['doc_no']}")
        if c.get("publish_date"):
            meta.append(f"发布日期：{c['publish_date']}")
        if c.get("effective_to"):
            meta.append(f"有效期至：{c['effective_to']}")
        body = (c.get("text") or "").strip()
        if len(body) > 700:
            body = body[:700] + "…"
        parts.append(head + ("｜" + "｜".join(meta) if meta else "") + "\n" + body)
    return ("以下是从福建省文化和旅游厅公开文件中检索到的片段：\n\n"
            + "\n\n".join(parts)
            + f"\n\n问题：{question}\n请按资料作答，逐句标注引用编号。")


# ====================================================================== 本地摘录
_STOP = {"多少", "什么", "怎么", "如何", "哪些", "哪个", "是否", "可以", "请问",
         "告诉", "我", "的", "了", "吗", "呢", "呀", "有", "是", "在", "和", "与",
         "现在", "最近", "今天", "目前", "情况", "介绍", "一下", "请问一下"}


def _keywords(question: str) -> list[str]:
    q = re.sub(r"[？?。，,！!、；;：:\s]+", " ", question or "")
    out = []
    for w in q.split():
        if w in _STOP or len(w) < 2:
            continue
        out.append(w)
        # 长词再拆 2-gram，避免「高峰谷景区门票」整体匹配不上原文的分句
        if len(w) > 3:
            out.extend(w[i:i + 2] for i in range(len(w) - 1))
    return list(dict.fromkeys(out))


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[。！？；\n])", text or "") if s.strip()]


def local_answer(question: str, contexts: Sequence[dict]) -> str:
    """从检索片段里挑最相关的句子拼成回答。

    打分只认两件看得见的事实：命中问题关键词的数量，以及句子是否含数字
    （票价、时间、有效期都带数字）。不做任何改写，因此不需要标注「可能不准」，
    因为它本来就是原文。
    """
    if not contexts:
        return NO_RESULT_TEXT

    kws = _keywords(question)
    scored: list[tuple[float, int, str]] = []
    seen: set[str] = set()
    for idx, ctx in enumerate(contexts, 1):
        for sent in _sentences(ctx.get("text") or ""):
            if sent in seen or len(sent) < 8:
                continue
            seen.add(sent)
            hits = sum(1 for k in kws if k in sent)
            score = float(hits) + (0.3 if re.search(r"\d", sent) else 0.0)
            if score > 0:
                scored.append((score, idx, sent))

    if not scored:
        first = _sentences(contexts[0].get("text") or "")
        if not first:
            return NO_RESULT_TEXT
        scored = [(0.0, 1, first[0])]

    scored.sort(key=lambda x: (-x[0], x[1]))
    picked = scored[:5]
    lines = [f"{sent}（来源 [{idx}]）" for _, idx, sent in picked]

    latest = max((c.get("publish_date") or "" for c in contexts), default="")
    tail = ["以上内容摘自知识库原文，票价、开放时间与政策有效期以官方最新公告为准。"]
    if latest:
        tail.append(f"（资料发布日期最晚为 {latest}）")
    tail.append(f"（{FALLBACK_NOTE}）")
    return "\n".join(lines) + "\n\n" + "".join(tail)


_CLIENT: LLMClient | None = None


def get_llm(refresh: bool = False) -> LLMClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = LLMClient()
    elif refresh:
        _CLIENT.reload()
    return _CLIENT
