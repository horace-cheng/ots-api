import logging
import os
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, select_autoescape

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"

_env: Environment | None = None


def _get_env() -> Environment:
    global _env
    if _env is None:
        _env = Environment(
            loader=FileSystemLoader(str(TEMPLATE_DIR)),
            autoescape=select_autoescape(["html", "xml"]),
        )
    return _env


def render_template(
    lang: str,
    event_type: str,
    context: dict | None = None,
) -> tuple[str, str]:
    ctx = context or {}
    template_path = f"{lang}/{event_type}.html"
    logger.info(f"Rendering HTML template: {template_path}")
    html = _get_env().get_template(template_path).render(**ctx)
    try:
        text_path = f"{lang}/{event_type}.txt"
        text = _get_env().get_template(text_path).render(**ctx)
        logger.info(f"Text template rendered: {text_path} ({len(text)} chars)")
    except Exception:
        text = ""
    return html, text
