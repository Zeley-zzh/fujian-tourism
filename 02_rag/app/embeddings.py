"""向量化：真实语义模型优先，无模型时退回内置字符 n-gram。

两条后端，同一个接口
--------------------
- `st`   → `sentence-transformers` 加载中文 embed 模型（默认 `BAAI/bge-small-zh-v1.5`，
  512 维，权重约 95MB）。语义近邻可靠，是正式用法。
- `hash` → 字符 2/3-gram 哈希到 512 桶。不下载任何权重，完全确定性，
  用于「模型没装好也要能跑起来」的兜底，以及测试里断言向量稳定性。

选哪个由 `settings_store` 的 `embedding_backend` 决定，后台配置页可改。
`name` 属性会写进 `faiss_index_meta.embedding_model`：换后端/换模型时索引
必须重建，因为不同模型的向量空间不能混用（混用等于往索引里投毒）。

维度为什么是 512
----------------
bge-small-zh 原生 512 维；FAISS `IndexFlatIP` 在万级向量下暴力检索是微秒级，
不需要 IVF/PQ 训练。哈希后端也统一到 512，两套后端可以共用同一份 FAISS 结构
（但索引内容不兼容，靠 `embedding_model` 字段拦住）。
"""
from __future__ import annotations

import hashlib
import math
import re
from typing import Sequence

import numpy as np

from . import settings_store

DIM = 512

_CJK = re.compile(r"[\u4e00-\u9fff]")
_WORD = re.compile(r"[a-zA-Z0-9]+")


def _grams(run: str) -> list[str]:
    out = [run[i:i + 2] for i in range(len(run) - 1)]
    out += [run[i:i + 3] for i in range(len(run) - 2)]
    if len(run) == 1:
        out.append(run)
    return out


def _tokens(text: str) -> list[str]:
    """中文按连续汉字段切 2/3-gram，英文数字取整词。

    只取单字会让「门票」和「门票价格」几乎无区别；只取整句又太稀疏。
    2-gram 抓词、3-gram 抓短语，混合后长短 query 都有落点。
    """
    text = (text or "").lower()
    grams: list[str] = []
    buf: list[str] = []
    for ch in text:
        if _CJK.match(ch):
            buf.append(ch)
        elif buf:
            grams.extend(_grams("".join(buf)))
            buf = []
    if buf:
        grams.extend(_grams("".join(buf)))
    grams.extend(_WORD.findall(text))
    return [g for g in grams if g.strip()]


def _bucket(token: str) -> tuple[int, float]:
    """稳定哈希到桶，返回 (桶号, 符号)。

    带符号是为了让碰撞相互抵消而非单向累加：512 桶面对数千 token 时碰撞很密，
    符号位能把噪声压下去一截。
    """
    h = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    v = int.from_bytes(h, "little")
    return v % DIM, (1.0 if (v >> 63) & 1 else -1.0)


def embed_hash(text: str) -> np.ndarray:
    """单条文本 → L2 归一化 float32 向量。"""
    vec = np.zeros(DIM, dtype=np.float32)
    toks = _tokens(text)
    if not toks:
        return vec
    counts: dict[str, int] = {}
    for t in toks:
        counts[t] = counts.get(t, 0) + 1
    for tok, c in counts.items():
        b, sign = _bucket(tok)
        vec[b] += sign * (1.0 + math.log(c))   # 次线性词频，长文档不被高频字词主导
    norm = float(np.linalg.norm(vec))
    if norm > 0:
        vec /= norm
    return vec


class Embedder:
    """统一入口：`encode(list[str]) -> (n, dim) float32`，行已归一化。"""

    def __init__(self, backend: str | None = None, model_name: str | None = None):
        cfg = settings_store.load()
        self.backend = (backend or cfg["embedding_backend"] or "hash").lower()
        self.model_name = model_name or cfg["embedding_model"]
        self._st = None
        self._dim = DIM
        self.load_error: str | None = None
        if self.backend == "st":
            self._load_st()

    def _load_st(self) -> None:
        try:
            from sentence_transformers import SentenceTransformer
            self._st = SentenceTransformer(self.model_name)
            self._dim = int(self._st_dim())
        except Exception as e:  # 缺包、权重拉不下来、显存不足都落这里
            # 不抛异常：装了别的东西不该让整个问答站起不来。降级并用 error 说明。
            self._st = None
            self._dim = DIM
            self.load_error = f"{type(e).__name__}: {e}"
            self.backend = "hash"

    def _st_dim(self) -> int:
        """sentence-transformers 5.x 起把 get_sentence_embedding_dimension 改名为
        get_embedding_dimension；两个名字都在过渡期，按可用者取，避免钉死版本。"""
        for meth in ("get_embedding_dimension", "get_sentence_embedding_dimension"):
            fn = getattr(self._st, meth, None)
            if callable(fn):
                return int(fn())
        return int(self._st.encode(["x"]).shape[1])

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def name(self) -> str:
        """写进 faiss_index_meta.embedding_model；换模型时用来判定索引失效。"""
        return self.model_name if self._st is not None else "char-ngram-hash-v1"

    @property
    def is_semantic(self) -> bool:
        return self._st is not None

    def encode(self, texts: Sequence[str], batch_size: int = 32) -> np.ndarray:
        texts = list(texts)
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        if self._st is None:
            return np.vstack([embed_hash(t) for t in texts]).astype(np.float32)
        vecs = self._st.encode(
            texts, batch_size=batch_size, normalize_embeddings=True,
            show_progress_bar=False, convert_to_numpy=True,
        )
        return np.asarray(vecs, dtype=np.float32)

    def encode_one(self, text: str) -> np.ndarray:
        return self.encode([text])[0]


_DEFAULT: Embedder | None = None


def get_embedder(refresh: bool = False) -> Embedder:
    global _DEFAULT
    if _DEFAULT is None or refresh:
        _DEFAULT = Embedder()
    return _DEFAULT


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    """归一化向量的余弦 = 内积；给测试与调试用。"""
    return float(np.dot(a, b))


# ====================================================================== LangChain
def as_langchain_embeddings(embedder: Embedder | None = None):
    """包成 LangChain 的 `Embeddings` 接口，供 `FAISS.from_documents` 使用。

    只实现两个必需方法，不继承任何基类 —— LangChain 的 `Embeddings` 是
    Protocol 风格，鸭子类型即可，省掉一层版本兼容风险。
    """
    from langchain_core.embeddings import Embeddings

    emb = embedder or get_embedder()

    class _Bridge(Embeddings):
        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            return emb.encode(texts).tolist()

        def embed_query(self, text: str) -> list[float]:
            return emb.encode_one(text).tolist()

        @property
        def model_name(self) -> str:      # 部分 LangChain 组件会读这个属性
            return emb.name

    return _Bridge()
