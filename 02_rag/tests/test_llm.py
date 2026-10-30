"""模型出口：唯一调用点、降级路径、状态标签、prompt 拼装、本地摘录。

两种状态都要断言清楚：没配 Key 走本地摘录（mode=fallback），配了 Key 但连不上
也必须降级并把错误暴露出来，不允许假装成功。
"""
from __future__ import annotations

import pytest

from app import settings_store
from app.llm import LLMClient, build_prompt, local_answer

CTX = [{
    "chunk_id": "chunk_d0001_0000", "doc_id": "d0001",
    "text": "甲峰景区开放时间 08:00—17:30。门票价格 65 元。预约方式为官方小程序实名预约。",
    "doc_title": "甲峰景区门票与开放时间公告", "issuer": "省文化和旅游厅",
    "publish_date": "2026-03-20", "effective_to": "2028-03-31",
}]


def test_unconfigured_is_fallback():
    c = LLMClient()
    assert c.configured is False
    assert c.mode == "fallback"
    st = c.status()
    assert st["badge"] == "未接入模型·本地摘录"
    assert "API Key" in st["hint"]


def test_fallback_badge_never_claims_live():
    r = LLMClient().complete("甲峰景区门票多少", context=CTX)
    assert r.mode == "fallback"
    assert r.badge == "未接入模型·本地摘录"
    # 回答必须自带来源声明，不能让读者误以为是模型组织过的
    assert "未经模型改写" in r.text


def test_local_answer_quotes_source_numbers():
    text = local_answer("甲峰景区门票多少", CTX)
    assert "65 元" in text or "门票价格" in text
    assert "来源 [1]" in text
    assert "以官方最新公告为准" in text
    assert "2026-03-20" in text, "必须指出资料发布日期"


def test_local_answer_without_context():
    assert "资料中未收录" in local_answer("完全无关的问题", [])


def test_local_answer_falls_back_to_first_sentence():
    ctx = [{"text": "本段完全不包含问题里的任何词。", "doc_title": "X"}]
    assert "来源 [1]" in local_answer("zzz", ctx)


def test_configured_status_has_no_badge(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-abcdefgh12345678")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:1")
    c = LLMClient()
    st = c.status()
    assert st["configured"] is True
    assert st["badge"] is None
    assert st["mode"] == "live"
    # 掩码只露头尾，中间打码；完整 Key 绝不出现在状态里
    assert "abcdefgh12345678" not in st["api_key_mask"]
    assert st["api_key_mask"].startswith("sk-a")
    assert st["api_key_mask"].endswith("5678")


def test_live_failure_degrades_and_reports_error(monkeypatch):
    """配了 Key 但连不上 → 仍返回可交付答案，但 mode 回落且错误必须暴露。"""
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:1")
    r = LLMClient().complete("甲峰景区门票多少", context=CTX)
    assert r.mode == "fallback"
    assert r.error
    assert r.badge == "未接入模型·本地摘录"
    assert "资料中未收录" not in r.text


def test_build_prompt_contains_numbered_context():
    p = build_prompt("门票多少", CTX)
    assert "[1]" in p
    assert "甲峰景区门票与开放时间公告" in p
    assert "省文化和旅游厅" in p
    assert "2026-03-20" in p
    assert "有效期至：2028-03-31" in p
    assert p.index("[1]") < p.index("问题：")


def test_build_prompt_with_no_context():
    assert "（无检索结果）" in build_prompt("问题", [])


def test_system_prompt_forbids_outside_knowledge():
    from app.llm import SYSTEM_PROMPT
    assert "只根据提供的资料片段作答" in SYSTEM_PROMPT
    assert "资料中未收录" in SYSTEM_PROMPT


def test_saved_key_takes_effect_after_reload():
    """配置页保存后无需重启：reload 就能读到新 Key。"""
    settings_store.save({"api_key": "sk-abcdefgh1234"})
    c = LLMClient()
    assert c.configured is True
    settings_store.clear_key()
    c.reload()
    assert c.configured is False
