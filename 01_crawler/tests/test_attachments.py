"""附件解析：格式判型与二进制 Word 正文提取。

这里的样本是**真实字节前缀**，不是伪造的桩：`.doc` 用真实的 OLE2 签名，
OFD 用真实 zip 结构。伪造字节测不出格式判定的真问题。
"""
from __future__ import annotations

import io
import zipfile

import pytest

from crawler.parsers.attachments import _guess_ext, parse_bytes


def _minimal_ole2() -> bytes:
    """OLE2 文件头 + 少量占位，够走「是不是复合文档」这一层判断。"""
    return b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504


def _zip_ofd() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("OFD.xml", "<ofd:OFD></ofd:OFD>")
        zf.writestr(
            "Doc_0/Pages/Page_0.xml",
            '<ofd:Page><ofd:Content><ofd:TextObject>'
            '<ofd:TextCode>第一条 为规范旅游景区管理</ofd:TextCode>'
            '<ofd:TextCode>第二条 质量等级划分为五个等级</ofd:TextCode>'
            "</ofd:TextObject></ofd:Content></ofd:Page>")
    return buf.getvalue()


def test_guess_ext_prefers_magic_over_filename():
    assert _guess_ext(b"%PDF-1.7 x") == ".pdf"
    assert _guess_ext(_minimal_ole2()) == ".doc"
    assert _guess_ext(b"PK\x03\x04") == ".docx"
    assert _guess_ext(b"plain text") == ""


def test_doc_without_ole_magic_is_rejected_not_silently_empty():
    r = parse_bytes(b"not a word file", filename="x.doc")
    assert r.text == ""
    assert "OLE2" in r.error


def test_minimal_ole2_reports_missing_stream():
    r = parse_bytes(_minimal_ole2(), filename="x.doc")
    assert r.text == ""
    assert r.error


def test_wps_extension_routes_to_doc_parser():
    r = parse_bytes(b"junk", filename="a.wps")
    # 走的是 parse_doc，因此错误信息是 OLE2 相关，而不是「无法识别附件类型」
    assert "无法识别附件类型" not in r.error


def test_ofd_text_is_extracted():
    r = parse_bytes(_zip_ofd(), filename="a.ofd")
    assert r.error == ""
    assert "质量等级划分为五个等级" in r.text
    assert r.text.index("第一条") < r.text.index("第二条")


def test_zip_without_ofd_marker_is_not_treated_as_ofd():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", "<w:document/>")
    r = parse_bytes(buf.getvalue(), filename="unknown.bin")
    # 按 zip 内容判成 docx，而不是 ofd
    assert r.kind == "docx"


def test_unknown_binary_reports_type():
    r = parse_bytes(b"\x00\x01\x02\x03", filename="a.xyz")
    assert "无法识别附件类型" in r.error


@pytest.mark.parametrize("name,kind", [
    ("x.pdf", "pdf"),
    ("x.doc", "doc"),
])
def test_extension_routes_to_expected_parser(name, kind):
    """扩展名与 magic 一致时不能被内部再分逻辑改判到别的解析器。

    字节本身是残缺的，解析必然报错——这里只断言「派发到了正确的解析器」，
    错误信息由具体解析器给，不在这里断言。
    """
    magic = {"x.pdf": b"%PDF-1.4", "x.doc": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"}[name]
    r = parse_bytes(magic + b"\x00" * 64, filename=name)
    assert r.kind == kind
