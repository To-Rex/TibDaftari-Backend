"""QR authenticity element — pure renderer tests (no DB)."""
from __future__ import annotations

from app.modules.templates.renderer import context as ctx_mod
from app.modules.templates.renderer.pdf import render
from app.modules.templates.schemas import validate_doc


def _doc_with_qr(value: str) -> dict:
    return {
        "paper": "A4",
        "orientation": "portrait",
        "background": "#ffffff",
        "margin": 40,
        "elements": [{"id": "qr1", "type": "qr", "x": 40, "y": 40, "w": 160, "h": 160, "value": value}],
    }


def _ctx(doc_link: str | None) -> dict:
    return ctx_mod.build_render_context(company={"name": "Klinika"}, doc_link=doc_link)


def test_validate_doc_accepts_qr() -> None:
    validated = validate_doc(_doc_with_qr("{doc.link}"))
    assert validated["elements"][0]["type"] == "qr"


def test_render_qr_embeds_the_link() -> None:
    pdf = render(_doc_with_qr("{doc.link}"), _ctx("https://temo.uz/d/TOKEN1234567890abc"))
    assert pdf.startswith(b"%PDF")
    # a rendered QR adds noticeably more than the blank-page baseline (~7.9 KB)
    assert len(pdf) > 9500


def test_render_qr_empty_link_is_skipped() -> None:
    # no doc.link in the context → the element renders nothing, the page still renders
    pdf = render(_doc_with_qr("{doc.link}"), _ctx(None))
    assert pdf.startswith(b"%PDF")
    assert len(pdf) < 9000  # nothing beyond the blank page


def test_render_qr_custom_value() -> None:
    pdf = render(_doc_with_qr("https://example.com/check"), _ctx(None))
    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 9500


def test_context_carries_doc_link() -> None:
    assert _ctx("https://temo.uz/d/abc")["doc"]["link"] == "https://temo.uz/d/abc"
    assert "doc" not in _ctx(None)
