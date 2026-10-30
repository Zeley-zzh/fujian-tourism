"""向量化的确定性、归一化与判别力。"""
from __future__ import annotations

import numpy as np

from app.embeddings import DIM, Embedder, embed_hash, get_embedder, cosine


def test_dim_and_norm():
    v = embed_hash("甲峰景区门票价格 65 元，开放时间 08:00—17:30")
    assert v.shape == (DIM,)
    assert abs(float(np.linalg.norm(v)) - 1.0) < 1e-5


def test_deterministic():
    a = embed_hash("门票 65 元")
    b = embed_hash("门票 65 元")
    assert np.array_equal(a, b), "同文本必须产生同向量，否则索引无法重建"


def test_empty_text_is_zero_vector():
    v = embed_hash("")
    assert v.shape == (DIM,)
    assert float(np.linalg.norm(v)) == 0.0


def test_similar_beats_unrelated():
    q = embed_hash("甲峰景区门票多少钱")
    near = embed_hash("甲峰景区门票价格 65 元")
    far = embed_hash("福州天气预报明天有雨")
    assert cosine(q, near) > cosine(q, far), "同主题相似度必须高于无关文本"


def test_chinese_and_ascii_both_indexed():
    v1 = embed_hash("门票价格 65 元")
    v2 = embed_hash("开放时间 08:00")
    assert cosine(v1, v2) < 0.5, "不同字段的文本不该高度相似"
    assert cosine(v1, embed_hash("门票价格 80 元")) > 0.5, "同字段不同值应保持相似"


def test_embedder_encode_batch_shape():
    e = Embedder(backend="hash")
    mat = e.encode(["第一段文本内容", "第二段", ""])
    assert mat.shape == (3, DIM)
    assert mat.dtype == np.float32
    # 空文本不归一化，其余行范数为 1
    assert abs(float(np.linalg.norm(mat[0])) - 1.0) < 1e-5
    assert float(np.linalg.norm(mat[2])) == 0.0


def test_embedder_name_marks_backend():
    e = Embedder(backend="hash")
    assert e.name == "char-ngram-hash-v1"
    assert e.dim == DIM


def test_get_embedder_is_singleton():
    assert get_embedder() is get_embedder()
