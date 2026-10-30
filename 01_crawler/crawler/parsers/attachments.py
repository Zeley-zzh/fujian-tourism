"""附件解析：PDF / DOCX / XLSX / CSV → 纯文本 + 页码/标题层级。

页码与层级都要保留，因为 RAG 侧引用要能跳回原文位置，
而「跳回」在 PDF 里只能靠页码、在 HTML 里只能靠锚点。
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ParsedDoc:
    text: str
    pages: list[str] = field(default_factory=list)      # 每页文本（PDF）
    headings: list[tuple[str, int]] = field(default_factory=list)  # (标题, 起始偏移)
    tables: list[list[list[str]]] = field(default_factory=list)
    kind: str = "text"
    error: str = ""


def parse_pdf(data: bytes) -> ParsedDoc:
    try:
        from pypdf import PdfReader
    except ImportError:
        return ParsedDoc(text="", kind="pdf", error="pypdf 未安装")
    try:
        reader = PdfReader(io.BytesIO(data))
        pages: list[str] = []
        for page in reader.pages:
            try:
                pages.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001
                pages.append("")
        text = "\n\n".join(p.strip() for p in pages if p.strip())
        return ParsedDoc(text=_normalize(text), pages=pages, kind="pdf")
    except Exception as e:  # noqa: BLE001
        return ParsedDoc(text="", kind="pdf", error=f"PDF 解析失败: {e}")


def parse_docx(data: bytes) -> ParsedDoc:
    try:
        import docx  # python-docx
    except ImportError:
        return ParsedDoc(text="", kind="docx", error="python-docx 未安装")
    try:
        doc = docx.Document(io.BytesIO(data))
        lines: list[str] = []
        headings: list[tuple[str, int]] = []
        offset = 0
        for para in doc.paragraphs:
            t = (para.text or "").strip()
            if not t:
                continue
            style = (para.style.name or "").lower()
            if "heading" in style or re.match(r"^[一二三四五六七八九十]+、", t):
                headings.append((t, offset))
            lines.append(t)
            offset += len(t) + 1
        tables = []
        for tb in doc.tables:
            rows = [[c.text.strip() for c in row.cells] for row in tb.rows]
            tables.append(rows)
            lines.append("\n".join(" | ".join(r) for r in rows))
        return ParsedDoc(text=_normalize("\n".join(lines)), headings=headings,
                         tables=tables, kind="docx")
    except Exception as e:  # noqa: BLE001
        return ParsedDoc(text="", kind="docx", error=f"DOCX 解析失败: {e}")


def parse_xlsx(data: bytes) -> ParsedDoc:
    try:
        import openpyxl
    except ImportError:
        return ParsedDoc(text="", kind="xlsx", error="openpyxl 未安装")
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        lines: list[str] = []
        tables: list[list[list[str]]] = []
        for ws in wb.worksheets:
            rows: list[list[str]] = []
            lines.append(f"# 工作表：{ws.title}")
            for row in ws.iter_rows(values_only=True):
                cells = ["" if c is None else str(c).strip() for c in row]
                if not any(cells):
                    continue
                rows.append(cells)
                lines.append(" | ".join(cells))
            if rows:
                tables.append(rows)
        wb.close()
        return ParsedDoc(text=_normalize("\n".join(lines)), tables=tables, kind="xlsx")
    except Exception as e:  # noqa: BLE001
        return ParsedDoc(text="", kind="xlsx", error=f"XLSX 解析失败: {e}")


def parse_csv(data: bytes) -> ParsedDoc:
    for enc in ("utf-8-sig", "gb18030", "utf-8"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        return ParsedDoc(text="", kind="csv", error="CSV 编码无法识别")
    try:
        rows = list(csv.reader(io.StringIO(text)))
        lines = [" | ".join(r) for r in rows if any(c.strip() for c in r)]
        return ParsedDoc(text=_normalize("\n".join(lines)), tables=[rows], kind="csv")
    except Exception as e:  # noqa: BLE001
        return ParsedDoc(text="", kind="csv", error=f"CSV 解析失败: {e}")


def parse_doc(data: bytes) -> ParsedDoc:
    """Word 97-2003 二进制（OLE2 复合文档）正文提取。

    走 piece table 而不是「扫可打印字符」：后者会把页眉、样式名、修订记录一起
    捞进来，切出来的段落顺序也是乱的。piece table 是 Word 自己记录「哪个区间
    对应正文第几段」的索引，按它解出来的顺序就是阅读顺序。

    关键结构（MS-DOC）：
      WordDocument 流的 FIB → fWhichTblStm 决定用 0Table 还是 1Table
      → 该流的 Clx（偏移 0x01A2 / 长度 0x01A6）→ Pcdt → PlcPcd
      → 每段 PCD 的 fc 高位 0x40000000 标记「单字节 CP1252」否则 UTF-16LE
    """
    try:
        import olefile
    except ImportError:
        return ParsedDoc(text="", kind="doc", error="olefile 未安装")
    if data[:4] != b"\xd0\xcf\x11\xe0":
        return ParsedDoc(text="", kind="doc", error="不是 OLE2 复合文档")
    try:
        ole = olefile.OleFileIO(io.BytesIO(data))
    except Exception as e:  # noqa: BLE001
        return ParsedDoc(text="", kind="doc", error=f"OLE2 打开失败: {e}")
    try:
        if not ole.exists("WordDocument"):
            return ParsedDoc(text="", kind="doc", error="缺少 WordDocument 流")
        wd = ole.openstream("WordDocument").read()

        # fWhichTblStm 是 FIB 基础区 0x0A 处的第 9 位
        flags = int.from_bytes(wd[0x0A:0x0C], "little")
        table_name = "1Table" if (flags & 0x0200) else "0Table"
        if not ole.exists(table_name):
            table_name = "0Table" if table_name == "1Table" else "1Table"
        if not ole.exists(table_name):
            return ParsedDoc(text="", kind="doc", error="缺少 Table 流")
        tbl = ole.openstream(table_name).read()

        fc_clx = int.from_bytes(wd[0x01A2:0x01A6], "little")
        lcb_clx = int.from_bytes(wd[0x01A6:0x01AA], "little")
        if not lcb_clx or fc_clx + lcb_clx > len(tbl):
            return ParsedDoc(text="", kind="doc", error="Clx 偏移越界")
        clx = tbl[fc_clx:fc_clx + lcb_clx]

        text = _doc_text_from_clx(wd, clx)
        if not text.strip():
            return ParsedDoc(text="", kind="doc", error="piece table 未解出正文")
        return ParsedDoc(text=_normalize(text), kind="doc")
    except Exception as e:  # noqa: BLE001
        return ParsedDoc(text="", kind="doc", error=f"DOC 解析失败: {e}")
    finally:
        ole.close()


def _doc_text_from_clx(wd: bytes, clx: bytes) -> str:
    """在 Clx 里跳过 Prc 块，找到 0x02 开头的 Pcdt，再解 PlcPcd。"""
    i = 0
    while i < len(clx):
        tag = clx[i]
        if tag == 0x01:            # Prc：2 字节大小 + 内容，跳过
            size = int.from_bytes(clx[i + 1:i + 3], "little")
            i += 3 + size
        elif tag == 0x02:          # Pcdt
            lcb = int.from_bytes(clx[i + 1:i + 5], "little")
            return _doc_text_from_pcdt(wd, clx[i + 5:i + 5 + lcb])
        else:
            break
    return ""


def _doc_text_from_pcdt(wd: bytes, plc: bytes) -> str:
    # PlcPcd = (n+1) 个 4 字节 CP + n 个 8 字节 PCD，反推 n
    if len(plc) < 16:
        return ""
    n = (len(plc) - 4) // 12
    if n <= 0:
        return ""
    cps = [int.from_bytes(plc[k * 4:k * 4 + 4], "little") for k in range(n + 1)]
    pcd_off = (n + 1) * 4
    out: list[str] = []
    for k in range(n):
        pcd = plc[pcd_off + k * 8: pcd_off + k * 8 + 8]
        if len(pcd) < 8:
            break
        fc = int.from_bytes(pcd[2:6], "little")
        compressed = bool(fc & 0x40000000)
        fc = fc & 0x3FFFFFFF
        nchars = cps[k + 1] - cps[k]
        if nchars <= 0:
            continue
        if compressed:
            start = fc // 2
            raw = wd[start:start + nchars]
            out.append(raw.decode("cp1252", errors="replace"))
        else:
            raw = wd[fc:fc + nchars * 2]
            out.append(raw.decode("utf-16-le", errors="replace"))
    text = "".join(out)
    # Word 用 \r 分段、\x07 标记表格单元与行尾、\x0c 分页，换成真实换行
    text = text.replace("\x07", "\t").replace("\r", "\n").replace("\x0c", "\n")
    text = re.sub(r"[\x00-\x08\x0b\x0e-\x1f]", "", text)
    return text


def parse_ofd(data: bytes) -> ParsedDoc:
    """OFD 是 zip + XML（国产版式文档）。取每页 TextCode 的文字，按出现顺序拼接。

    版式文档没有「段落」概念，文字坐标散落，只能按 XML 文档顺序还原，
    因此这里宁可输出稍碎也不做重排——重排会编造原文没有的顺序。
    """
    import zipfile
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except Exception as e:  # noqa: BLE001
        return ParsedDoc(text="", kind="ofd", error=f"OFD 不是有效 zip: {e}")
    try:
        pages = sorted(n for n in zf.namelist()
                       if re.search(r"Pages/Page_\d+\.xml$", n, re.I))
        if not pages:
            return ParsedDoc(text="", kind="ofd", error="OFD 内没有页面 XML")
        from bs4 import BeautifulSoup
        lines: list[str] = []
        for name in pages:
            soup = BeautifulSoup(zf.read(name), "xml")
            for node in soup.find_all("TextCode"):
                t = (node.get_text() or "").strip()
                if t:
                    lines.append(t)
        return ParsedDoc(text=_normalize("\n".join(lines)), kind="ofd")
    except Exception as e:  # noqa: BLE001
        return ParsedDoc(text="", kind="ofd", error=f"OFD 解析失败: {e}")
    finally:
        zf.close()


DISPATCH = {
    ".pdf": parse_pdf,
    ".docx": parse_docx,
    ".doc": parse_doc,
    ".wps": parse_doc,          # WPS 文字默认存成 OLE2，与 .doc 同构
    ".ofd": parse_ofd,
    ".xlsx": parse_xlsx,
    ".xlsm": parse_xlsx,
    ".csv": parse_csv,
}


def _guess_ext(data: bytes) -> str:
    """按 magic 判类型：站点给的扩展名经常与真实字节不符（.wps/.doc 混用）。"""
    if data[:4] == b"%PDF":
        return ".pdf"
    if data[:4] == b"\xd0\xcf\x11\xe0":
        return ".doc"          # OLE2：老 Word / WPS
    if data[:2] == b"PK":
        return ".docx"         # zip：docx/xlsx/ofd 都在里面，具体看后续
    return ""


def parse_file(path: Path) -> ParsedDoc:
    return parse_bytes(path.read_bytes(), filename=path.name)


def _normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\u3000]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_bytes(data: bytes, *, filename: str = "") -> ParsedDoc:
    """给内存里的附件字节用（抓取器直接拿到 bytes 时走这条）。

    先信 magic 再信扩展名：政务站常见 `xxx.doc` 实为 docx、`xxx.wps` 实为 OLE2。
    """
    ext = (Path(filename).suffix.lower() if filename else "")
    if data[:2] == b"PK" and ext in (".doc", ".docx", ".ofd", ".bin", ""):
        # zip 家族内部再分：OFD 有 OFD.xml，docx 有 word/document.xml
        import zipfile
        try:
            names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        except Exception:  # noqa: BLE001
            names = []
        if any(n.lower().endswith("ofd.xml") for n in names):
            ext = ".ofd"
        elif any(n.lower() == "word/document.xml" for n in names):
            ext = ".docx"
        elif any(n.lower() == "xl/workbook.xml" for n in names):
            ext = ".xlsx"
    if ext not in DISPATCH:
        # 扩展名缺失或不认识时按 magic 兜底，避免把 docx 当成未知类型丢掉
        ext = _guess_ext(data) or ext
    fn = DISPATCH.get(ext)
    if fn:
        return fn(data)
    return ParsedDoc(text="", kind="unknown", error=f"无法识别附件类型: {filename or ext}")
