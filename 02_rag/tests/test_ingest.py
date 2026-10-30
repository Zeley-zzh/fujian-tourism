"""索引构建、幂等判定、faiss_row 回填、模型不匹配拦截。"""
from __future__ import annotations

import pytest

from app.embeddings import Embedder
from app.ingest import build_index, index_status
from app.store import TourismDB, VectorStore

from conftest import seed


def _db_on(tmp_path) -> TourismDB:
    """用 01_crawler 的 Database 造库，再用 02_rag 的 TourismDB 打开同一文件。"""
    from crawler.db import Database
    d = Database(tmp_path / "idx.db")
    d.init_schema()
    seed(d)
    d.close()
    return TourismDB(tmp_path / "idx.db")


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("RAG_INDEX_DIR", str(tmp_path / "faiss"))
    db = _db_on(tmp_path)
    yield db, VectorStore(db, Embedder(backend="hash"))
    db.close()


def test_build_creates_index_and_meta(store):
    db, vs = store
    rep = vs.build(force=True)
    assert rep.status == "built"
    assert rep.chunks == 1
    assert (vs.index_path().parent / "fujian_tourism.faiss").exists()

    meta = db.load_index_meta()
    assert meta["embedding_model"] == "char-ngram-hash-v1"
    assert meta["dim"] == rep.dim
    assert meta["total_vectors"] == 1
    assert meta["metric"] == "ip"


def test_faiss_row_backfilled(store):
    db, vs = store
    vs.build(force=True)
    chunks = db.chunks()
    assert chunks[0].chunk_index == 0
    row = db.chunk_by_faiss_row(0)
    assert row is not None and row.chunk_id == chunks[0].chunk_id
    assert db.chunk_by_faiss_row(999) is None


def test_second_build_skips_when_unchanged(store):
    db, vs = store
    first = vs.build(force=True)
    second = vs.build()
    assert first.status == "built"
    assert second.status == "skipped"
    assert "没变" in second.note


def test_force_rebuilds(store):
    _, vs = store
    vs.build(force=True)
    assert vs.build(force=True).status == "built"


def test_empty_corpus_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("RAG_INDEX_DIR", str(tmp_path / "faiss"))
    from crawler.db import Database
    d = Database(tmp_path / "e.db")
    d.init_schema()
    d.close()
    db = TourismDB(tmp_path / "e.db")
    rep = build_index(db, force=True, embedder=Embedder(backend="hash"))
    assert rep.status == "empty"
    assert rep.chunks == 0
    db.close()


def test_search_returns_documents_with_metadata(store):
    db, vs = store
    vs.build(force=True)
    hits = vs.search("门票价格", top_k=3)
    assert hits
    doc, _score = hits[0]
    assert doc.metadata["chunk_id"].startswith("chunk_")
    assert doc.metadata["doc_title"]


def test_search_on_missing_index_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("RAG_INDEX_DIR", str(tmp_path / "none"))
    db = _db_on(tmp_path)
    vs = VectorStore(db, Embedder(backend="hash"))
    assert vs.search("任意", top_k=3) == []
    db.close()


def test_save_and_load_roundtrip(store, tmp_path, monkeypatch):
    db, vs = store
    vs.build(force=True)
    fresh = VectorStore(db, Embedder(backend="hash"))
    assert fresh.load() is True
    assert fresh.total == 1


def test_model_mismatch_refuses_to_load(store):
    """换模型后旧索引必须拒载 —— 不同向量空间混算只会给出无意义的分数。"""
    db, vs = store
    vs.build(force=True)
    wrong = VectorStore(db, Embedder(backend="hash", model_name="other-model"))
    # Embedder 的 name 由后端决定，hash 后端恒为 char-ngram-hash-v1，
    # 这里直接改元数据模拟「索引由语义模型建立」的场景
    db.save_index_meta(embedding_model="BAAI/bge-small-zh-v1.5", dim=512,
                       total_vectors=1, index_path=str(vs.index_path()),
                       corpus_hash=db.corpus_hash())
    with pytest.raises(RuntimeError, match="请重建索引"):
        wrong.load()


def test_index_status_reports_mismatch(store):
    db, vs = store
    vs.build(force=True)
    assert index_status(db).ready is True
    db.save_index_meta(embedding_model="some-other-model", dim=512,
                       total_vectors=1, index_path=str(vs.index_path()),
                       corpus_hash=db.corpus_hash())
    st = index_status(db)
    assert st.ready is False
    assert "需要重建" in st.note
