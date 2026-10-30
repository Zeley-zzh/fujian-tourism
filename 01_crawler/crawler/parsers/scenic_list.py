"""景区名录解析器：从公开名录表/清单里抽景区档案。

政务名录常见三种形态：HTML table、ul 列表、附带 Excel 附件。
这个解析器覆盖前两种，Excel 交给 attachments.py。
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..utils import clean_text, normalize_url, parse_date
from .base import BaseParser, DocRecord

LEVEL_RE = re.compile(r"(\dA|AAAAA|AAAA|AAA|5A|4A|3A|2A|1A|国家级|省级)", re.I)
CITY_LIST = ["福州", "厦门", "漳州", "泉州", "三明", "莆田", "南平", "龙岩", "宁德", "平潭"]


class ScenicListParser(BaseParser):
    name = "scenic_list"

    def parse_list(self, html: str, page_url: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        items: list[dict] = []
        seen: set[str] = set()

        # 形态 A：表格
        for tr in soup.select("table tr"):
            tds = tr.find_all("td")
            if len(tds) < 2:
                continue
            texts = [clean_text(td.get_text(" ", strip=True)) for td in tds]
            a = tr.find("a", href=True)
            name = texts[1] if len(texts) > 1 else texts[0]
            if not name or len(name) < 2:
                continue
            url = (normalize_url(urljoin(page_url, a["href"])) if a
                   # 名录表常常整行没有链接：用锚点保证每行独立，否则会被 URL 去重塌成一条
                   else f"{page_url}#spot={name}")
            if url in seen:
                continue
            seen.add(url)
            items.append({
                "url": url,
                "title": name,
                "date": parse_date(" ".join(texts)),
                "row": texts,
            })

        # 形态 B：ul/li 列表
        if not items:
            for li in soup.select("ul li, div.list-item"):
                a = li.find("a", href=True)
                if not a:
                    continue
                url = normalize_url(urljoin(page_url, a["href"]))
                if url in seen:
                    continue
                seen.add(url)
                items.append({
                    "url": url,
                    "title": clean_text(a.get_text(" ", strip=True)),
                    "date": parse_date(clean_text(li.get_text(" ", strip=True))),
                })
        return items

    def parse_detail(self, html: str, url: str) -> DocRecord:
        soup = BeautifulSoup(html, "lxml")
        text = clean_text(soup.get_text("\n", strip=True))

        title = ""
        if self.sel.get("title"):
            title = self.text_of(soup.select_one(self.sel["title"]))
        if not title:
            node = soup.select_one("h1") or soup.select_one("title")
            title = self.text_of(node)

        body = soup.select_one(self.sel.get("body") or "div.article-content") or soup.body or soup
        for junk in body.select("script, style, nav, .share"):
            junk.decompose()
        content_text = clean_text(body.get_text("\n", strip=True))

        level = None
        m = LEVEL_RE.search(text)
        if m:
            raw = m.group(1).upper()
            level = {"AAAAA": "5A", "AAAA": "4A", "AAA": "3A"}.get(raw, raw)

        city = next((c for c in CITY_LIST if c in text), None)
        address = self.first_match(
            [r"地址[：:]\s*([^\n。；]{4,60})", r"位于([^\n。；]{4,60})"], content_text)
        lng = self.first_match([r"经度[：:]\s*(\d{2,3}\.\d+)"], content_text)
        lat = self.first_match([r"纬度[：:]\s*(\d{1,2}\.\d+)"], content_text)

        return DocRecord(
            url=url,
            title=title or "[无标题]",
            content_text=content_text,
            content_html=str(body),
            doc_type="attachment",
            publish_date=parse_date(self.first_match(
                [r"(\d{4}-\d{2}-\d{2})", r"(\d{4}年\d{1,2}月\d{1,2}日)"], text)),
            extra={
                "level": level,
                "city": city,
                "address": address,
                "lng": float(lng) if lng else None,
                "lat": float(lat) if lat else None,
                "spot_name": title,
            },
        )
