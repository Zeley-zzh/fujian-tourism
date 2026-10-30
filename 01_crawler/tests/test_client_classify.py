"""反爬客户端单测：拦截判定与验证码识别。

`_classify(status, text, content) -> (blocked: bool, reason: str)`，
reason ∈ ok | block | rate_limit | captcha。全部离线，不打网络。
"""

from __future__ import annotations

import pytest

from crawler.anticrawl.client import AntiCrawlClient


@pytest.fixture()
def client(settings):
    c = AntiCrawlClient(settings)
    yield c
    c.close()


def test_200_normal_page_is_ok(client):
    blocked, reason = client._classify(200, "正常正文内容" * 100, b"x" * 8192)
    assert blocked is False
    assert reason == "ok"


def test_403_is_block(client):
    blocked, reason = client._classify(403, "forbidden", b"")
    assert blocked is True
    assert reason == "block"


def test_401_is_block(client):
    blocked, reason = client._classify(401, "unauthorized", b"")
    assert blocked is True
    assert reason == "block"


def test_429_is_rate_limit(client):
    blocked, reason = client._classify(429, "too many requests", b"")
    assert blocked is True
    assert reason == "rate_limit"


def test_200_with_short_block_page_is_detected(client):
    """站点常用 200 返回拦截页——短正文里出现拦截标记必须判 block。"""
    html = "<html><body>您的访问过于频繁，请完成安全验证</body></html>"
    blocked, reason = client._classify(200, html, html.encode("utf-8"))
    assert blocked is True
    assert reason in ("block", "captcha")


def test_200_with_long_body_containing_marker_is_not_blocked(client):
    """长正文里出现「安全验证」等字样属正常内容，不能误杀。"""
    body = "本景区实行安全验证制度，游客需实名预约。" * 500
    blocked, reason = client._classify(200, body, body.encode("utf-8"))
    assert blocked is False
    assert reason == "ok"


def test_captcha_marker_detected(client):
    html = '<html><body><img src="/captcha.php?t=1"><input name="code"></body></html>'
    blocked, reason = client._classify(200, html, html.encode("utf-8"))
    assert blocked is True
    assert reason == "captcha"


def test_503_with_rate_limit_text_is_rate_limit(client):
    blocked, reason = client._classify(503, "请求过于频繁，请稍后再试", b"")
    assert blocked is True
    assert reason == "rate_limit"


def test_fetch_stats_start_at_zero(client):
    """真实采集路径下不再有离线样例分支，统计里只该有真实计数。"""
    rep = client.report()
    assert rep["fetch_stats"]["ok"] == 0
    assert rep["fetch_stats"]["blocked"] == 0
    assert set(rep["fetch_stats"]) == {"ok", "failed", "blocked", "captcha", "retried"}
