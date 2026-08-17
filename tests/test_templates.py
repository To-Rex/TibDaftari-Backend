"""Templates module: renderer unit tests (pure), a legacy-template render smoke test, and API tests
against the real dev DB (fixture rows prefixed T-templates-)."""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from app.core.config import settings
from app.core.permissions import COMPANY_ADMIN_PERMISSIONS
from app.core.security import hash_password
from app.infrastructure.db.models import (
    AttributeSchema,
    Branch,
    Category,
    Company,
    Employee,
    Order,
    OrderItem,
    Patient,
    Role,
)
from app.infrastructure.db.session import engine, session_scope
from app.modules.files.service import decode_data_url
from app.modules.templates import service as tpl_service
from app.modules.templates.renderer import build_render_context, render
from app.modules.templates.renderer import expressions as ex
from app.modules.templates.service import sample_values
from fastapi.testclient import TestClient

settings.workers_enabled = False
settings.telegram_enabled = False

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "tests" / "out"
SFX = uuid.uuid4().hex[:8]
PASSWORD = "T-templates-pass-1"

# 1x1 red PNG
PNG_1PX = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg==")

SCHEMA = {
    "id": "s1",
    "fields": [
        {"key": "hb", "label": "Gemoglobin", "type": "number", "unit": "g/l", "decimals": 2, "references": [{"gender": "female", "min": 120, "max": 140}, {"min": 130, "max": 160}]},
        {"key": "wbc", "label": "Leykotsitlar", "type": "number", "decimals": 0, "references": [{"min": 4, "max": 9}]},
        {"key": "res", "label": "Natija", "type": "select", "options": [{"value": "neg", "label": "Manfiy"}, {"value": "pos", "label": "Musbat", "flag": "abnormal"}]},
        {"key": "flags", "label": "Belgilar", "type": "multiselect", "options": [{"value": "a", "label": "A"}, {"value": "b", "label": "B"}]},
        {"key": "ok", "label": "Sifat", "type": "boolean"},
        {"key": "rows", "label": "Jadval", "type": "table", "columns": [{"key": "name", "type": "text"}, {"key": "v", "type": "number", "references": [{"min": 1, "max": 5}]}], "presetRows": []},
    ],
}


def _ctx(values: dict, items: list | None = None) -> dict:
    return build_render_context(
        patient={"fullName": "Test Bemor", "phone": "998901234567", "birthDate": "1990-01-15", "gender": "female"},
        order={"number": "UR-000001", "createdAt": "2026-08-16T05:00:00Z"},
        item={"serviceName": "UQT", "approvedAt": "2026-08-16T07:30:00Z", "technicianName": "Lab", "doctorName": "Doc", "values": values},
        company={"name": "Shifo Med", "phone": "+998 62 228-82-81"},
        branch={"name": "Markaziy"},
        category={"name": "Gematologiya", "phone": "97-092-08-88"},
        schema=SCHEMA,
        district_name="Urganch shahri",
        items=items,
        language="uz",
    )


# ----------------------------------------------------------------------------- unit: expressions


