"""FastAPI 应用：全部服务端渲染，不引前端框架。

路由一览
--------
访客侧
    GET  /                     首页：库概况、景区列表、最近入库文档
    GET  /spots                景区名录，可按城市与字段状态筛选
    GET  /spot/{spot_id}       景区详情：三态字段、冲突双值对比、溯源展开、关联政策
    GET  /doc/{doc_id}         文档详情：原文、版本历史、切片、作为证据支撑的字段
    GET  /ask                  问答页（表单）
    POST /ask                  问答（服务端渲染结果）
后台侧
    GET  /admin                模型配置页
    POST /admin                保存配置，立即生效
    POST /admin/test           用当前配置试打一次模型
    POST /admin/reindex        重建向量索引
接口
    GET  /api/ask              JSON 问答
    GET  /api/status           模型、索引、库内统计
    GET  /healthz              健康检查

数据全部来自 SQLite，页面没有硬编码内容，也没有只会返回空白的路由。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import settings_store
from .ingest import ensure_index, index_status
from .llm import get_llm
from .qa import FIELD_LABELS, QAService, group_fields
from .store import TourismDB, index_dir, resolve_db_path

APP_ROOT = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(APP_ROOT / "templates"))
STATIC = APP_ROOT / "static"

app = FastAPI(title="福建文旅政策与景区知识库", docs_url="/api/docs")
if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

# ------------------------------------------------------------------ 全局单例
_db: TourismDB | None = None
_qa: QAService | None = None
_index_note: str = ""
_started_at = time.time()


def get_db() -> TourismDB:
    global _db
    if _db is None:
        _db = TourismDB(resolve_db_path())
    return _db


def get_qa() -> QAService:
    global _qa
    if _qa is None:
        _qa = QAService(get_db(), llm=get_llm())
    return _qa


_STATUS_CN = {"built": "已重建", "skipped": "复用现有", "empty": "语料为空"}


def bootstrap() -> dict:
    """启动时确保索引可用；失败不阻断服务，把原因挂到 _index_note 给页面显示。"""
    global _index_note
    try:
        rep = ensure_index(get_db())
        label = _STATUS_CN.get(rep.status, rep.status)
        _index_note = f"索引{label}" + (f" · {rep.note}" if rep.note else "")
        return {"ok": True, "index": rep.as_dict()}
    except Exception as e:
        _index_note = f"索引不可用：{type(e).__name__}: {e}"
        return {"ok": False, "error": _index_note}


@app.on_event("startup")
def _startup() -> None:
    bootstrap()


@app.on_event("shutdown")
def _shutdown() -> None:
    if _db is not None:
        _db.close()


# ------------------------------------------------------------------ 模板上下文
def base_ctx(request: Request, **extra: Any) -> dict:
    llm = get_llm()
    try:
        stats = get_db().stats()
    except Exception:
        stats = {}
    st = index_status(get_db()) if stats else None
    ctx = {
        "request": request,
        "llm": llm.status(),
        "stats": stats,
        "index_note": _index_note,
        "index": st.as_dict() if st else None,
    }
    ctx.update(extra)
    return ctx


def _decorate(spots: list[dict], db: TourismDB) -> list[dict]:
    """给景区行补上三态与字段数，列表页和首页共用。"""
    for s in spots:
        fs = db.fields(s["spot_id"])
        statuses = {f["status"] for f in fs}
        s["status"] = ("冲突" if "冲突" in statuses
                       else ("已核验" if statuses == {"已核验"} else "待核验"))
        s["field_count"] = len({f["field_name"] for f in fs})
        s["conflict_count"] = sum(1 for f in fs if f["status"] == "冲突")
        # 名录来的景区没有 A 级「等级」，但一定有「类别」（A级旅游景区/旅游度假区）。
        # 只显示 level 的话，度假区整列都是「—」，等于把已经核验的信息藏起来。
        s["rank"] = s.get("level") or s.get("category") or ""
    return spots


# ================================================================== 访客页面
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    db = get_db()
    spots = _decorate(db.spots(), db)
    docs = db.documents()[:8]
    return TEMPLATES.TemplateResponse(request, "index.html", base_ctx(
        request, spots=spots, docs=docs, active="home"))


@app.get("/spots", response_class=HTMLResponse)
def spot_list(request: Request, city: str = Query(""), status: str = Query("")):
    db = get_db()
    all_spots = _decorate(db.spots(), db)
    spots = all_spots
    if city:
        spots = [s for s in spots if (s.get("city") or "") == city]
    if status:
        spots = [s for s in spots if s["status"] == status]
    cities = sorted({s["city"] for s in all_spots if s.get("city")})
    return TEMPLATES.TemplateResponse(request, "spots.html", base_ctx(
        request, spots=spots, cities=cities, city=city, status=status,
        total=len(all_spots), active="spots"))


@app.get("/spot/{spot_id}", response_class=HTMLResponse)
def spot_detail(request: Request, spot_id: str):
    db = get_db()
    spot = db.spot(spot_id)
    if not spot:
        raise HTTPException(404, f"景区不存在：{spot_id}")
    fields = group_fields(db.fields(spot_id))
    conflicts = db.conflicts(spot_id)
    # 冲突项带出各来源文档的最新变更摘要，页面据此做「变更对比」
    for c in conflicts:
        for item in c["items"]:
            versions = db.doc_versions(item["source_doc_id"])
            item["latest_diff"] = versions[0]["diff_summary"] if versions else None
    docs = db.related_docs(spot_id)
    return TEMPLATES.TemplateResponse(request, "spot.html", base_ctx(
        request, spot=spot, fields=fields, conflicts=conflicts,
        docs=docs, active="spots"))


@app.get("/doc/{doc_id}", response_class=HTMLResponse)
def doc_detail(request: Request, doc_id: str):
    db = get_db()
    doc = db.doc(doc_id)
    if not doc:
        raise HTTPException(404, f"文档不存在：{doc_id}")
    row = db.conn.execute(
        "SELECT content_text, content_html, raw_path FROM documents WHERE doc_id=?",
        (doc_id,)).fetchone()
    versions = db.doc_versions(doc_id)
    chunks = db.chunks(doc_id)
    prov = [dict(r) for r in db.conn.execute(
        "SELECT p.*, s.name AS spot_name FROM field_provenance p "
        "JOIN scenic_spots s ON s.spot_id=p.spot_id "
        "WHERE p.source_doc_id=? ORDER BY p.field_name", (doc_id,))]
    return TEMPLATES.TemplateResponse(request, "doc.html", base_ctx(
        request, doc=doc, content_text=row["content_text"] if row else "",
        raw_path=row["raw_path"] if row else None,
        versions=versions, chunks=chunks, prov=prov, active="docs"))


@app.get("/ask", response_class=HTMLResponse)
def ask_form(request: Request, q: str = Query("")):
    # /ask?q=xxx 只预填输入框、不自动提交：避免把「预填」误当成「已有答案」
    return TEMPLATES.TemplateResponse(request, "ask.html", base_ctx(
        request, question=q, result=None, active="ask"))


@app.post("/ask", response_class=HTMLResponse)
def ask_submit(request: Request, question: str = Form(""), top_k: int = Form(5)):
    result = get_qa().ask(question, top_k=max(1, min(int(top_k or 5), 10)))
    return TEMPLATES.TemplateResponse(request, "ask.html", base_ctx(
        request, question=question, result=result, active="ask"))


@app.get("/review", response_class=HTMLResponse)
def review(request: Request):
    db = get_db()
    queue = db.review_queue(200)
    for item in queue:
        s = db.spot(item["spot_id"]) if item.get("spot_id") else None
        item["spot_name"] = s["name"] if s else (item.get("spot_id") or "—")
        item["field_label"] = FIELD_LABELS.get(item.get("field_name") or "",
                                               item.get("field_name") or "—")
    return TEMPLATES.TemplateResponse(request, "review.html", base_ctx(
        request, queue=queue, active="review"))


# ================================================================== 后台
@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request, saved: int = Query(0), error: str = Query(""),
          rebuilt: str = Query("")):
    return TEMPLATES.TemplateResponse(request, "admin.html", base_ctx(
        request,
        cfg=settings_store.public_view(),
        saved=bool(saved),
        error=error,
        rebuilt=rebuilt,
        active="admin",
    ))


@app.post("/admin")
def admin_save(
    provider: str = Form(""),
    base_url: str = Form(""),
    model: str = Form(""),
    api_key: str = Form(""),
    temperature: str = Form("0.2"),
    max_tokens: str = Form("900"),
    embedding_backend: str = Form("st"),
    embedding_model: str = Form(""),
    clear_key: str = Form(""),
):
    updates: dict[str, Any] = {
        "provider": provider, "base_url": base_url, "model": model,
        "temperature": temperature, "max_tokens": max_tokens,
        "embedding_backend": embedding_backend,
        "embedding_model": embedding_model,
    }
    if clear_key:
        settings_store.clear_key()
    else:
        updates["api_key"] = api_key
    cfg = settings_store.save(updates)

    # 立即生效：重建 LLM 客户端；embedding 后端变了就得重建索引
    global _qa
    get_llm(refresh=True)
    rebuild_note = ""
    emb_changed = index_status(get_db()).model != _embedding_name_after_save(cfg)
    if emb_changed:
        try:
            rep = ensure_index(get_db())
            rebuild_note = f"&rebuilt={rep.status}"
        except Exception as e:
            return RedirectResponse(
                f"/admin?saved=1&error={type(e).__name__}%3A%20{e}", status_code=303)
    _qa = None   # 下个请求重新装配 QAService，让它拿到新的 retriever
    return RedirectResponse(f"/admin?saved=1{rebuild_note}", status_code=303)


def _embedding_name_after_save(cfg: dict) -> str:
    """保存后索引里应有的模型名。

    选了语义后端但模型其实没加载成功时，`Embedder.name` 会回落成
    `char-ngram-hash-v1`；这里对齐真实运行结果，而不是配置里写的字符串。
    """
    from .embeddings import get_embedder
    emb = get_embedder(refresh=True)
    return emb.name


@app.post("/admin/test")
def admin_test():
    """用当前配置打一次模型，返回成或不成。给配置页的「测试连接」按钮用。"""
    from .llm import LLMClient
    client = LLMClient()
    if not client.configured:
        return JSONResponse(
            {"ok": False, "message": "尚未填写 API Key"}, status_code=400)
    resp = client.complete(
        "只回复两个字：收到", system="你是连通性测试，只回应用户要求的那两个字。")
    return JSONResponse({
        "ok": resp.mode == "live",
        "mode": resp.mode,
        "model": resp.model,
        "latency_ms": resp.latency_ms,
        "reply": resp.text[:200],
        "error": resp.error,
    })


@app.post("/admin/reindex")
def admin_reindex():
    global _qa
    try:
        from .embeddings import get_embedder
        from .ingest import build_index
        rep = build_index(get_db(), force=True, embedder=get_embedder(refresh=True))
        _qa = None
        return RedirectResponse(f"/admin?rebuilt={rep.status}", status_code=303)
    except Exception as e:
        return RedirectResponse(
            f"/admin?error={type(e).__name__}%3A%20{e}", status_code=303)


# ================================================================== 接口
@app.get("/api/ask")
def api_ask(q: str = Query(..., min_length=1), top_k: int = Query(5, ge=1, le=10)):
    res = get_qa().ask(q, top_k=top_k)
    return JSONResponse({
        "question": res.question,
        "answer": res.answer,
        "mode": res.mode,
        "badge": res.badge,
        "model": res.model,
        "error": res.error,
        "latency_ms": res.latency_ms,
        "used_chunks": res.used_chunks,
        "citations": [c.__dict__ for c in res.citations],
    })


@app.get("/api/status")
def api_status():
    db = get_db()
    st = index_status(db)
    return JSONResponse({
        "llm": get_llm().status(),
        "index": _index_note,
        "index_meta": db.load_index_meta(),
        "index_status": st.as_dict(),
        "index_dir": str(index_dir()),
        "db_path": str(db.path),
        "stats": db.stats(),
        "uptime_sec": int(time.time() - _started_at),
    })


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.exception_handler(404)
def not_found(request: Request, exc: Exception):
    return TEMPLATES.TemplateResponse(request, "404.html", base_ctx(
        request, message=str(getattr(exc, "detail", "页面不存在"))), status_code=404)
