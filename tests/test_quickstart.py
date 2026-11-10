"""Quick start ("tezkor ishga tushirish"): one request creates a facility ready to work — missing region / district
nodes, the facility, the default staff (one password), the sample facility's catalogue and templates re-bound to
the new facility, an active cheque. Fixture rows are prefixed `T-`."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from app.infrastructure.db.base import alive
from app.infrastructure.db.models import (
    AttributeSchema,
    Category,
    District,
    Employee,
    OrgUnit,
    Region,
    ResultTemplate,
    ServiceType,
    TemplateUnit,
)
from app.modules.templates.defaults import default_receipt_doc
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.helpers import (
    API,
    COUNTRY_ID,
    COUNTRY_UNIT_ID,
    app_instance,
    bearer,
    db,
    make_employee,
    make_tree,
    seed,
    staff_session,
    staff_token,
)

DOC = {
    "paper": "A4",
    "orientation": "portrait",
    "background": "#ffffff",
    "margin": 40,
    "elements": [{"id": "t1", "type": "text", "x": 40, "y": 40, "w": 300, "h": 20, "text": "{patient.fullName}"}],
}


@pytest.fixture(scope="module")
def ctx() -> Iterator[dict]:
    async def rows(s):
        a = await make_tree(s)
        b = await make_tree(s)
        # the catalogue of region A: one category, one published schema, one service using it
        cat = Category(
            owner_unit_id=a.region.id, name=f"T-{a.tag} Bakteriologiya", code="TBAK", workflow="lab", sort_order=1
        )
        schema = AttributeSchema(
            owner_unit_id=a.region.id,
            name=f"T-{a.tag} sxema",
            status="published",
            version=1,
            fields=[{"key": "result", "label": "Natija", "type": "text", "required": True, "order": 1}],
        )
        s.add_all([cat, schema])
        await s.flush()
        svc = ServiceType(
            owner_unit_id=a.region.id,
            category_id=cat.id,
            name=f"T-{a.tag} xizmat",
            code="TQS1",
            price=50_000,
            schema_id=schema.id,
            sort_order=1,
        )
        s.add(svc)
        await s.flush()
        # templates are bound to facility A (one letterhead per facility, as in production)
        tpl = ResultTemplate(
            owner_unit_id=a.region.id,
            name=f"T-{a.tag} natija",
            status="active",
            version=1,
            service_type_ids=[svc.id],
            category_ids=[],
            scope="item",
            language="uz",
            doc=DOC,
            usage=0,
        )
        receipt = ResultTemplate(
            owner_unit_id=a.region.id,
            name=f"T-{a.tag} chek",
            status="active",
            version=1,
            service_type_ids=[],
            category_ids=[],
            scope="receipt",
            language="uz",
            doc=default_receipt_doc(),
            usage=0,
        )
        s.add_all([tpl, receipt])
        await s.flush()
        s.add_all(
            [
                TemplateUnit(template_id=tpl.id, unit_id=a.facility.id),
                TemplateUnit(template_id=receipt.id, unit_id=a.facility.id),
            ]
        )
        svc.default_template_id = tpl.id
        # geo rows without nodes yet: another district of region A, and a whole new region with one district
        free_district = District(region_id=a.geo_region.id, name=f"T-{a.tag} shahri", sort_order=2, kind="city")
        new_region = Region(country_id=COUNTRY_ID, name=f"T-{a.tag}N viloyati", sort_order=991)
        s.add_all([free_district, new_region])
        await s.flush()
        new_district = District(region_id=new_region.id, name=f"T-{a.tag}N tumani", sort_order=1, kind="district")
        s.add(new_district)
        await s.flush()
        sup = await make_employee(s, None, super_admin=True)
        admin_a = await make_employee(s, a.region, "admin")
        admin_b = await make_employee(s, b.region, "admin")
        reg = await make_employee(s, a.facility, "registrator")
        return {
            "a": {k: str(getattr(a, k).id) for k in ("region", "district", "facility")},
            "b": {k: str(getattr(b, k).id) for k in ("region", "district", "facility")},
            "tag": a.tag,
            "region_name": a.region.name,
            "geo": {
                "free_district": str(free_district.id),
                "new_region": str(new_region.id),
                "new_district": str(new_district.id),
            },
            "ids": {"svc": str(svc.id), "schema": str(schema.id), "tpl": str(tpl.id)},
            "logins": {"super": sup.login, "admin-a": admin_a.login, "admin-b": admin_b.login, "reg": reg.login},
        }

    data = seed(rows)
    with TestClient(app_instance()) as client:
        data["client"] = client
        data["tokens"] = {who: staff_token(client, login) for who, login in data["logins"].items()}
        yield data


def h(ctx: dict, who: str) -> dict[str, str]:
    return bearer(ctx["tokens"][who])


def facility(tag: str, code: str) -> dict:
    return {"name": f"T-{tag} {code} SES", "kind": "ses", "code": code, "phone": "+998 62 000-00-00"}


def staff(code: str) -> list[dict]:
    base = code.lower()
    return [
        {"role": "registrator", "fullName": f"T-{code} kassir", "login": f"{base}-kassa"},
        {"role": "vrach", "fullName": f"T-{code} vrach", "login": f"{base}-vrach"},
        {"role": "laborant", "fullName": f"T-{code} laborant", "login": f"{base}-laborant"},
        {"role": "rahbar", "fullName": f"T-{code} rahbar", "login": f"{base}-rahbar"},
    ]


def test_preview_tells_what_a_facility_under_the_district_inherits(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    r = c.get(f"{API}/units/{ctx['a']['district']}/quick-start", headers=h(ctx, "admin-a"))
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["parent"]["level"] == "district"
    assert p["inherits"]["serviceTypes"] == 1 and p["inherits"]["schemas"] == 1 and p["inherits"]["categories"] == 1
    # bound templates belong to facility A — a new facility would not see them
    assert p["inherits"]["templates"] == 0 and p["inherits"]["receipts"] == 0
    assert ctx["region_name"] in p["inheritsFrom"]
    sample = next(s for s in p["samples"] if s["id"] == ctx["a"]["facility"])
    assert sample["counts"] == {"categories": 1, "serviceTypes": 1, "schemas": 1, "templates": 1, "receipts": 1}
    assert sample["sameCatalogue"] is True
    assert [r_["key"] for r_ in p["roles"]] == ["registrator", "vrach", "laborant", "rahbar", "admin"]
    assert p["defaultPassword"] == "123456"
    # a region administrator does not see the other region's facilities as samples
    assert all(s["id"] != ctx["b"]["facility"] for s in p["samples"])


def test_preview_from_the_country_resolves_existing_and_missing_nodes(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    r = c.get(
        f"{API}/units/{COUNTRY_UNIT_ID}/quick-start",
        params={"regionId": ctx["geo"]["new_region"], "districtId": ctx["geo"]["new_district"]},
        headers=h(ctx, "super"),
    )
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["regionNode"] is None and p["districtNode"] is None
    assert p["inherits"]["serviceTypes"] == 0


def test_quick_start_in_the_same_region_inherits_the_catalogue_and_copies_the_templates(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    code = f"TQ{ctx['tag'][:3]}A"
    body = {
        "facility": facility(ctx["tag"], code),
        "staff": staff(code),
        "password": "123456",
        "sample": {"facilityId": ctx["a"]["facility"], "owner": "region"},
    }
    r = c.post(f"{API}/units/{ctx['a']['district']}/quick-start", json=body, headers=h(ctx, "admin-a"))
    assert r.status_code == 201, r.text
    out = r.json()
    assert (
        out["facility"]["level"] == "facility"
        and out["facility"]["parentId"] == ctx["a"]["district"]
        and out["facility"]["code"] == code
    )
    assert out["region"] is None and out["district"] is None
    assert [e["roleKey"] for e in out["employees"]] == ["registrator", "vrach", "laborant", "rahbar"]
    assert all(e["password"] == "123456" for e in out["employees"])
    assert out["catalog"]["inherited"]["serviceTypes"] == 1 and out["catalog"]["copied"]["serviceTypes"] == 0
    assert out["catalog"]["copied"]["templates"] == 1 and out["catalog"]["copied"]["receipts"] == 1
    assert out["catalog"]["sampleName"].endswith("SES") and out["catalog"]["ownerName"] == ctx["region_name"]
    assert out["receipt"]["status"] == "active"
    new_id = out["facility"]["id"]

    # every default account logs in with the shared password and lands in the new facility
    kassa = next(e for e in out["employees"] if e["roleKey"] == "registrator")
    session = staff_session(c, kassa["login"], "123456")
    assert session["unitId"] == new_id and session["facilityId"] == new_id and session["roleKey"] == "registrator"

    async def check(s):
        tpls = (
            (
                await s.execute(
                    select(ResultTemplate)
                    .join(TemplateUnit, TemplateUnit.template_id == ResultTemplate.id)
                    .where(TemplateUnit.unit_id == uuid.UUID(new_id), alive(ResultTemplate))
                )
            )
            .scalars()
            .all()
        )
        emps = (
            (await s.execute(select(Employee).where(Employee.unit_id == uuid.UUID(new_id), alive(Employee))))
            .scalars()
            .all()
        )
        return {
            "scopes": sorted(t.scope for t in tpls),
            "owners": {str(t.owner_unit_id) for t in tpls},
            "services": [str(x) for t in tpls for x in t.service_type_ids],
            "emps": len(emps),
        }

    got = db(c, check)
    assert got["scopes"] == ["item", "receipt"]
    assert got["owners"] == {ctx["a"]["region"]}
    # the copied template still points at the inherited (same) service
    assert got["services"] == [ctx["ids"]["svc"]]
    assert got["emps"] == 4


def test_quick_start_from_the_country_creates_the_nodes_and_copies_the_catalogue(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    code = f"TQ{ctx['tag'][:3]}N"
    body = {
        "regionId": ctx["geo"]["new_region"],
        "districtId": ctx["geo"]["new_district"],
        "facility": facility(ctx["tag"], code),
        "staff": staff(code)[:2],
        "password": "qwerty12",
        "sample": {"facilityId": ctx["a"]["facility"], "owner": "region"},
    }
    r = c.post(f"{API}/units/{COUNTRY_UNIT_ID}/quick-start", json=body, headers=h(ctx, "super"))
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["region"]["level"] == "region" and out["region"]["name"].endswith("N viloyati")
    assert out["district"]["level"] == "district" and out["district"]["parentId"] == out["region"]["id"]
    assert out["facility"]["parentId"] == out["district"]["id"]
    assert out["catalog"]["copied"] == {"categories": 1, "serviceTypes": 1, "schemas": 1, "templates": 1, "receipts": 1}
    assert out["catalog"]["inherited"]["serviceTypes"] == 0
    assert out["catalog"]["ownerName"] == out["region"]["name"]
    assert staff_session(c, out["employees"][1]["login"], "qwerty12")["roleKey"] == "vrach"
    region_id = uuid.UUID(out["region"]["id"])

    async def check(s):
        svc = (
            await s.execute(select(ServiceType).where(ServiceType.owner_unit_id == region_id, alive(ServiceType)))
        ).scalar_one()
        schema = (
            await s.execute(
                select(AttributeSchema).where(AttributeSchema.owner_unit_id == region_id, alive(AttributeSchema))
            )
        ).scalar_one()
        cat = (
            await s.execute(select(Category).where(Category.owner_unit_id == region_id, alive(Category)))
        ).scalar_one()
        tpls = (
            (
                await s.execute(
                    select(ResultTemplate).where(ResultTemplate.owner_unit_id == region_id, alive(ResultTemplate))
                )
            )
            .scalars()
            .all()
        )
        item = next(t for t in tpls if t.scope == "item")
        return {
            "schema_mapped": svc.schema_id == schema.id,
            "category_mapped": svc.category_id == cat.id,
            "schema_status": schema.status,
            "default_tpl": svc.default_template_id == item.id,
            "tpl_services": [str(x) for x in item.service_type_ids] == [str(svc.id)],
            "price": svc.price,
            "code": svc.code,
        }

    got = db(c, check)
    assert got == {
        "schema_mapped": True,
        "category_mapped": True,
        "schema_status": "published",
        "default_tpl": True,
        "tpl_services": True,
        "price": 50_000,
        "code": "TQS1",
    }
    # the second call under the same country reuses the nodes just created (preview shows them, nothing new is made)
    p = c.get(
        f"{API}/units/{COUNTRY_UNIT_ID}/quick-start",
        params={"regionId": ctx["geo"]["new_region"], "districtId": ctx["geo"]["new_district"]},
        headers=h(ctx, "super"),
    ).json()
    assert p["regionNode"]["id"] == out["region"]["id"] and p["districtNode"]["id"] == out["district"]["id"]
    assert p["inherits"]["serviceTypes"] == 1


def test_taken_logins_get_a_number_and_a_missing_sample_still_gives_a_cheque(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    code = f"TQ{ctx['tag'][:3]}L"
    body = {
        "districtId": ctx["geo"]["free_district"],
        "facility": facility(ctx["tag"], code),
        "staff": [
            {"role": "rahbar", "fullName": "T-dup", "login": ctx["logins"]["reg"].upper()},
            {"role": "rahbar", "fullName": "T-dup2", "login": ctx["logins"]["reg"]},
        ],
    }
    r = c.post(f"{API}/units/{ctx['a']['region']}/quick-start", json=body, headers=h(ctx, "admin-a"))
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["district"]["level"] == "district" and out["district"]["parentId"] == ctx["a"]["region"]
    logins = [e["login"] for e in out["employees"]]
    assert logins == [f"{ctx['logins']['reg']}2", f"{ctx['logins']['reg']}3"]
    # no sample: nothing copied, but a cheque is usable (the standard one, active, bound to the facility)
    assert (
        out["catalog"]["copied"]["templates"] == 0
        and out["receipt"] is not None
        and out["receipt"]["status"] == "active"
    )
    assert staff_session(c, logins[0], "123456")["unitId"] == out["facility"]["id"]


def test_rights_and_shape_errors(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    code = f"TQ{ctx['tag'][:3]}R"
    body = {
        "facility": facility(ctx["tag"], code),
        "staff": [],
        "sample": {"facilityId": ctx["b"]["facility"], "owner": "region"},
    }
    # a registrar may not; a region administrator may not act in another region
    assert (
        c.post(f"{API}/units/{ctx['a']['district']}/quick-start", json=body, headers=h(ctx, "reg")).status_code == 403
    )
    assert (
        c.post(f"{API}/units/{ctx['a']['district']}/quick-start", json=body, headers=h(ctx, "admin-b")).status_code
        == 403
    )
    # the sample must be visible to the caller
    assert (
        c.post(f"{API}/units/{ctx['a']['district']}/quick-start", json=body, headers=h(ctx, "admin-a")).status_code
        == 403
    )
    # nothing goes under a facility; a country parent needs the geo
    body["sample"] = None
    r = c.post(f"{API}/units/{ctx['a']['facility']}/quick-start", json=body, headers=h(ctx, "admin-a"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "leaf"
    r = c.post(f"{API}/units/{COUNTRY_UNIT_ID}/quick-start", json=body, headers=h(ctx, "super"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "region_required"
    # a bad login
    body["staff"] = [{"role": "vrach", "fullName": "T-x", "login": "a b"}]
    r = c.post(f"{API}/units/{ctx['a']['district']}/quick-start", json=body, headers=h(ctx, "admin-a"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_login"

    # nothing was created by the failed calls
    async def count(s):
        return (await s.execute(select(OrgUnit).where(OrgUnit.code == code, alive(OrgUnit)))).scalars().all()

    assert db(c, count) == []