def test_format_value_and_interpolate() -> None:
    ctx = _ctx({"hb": 12.00, "wbc": 7.6, "res": "pos", "flags": ["a", "b"], "ok": True, "rows": [{"name": "x", "v": 9}]})
    assert ex.format_value(ctx, "hb") == "12"
    assert ex.format_value(ctx, "wbc") == "8"  # toFixed(0) rounds half up
    assert ex.format_value(ctx, "res") == "Musbat"
    assert ex.format_value(ctx, "flags") == "A, B"
    assert ex.format_value(ctx, "ok") == "Ha"
    assert ex.format_value(ctx, "rows") == ""  # documented divergence (JS: [object Object])
    assert ex.format_value(ctx, "missing") == ""
    ctx2 = _ctx({"hb": 12.30})
    assert ex.format_value(ctx2, "hb") == "12.30"
    text = "Bemor: {patient.fullName} ({patient.gender}, {patient.age}) · {order.number} · Hb {values.hb} · {unknown.path} {not a ph}"
    assert ex.interpolate(text, ctx) == "Bemor: Test Bemor (Ayol, 36) · UR-000001 · Hb 12 ·  {not a ph}"
    assert ctx["patient"]["phone"] == "+998 90 123-45-67" and ctx["patient"]["birthDate"] == "15.01.1990"
    assert ctx["patient"]["address"] == "Urganch shahri" and ctx["order"]["date"] == "16.08.2026"
    assert ctx["item"]["approvedAt"] == "16.08.2026 12:30"
    # row context: {i} and {row.x}
    assert ex.interpolate("{i}. {row.name} = {row.v}", ctx, {"name": "x", "v": 9, "__i": 1}) == "1. x = 9"
    # field helpers
    assert ex.field_flag(ctx, "hb") == "abnormal" and ex.field_flag(ctx, "res") == "abnormal" and ex.field_flag(ctx, "wbc") == "normal"
    assert ex.field_reference(ctx, "hb") == "120 – 140" and ex.field_unit(ctx, "hb") == "g/l"
    assert ex.reference_text({"references": [{"min": 4}]}, None) == "≥ 4"
    assert ex.reference_text({"references": [{"text": "yo‘q"}]}, None) == "yo‘q"


def test_show_if_presence_and_service_value() -> None:
    ctx = _ctx({"hb": 0})
    assert ex.show_if("{values.hb}", ctx) is True  # '0' counts as present
    assert ex.show_if("{values.wbc}", ctx) is False
    assert ex.show_if("{row.natija}", ctx, {"natija": "  "}) is False
    assert ex.show_if("{row.natija}", ctx, {"natija": "—"}) is True
    items = [
        {"code": "LG-85", "serviceTypeId": "st1", "serviceName": "HBsAg", "status": "approved", "values": {"res": "pos", "hb": 130}, "schema": SCHEMA, "approvedAt": "16.08.2026 12:30", "doctor": "Dr"},
        {"code": "LG-86", "serviceTypeId": "st2", "serviceName": "HCV", "status": "submitted", "values": {"res": "neg"}, "schema": SCHEMA},
    ]
    ctx = _ctx({}, items=items)
    assert ex.service_value(ctx, "lg-85.res") == "Musbat"
    assert ex.service_value(ctx, "LG-85.name") == "HBsAg" and ex.service_value(ctx, "LG-85.doctor") == "Dr"
    assert ex.service_value(ctx, "LG-86.technician") == "" and ex.service_value(ctx, "LG-99.res") == ""
    assert ex.interpolate("{svc.LG-85.hb} {svc.LG-86.status}", ctx) == "130 submitted"
    rows = ex.table_rows(ctx, "items")
    assert [r["i"] for r in rows] == [1, 2] and rows[0]["name"] == "HBsAg" and rows[0]["res"] == "Musbat" and "rows" not in rows[0]


# ----------------------------------------------------------------------------- render: legacy template


def _legacy_loader(assets: dict[str, dict]):
    def load(asset_id: str):
        a = assets.get(asset_id)
        if not a:
            return None
        url = a["url"]
        if url.startswith("/legacy/"):
            p = ROOT / "seed" / "assets" / url[len("/legacy/") :]
            return p.read_bytes(), mimetypes.guess_type(str(p))[0] or "image/png"
        return decode_data_url(url)

    return load


