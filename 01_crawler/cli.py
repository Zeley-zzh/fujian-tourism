"""01_crawler 命令行入口。

在 01_crawler 目录下运行：

    python cli.py init                       建库（幂等）
    python cli.py crawl --site fj_wlt        全量抓取
    python cli.py crawl --all --incremental  增量抓取所有配置站点
    python cli.py clean                      清洗 + 抽字段 + 切片
    python cli.py validate                   字段校验（不通过只降级，不删证据）
    python cli.py export                     导出语料与质量报告
    python cli.py pipeline --all             一条龙：抓取→清洗→校验→导出
    python cli.py stats                      统计
    python cli.py report                     质量报告摘要
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 支持 `python cli.py` 直接跑（把 01_crawler 加进 sys.path）
sys.path.insert(0, str(Path(__file__).resolve().parent))

from crawler.anticrawl import AntiCrawlClient  # noqa: E402
from crawler.config import load_settings  # noqa: E402
from crawler.db import open_db  # noqa: E402
from crawler.pipeline import Cleaner, Crawler  # noqa: E402
from crawler.pipeline.exporter import Exporter  # noqa: E402
from crawler.pipeline.validator import FieldValidator  # noqa: E402
from crawler.utils import get_logger  # noqa: E402


def build(args):
    s = load_settings(args.config)
    if args.bypass:
        s.project.bypass_mode = True
        s.project.respect_robots = False
    if args.max_requests:
        s.throttle.max_requests_per_run = args.max_requests
    if args.detail_limit:
        s.throttle.detail_limit = args.detail_limit
    db = open_db(s.p("db_path"))
    client = AntiCrawlClient(s)
    log = get_logger("cli", s.p("log_dir"))
    return s, db, client, log


def _site_keys(s, args) -> list[str]:
    if args.all:
        return [x.get("key") for x in s.sites if x.get("key")]
    if args.site:
        return [args.site]
    return [s.sites[0]["key"]] if s.sites else []


def cmd_init(args) -> int:
    s, db, client, log = build(args)
    db.init_schema()
    print(f"数据库已就绪: {s.p('db_path')}")
    print(json.dumps(db.stats(), ensure_ascii=False, indent=2))
    client.close()
    db.close()
    return 0


def cmd_crawl(args) -> int:
    s, db, client, log = build(args)
    db.init_schema()
    keys = _site_keys(s, args)
    if not keys:
        print("没有可抓的站点：检查 settings.yaml 的 sites 段", file=sys.stderr)
        return 2

    mode = "incremental" if args.incremental else "full"
    grand = {"pages": 0, "new": 0, "changed": 0, "failed": 0}
    for key in keys:
        job_id = db.start_job("crawl", mode, key)
        log.info("=== 开始抓取 %s (%s) job=%d ===", key, mode, job_id)
        try:
            out = Crawler(s, db, client, job_id=job_id).crawl_site(key, mode=mode)
            db.finish_job(job_id, status="done", pages=out.pages, new=out.docs_new,
                          changed=out.docs_changed, failed=out.docs_failed,
                          note=f"{len(out.errors)} errors")
            grand["pages"] += out.pages
            grand["new"] += out.docs_new
            grand["changed"] += out.docs_changed
            grand["failed"] += out.docs_failed
            print(f"[{key}] 页 {out.pages} / 新 {out.docs_new} / 变更 {out.docs_changed} "
                  f"/ 失败 {out.docs_failed}")
            for e in out.errors[:5]:
                print(f"  ! {e}")
        except Exception as e:  # noqa: BLE001
            db.finish_job(job_id, status="failed", note=str(e)[:500])
            log.exception("抓取失败: %s", e)
            print(f"[{key}] 抓取异常: {e}", file=sys.stderr)

    print(json.dumps(client.report(), ensure_ascii=False, indent=2))
    print(f"合计: {grand}")
    client.close()
    db.close()
    return 0


def cmd_clean(args) -> int:
    s, db, client, log = build(args)
    st = Cleaner(s, db).run()
    print(json.dumps(st.__dict__, ensure_ascii=False, indent=2))
    client.close()
    db.close()
    return 0


def cmd_validate(args) -> int:
    s, db, client, log = build(args)
    rep = FieldValidator(s, db).validate_all()
    print(json.dumps(rep.to_dict(), ensure_ascii=False, indent=2)[:4000])
    client.close()
    db.close()
    return 0


def cmd_export(args) -> int:
    s, db, client, log = build(args)
    rep = Exporter(s, db).run()
    print(json.dumps(rep.__dict__, ensure_ascii=False, indent=2))
    client.close()
    db.close()
    return 0


def cmd_pipeline(args) -> int:
    s, db, client, log = build(args)
    db.init_schema()
    keys = _site_keys(s, args)
    for key in keys:
        job_id = db.start_job("pipeline", "full", key)
        try:
            out = Crawler(s, db, client, job_id=job_id).crawl_site(key, mode="full")
            db.finish_job(job_id, status="done", pages=out.pages, new=out.docs_new,
                          changed=out.docs_changed, failed=out.docs_failed)
            print(f"[{key}] 抓取: 页 {out.pages} 新 {out.docs_new} 变更 {out.docs_changed} "
                  f"失败 {out.docs_failed}")
        except Exception as e:  # noqa: BLE001
            db.finish_job(job_id, status="failed", note=str(e)[:500])
            print(f"[{key}] 抓取异常: {e}", file=sys.stderr)

    print("→ 清洗")
    st = Cleaner(s, db).run()
    print(f"  文档 {st.cleaned} / 重复 {st.duplicates} / 景区 {st.spots} "
          f"/ 字段 {st.fields_extracted} / 切片 {st.chunks}")

    print("→ 校验")
    vr = FieldValidator(s, db).validate_all()
    print(f"  检查 {vr.checked} 通过 {vr.passed} 降级 {vr.downgraded}")

    print("→ 导出")
    er = Exporter(s, db).run()
    print(f"  {er.corpus_path}")
    print(f"  {er.report_path}")

    print(json.dumps(client.report(), ensure_ascii=False, indent=2))
    client.close()
    db.close()
    return 0


def cmd_stats(args) -> int:
    s = load_settings(args.config)
    db = open_db(s.p("db_path"))
    print(json.dumps(db.stats(), ensure_ascii=False, indent=2))
    db.close()
    return 0


def cmd_report(args) -> int:
    s, db, client, log = build(args)
    er = Exporter(s, db).run()
    data = json.loads(Path(er.report_path).read_text(encoding="utf-8"))
    print(json.dumps({
        "summary": data["summary"],
        "status_matrix": data["status_matrix"],
        "coverage": data["coverage"],
        "conflict_count": len(data["conflicts"]),
        "pending_count": len(data["pending_review"]),
        "review_queue": data["review_queue"],
    }, ensure_ascii=False, indent=2))
    client.close()
    db.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="福建文旅数据采集与清洗（01_crawler）")
    p.add_argument("--config", default=None, help="settings.yaml 路径")
    p.add_argument("--bypass", action="store_true",
                   help="打开绕过模式（不读 robots，启用代理池与验证码识别）")
    p.add_argument("--max-requests", type=int, default=0, help="覆盖单次运行的请求预算")
    p.add_argument("--detail-limit", type=int, default=0, help="覆盖单次抓取的详情篇数上限")

    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="建库").set_defaults(func=cmd_init)

    c = sub.add_parser("crawl", help="抓取")
    c.add_argument("--site", default=None)
    c.add_argument("--all", action="store_true")
    c.add_argument("--incremental", action="store_true")
    c.set_defaults(func=cmd_crawl)

    sub.add_parser("clean", help="清洗 + 抽字段 + 切片").set_defaults(func=cmd_clean)
    sub.add_parser("validate", help="字段校验").set_defaults(func=cmd_validate)
    sub.add_parser("export", help="导出语料与质量报告").set_defaults(func=cmd_export)

    pl = sub.add_parser("pipeline", help="抓取→清洗→校验→导出")
    pl.add_argument("--site", default=None)
    pl.add_argument("--all", action="store_true")
    pl.set_defaults(func=cmd_pipeline)

    sub.add_parser("stats", help="统计").set_defaults(func=cmd_stats)
    sub.add_parser("report", help="质量报告摘要").set_defaults(func=cmd_report)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
