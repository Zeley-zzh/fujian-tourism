"""抓取调度器：列表 → 详情 → 附件，全量 / 增量两条路径。

站点分两类，配置里用有无 `api` 段区分：

- **静态列表页**：`list_urls` + `pagination`，直接翻页抓 HTML。
- **检索接口**：`api` + `channels`，按栏目拼 query 参数请求 JSON。
  福建省文旅厅就是这一类：栏目页由 avalon.js 渲染，HTML 里只有骨架，
  真实列表在 `/was5/web/search` 返回的 JSON 里。

详情失败只记日志不中断整批。附件下载并解析后并进正文 —— 政务站大量
「规范性文件」页的正文本体就在 PDF 里，不取附件等于丢掉整篇。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode, urlparse

from ..anticrawl import AntiCrawlClient
from ..config import Settings
from ..db import Database
from ..parsers import get_parser
from ..parsers.attachments import parse_bytes
from ..utils import get_logger, guess_ext, normalize_url, parse_date, same_host


@dataclass
class CrawlOutcome:
    pages: int = 0
    docs_new: int = 0
    docs_changed: int = 0
    docs_failed: int = 0
    doc_ids: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def merge(self, other: "CrawlOutcome") -> None:
        self.pages += other.pages
        self.docs_new += other.docs_new
        self.docs_changed += other.docs_changed
        self.docs_failed += other.docs_failed
        self.doc_ids.extend(other.doc_ids)
        self.errors.extend(other.errors)


class Crawler:
    def __init__(self, settings: Settings, db: Database, client: AntiCrawlClient,
                 *, job_id: int | None = None):
        self.s = settings
        self.db = db
        self.client = client
        self.job_id = job_id
        self.log = get_logger("crawler", settings.p("log_dir"))
        self.raw_dir = settings.p("raw_dir")

    # ------------------------------------------------------------------ 入口
    def crawl_site(self, site_key: str, *, mode: str = "full") -> CrawlOutcome:
        site = self.s.site(site_key)
        parser = get_parser(site.get("parser", "gov_notice"), site)
        out = CrawlOutcome()
        cutoff = self._incremental_cutoff() if mode == "incremental" else None

        items: list[dict] = []
        # 指定 URL 排最前：景区名录类文件发布早、在列表里沉到几十页之后，
        # 靠翻页找到它们要花掉大半请求预算，直接给定最省。
        for seed in site.get("seed_urls") or []:
            u = seed.get("url") if isinstance(seed, dict) else seed
            if not u:
                continue
            items.append({
                "url": normalize_url(u),
                "title": "",
                "date": None,
                "list_page": site.get("base_url"),
                "doc_type": (seed.get("doc_type") if isinstance(seed, dict) else None)
                            or site.get("doc_type"),
            })
        if site.get("api"):
            for channel in site.get("channels") or []:
                items.extend(self._collect_from_api(site, channel, parser, out, cutoff))
        else:
            items.extend(self._collect_from_html(site, parser, out, cutoff,
                                                 with_sitemap=(mode == "full")))

        # 检索接口会带回跨站链接（文旅部、税务总局、国务院…）。这些页面的结构
        # 与本站完全不同，用本站解析器抓只会得到 0 字正文，还白烧请求预算。
        # 默认只收本站，需要时在 site 里加 allow_external: {host: parser} 显式放行。
        external = site.get("allow_external") or {}
        kept: list[dict] = []
        skipped: dict[str, int] = {}
        for it in items:
            host = urlparse(it["url"]).hostname or ""
            if same_host(it["url"], site.get("base_url", "")):
                kept.append(it)
            elif host in external:
                it["forced_parser"] = external[host]
                kept.append(it)
            else:
                skipped[host] = skipped.get(host, 0) + 1
        if skipped:
            detail = "、".join(f"{h}×{n}" for h, n in sorted(skipped.items(), key=lambda x: -x[1]))
            self._log("INFO", "external_skipped", detail=f"跨站链接不计入本次抓取: {detail}")
        items = kept

        # 去重：同一篇公告可能同时挂在多个栏目下（政策法规与信息公开目录经常重叠）
        seen: set[str] = set()
        budget = self.s.throttle.max_requests_per_run
        detail_limit = self.s.throttle.detail_limit
        details = 0
        for it in items:
            url = it["url"]
            if url in seen:
                continue
            seen.add(url)
            if details >= detail_limit:
                self._log("INFO", "detail_limit", url=url,
                          detail=f"本次已抓 {details} 篇详情，剩余 {len(items) - len(seen)} 条留给增量任务")
                break
            if out.pages >= budget:
                self._log("INFO", "budget_reached", url=url,
                          detail=f"已达请求预算 {budget}，剩余 {len(items) - len(seen)} 条留给增量任务")
                break
            details += 1
            self._crawl_detail(site_key, site, parser, url, out,
                               referer=it.get("list_page"),
                               doc_type=it.get("doc_type"))

        return out

    # ------------------------------------------------------------------ 接口列表
    def _collect_from_api(self, site: dict, channel: dict, parser,
                          out: CrawlOutcome, cutoff: str | None) -> list[dict]:
        api = site["api"]
        page_url = channel.get("page_url") or api.get("referer") or site["base_url"]
        max_pages = int(channel.get("max_pages") or 10)
        collected: list[dict] = []

        for page in range(1, max_pages + 1):
            params = {
                "channelid": api.get("channelid", ""),
                "templet": api.get("templet", "advsch.jsp"),
                "classsql": channel.get("classsql", ""),
                "sortfield": api.get("sortfield", "-docorderpri,-docreltime"),
                "searchWord": api.get("searchWord", ""),
                "years": api.get("years", ""),
                "prepage": str(api.get("prepage", 50)),
                "page": str(page),
            }
            url = f"{api['url']}?{urlencode(params)}"
            r = self.client.fetch(
                url, referer=page_url,
                extra_headers={
                    # 缺这个头，接口会返回渲染后的 HTML 而不是 JSON
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                },
            )
            out.pages += 1
            if not r.ok:
                self._log("WARN", "api_fail", url=url, detail=r.error, elapsed=r.elapsed_ms)
                out.errors.append(f"列表接口失败 {channel.get('key')} p{page}: {r.error}")
                break

            page_items = parser.parse_list(r.text, page_url)
            for it in page_items:
                it["list_page"] = page_url
                if not it.get("doc_type"):
                    it["doc_type"] = channel.get("doc_type") or site.get("doc_type")
            self._log("INFO", "api_ok", url=url,
                      detail=f"{channel.get('key')} p{page} 发现 {len(page_items)} 条")

            if not page_items:
                break  # 该栏目已到底

            if cutoff:
                fresh = [it for it in page_items if it.get("date") and it["date"] >= cutoff]
                collected.extend(fresh)
                if not fresh:
                    # 列表按时间倒序，整页都比水位旧 → 更早的页不必再翻
                    self._log("INFO", "incremental_stop", url=url,
                              detail=f"{channel.get('key')} 第 {page} 页已全部早于 {cutoff}")
                    break
            else:
                collected.extend(page_items)

            if len(page_items) < int(api.get("prepage", 50)):
                break  # 不足一页 = 最后一页
        return collected

    # ------------------------------------------------------------------ HTML 列表
    def _collect_from_html(self, site: dict, parser, out: CrawlOutcome,
                           cutoff: str | None, *, with_sitemap: bool) -> list[dict]:
        collected: list[dict] = []
        max_pages = self.s.throttle.max_requests_per_run

        for list_url in site.get("list_urls", []):
            for page_url in self._paginate(site, list_url):
                if out.pages >= max_pages:
                    break
                r = self.client.fetch(page_url, referer=site.get("base_url"))
                out.pages += 1
                if not r.ok:
                    self._log("WARN", "list_fail", url=page_url, detail=r.error,
                              elapsed=r.elapsed_ms)
                    out.errors.append(f"列表页失败 {page_url}: {r.error}")
                    continue
                items = parser.parse_list(r.text, page_url)
                self._log("INFO", "list_ok", url=page_url, detail=f"发现 {len(items)} 条")
                if not items:
                    # 列表解析为空是重要信号（selector 过期或列表改接口），要显眼记录
                    self._log("WARN", "list_empty", url=page_url,
                              detail=f"列表选择器可能失效: {site.get('list_selector')}")
                for it in items:
                    it["list_page"] = page_url
                    if not it.get("doc_type"):
                        it["doc_type"] = site.get("doc_type")
                collected.extend(items)

        if with_sitemap and site.get("base_url"):
            for u in self.client.robots.sitemap_entries(site, max_urls=500):
                if same_host(u, site["base_url"]):
                    collected.append({"url": normalize_url(u), "title": "",
                                      "date": None, "list_page": site["base_url"]})

        if cutoff:
            collected = [it for it in collected if it.get("date") and it["date"] >= cutoff]
        return collected

    # ------------------------------------------------------------------ 详情
    def _crawl_detail(self, site_key: str, site: dict, parser, url: str,
                      out: CrawlOutcome, referer: str | None = None,
                      doc_type: str | None = None) -> None:
        # 已抓过的仍要重抓：靠 content_hash 判断是否变更，否则永远发现不了更新
        r = self.client.fetch(url, referer=referer or site.get("base_url"))
        out.pages += 1
        if not r.ok:
            if "403" in r.error or "blocked" in r.error:
                br = self.client.browser_fallback(url)
                if br.ok:
                    r = br
            if not r.ok:
                out.docs_failed += 1
                self._log("WARN", "detail_fail", url=url, detail=r.error, elapsed=r.elapsed_ms)
                out.errors.append(f"{url}: {r.error}")
                return

        rec = parser.parse_detail(r.text, r.final_url or url)
        if doc_type and not rec.doc_type:
            rec.doc_type = doc_type

        # 正文过短时用附件补：政务站常见的「正文在 PDF 里」
        attach_texts: list[str] = []
        for att in self._attachment_urls(rec, r.text, url)[:8]:
            body = self._fetch_attachment(att, referer=url)
            if body:
                attach_texts.append(f"【附件】{urlparse(att).path.rsplit('/', 1)[-1]}\n{body}")
                out.pages += 1
        if attach_texts:
            rec.content_text = (rec.content_text + "\n\n" + "\n\n".join(attach_texts)).strip()
            rec.extra["attachments_parsed"] = len(attach_texts)

        if not rec.is_useful():
            # 正文提取失败有三种情况，必须分开处理：
            #   1) 确实是图片版公告（容器里有 img）→ 入库并标 image_only；
            #   2) 正文在附件里但附件没解析出来 → 丢弃，记 text_missing；
            #   3) 容器选错／跨站页面结构不认识 → 丢弃，记 parse_fail。
            # 第 2、3 种以前会被误判成「图片版」混进语料，几篇同名的空文档
            # 直接把检索结果占满 —— 宁缺勿滥。
            if rec.extra.get("image_only") and rec.title.strip():
                rec.doc_type = rec.doc_type or "notice"
                self._log("WARN", "text_image_only", url=url,
                          detail=f"{rec.title[:40]} 正文由图片承载，按无文字入库")
            else:
                out.docs_failed += 1
                self._log("WARN", "text_missing", url=url,
                          detail=f"正文 {len(rec.content_text)} 字，标题 {rec.title[:40]!r}，不入库")
                return

        raw_path = self._save_raw(site_key, url, r)
        doc_id, action = self.db.upsert_document(rec, raw_path=raw_path)
        if action == "new":
            out.docs_new += 1
            out.doc_ids.append(doc_id)
        elif action == "updated":
            out.docs_changed += 1
            out.doc_ids.append(doc_id)
        self._log("INFO", f"doc_{action}", url=url,
                  detail=f"{doc_id} {rec.title[:40]}", elapsed=r.elapsed_ms)

    def _attachment_urls(self, rec, html: str, page_url: str) -> list[str]:
        """解析器已给出附件；再补一遍漏网的（有些模板把附件放在正文容器外）。"""
        from urllib.parse import urljoin

        urls = list(rec.attachments or [])
        for m in re.finditer(
                r'href=["\']([^"\']+\.(?:pdf|docx?|xlsx?|wps|csv)(?:$|[?#])[^"\']*)["\']',
                html or "", flags=re.I):
            urls.append(urljoin(page_url, m.group(1)))
        return list(dict.fromkeys(urls))

    def _fetch_attachment(self, url: str, referer: str) -> str:
        r = self.client.fetch(url, referer=referer, is_asset=True, allow_retry=False)
        if not r.ok or not r.content:
            self._log("WARN", "attach_fail", url=url, detail=r.error)
            return ""
        parsed = parse_bytes(r.content, filename=urlparse(url).path)
        if parsed.error:
            self._log("WARN", "attach_parse_fail", url=url, detail=parsed.error)
            return ""
        return parsed.text

    def _save_raw(self, site_key: str, url: str, r) -> str | None:
        try:
            ext = guess_ext(url, r.content_type)
            h = normalize_url(url).rsplit("/", 1)[-1][:40] or "index"
            h = re.sub(r"[^\w.\-]", "_", h) or "index"
            d = self.raw_dir / site_key
            d.mkdir(parents=True, exist_ok=True)
            name = f"{h}{ext if not h.endswith(ext) else ''}"
            path = d / f"{int(time.time())}_{name}"
            path.write_bytes(r.content)
            return str(path.relative_to(self.s.p("data_dir")))
        except Exception as e:  # noqa: BLE001
            self.log.debug("原始快照落盘失败: %s", e)
            return None

    # ------------------------------------------------------------------ 分页
    def _paginate(self, site: dict, list_url: str) -> list[str]:
        pg = site.get("pagination") or {}
        if pg.get("type") != "query":
            return [list_url]
        param = pg.get("param", "page")
        start = int(pg.get("start", 1))
        maximum = int(pg.get("max", 10))
        urls = []
        for i in range(start, start + maximum):
            sep = "&" if "?" in list_url else "?"
            urls.append(f"{list_url}{sep}{param}={i}")
        return urls

    def _incremental_cutoff(self) -> str | None:
        row = self.db.conn.execute(
            "SELECT MAX(finished_at) FROM crawl_jobs WHERE status='done'"
        ).fetchone()
        if not row or not row[0]:
            return None
        return parse_date(row[0])

    def _log(self, level: str, event: str, *, url: str | None = None,
             detail: str = "", elapsed: int | None = None) -> None:
        self.db.log(self.job_id, level, event, url=url, detail=detail, elapsed_ms=elapsed)
        getattr(self.log, level.lower(), self.log.info)("%s %s %s", event, url or "", detail[:120])