def test_render_legacy_template_to_pdf() -> None:
    core = json.loads((ROOT / "seed" / "demo" / "core.json").read_text(encoding="utf-8"))
    tpl = next(t for t in core["templates"] if t["id"] == "tpl_andoza_parazitologiya")
    st = next(s for s in core["serviceTypes"] if s["id"] == tpl["serviceTypeIds"][0])
    schema = next(s for s in core["schemas"] if s["id"] == st["schemaId"])
    values = sample_values(schema)
    values["rows"][0]["natija"] = "+"
    ctx = build_render_context(
        patient={"fullName": "Karimova Madina Aziz qizi", "phone": "998901234567", "birthDate": "1992-04-12", "gender": "female", "street": "Al-Xorazmiy 12"},
        order={"number": "UR-001240", "createdAt": "2026-08-16T05:00:00Z"},
        item={"serviceName": st["name"], "approvedAt": "2026-08-16T07:30:00Z", "technicianName": "D. Rahimova", "doctorName": "A. Jumaniyazov", "values": values},
        company={"name": "Shifo Med", "phone": "+998 62 228-82-81"},
        branch={"name": "Markaziy filial", "address": "Urganch sh."},
        category={"name": "Parazitologiya", "phone": "97-092-08-88"},
        schema=schema,
        district_name="Urganch shahri",
        language=tpl["language"],
    )
    pdf = render(tpl["doc"], ctx, _legacy_loader({a["id"]: a for a in core["assets"]}))
    assert pdf[:5] == b"%PDF-" and len(pdf) > 20_000
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "sample.pdf").write_bytes(pdf)
    try:
        import pypdfium2 as pdfium
    except ImportError:  # pragma: no cover - optional inspection aid
        return
    doc = pdfium.PdfDocument(pdf)
    assert len(doc) == 1
    doc[0].render(scale=1.2).to_pil().save(OUT_DIR / "sample.png")


