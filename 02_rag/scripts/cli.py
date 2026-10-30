"""命令行入口：建索引 / 试检索 / 试问答 / 打印状态。

用法：
    python -m scripts.cli build            建或复用索引
    python -m scripts.cli build --force    强制重建
    python -m scripts.cli search "门票多少钱"
    python -m scripts.cli ask "4A 级旅游景区的评定标准是什么"
    python -m scripts.cli status           库与索引状态
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ingest import build_index                      # noqa: E402
from app.llm import LLMClient                           # noqa: E402
from app.qa import QAService                            # noqa: E402
from app.store import Retriever, TourismDB, dumps, index_dir, resolve_db_path  # noqa: E402


def _db() -> TourismDB:
    return TourismDB(resolve_db_path())


def cmd_build(args) -> int:
    db = _db()
    rep = build_index(db, force=args.force)
    print(f"索引状态: {rep.status}")
    if rep.note:
        print(f"说明: {rep.note}")
    print(f"切片数: {rep.chunks}  维度: {rep.dim}  模型: {rep.model}")
    print(f"索引文件: {rep.index_path}")
    print(f"语料指纹: {rep.corpus_hash[:16]}…  耗时: {rep.elapsed_ms} ms")
    db.close()
    return 0


def cmd_search(args) -> int:
    db = _db()
    r = Retriever(db)
    if not r.load():
        print("没有索引，先跑 build", file=sys.stderr)
        return 2
    hits = r.search(args.query, top_k=args.top_k)
    if not hits:
        print("没有命中。")
        return 0
    for i, h in enumerate(hits, 1):
        print(f"[{i}] score={h['score']:.4f} (vec={h['vec_score']:.4f} kw={h['kw_score']:.4f})")
        print(f"    {h['doc_title']} | {h['publish_date']} | {h['doc_id']}")
        print(f"    {h['text'][:160].replace(chr(10), ' ')}")
    db.close()
    return 0


def cmd_ask(args) -> int:
    db = _db()
    res = QAService(db, llm=LLMClient()).ask(args.question, top_k=args.top_k)
    print(f"—— 回答（mode={res.mode}{'，' + res.badge if res.badge else ''}）——")
    print(res.answer)
    if res.citations:
        print("\n—— 引用 ——")
        for c in res.citations:
            print(f"[{c.index}] {c.doc_title} | {c.publish_date} | score={c.score:.3f} | {c.chunk_id}")
    db.close()
    return 0


def cmd_status(args) -> int:
    db = _db()
    print(dumps({
        "db": str(db.path),
        "index_dir": str(index_dir()),
        "stats": db.stats(),
        "index_meta": db.load_index_meta(),
        "llm": LLMClient().status(),
    }))
    db.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="rag", description="福建文旅知识库问答 - 命令行工具")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="建索引")
    b.add_argument("--force", action="store_true")
    b.set_defaults(func=cmd_build)

    s = sub.add_parser("search", help="只检索，不调模型")
    s.add_argument("query")
    s.add_argument("--top-k", type=int, default=5)
    s.set_defaults(func=cmd_search)

    a = sub.add_parser("ask", help="完整问答")
    a.add_argument("question")
    a.add_argument("--top-k", type=int, default=5)
    a.set_defaults(func=cmd_ask)

    st = sub.add_parser("status", help="库与索引状态")
    st.set_defaults(func=cmd_status)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
