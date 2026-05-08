"""
services/document_converter.py

Convert uploaded documents (TXT, MD, DOCX, PDF, CSV) to HTML
for the in-browser original content viewer.
"""

import io
import re
from dataclasses import dataclass


@dataclass
class DocumentContent:
    filename: str
    content_type: str
    html: str
    text: str


def convert_document(raw_bytes: bytes, filename: str) -> DocumentContent:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    content_type = _infer_content_type(ext)

    if ext == "md":
        return _convert_markdown(raw_bytes, filename, content_type)
    elif ext == "docx":
        return _convert_docx(raw_bytes, filename, content_type)
    elif ext == "pdf":
        return _convert_pdf(raw_bytes, filename, content_type)
    elif ext in ("csv", "tsv"):
        return _convert_text(raw_bytes, filename, content_type, pre=True)
    elif ext in ("txt", ""):
        return _convert_text(raw_bytes, filename, content_type, pre=True)
    else:
        return _convert_text(raw_bytes, filename, content_type, pre=True)


def _infer_content_type(ext: str) -> str:
    mapping = {
        "txt":  "text/plain",
        "md":   "text/markdown",
        "csv":  "text/csv",
        "tsv":  "text/tab-separated-values",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "pdf":  "application/pdf",
    }
    return mapping.get(ext, "text/plain")


def _decode_text(raw_bytes: bytes) -> str:
    try:
        return raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return raw_bytes.decode("big5", errors="replace")


def _convert_text(raw_bytes: bytes, filename: str, content_type: str, pre: bool = False) -> DocumentContent:
    text = _decode_text(raw_bytes)
    html = f"<pre class=\"doc-content\">{_escape_html(text)}</pre>" if pre else _escape_html(text)
    return DocumentContent(filename=filename, content_type=content_type, html=html, text=text)


def _convert_markdown(raw_bytes: bytes, filename: str, content_type: str) -> DocumentContent:
    import markdown as md_lib
    text = _decode_text(raw_bytes)
    html = md_lib.markdown(text, extensions=["fenced_code", "tables"])
    return DocumentContent(filename=filename, content_type=content_type, html=html, text=text)


def _convert_docx(raw_bytes: bytes, filename: str, content_type: str) -> DocumentContent:
    import mammoth
    result = mammoth.convert_to_html(io.BytesIO(raw_bytes))
    html = result.value or "<p>(empty document)</p>"
    # Extract plain text from mammoth HTML for the text field
    text = re.sub(r"<[^>]+>", "", html)
    text = re.sub(r"\s+", " ", text).strip()
    return DocumentContent(filename=filename, content_type=content_type, html=html, text=text)


def _convert_pdf(raw_bytes: bytes, filename: str, content_type: str) -> DocumentContent:
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTTextBox, LTTextLine, LTChar, LTAnno

    paragraphs = []
    for page_layout in extract_pages(io.BytesIO(raw_bytes)):
        for element in page_layout:
            if isinstance(element, (LTTextBox, LTTextLine)):
                text = element.get_text().strip()
                if text:
                    paragraphs.append(text)

    text = "\n\n".join(paragraphs)
    html = "".join(f"<p>{_escape_html(p)}</p>" for p in paragraphs)
    return DocumentContent(filename=filename, content_type=content_type, html=html, text=text)


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