def test_render_all_element_types_and_broken_bits() -> None:
    doc = {
        "paper": "A5",
        "orientation": "landscape",
        "background": "#fffdf5",
        "margin": 20,
        "elements": [
            {"id": "t1", "type": "text", "x": 20, "y": 20, "w": 300, "h": 60, "text": "{company.name}\nQator 2 uzun matn uzun matn uzun matn uzun matn", "style": {"fontFamily": "serif", "fontSize": 14, "fontWeight": 700, "italic": True, "underline": True, "color": "#123456", "align": "justify", "letterSpacing": 0.5, "background": "#eef"}, "padding": 4, "rotation": 3, "opacity": 0.8},
            {"id": "f1", "type": "field", "x": 20, "y": 90, "w": 400, "h": 24, "fieldKey": "hb", "showLabel": True, "showUnit": True, "showReference": True, "highlightAbnormal": True, "style": {"fontFamily": "sans", "fontSize": 12, "fontWeight": 400, "color": "#000", "align": "left"}},
            {"id": "f2", "type": "field", "x": 20, "y": 120, "w": 400, "h": 24, "fieldKey": "rows", "showLabel": True, "showUnit": False, "showReference": False, "highlightAbnormal": False, "style": {"fontFamily": "mono", "fontSize": 12, "fontWeight": 400, "color": "#000", "align": "left"}},
            {"id": "r1", "type": "rect", "x": 450, "y": 20, "w": 100, "h": 50, "fill": "#0f7a6b", "stroke": "#000", "strokeWidth": 2, "radius": 8},
            {"id": "e1", "type": "ellipse", "x": 570, "y": 20, "w": 60, "h": 60, "stroke": "#c2413f", "strokeWidth": 1},
            {"id": "l1", "type": "line", "x": 20, "y": 150, "w": 600, "h": 0, "stroke": "#333", "strokeWidth": 1, "orientation": "horizontal", "dashed": True},
            {"id": "l2", "type": "line", "x": 640, "y": 20, "w": 0, "h": 200, "stroke": "#333", "strokeWidth": 2, "orientation": "vertical"},
            {"id": "i1", "type": "image", "x": 450, "y": 90, "w": 80, "h": 60, "fit": "cover", "src": "data:image/png;base64," + base64.b64encode(PNG_1PX).decode()},
            {"id": "i2", "type": "image", "x": 540, "y": 90, "w": 80, "h": 60, "fit": "contain", "assetId": "missing"},
            {"id": "i3", "type": "image", "x": 630, "y": 90, "w": 40, "h": 40, "fit": "fill", "src": "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='10'%3E%3Ccircle cx='5' cy='5' r='4' fill='red'/%3E%3C/svg%3E"},
            {"id": "tb", "type": "table", "x": 20, "y": 170, "w": 500, "h": 200, "fieldKey": "rows", "showHeader": True, "showRowNumber": True, "highlightAbnormal": True, "zebra": "#f5f7f6", "rowHeight": 22, "borderColor": "#c3cec9", "borderWidth": 1, "columns": [{"id": "c1", "header": "Nomi", "bind": "name", "width": 300, "align": "left"}, {"id": "c2", "header": "Qiymat", "bind": "v", "width": 100, "align": "right"}], "headerStyle": {"fontFamily": "sans", "fontSize": 10, "fontWeight": 600, "color": "#5c6b66", "align": "left"}, "cellStyle": {"fontFamily": "sans", "fontSize": 10.5, "fontWeight": 400, "color": "#14201d", "align": "left"}},
            {"id": "ts", "type": "table", "x": 20, "y": 380, "w": 300, "h": 100, "fieldKey": "", "staticRows": [["a", "1"], ["b"]], "showHeader": False, "showRowNumber": False, "highlightAbnormal": False, "rowHeight": 18, "borderColor": "#000", "borderWidth": 0, "columns": [{"id": "c1", "header": "", "bind": "k", "width": 1, "align": "left"}, {"id": "c2", "header": "", "bind": "", "width": 1, "align": "center"}], "headerStyle": {"fontFamily": "sans", "fontSize": 10, "fontWeight": 600, "color": "#5c6b66", "align": "left"}, "cellStyle": {"fontFamily": "sans", "fontSize": 10, "fontWeight": 400, "color": "#14201d", "align": "left"}},
            {"id": "rp", "type": "text", "x": 540, "y": 170, "w": 200, "h": 16, "repeat": {"fieldKey": "rows", "step": 18}, "showIf": "{row.v}", "text": "{i}. {row.name}: {row.v}", "style": {"fontFamily": "sans", "fontSize": 11, "fontWeight": 400, "color": "#000", "align": "left"}},
            {"id": "hid", "type": "text", "x": 0, "y": 0, "w": 10, "h": 10, "text": "x", "hidden": True, "style": {}},
            {"id": "junk", "type": "text", "x": "bad", "y": None, "w": 10, "h": 10, "text": "junk", "style": {"fontSize": "nope"}},
        ],
    }
    ctx = _ctx({"hb": 150.5, "rows": [{"name": "Uzun nom " * 8, "v": 3}, {"name": "b", "v": 9}, {"name": "c", "v": None}]})
    pdf = render(doc, ctx, lambda _aid: None)
    assert pdf[:5] == b"%PDF-"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "elements.pdf").write_bytes(pdf)


# ----------------------------------------------------------------------------- API


async def _seed() -> dict[str, str]:
    async with session_scope() as s:
        a = Company(name=f"T-templates-A-{SFX}", slug=f"t-templates-a-{SFX}", phone="998712000000", address="Urganch")
        b = Company(name=f"T-templates-B-{SFX}", slug=f"t-templates-b-{SFX}")
        s.add_all([a, b])
        await s.flush()
        admin_a = Role(company_id=a.id, key="admin", name="Admin", permissions=list(COMPANY_ADMIN_PERMISSIONS), is_system=True)
        admin_b = Role(company_id=b.id, key="admin", name="Admin", permissions=list(COMPANY_ADMIN_PERMISSIONS), is_system=True)
        viewer_a = Role(company_id=a.id, key="viewer", name="Viewer", permissions=["reception.patient.read"], is_system=False)
        s.add_all([admin_a, admin_b, viewer_a])
        await s.flush()
        pw = hash_password(PASSWORD)
        s.add_all(
            [
                Employee(company_id=a.id, full_name="T-templates Admin A", login=f"t-templates-admin-a-{SFX}", password_hash=pw, role_id=admin_a.id),
                Employee(company_id=b.id, full_name="T-templates Admin B", login=f"t-templates-admin-b-{SFX}", password_hash=pw, role_id=admin_b.id),
                Employee(company_id=a.id, full_name="T-templates Viewer A", login=f"t-templates-viewer-a-{SFX}", password_hash=pw, role_id=viewer_a.id),
            ]
        )
        ids = {"a": str(a.id), "b": str(b.id)}
    await engine.dispose()
    return ids


