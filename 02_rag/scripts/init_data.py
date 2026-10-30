"""准备采集数据与向量索引。

顺序：
1. 若 `../01_crawler/data/tourism.db` 不存在 → 调 01_crawler 的真实采集流水线；
2. 建 FAISS 索引（语料与模型都没变则复用）。

用法：
    python -m scripts.init_data             # 缺才采、缺才建
    python -m scripts.init_data --rebuild   # 强制重建索引
    python -m scripts.init_data --recrawl   # 清库重采（会删 tourism.db 后全量重抓）
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECT = ROOT.parent
CRAWLER = PROJECT / "01_crawler"
DB = CRAWLER / "data" / "tourism.db"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def run_crawler(max_requests: int | None = None, detail_limit: int | None = None) -> int:
    """调 01_crawler 的 CLI 走一遍采集→清洗→校验→导出。"""
    if not (CRAWLER / "cli.py").exists():
        print(f"找不到 01_crawler/cli.py（{CRAWLER}）", file=sys.stderr)
        return 2
    cmd = [sys.executable, "cli.py", "pipeline", "--all"]
    if max_requests:
        cmd += ["--max-requests", str(max_requests)]
    if detail_limit:
        cmd += ["--detail-limit", str(detail_limit)]
    print("→ 运行 01_crawler:", " ".join(cmd))
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(cmd, cwd=str(CRAWLER), env=env).returncode


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="准备采集数据与向量索引")
    p.add_argument("--rebuild", action="store_true", help="强制重建索引")
    p.add_argument("--recrawl", action="store_true", help="清库重采")
    p.add_argument("--max-requests", type=int, default=None,
                   help="单次采集的请求上限（默认取 settings.yaml）")
    p.add_argument("--detail-limit", type=int, default=None,
                   help="单次采集的详情页篇数上限（默认取 settings.yaml）")
    args = p.parse_args(argv)

    if args.recrawl or not DB.exists():
        for f in (CRAWLER / "data").glob("tourism.db*"):
            try:
                f.unlink(missing_ok=True)
            except PermissionError:
                print(f"无法删除 {f}：文件被占用。先停掉 uvicorn 再跑 --recrawl。",
                      file=sys.stderr)
                return 3
        code = run_crawler(args.max_requests, args.detail_limit)
        if code != 0:
            print(f"01_crawler 退出码 {code}", file=sys.stderr)
            return code

    from app.embeddings import get_embedder
    from app.ingest import build_index
    from app.store import TourismDB

    db = TourismDB()
    emb = get_embedder()
    if emb.load_error:
        print(f"向量后端降级：{emb.load_error}", file=sys.stderr)
    rep = build_index(db, emb, force=args.rebuild)
    print(f"索引: {rep.status} | 切片 {rep.chunks} | 维度 {rep.dim} | 模型 {rep.model}")
    if rep.note:
        print(f"说明: {rep.note}")
    print(f"索引文件: {rep.index_path}")
    s = db.stats()
    print(f"库内: 文档 {s['documents']} / 景区 {s['spots']} / 证据 {s['provenance']} "
          f"/ 已核验 {s['verified']} / 待核验 {s['pending']} / 冲突 {s['conflicts']}")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
