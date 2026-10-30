"""福建省文化和旅游厅（wlt.fujian.gov.cn）解析器。

这个站有四处与通用政务站不同，逐条写清楚，免得后人重复踩：

**一、列表不在 HTML 里。**
栏目页由 avalon.js 渲染，静态抓 `index.html` 只有骨架。真实数据来自 WAS5 检索接口
`/was5/web/search`：响应头写的 `text/html;charset=UTF-8`，正文却是 JSON，而且 JSON
里带裸换行与裸 tab，`json.loads()` 必须传 `strict=False`，否则报
`Invalid control character at: line 19 column 16`。

**二、旧模板正文容器里嵌了一层完整的 `<html><body>`。**
`div.detp` 的内部是 `<html><head></head><body><p>…</p></body></html>` 这样的整页片段。
BeautifulSoup 遇到内嵌 html/body 会把结构重排，`get_text()` 直接返回空串。
解法是解析前先把这些标签剥掉（`_strip_nested_html`），再交给 lxml。

**三、正文有两套模板，容器不能只看第一个。**
政府信息公开目录（`/zfxxgkzl/`）用 `div.article_content.article_content_01` 包
`div.TRS_Editor`；政策法规旧页用 `div.detp`。两者长度差别极大，所以按候选顺序全部
试一遍、取文本最长的那个 —— 只取第一个命中的往往是空壳容器。

**四、附件不在 `<a>` 里，在 `var apps = [...]` 的 JS 数组里。**
新模板的下载区由 avalon 渲染，HTML 里的 `<a>` 被注释掉了（`<!-- <a href=…> -->`），
真正的 `{name, url}` 数组写在页面内联脚本里。只用 CSS 选择器会一条附件都拿不到。

**五、正文为空有两种完全不同的原因，不能混为一谈。**
- 正文是图片（规范性文件的扫描件）→ 标 `image_only`，条目仍然入库。
- 正文由 PDF/DOCX 附件承载 → 标 `needs_attachment`，由上层下载附件后并进正文。
两种都绝不回退「抓整页 body」：那会把导航栏、页脚、相关链接一起当正文，污染语料。

页面 `<meta>` 里有 `ArticleTitle / PubDate / ContentSource / Description`，
比正则稳得多，所以元信息优先从 meta 取，正则只做兜底。
"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..utils import clean_text, normalize_url, parse_date
from .base import BaseParser, DocRecord

# 正文容器候选，按命中率排序；实际取文本最长者
BODY_SELECTORS = (
    "div.article_content.article_content_01",
    "div.article_content",
    "div.TRS_Editor",
    "div.detp",
    "div.word_con",
    "div.article_area",
    "div.tabs_content.tabs_bd",
    "div.article-content",
    "article",
    "#content",
)

# 正文容器里要剔除的噪音：打印/收藏按钮、相关链接、分享条、来源行、附件下载条
JUNK_SELECTORS = (
    "script", "style", "iframe",
    "div.dybox", "div.dyy_box", "div.dyh_Box", "div.xglj_box",
    "div.article_source_col", "div.article_source_coll", "div.article_source_colll",
    "div.share", "div.related", "div.pagination", "div.article_tools",
    "div.article_attachment",
)

ATTACH_RE = re.compile(r"\.(?:pdf|docx?|xlsx?|wps|ofd|zip|rar|csv)(?:$|[?#])", re.I)
# 内嵌的整页片段标签，必须剥掉否则 get_text() 返回空
NESTED_HTML_RE = re.compile(r"</?(?:html|head|body)\b[^>]*>", re.I)
# 附件数组：var apps = [{name:"…", url:"…"}, …]
APPS_RE = re.compile(r"var\s+apps\s*=\s*\[(.*?)\]\s*(?://|;|\n\s*var)", re.S)
APP_ITEM_RE = re.compile(r'name\s*:\s*"([^"]*)"\s*,\s*url\s*:\s*"([^"]*)"')

INDEX_NO_RE = re.compile(r"索\s*引\s*号[：:]\s*([A-Z0-9][A-Z0-9\-]{6,40})")
DOC_NO_RE = re.compile(r"文\s*号[：:]\s*([^\s<]{4,60})")
ISSUER_RE = re.compile(r"来\s*源[：:]\s*([^\s<]{2,30})")
PUB_TIME_RE = re.compile(
    r"(?:发\s*布\s*时\s*间|成\s*文\s*日\s*期|时\s*间)[：:]\s*(\d{4}[\-年]\d{1,2}[\-月]\d{1,2})")

EFFECTIVE_FROM_RE = re.compile(
    r"自\s*(\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)\s*起\s*(?:施行|执行|实施|生效|试用)")
EFFECTIVE_TO_RE = re.compile(
    r"有效期(?:至|到)\s*(\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)|"
    r"至\s*(\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)\s*止")

# 正文里出现这些词说明它是导航/页脚，不是正文
BOILERPLATE_MARKS = ("网站支持IPv6", "繁体版", "无障碍浏览", "长者模式", "主办单位",
                     "ICP备", "网站标识码", "公安机关备案")


class WltGovParser(BaseParser):
    """福建省文旅厅：WAS5 JSON 列表 + 双模板详情 + JS 附件数组。"""

    name = "wlt_gov"

    # ------------------------------------------------------------------ 列表
    def parse_list(self, html: str, page_url: str) -> list[dict]:
        """列表既可能是 WAS5 的 JSON，也可能是普通 HTML 列表页。"""
        text = (html or "").lstrip()
        if text.startswith("{"):
            return self._parse_json_list(text)
        return self._parse_html_list(text, page_url)

    def _parse_json_list(self, text: str) -> list[dict]:
        try:
            data: dict[str, Any] = json.loads(text, strict=False)
        except json.JSONDecodeError:
            return []

        items: list[dict] = []
        base = (self.cfg.get("base_url") or "").rstrip("/")
        for doc in data.get("docs") or []:
            url = (doc.get("url") or doc.get("chnldocurl") or "").strip()
            if not url:
                continue
            if url.startswith("//"):
                url = "https:" + url
            elif url.startswith("/") and base:
                # 接口偶尔给相对路径；不补全的话 hostname 为空，
                # 上层按「跨站」过滤会把本站文章一起丢掉
                url = base + url
            title = self._clean_title(doc.get("title") or doc.get("title2")
                                      or doc.get("subdoctitle") or "")
            if not title:
                continue
            items.append({
                "url": normalize_url(url),
                "title": title,
                # 列表时间精确到秒，只留日期；这是发布时间，不是抓取时间
                "date": parse_date(doc.get("time") or doc.get("pubtime") or doc.get("pubdate")),
                "issuer": (doc.get("src") or "").strip() or None,
                "doc_no": self._doc_no_from_desc(doc),
                "summary": (doc.get("docabs") or "").strip()[:300] or None,
                "channel": (doc.get("chnlname") or "").strip() or None,
                "docid": (doc.get("docid") or "").strip() or None,
                # 附件名用分号分隔；filedesc 按同序号给出可读标题
                "attach_names": [x for x in (doc.get("file") or "").split(";") if x.strip()],
                "attach_titles": [x for x in (doc.get("filedesc") or "").split(";") if x.strip()],
            })
        return items

    def _parse_html_list(self, html: str, page_url: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        ls = self.cfg.get("list_selector") or {}
        item_sel = ls.get("item") or "ul li"
        link_sel = ls.get("link") or "a"
        date_sel = ls.get("date") or "span"

        items: list[dict] = []
        seen: set[str] = set()
        for li in soup.select(item_sel):
            a = li.select_one(link_sel)
            if not a or not a.get("href"):
                continue
            href = a["href"].strip()
            if href.startswith(("javascript:", "#", "mailto:")):
                continue
            url = normalize_url(urljoin(page_url, href))
            if url in seen:
                continue
            seen.add(url)
            d = li.select_one(date_sel)
            items.append({
                "url": url,
                "title": self._clean_title(self.text_of(a) or a.get("title", "")),
                "date": parse_date(self.text_of(d) if d else self.text_of(li)),
            })
        return items

    # ------------------------------------------------------------------ 详情
    def parse_detail(self, html: str, url: str) -> DocRecord:
        raw = html or ""
        soup = BeautifulSoup(self._strip_nested_html(raw), "lxml")
        meta = self._meta(soup)

        title = self._clean_title(meta.get("ArticleTitle") or self._pick_title(soup))
        body_node, body_selector = self._pick_body(soup)
        content_text = ""
        content_html = ""
        has_image = False
        if body_node is not None:
            has_image = bool(body_node.select_one("img"))
            for sel in JUNK_SELECTORS:
                for junk in body_node.select(sel):
                    junk.decompose()
            content_text = clean_text(body_node.get_text("\n", strip=True))
            if self._looks_like_boilerplate(content_text):
                # 容器抽出来的是导航/页脚 —— 宁可留空也不能污染语料
                content_text = ""
            content_html = str(body_node)

        page_text = self._readable_page_text(soup)
        issuer = (meta.get("ContentSource") or "").strip() or self._pick_issuer(soup, page_text)
        doc_no = self._first(DOC_NO_RE, page_text)
        index_no = self._first(INDEX_NO_RE, page_text)
        publish_date = parse_date(meta.get("PubDate")) or self._pick_publish_date(soup, page_text)

        eff_from = self._first(EFFECTIVE_FROM_RE, content_text)
        m_to = EFFECTIVE_TO_RE.search(content_text or "")
        eff_to = (m_to.group(1) or m_to.group(2)) if m_to else None

        return DocRecord(
            url=url,
            title=title or "[无标题]",
            content_text=content_text,
            content_html=content_html,
            doc_type=self.cfg.get("doc_type", "policy"),
            issuer=issuer or None,
            doc_no=doc_no,
            publish_date=publish_date,
            effective_from=parse_date(eff_from),
            effective_to=parse_date(eff_to),
            attachments=self._attachments(soup, raw, url),
            extra={
                "index_no": index_no,
                "body_selector": body_selector,
                "body_chars": len(content_text),
                "image_only": has_image and len(content_text) < 40,
                # 正文短又没图片 = 正文在附件里，交给上层下载补全
                "needs_attachment": len(content_text) < 40 and not has_image,
                "meta_description": (meta.get("Description") or "").strip()[:500] or None,
            },
        )

    # ------------------------------------------------------------------ 细节
    @staticmethod
    def _clean_title(raw: str) -> str:
        t = raw or ""
        # 检索接口偶尔把标题连 HTML 一起返回（实测 `<p>…</p>`），
        # 不剥掉会一路进数据库，页面上也显示标签。
        if "<" in t and ">" in t:
            t = BeautifulSoup(t, "lxml").get_text(" ")
        t = re.sub(r"\s+", " ", t).strip()
        # 去尾部「_福建省文化和旅游厅」这类站名后缀
        t = re.sub(r"\s*[_\-—|｜]\s*福建省文化和旅游厅\s*$", "", t)
        return t.strip()

    @staticmethod
    def _strip_nested_html(html: str) -> str:
        """剥掉容器内嵌的 `<html><head></head><body>`。

        旧模板 `div.detp` 里塞了一整页 HTML 片段，lxml 遇到嵌套 body 会重排结构，
        导致 `get_text()` 返回空串 —— 文章有正文却抽不出来，是最隐蔽的一种失败。
        """
        return NESTED_HTML_RE.sub("", html)

    @staticmethod
    def _meta(soup) -> dict[str, str]:
        out: dict[str, str] = {}
        for m in soup.find_all("meta"):
            name = m.get("name") or m.get("property")
            if name and m.get("content"):
                out[name] = m["content"]
        return out

    def _pick_title(self, soup) -> str:
        sels = self.sel.get("title")
        if isinstance(sels, str):
            sels = [sels]
        for sel in (sels or ["h1", "h2", "div.article_title"]):
            node = soup.select_one(sel)
            if node:
                t = self.text_of(node)
                if t:
                    return t
        node = soup.select_one("title")
        return self.text_of(node)

    def _pick_body(self, soup) -> tuple[Any, str]:
        """所有候选容器都试，取正文字符最多者；都不像正文就返回 (None, "")。

        不回退整页 body：整页里 90% 是导航与页脚，混进语料会毁掉检索质量。
        """
        configured = self.sel.get("body")
        if isinstance(configured, str):
            configured = [configured]
        candidates = tuple(configured or ()) + BODY_SELECTORS

        best_node, best_sel, best_len = None, "", 0
        seen: set[str] = set()
        for sel in candidates:
            if sel in seen:
                continue
            seen.add(sel)
            for node in soup.select(sel):
                text = node.get_text("", strip=True)
                if self._looks_like_boilerplate(text):
                    continue
                if len(text) > best_len:
                    best_node, best_sel, best_len = node, sel, len(text)

        if best_node is not None:
            return best_node, best_sel

        # 纯图片正文：容器文本为 0，但仍要拿到 img 才能判「图片版」
        for sel in candidates:
            node = soup.select_one(sel)
            if node is not None and node.select_one("img"):
                return node, sel
        return None, ""

    @staticmethod
    def _looks_like_boilerplate(text: str) -> bool:
        if not text:
            return False
        hits = sum(1 for m in BOILERPLATE_MARKS if m in text)
        return hits >= 2

    @staticmethod
    def _readable_page_text(soup) -> str:
        """整页纯文本，但先剔导航/页脚/脚本，避免从菜单里捡出假文号。"""
        clone = BeautifulSoup(str(soup), "lxml")
        for sel in ("script", "style", "header", "footer", "nav",
                    "div.header", "div.footer", "div.nav", "div.crumb",
                    "div.article_source_col", "div.article_source_coll",
                    "div.article_source_colll"):
            for junk in clone.select(sel):
                junk.decompose()
        return clean_text(clone.get_text("\n", strip=True))

    def _pick_issuer(self, soup, page_text: str) -> str | None:
        for sel in ("div.article_source_col", "div.article_source_coll",
                    "div.article_source_colll", self.sel.get("source") or ""):
            if not sel:
                continue
            node = soup.select_one(sel)
            if node:
                m = ISSUER_RE.search(self.text_of(node))
                if m:
                    return m.group(1).strip()
        m = ISSUER_RE.search(page_text)
        return m.group(1).strip() if m else None

    def _pick_publish_date(self, soup, page_text: str) -> str | None:
        for sel in ("div.article_source_col", "div.article_source_coll",
                    "div.article_source_colll", self.sel.get("source") or ""):
            if not sel:
                continue
            node = soup.select_one(sel)
            if not node:
                continue
            m = PUB_TIME_RE.search(self.text_of(node))
            if m:
                return parse_date(m.group(1))
        m = PUB_TIME_RE.search(page_text)
        return parse_date(m.group(1)) if m else None

    def _attachments(self, soup, raw_html: str, url: str) -> list[str]:
        """附件地址：JS 的 `var apps` 数组 + 未被注释掉的 `<a href>`。

        新模板的下载区是 avalon 渲染的，HTML 里的 `<a>` 被 `<!-- -->` 注释掉了，
        单靠 CSS 选择器会一条都拿不到。注释掉的 a 会被 lxml 丢弃，所以这里
        先扫 JS 数组，再扫仍存活的 a 标签，两边合并去重。
        """
        out: list[str] = []

        m = APPS_RE.search(raw_html or "")
        if m:
            for name, href in APP_ITEM_RE.findall(m.group(1)):
                href = (href or "").strip()
                if href:
                    out.append(urljoin(url, href))

        for a in soup.select("a[href]"):
            href = a["href"].strip()
            if not href or href.startswith(("javascript:", "#", "mailto:")):
                continue
            if ATTACH_RE.search(href):
                out.append(urljoin(url, href))

        return list(dict.fromkeys(out))

    @staticmethod
    def _first(pattern: re.Pattern, text: str) -> str | None:
        if not text:
            return None
        m = pattern.search(text)
        if not m:
            return None
        for g in m.groups():
            if g:
                return g.strip()
        return None

    def _doc_no_from_desc(self, doc: dict) -> str | None:
        """列表没有独立文号字段，偶尔能从附件标题里捡到，捡不到就留空，
        由详情页的 `文 号：` 行补上 —— 不猜、不编。"""
        for t in (doc.get("filedesc") or "").split(";"):
            m = DOC_NO_RE.search(t) or re.search(
                r"([\u4e00-\u9fa5]{2,10}规文?〔\d{4}〕\d{1,4}号)", t)
            if m:
                return m.group(1).strip()
        return None