@pytest.fixture(scope="module")
def ctx() -> Iterator[dict[str, object]]:
    ids = asyncio.run(_seed())
    from app.main import app

    with TestClient(app) as client:
        tokens = {}
        for who in ("admin-a", "admin-b", "viewer-a"):
            r = client.post("/api/v1/auth/staff/login", json={"login": f"t-templates-{who}-{SFX}", "password": PASSWORD})
            assert r.status_code == 200, r.text
            tokens[who] = r.json()["accessToken"]
        yield {"client": client, "ids": ids, "tokens": tokens}


def h(ctx: dict, who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {ctx['tokens'][who]}"}


TEMPLATE_KEYS = {"id", "companyId", "name", "description", "status", "version", "serviceTypeIds", "categoryIds", "scope", "language", "doc", "thumbnailUrl", "usage", "createdAt", "updatedAt"}


def test_templates_api_lifecycle(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    cid = ctx["ids"]["a"]
    st_id = str(uuid.uuid4())
    # create with defaults
    r = c.post(f"/api/v1/companies/{cid}/templates", json={}, headers=h(ctx, "admin-a"))
    assert r.status_code == 201, r.text
    t = r.json()
    assert set(t) == TEMPLATE_KEYS
    assert t["name"] == "Yangi shablon" and t["status"] == "draft" and t["version"] == 1 and t["scope"] == "item" and t["language"] == "uz"
    assert t["doc"] == {"paper": "A4", "orientation": "portrait", "background": "#ffffff", "margin": 40, "elements": []} and t["usage"] == 0
    assert t["companyId"] == cid and t["createdAt"].endswith("Z")
    # create bound template with a doc; permission checks
    doc = {"paper": "A4", "orientation": "portrait", "background": "#ffffff", "margin": 40, "elements": [{"id": "e1", "type": "text", "x": 40, "y": 40, "w": 300, "h": 24, "text": "Bemor: {patient.fullName}", "style": {"fontFamily": "sans", "fontSize": 13, "fontWeight": 600, "color": "#14201d", "align": "left"}}]}
    r = c.post(f"/api/v1/companies/{cid}/templates", json={"name": "T-templates Bound", "serviceTypeIds": [st_id], "doc": doc, "scope": "item"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 201, r.text
    bound = r.json()
    assert bound["serviceTypeIds"] == [st_id]
    assert c.post(f"/api/v1/companies/{cid}/templates", json={}, headers=h(ctx, "viewer-a")).status_code == 403
    assert c.post(f"/api/v1/companies/{cid}/templates", json={}, headers=h(ctx, "admin-b")).status_code == 403
    r = c.post(f"/api/v1/companies/{cid}/templates", json={"doc": {"paper": "A3", "elements": []}}, headers=h(ctx, "admin-a"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    r = c.post(f"/api/v1/companies/{cid}/templates", json={"doc": {"paper": "A4", "elements": [{"id": "x", "type": "text"}]}}, headers=h(ctx, "admin-a"))
    assert r.status_code == 422
    # list: viewer sees all (sorted updatedAt desc), filters status / serviceTypeId (bound OR generic) / search
    lst = c.get(f"/api/v1/companies/{cid}/templates", headers=h(ctx, "viewer-a"))
    assert lst.status_code == 200 and [x["id"] for x in lst.json()][:2] == [bound["id"], t["id"]]
    ids = lambda r: {x["id"] for x in r.json()}  # noqa: E731
    assert ids(c.get(f"/api/v1/companies/{cid}/templates", params={"serviceTypeId": st_id}, headers=h(ctx, "admin-a"))) == {bound["id"], t["id"]}
    assert ids(c.get(f"/api/v1/companies/{cid}/templates", params={"serviceTypeId": str(uuid.uuid4())}, headers=h(ctx, "admin-a"))) == {t["id"]}
    assert ids(c.get(f"/api/v1/companies/{cid}/templates", params={"search": "боунд"}, headers=h(ctx, "admin-a"))) == {bound["id"]}
    assert c.get(f"/api/v1/companies/{cid}/templates", headers=h(ctx, "admin-b")).status_code == 403
    # get + tenant isolation
    assert c.get(f"/api/v1/templates/{bound['id']}", headers=h(ctx, "viewer-a")).json()["doc"] == doc
    r = c.get(f"/api/v1/templates/{bound['id']}", headers=h(ctx, "admin-b"))
    assert r.status_code == 404 and r.json()["error"]["message"] == "Shablon topilmadi"
    # status: empty → 422 empty; bound → active; then doc update bumps version, non-doc update does not
    r = c.post(f"/api/v1/templates/{t['id']}/status", json={"status": "active"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "empty" and r.json()["error"]["message"] == "Bo‘sh shablonni faollashtirib bo‘lmaydi"
    assert c.post(f"/api/v1/templates/{bound['id']}/status", json={"status": "active"}, headers=h(ctx, "viewer-a")).status_code == 403
    r = c.post(f"/api/v1/templates/{bound['id']}/status", json={"status": "active"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 200 and r.json()["status"] == "active" and r.json()["version"] == 1
    r = c.put(f"/api/v1/templates/{bound['id']}", json={"description": "desc", "categoryIds": [st_id]}, headers=h(ctx, "admin-a"))
    assert r.status_code == 200 and r.json()["version"] == 1 and r.json()["description"] == "desc" and r.json()["categoryIds"] == [st_id]
    r = c.put(f"/api/v1/templates/{bound['id']}", json={"doc": doc}, headers=h(ctx, "admin-a"))
    assert r.status_code == 200 and r.json()["version"] == 2
    assert c.put(f"/api/v1/templates/{bound['id']}", json={"name": "hack"}, headers=h(ctx, "admin-b")).status_code == 404
    lst = c.get(f"/api/v1/companies/{cid}/templates", params={"status": "active"}, headers=h(ctx, "admin-a")).json()
    assert {x["id"] for x in lst} == {bound["id"]}
    # duplicate
    r = c.post(f"/api/v1/templates/{bound['id']}/duplicate", headers=h(ctx, "admin-a"))
    assert r.status_code == 201, r.text
    dup = r.json()
    assert dup["name"] == "T-templates Bound (nusxa)" and dup["status"] == "draft" and dup["version"] == 1 and dup["usage"] == 0 and dup["serviceTypeIds"] == [st_id] and dup["doc"] == doc
    # delete: active → 409; draft ok (soft) and gone
    r = c.delete(f"/api/v1/templates/{bound['id']}", headers=h(ctx, "admin-a"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "active" and r.json()["error"]["message"] == "Faol shablonni o‘chirib bo‘lmaydi — avval arxivlang"
    assert c.delete(f"/api/v1/templates/{dup['id']}", headers=h(ctx, "admin-b")).status_code == 404
    assert c.delete(f"/api/v1/templates/{dup['id']}", headers=h(ctx, "admin-a")).status_code == 204
    assert c.get(f"/api/v1/templates/{dup['id']}", headers=h(ctx, "admin-a")).status_code == 404
    # preview: any staff of the company; unsaved doc override; PDF bytes
    r = c.post(f"/api/v1/templates/{bound['id']}/preview.pdf", headers=h(ctx, "viewer-a"))
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/pdf") and r.content[:5] == b"%PDF-"
    r = c.post(f"/api/v1/templates/{bound['id']}/preview.pdf", json={"doc": {**doc, "paper": "A5"}}, headers=h(ctx, "admin-a"))
    assert r.status_code == 200 and r.content[:5] == b"%PDF-"
    assert c.post(f"/api/v1/templates/{bound['id']}/preview.pdf", headers=h(ctx, "admin-b")).status_code == 404


def test_assets_api(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    cid = ctx["ids"]["a"]
    data_url = "data:image/png;base64," + base64.b64encode(PNG_1PX).decode()
    r = c.post(f"/api/v1/companies/{cid}/assets", json={"kind": "logo", "name": "T-templates logo", "url": data_url, "width": 0, "height": 0}, headers=h(ctx, "admin-a"))
    assert r.status_code == 201, r.text
    a = r.json()
    assert set(a) == {"id", "companyId", "kind", "name", "url", "width", "height", "employeeId"}
    assert a["url"].startswith("/api/v1/files/") and a["width"] == 1 and a["height"] == 1 and a["employeeId"] is None and a["companyId"] == cid
    assert c.post(f"/api/v1/companies/{cid}/assets", json={"kind": "logo", "name": "x", "url": data_url}, headers=h(ctx, "viewer-a")).status_code == 403
    r = c.post(f"/api/v1/companies/{cid}/assets", json={"kind": "logo", "name": "bad", "url": "data:text/plain;base64,aGVsbG8="}, headers=h(ctx, "admin-a"))
    assert r.status_code == 422
    lst = c.get(f"/api/v1/companies/{cid}/assets", headers=h(ctx, "viewer-a"))
    assert lst.status_code == 200 and a["id"] in {x["id"] for x in lst.json()}
    assert c.get(f"/api/v1/companies/{cid}/assets", headers=h(ctx, "admin-b")).status_code == 403
    assert a["id"] not in {x["id"] for x in c.get(f"/api/v1/companies/{ctx['ids']['b']}/assets", headers=h(ctx, "admin-b")).json()}
    # the stored file is publicly served
    f = c.get(a["url"])
    assert f.status_code == 200 and f.content == PNG_1PX
    # a template using the asset renders it in the preview
    doc = {"paper": "A4", "orientation": "portrait", "background": "#ffffff", "margin": 40, "elements": [{"id": "img", "type": "image", "x": 40, "y": 40, "w": 100, "h": 100, "fit": "contain", "assetId": a["id"]}]}
    r = c.post(f"/api/v1/companies/{cid}/templates", json={"name": "T-templates With asset", "doc": doc}, headers=h(ctx, "admin-a"))
    assert r.status_code == 201
    r = c.post(f"/api/v1/templates/{r.json()['id']}/preview.pdf", headers=h(ctx, "admin-a"))
    assert r.status_code == 200 and r.content[:5] == b"%PDF-"


async def _snapshot_roundtrip(company_id: str, template_id: str) -> tuple[list[str], dict, bytes]:
    """Internal API used by orders/portal: active-list cache, snapshot build from (transient) rows, PDF."""
    async with session_scope() as s:
        cid = uuid.UUID(company_id)
        active = await tpl_service.list_active_templates(s, cid)
        active_again = await tpl_service.list_active_templates(s, cid)  # cache hit → transient rows
        assert [t.id for t in active] == [t.id for t in active_again]
        template = await tpl_service.get_template_or_404(s, uuid.UUID(template_id), cid)
        company = await s.get(Company, cid)
        st_id = uuid.uuid4()
        schema = AttributeSchema(id=uuid.uuid4(), company_id=cid, name="T-templates schema", version=1, status="published", fields=SCHEMA["fields"])
        patient = Patient(id=uuid.uuid4(), company_id=cid, full_name="T-templates Bemor", phone="998901112233", gender="male", birth_date=None, street="Ko‘cha 1")
        order = Order(id=uuid.uuid4(), company_id=cid, branch_id=uuid.uuid4(), number="TT-000007", patient_id=patient.id, patient_name=patient.full_name, patient_phone=patient.phone, created_by_employee_id=uuid.uuid4())
        order.created_at = tpl_service.utcnow()
        item = OrderItem(id=uuid.uuid4(), company_id=cid, order_id=order.id, branch_id=order.branch_id, service_type_id=st_id, service_name="UQT", category_id=uuid.uuid4(), category_name="Lab", schema_id=schema.id, values={"hb": 99, "res": "pos"}, status="submitted", technician_name="Lab")
        snap = await tpl_service.build_document_snapshot(
            s, template=template, order=order, patient=patient, company=company, branch=Branch(name="Filial", code="TT", company_id=cid), category=Category(name="Gematologiya", company_id=cid, phone="12"),
            primary_item=item, items=[item], schemas={schema.id: schema}, service_codes={st_id: "UQT-1"}, district_name="Urganch", approved_at=tpl_service.utcnow(),
        )
        pdf = await tpl_service.render_snapshot_pdf(s, snap)
        return [str(t.id) for t in active], snap, pdf


def test_snapshot_and_internal_api(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    cid = ctx["ids"]["a"]
    doc = {"paper": "A4", "orientation": "portrait", "background": "#ffffff", "margin": 40, "elements": [
        {"id": "e1", "type": "text", "x": 40, "y": 40, "w": 400, "h": 24, "text": "{patient.fullName} · {patient.gender} · {order.number} · {values.hb}", "style": {"fontFamily": "sans", "fontSize": 13, "fontWeight": 400, "color": "#14201d", "align": "left"}},
        {"id": "f1", "type": "field", "x": 40, "y": 80, "w": 400, "h": 24, "fieldKey": "hb", "showLabel": True, "showUnit": True, "showReference": True, "highlightAbnormal": True, "style": {"fontFamily": "sans", "fontSize": 12, "fontWeight": 400, "color": "#000", "align": "left"}},
    ]}
    r = c.post(f"/api/v1/companies/{cid}/templates", json={"name": "T-templates Snapshot", "doc": doc, "language": "ru"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    assert c.post(f"/api/v1/templates/{tid}/status", json={"status": "active"}, headers=h(ctx, "admin-a")).status_code == 200
    active_ids, snap, pdf = c.portal.call(_snapshot_roundtrip, cid, tid)  # run inside the app's event loop
    assert tid in active_ids
    assert snap["version"] == 1 and snap["language"] == "ru" and snap["templateId"] == tid and snap["doc"] == doc and snap["assets"] == {}
    p = snap["context"]["patient"]
    assert p["fullName"] == "T-templates Bemor" and p["gender"] == "Мужской" and p["genderRaw"] == "male" and p["birthDate"] == "—" and p["age"] == "" and p["address"] == "Urganch, Ko‘cha 1"
    assert snap["context"]["order"]["number"] == "TT-000007" and snap["context"]["item"]["approvedAt"] and snap["context"]["schema"]["fields"] == SCHEMA["fields"]
    assert "items" not in snap["context"] and snap["context"]["values"] == {"hb": 99, "res": "pos"}
    assert json.dumps(snap)  # JSON-serialisable
    assert pdf[:5] == b"%PDF-"
    # archived → no longer in the active list (cache invalidated on write)
    assert c.post(f"/api/v1/templates/{tid}/status", json={"status": "archived"}, headers=h(ctx, "admin-a")).status_code == 200
    active_ids, _, _ = c.portal.call(_snapshot_roundtrip, cid, tid)
    assert tid not in active_ids
