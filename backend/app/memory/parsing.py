"""把上传的文件变成文本。

在此之前 upload 只接受 UTF-8：PDF 传上去直接 415，而 mime 字段记了却从不
参与任何判断。知识库里最常见的恰恰是 PDF 和 Word。

PDF/DOCX 的解析库走可选依赖（pyproject 的 [docs]），和 [db]、[microvm] 一样
——它们加起来几十兆，而只用纯文本的人不该为此付出安装时间。没装时给的报错
要能照着做，不是一句 ImportError。

HTML 用已经在依赖里的 beautifulsoup4，不额外引入东西。
"""

from __future__ import annotations

import io
import re

#: 认得出的扩展名 → 处理方式。mime 不可靠（浏览器给 .md 报 application/octet-stream）
_PDF = {".pdf"}
_DOCX = {".docx"}
_HTML = {".html", ".htm"}


class UnsupportedDocument(ValueError):
    """认不出或解析不了。message 直接给用户看。"""


def _ext(filename: str) -> str:
    idx = filename.rfind(".")
    return filename[idx:].lower() if idx >= 0 else ""


def _from_pdf(raw: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise UnsupportedDocument(
            "要读 PDF 得先装解析库：pip install 'agentlab-backend[docs]'"
        ) from e
    try:
        reader = PdfReader(io.BytesIO(raw))
    except Exception as e:  # noqa: BLE001
        raise UnsupportedDocument(f"这个 PDF 读不开：{type(e).__name__}: {e}") from e

    # 按页拼，页与页之间留空行——切块是按段落切的，页边界正好是天然的段落边界
    pages = []
    for i, page in enumerate(reader.pages):
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001 - 单页坏了不该毁掉整份文档
            pages.append(f"（第 {i + 1} 页解析失败）")
    # PDF 抽出来的每行都拖着排版留下的填充空格，不清掉会混进切块和词频统计
    cleaned = []
    for page in pages:
        lines = [ln.rstrip() for ln in page.splitlines()]
        body = "\n".join(ln for ln in lines if ln.strip())
        if body:
            cleaned.append(body)
    text = "\n\n".join(cleaned)
    if not text.strip():
        raise UnsupportedDocument(
            "这个 PDF 里没有可提取的文字——多半是扫描件。"
            "扫描件要先过 OCR，这里不做图像识别。"
        )
    return text


def _from_docx(raw: bytes) -> str:
    try:
        import docx
    except ImportError as e:
        raise UnsupportedDocument(
            "要读 Word 得先装解析库：pip install 'agentlab-backend[docs]'"
        ) from e
    try:
        doc = docx.Document(io.BytesIO(raw))
    except Exception as e:  # noqa: BLE001
        raise UnsupportedDocument(f"这个 Word 文档读不开：{type(e).__name__}: {e}") from e

    parts = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    # 表格也要：合同、口径说明这类文档的关键信息常常整个在表里，
    # 只取段落等于把它们全丢了
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    text = "\n\n".join(parts)
    if not text.strip():
        raise UnsupportedDocument("这个 Word 文档里没有文字内容")
    return text


def _from_html(raw: bytes) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(raw.decode("utf-8", errors="replace"), "html.parser")
    for tag in soup(["script", "style", "nav", "footer"]):
        tag.decompose()
    text = soup.get_text("\n")
    # 连续空行压成一个：HTML 转出来到处是空行，会把切块的段落判断搅乱
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def extract(raw: bytes, filename: str, mime: str = "") -> str:
    """文件字节 → 文本。认不出就按 UTF-8 文本试。"""
    ext = _ext(filename)
    if ext in _PDF or mime == "application/pdf":
        return _from_pdf(raw)
    if ext in _DOCX or "wordprocessingml" in mime:
        return _from_docx(raw)
    if ext in _HTML or mime == "text/html":
        return _from_html(raw)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise UnsupportedDocument(
            f"读不了 {filename or '这个文件'}：它不是 UTF-8 文本，"
            f"也不是认得出的 PDF / Word / HTML。"
        ) from e
