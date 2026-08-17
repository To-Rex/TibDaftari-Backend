"""Operations CLI — ``python -m app.cli <command>``.

Commands
--------
* ``seed-reference``      upsert Uzbekistan regions/districts from ``seed/reference/uz-regions.json``
* ``seed-demo``           load the demo dataset (``seed/demo/core.json``; ``--with-transactions`` adds
                          ``seed/demo/transactions.json``) into the real tables with fresh UUIDs
* ``create-superadmin``   platform superadmin role + employee (creates the company when needed)
* ``set-password``        reset a staff password by login
* ``ensure-partitions``   create upcoming monthly ``audit_log`` partitions

The seed files use the *frontend mock* shapes (camelCase, ids like ``c1``/``b1``/``st_lg_66``).
Every reference is remapped through an :class:`IdMap` (old id → new UUID); nothing is ever
inserted with a mock id. Seeding is transactional: either the whole dataset lands or nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import mimetypes
import secrets
import sys
import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.ids import uuid7
from app.core.permissions import COMPANY_ADMIN_PERMISSIONS, PERMISSIONS, SUPERADMIN_ROLE_KEY
from app.core.security import hash_password
from app.core.textutil import fold, slugify
from app.core.timeutil import parse_iso, utcnow
from app.infrastructure.db.base import alive
from app.infrastructure.db.models import (
    AttributeSchema,
    Branch,
    Category,
    Company,
    District,
    Employee,
    Notification,
    Order,
    OrderItem,
    OutboxMessage,
    Patient,
    Payment,
    Region,
    ResultDocument,
    ResultTemplate,
    Role,
    ServiceType,
    TemplateAsset,
)
from app.infrastructure.db.session import dispose_engine, session_scope
from app.modules.files.service import decode_data_url, store_bytes

ROOT = Path(__file__).resolve().parent.parent
SEED_DIR = ROOT / "seed"
REFERENCE_FILE = SEED_DIR / "reference" / "uz-regions.json"
DEMO_CORE_FILE = SEED_DIR / "demo" / "core.json"
DEMO_TX_FILE = SEED_DIR / "demo" / "transactions.json"
ASSETS_DIR = SEED_DIR / "assets"

BATCH_SIZE = 500
SEED_SETTINGS_KEY = "seed"  # companies.settings.seed = {"source": ..., "idMap": {...}, "transactions": bool}


class CliError(Exception):
    """User-facing failure: printed without a traceback, exit code 1."""


# --------------------------------------------------------------------------- id map


class IdMap:
    """Mock id → real UUID mapping used while importing the demo dataset.

    Ids are allocated lazily (UUIDv7) so every entity gets an id *before* the rows that
    reference it are built, regardless of insertion order.
    """

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self._map: dict[str, uuid.UUID] = {k: uuid.UUID(v) for k, v in (initial or {}).items()}

    def alloc(self, old_id: str) -> uuid.UUID:
        """Return the UUID for ``old_id``, allocating a fresh one on first use."""
        found = self._map.get(old_id)
        if found is None:
            found = uuid7()
            self._map[old_id] = found
        return found

    def get(self, old_id: str | None) -> uuid.UUID:
        """Resolve a *required* reference; unknown ids are a dataset bug, not a runtime condition."""
        if old_id is None or old_id not in self._map:
            raise CliError(f"Seed reference to unknown id: {old_id!r}")
        return self._map[old_id]

    def opt(self, old_id: str | None) -> uuid.UUID | None:
        """Resolve an optional reference (``None`` stays ``None``)."""
        return None if old_id is None else self.get(old_id)

    def alias(self, old_id: str, real_id: uuid.UUID) -> None:
        """Bind a mock id to an *existing* row (reference geo rows, the shared platform role)."""
        self._map[old_id] = real_id

    def many(self, old_ids: Iterable[str] | None) -> list[uuid.UUID]:
        return [self.get(o) for o in (old_ids or [])]

    def __contains__(self, old_id: object) -> bool:
        return old_id in self._map

    def as_dict(self) -> dict[str, str]:
        return {k: str(v) for k, v in self._map.items()}


def remap_template_doc(doc: dict[str, Any], ids: IdMap) -> dict[str, Any]:
    """Return a copy of a template ``doc`` with image ``assetId`` values remapped.

    Ad-hoc images keep their inline ``data:`` URI; only ids known to the map are rewritten,
    so a doc can be remapped safely even when it references assets outside the dataset.
    """
    out = dict(doc)
    elements: list[dict[str, Any]] = []
    for el in doc.get("elements") or []:
        el = dict(el)
        asset_id = el.get("assetId")
        if el.get("type") == "image" and isinstance(asset_id, str) and asset_id in ids:
            el["assetId"] = str(ids.get(asset_id))
        elements.append(el)
    out["elements"] = elements
    return out


# --------------------------------------------------------------------------- helpers


def _load_json(path: Path) -> Any:
    if not path.exists():
        raise CliError(f"Seed file not found: {path}")
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _dt(value: str | None) -> datetime | None:
    return parse_iso(value) if value else None


def _date(value: str | None) -> Any:
    return datetime.fromisoformat(value).date() if value else None


def _chunks(rows: Sequence[dict[str, Any]], size: int = BATCH_SIZE) -> Iterable[Sequence[dict[str, Any]]]:
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


async def _bulk_insert(session: AsyncSession, model: type, rows: list[dict[str, Any]]) -> int:
    """Insert plain dicts as multi-row ``INSERT ... VALUES`` statements — one round trip per chunk.

    All dicts of a chunk carry the same keys (rows are built with every column explicit).
    """
    for chunk in _chunks(rows):
        await session.execute(insert(model).values(list(chunk)))
    return len(rows)


def _order_seq(number: str) -> int:
    """'UR-000813' → 813 (0 when the suffix is not numeric)."""
    tail = number.rsplit("-", 1)[-1]
    return int(tail) if tail.isdigit() else 0


def _asset_bytes(url: str) -> tuple[bytes, str, str | None]:
    """Resolve a seed asset ``url`` (``/legacy/<file>`` or ``data:``) → (bytes, mime, filename)."""
    if url.startswith("data:"):
        data, mime = decode_data_url(url)
        return data, mime, None
    if url.startswith("/legacy/"):
        name = url.removeprefix("/legacy/")
        path = ASSETS_DIR / name
        if not path.exists():
            raise CliError(f"Legacy asset missing: {path}")
        data = path.read_bytes()
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if path.suffix.lower() == ".bmp":
            from PIL import Image

            with Image.open(io.BytesIO(data)) as im:
                buf = io.BytesIO()
                im.convert("RGBA").save(buf, format="PNG")
            data, mime, name = buf.getvalue(), "image/png", path.with_suffix(".png").name
        return data, mime, name
    raise CliError(f"Unsupported asset url in seed: {url[:60]}")


# --------------------------------------------------------------------------- seed-reference


async def seed_reference(session: AsyncSession) -> tuple[int, int]:
    """Upsert regions (by code) and districts (by region + name). Returns (regions, districts)."""
    data = _load_json(REFERENCE_FILE)
    regions_by_code: dict[str, Region] = {
        r.code: r for r in (await session.execute(select(Region).where(Region.code.is_not(None)))).scalars()
    }
    for item in data["regions"]:
        row = regions_by_code.get(item["code"])
        if row is None:
            row = Region(code=item["code"], name=item["name"], order=int(item.get("order", 0)))
            session.add(row)
            regions_by_code[item["code"]] = row
        else:
            row.name = item["name"]
            row.order = int(item.get("order", 0))
    await session.flush()

    existing_districts: dict[tuple[uuid.UUID, str], District] = {
        (d.region_id, fold(d.name)): d for d in (await session.execute(select(District))).scalars()
    }
    for item in data["districts"]:
        region = regions_by_code.get(item["regionCode"])
        if region is None:
            raise CliError(f"District {item['name']!r} references unknown region code {item['regionCode']!r}")
        key = (region.id, fold(item["name"]))
        row = existing_districts.get(key)
        if row is None:
            row = District(region_id=region.id, name=item["name"], code=item.get("code"), order=int(item.get("order", 0)))
            session.add(row)
            existing_districts[key] = row
        else:
            row.name = item["name"]
            row.order = int(item.get("order", 0))
    await session.flush()
    return len(data["regions"]), len(data["districts"])


# --------------------------------------------------------------------------- seed-demo


class GeoIndex:
    """Reference regions/districts looked up by *name* (mock ids rg12/d1 → real UUIDs)."""

    def __init__(self, regions: Iterable[Region], districts: Iterable[District]) -> None:
        self._regions = {fold(r.name): r.id for r in regions}
        self._districts = {(d.region_id, fold(d.name)): d.id for d in districts}

    @classmethod
    async def load(cls, session: AsyncSession) -> GeoIndex:
        regions = (await session.execute(select(Region))).scalars().all()
        districts = (await session.execute(select(District))).scalars().all()
        return cls(regions, districts)

    def region(self, name: str) -> uuid.UUID:
        found = self._regions.get(fold(name))
        if found is None:
            raise CliError(f"Region {name!r} not found in reference data (run seed-reference)")
        return found

    def district(self, region_id: uuid.UUID, name: str) -> uuid.UUID | None:
        return self._districts.get((region_id, fold(name)))


def _map_geo(core: dict[str, Any], geo: GeoIndex, ids: IdMap) -> None:
    """Register core.json regions/districts (rg*, d*) in the id map via reference-table names."""
    for r in core.get("regions") or []:
        ids.alias(r["id"], geo.region(r["name"]))
    for d in core.get("districts") or []:
        region_id = ids.get(d["regionId"])
        district_id = geo.district(region_id, d["name"])
        if district_id is not None:
            ids.alias(d["id"], district_id)


async def _platform_superadmin_role(session: AsyncSession) -> Role:
    """Get-or-create the platform-wide superadmin role (company_id NULL)."""
    stmt = select(Role).where(Role.company_id.is_(None), Role.key == SUPERADMIN_ROLE_KEY, alive(Role))
    role = (await session.execute(stmt)).scalar_one_or_none()
    if role is None:
        role = Role(company_id=None, key=SUPERADMIN_ROLE_KEY, name="Superadmin", description="Platforma egasi — hamma narsa", permissions=list(PERMISSIONS), is_system=True)
        session.add(role)
        await session.flush()
    return role


async def _company_by_slug(session: AsyncSession, slug: str) -> Company | None:
    return (await session.execute(select(Company).where(Company.slug == slug, alive(Company)))).scalar_one_or_none()


async def _seed_core(session: AsyncSession, core: dict[str, Any], ids: IdMap) -> dict[str, int]:
    """Insert companies/branches/roles/categories/schemas/employees/assets/templates/service types."""
    counts: dict[str, int] = {}
    credentials: dict[str, str] = core.get("credentials") or {}

    # allocate every id first so references can be resolved in any order
    for key in ("companies", "branches", "roles", "employees", "categories", "serviceTypes", "schemas", "templates", "assets"):
        for item in core.get(key) or []:
            ids.alloc(item["id"])
    platform_role = await _platform_superadmin_role(session)
    for r in core["roles"]:
        if r.get("companyId") is None and r["key"] == SUPERADMIN_ROLE_KEY:
            ids.alias(r["id"], platform_role.id)

    for c in core["companies"]:
        sms = c.get("sms") or {}
        session.add(
            Company(
                id=ids.get(c["id"]),
                name=c["name"],
                legal_name=c.get("legalName"),
                slug=c["slug"],
                logo_url=c.get("logoUrl"),
                phone=c.get("phone"),
                email=c.get("email"),
                address=c.get("address"),
                locale=c.get("locale") or "uz",
                is_active=bool(c.get("isActive", True)),
                sms_provider=sms.get("provider") or "none",
                sms_api_key_enc=None,
                sms_api_key_masked=sms.get("apiKeyMasked"),
                sms_default_priority=sms.get("defaultPriority") or "transactional",
                sms_sender_note=sms.get("senderNote"),
                settings={},
                created_at=_dt(c.get("createdAt")) or utcnow(),
                updated_at=_dt(c.get("updatedAt")) or utcnow(),
            )
        )
    counts["companies"] = len(core["companies"])

    for b in core["branches"]:
        session.add(
            Branch(
                id=ids.get(b["id"]),
                company_id=ids.get(b["companyId"]),
                name=b["name"],
                code=b["code"],
                address=b.get("address"),
                phone=b.get("phone"),
                timezone=b.get("timezone") or "Asia/Tashkent",
                is_active=bool(b.get("isActive", True)),
                order_seq=int(b.get("orderSeq") or 0),
                created_at=_dt(b.get("createdAt")) or utcnow(),
                updated_at=_dt(b.get("updatedAt")) or utcnow(),
            )
        )
    counts["branches"] = len(core["branches"])

    for r in core["roles"]:
        if ids.get(r["id"]) == platform_role.id:
            continue
        session.add(
            Role(
                id=ids.get(r["id"]),
                company_id=ids.opt(r.get("companyId")),
                key=r["key"],
                name=r["name"],
                description=r.get("description"),
                permissions=[p for p in r.get("permissions") or [] if p in PERMISSIONS],
                is_system=bool(r.get("isSystem", False)),
            )
        )
    counts["roles"] = len(core["roles"])

    for cat in core["categories"]:
        session.add(
            Category(
                id=ids.get(cat["id"]),
                company_id=ids.get(cat["companyId"]),
                parent_id=ids.opt(cat.get("parentId")),
                name=cat["name"],
                code=cat.get("code"),
                icon=cat.get("icon"),
                color=cat.get("color"),
                order=int(cat.get("order") or 0),
                is_active=bool(cat.get("isActive", True)),
                phone=cat.get("phone"),
                workflow=cat.get("workflow") or "lab",
                created_at=_dt(cat.get("createdAt")) or utcnow(),
                updated_at=_dt(cat.get("updatedAt")) or utcnow(),
            )
        )
    counts["categories"] = len(core["categories"])

    for s in core["schemas"]:
        session.add(
            AttributeSchema(
                id=ids.get(s["id"]),
                company_id=ids.get(s["companyId"]),
                name=s["name"],
                description=s.get("description"),
                version=int(s.get("version") or 1),
                status=s.get("status") or "draft",
                fields=s.get("fields") or [],
                created_at=_dt(s.get("createdAt")) or utcnow(),
                updated_at=_dt(s.get("updatedAt")) or utcnow(),
            )
        )
    counts["schemas"] = len(core["schemas"])
    await session.flush()  # roles/companies must exist before employees (FK)

    role_keys = {r["id"]: r["key"] for r in core["roles"]}
    for e in core["employees"]:
        password = credentials.get(e["login"])
        session.add(
            Employee(
                id=ids.get(e["id"]),
                company_id=ids.get(e["companyId"]),
                branch_ids=ids.many(e.get("branchIds")),
                full_name=e["fullName"],
                login=e["login"],
                password_hash=hash_password(password) if password else None,
                phone=e.get("phone"),
                email=e.get("email"),
                role_id=ids.get(e["roleId"]),
                overrides={"allow": list((e.get("overrides") or {}).get("allow") or []), "deny": list((e.get("overrides") or {}).get("deny") or [])},
                category_ids=ids.many(e.get("categoryIds")),
                status=e.get("status") or "active",
                avatar_hue=int(e.get("avatarHue") or 160),
                is_super_admin=role_keys.get(e["roleId"]) == SUPERADMIN_ROLE_KEY,
                last_login_at=_dt(e.get("lastLoginAt")),
                created_at=_dt(e.get("createdAt")) or utcnow(),
                updated_at=_dt(e.get("updatedAt")) or utcnow(),
            )
        )
    counts["employees"] = len(core["employees"])
    await session.flush()

    signature_by_employee: dict[uuid.UUID, uuid.UUID] = {}
    for a in core["assets"]:
        company_id = ids.get(a["companyId"])
        data, mime, filename = _asset_bytes(a["url"])
        stored = await store_bytes(session, company_id=company_id, data=data, mime=mime, filename=filename or f"{a['id']}", is_public=True)
        employee_id = ids.opt(a.get("employeeId"))
        asset_id = ids.get(a["id"])
        session.add(
            TemplateAsset(
                id=asset_id,
                company_id=company_id,
                kind=a["kind"],
                name=a["name"],
                file_id=stored.id,
                width=Decimal(str(round(float(a["width"]), 2))),
                height=Decimal(str(round(float(a["height"]), 2))),
                employee_id=employee_id,
            )
        )
        if employee_id is not None and a["kind"] == "signature":
            signature_by_employee.setdefault(employee_id, asset_id)
    counts["assets"] = len(core["assets"])
    await session.flush()
    for employee_id, asset_id in signature_by_employee.items():
        await session.execute(update(Employee).where(Employee.id == employee_id).values(signature_asset_id=asset_id))

    for t in core["templates"]:
        session.add(
            ResultTemplate(
                id=ids.get(t["id"]),
                company_id=ids.get(t["companyId"]),
                name=t["name"],
                description=t.get("description"),
                status=t.get("status") or "draft",
                version=int(t.get("version") or 1),
                service_type_ids=ids.many(t.get("serviceTypeIds")),
                category_ids=ids.many(t.get("categoryIds")),
                scope=t.get("scope") or "item",
                language=t.get("language") or "uz",
                doc=remap_template_doc(t.get("doc") or {}, ids),
                thumbnail_url=t.get("thumbnailUrl"),
                usage=int(t.get("usage") or 0),
                created_at=_dt(t.get("createdAt")) or utcnow(),
                updated_at=_dt(t.get("updatedAt")) or utcnow(),
            )
        )
    counts["templates"] = len(core["templates"])

    for st in core["serviceTypes"]:
        branch_prices = {str(ids.get(k)): int(v) for k, v in (st.get("branchPrices") or {}).items()}
        session.add(
            ServiceType(
                id=ids.get(st["id"]),
                company_id=ids.get(st["companyId"]),
                category_id=ids.get(st["categoryId"]),
                name=st["name"],
                code=st.get("code"),
                description=st.get("description"),
                price=int(st.get("price") or 0),
                branch_prices=branch_prices,
                turnaround_days=int(st.get("turnaroundDays") or 1),
                order=int(st.get("order") or 0),
                is_active=bool(st.get("isActive", True)),
                schema_id=ids.opt(st.get("schemaId")),
                document_scope=st.get("documentScope") or "item",
                default_template_id=ids.opt(st.get("defaultTemplateId")),
                created_at=_dt(st.get("createdAt")) or utcnow(),
                updated_at=_dt(st.get("updatedAt")) or utcnow(),
            )
        )
    counts["serviceTypes"] = len(core["serviceTypes"])
    await session.flush()
    return counts


async def _seed_transactions(session: AsyncSession, tx: dict[str, Any], ids: IdMap, default_company: str) -> dict[str, int]:
    """Bulk-insert patients, orders, items, payments, documents, outbox and notifications."""
    counts: dict[str, int] = {}
    now = utcnow()
    for key in ("patients", "orders", "items", "payments", "documents", "outbox", "notifications"):
        for item in tx.get(key) or []:
            ids.alloc(item["id"])

    patients: list[dict[str, Any]] = []
    for p in tx.get("patients") or []:
        addr = p.get("address") or {}
        stats = p.get("stats") or {}
        region_id = ids.opt(addr.get("regionId"))
        district_old = addr.get("districtId")
        district_id = ids.get(district_old) if district_old in ids else None
        patients.append(
            {
                "id": ids.get(p["id"]),
                "company_id": ids.get(p["companyId"]),
                "full_name": p["fullName"],
                "phone": p["phone"],
                "phone_extra": p.get("phoneExtra"),
                "gender": p.get("gender"),
                "birth_date": _date(p.get("birthDate")),
                "passport_number": p.get("passportNumber"),
                "pinfl": p.get("pinfl"),
                "region_id": region_id,
                "district_id": district_id,
                "street": addr.get("street"),
                "workplace": p.get("workplace"),
                "discount_percent": int(p.get("discountPercent") or 0),
                "contract_number": p.get("contractNumber"),
                "note": p.get("note"),
                "tags": list(p.get("tags") or []),
                "stats_orders": int(stats.get("orders") or 0),
                "stats_last_visit_at": _dt(stats.get("lastVisitAt")),
                "stats_total_spent": int(stats.get("totalSpent") or 0),
                "portal_linked": bool((p.get("portal") or {}).get("linked", False)),
                "telegram_chat_id": None,
                "portal_last_login_at": None,
                "created_at": _dt(p.get("createdAt")) or now,
                "updated_at": _dt(p.get("updatedAt")) or now,
                "created_by": None,
                "deleted_at": None,
                "deleted_by": None,
            }
        )
    counts["patients"] = await _bulk_insert(session, Patient, patients)

    order_patient: dict[str, str] = {}
    branch_seq: dict[uuid.UUID, int] = {}
    orders: list[dict[str, Any]] = []
    for o in tx.get("orders") or []:
        order_patient[o["id"]] = o["patientId"]
        branch_id = ids.get(o["branchId"])
        branch_seq[branch_id] = max(branch_seq.get(branch_id, 0), _order_seq(o["number"]))
        status = o.get("status") or "open"
        orders.append(
            {
                "id": ids.get(o["id"]),
                "company_id": ids.get(o["companyId"]),
                "branch_id": branch_id,
                "number": o["number"],
                "patient_id": ids.get(o["patientId"]),
                "patient_name": o["patientName"],
                "patient_phone": o["patientPhone"],
                "created_by_employee_id": ids.get(o["createdByEmployeeId"]),
                "status": status,
                "payment": o.get("payment") or "unpaid",
                "subtotal": int(o.get("subtotal") or 0),
                "discount_percent": int(o.get("discountPercent") or 0),
                "discount_amount": int(o.get("discountAmount") or 0),
                "total": int(o.get("total") or 0),
                "paid_amount": int(o.get("paidAmount") or 0),
                "item_count": int(o.get("itemCount") or 0),
                "progress": o.get("progress") or {},
                "note": o.get("note"),
                "cancelled_at": _dt(o.get("cancelledAt")) if status == "cancelled" else None,
                "cancel_reason": o.get("cancelReason"),
                "completed_at": _dt(o.get("updatedAt")) if status == "completed" else None,
                "created_at": _dt(o.get("createdAt")) or now,
                "updated_at": _dt(o.get("updatedAt")) or now,
                "created_by": ids.get(o["createdByEmployeeId"]),
                "deleted_at": None,
                "deleted_by": None,
            }
        )
    counts["orders"] = await _bulk_insert(session, Order, orders)

    items: list[dict[str, Any]] = []
    for it in tx.get("items") or []:
        items.append(
            {
                "id": ids.get(it["id"]),
                "company_id": ids.get(it["companyId"]),
                "order_id": ids.get(it["orderId"]),
                "branch_id": ids.get(it["branchId"]),
                "service_type_id": ids.get(it["serviceTypeId"]),
                "service_name": it["serviceName"],
                "category_id": ids.get(it["categoryId"]),
                "category_name": it["categoryName"],
                "price": int(it.get("price") or 0),
                "final_price": int(it.get("finalPrice") or 0),
                "status": it.get("status") or "pending",
                "schema_id": ids.opt(it.get("schemaId")),
                "schema_version": it.get("schemaVersion"),
                "values": it.get("values") or {},
                "technician_id": ids.opt(it.get("technicianId")),
                "technician_name": it.get("technicianName"),
                "entered_at": _dt(it.get("enteredAt")),
                "submitted_at": _dt(it.get("submittedAt")),
                "doctor_id": ids.opt(it.get("doctorId")),
                "doctor_name": it.get("doctorName"),
                "approved_at": _dt(it.get("approvedAt")),
                "reject_reason": it.get("rejectReason"),
                "document_id": ids.opt(it.get("documentId")),
                "lab_note": it.get("labNote"),
                "created_at": _dt(it.get("createdAt")) or now,
                "updated_at": _dt(it.get("updatedAt")) or now,
                "created_by": None,
                "deleted_at": None,
                "deleted_by": None,
            }
        )
    counts["items"] = await _bulk_insert(session, OrderItem, items)

    payments: list[dict[str, Any]] = []
    for pay in tx.get("payments") or []:
        payments.append(
            {
                "id": ids.get(pay["id"]),
                "company_id": ids.get(pay["companyId"]),
                "order_id": ids.get(pay["orderId"]),
                "branch_id": ids.get(pay["branchId"]),
                "amount": int(pay["amount"]),
                "method": pay.get("method") or "cash",
                "employee_id": ids.get(pay["employeeId"]),
                "note": pay.get("note"),
                "refunded_at": _dt(pay.get("refundedAt")),
                "refund_reason": pay.get("refundReason"),
                "created_at": _dt(pay.get("createdAt")) or now,
                "updated_at": _dt(pay.get("updatedAt")) or now,
                "created_by": ids.get(pay["employeeId"]),
                "deleted_at": None,
                "deleted_by": None,
            }
        )
    counts["payments"] = await _bulk_insert(session, Payment, payments)

    documents: list[dict[str, Any]] = []
    for d in tx.get("documents") or []:
        item_ids = ids.many(d.get("orderItemIds") or ([d["orderItemId"]] if d.get("orderItemId") else []))
        documents.append(
            {
                "id": ids.get(d["id"]),
                "company_id": ids.get(d["companyId"]),
                "order_id": ids.get(d["orderId"]),
                "patient_id": ids.get(order_patient.get(d["orderId"])),
                "order_item_id": ids.opt(d.get("orderItemId")),
                "order_item_ids": item_ids,
                "template_id": ids.get(d["templateId"]),
                "template_version": int(d.get("templateVersion") or 1),
                "title": d["title"],
                "status": d.get("status") or "final",
                "pdf_file_id": None,
                "deliveries": d.get("deliveries") or [],
                "snapshot": {},
                "public_token": secrets.token_urlsafe(24),
                "created_at": _dt(d.get("createdAt")) or now,
                "updated_at": _dt(d.get("updatedAt")) or now,
                "created_by": None,
                "deleted_at": None,
                "deleted_by": None,
            }
        )
    counts["documents"] = await _bulk_insert(session, ResultDocument, documents)

    outbox: list[dict[str, Any]] = []
    for m in tx.get("outbox") or []:
        status = m.get("status") or "queued"
        scheduled_at = _dt(m.get("scheduledAt"))
        pending = status in ("queued", "scheduled")
        outbox.append(
            {
                "id": ids.get(m["id"]),
                "company_id": ids.get(m["companyId"]),
                "branch_id": ids.opt(m.get("branchId")),
                "patient_id": ids.opt(m.get("patientId")),
                "order_id": ids.opt(m.get("orderId")),
                "document_id": ids.opt(m.get("documentId")),
                "channel": m.get("channel") or "sms",
                "kind": m.get("kind") or "broadcast",
                "to": m["to"],
                "text": m.get("text") or "",
                "status": status,
                "scheduled_at": scheduled_at,
                "sent_at": _dt(m.get("sentAt")),
                "attempts": int(m.get("attempts") or 0),
                "next_attempt_at": (scheduled_at or _dt(m.get("createdAt")) or now) if pending else None,
                "leased_until": None,
                "provider_message_id": m.get("providerMessageId"),
                "error": m.get("error"),
                "payload": {},
                "created_at": _dt(m.get("createdAt")) or now,
                "updated_at": _dt(m.get("updatedAt")) or now,
                "created_by": None,
                "deleted_at": None,
                "deleted_by": None,
            }
        )
    counts["outbox"] = await _bulk_insert(session, OutboxMessage, outbox)

    notifications: list[dict[str, Any]] = []
    for n in tx.get("notifications") or []:
        notifications.append(
            {
                "id": ids.get(n["id"]),
                "company_id": ids.get(n.get("companyId") or default_company),
                "employee_id": None,
                "title": n["title"],
                "body": n["body"],
                "kind": n.get("kind") or "info",
                "link": n.get("link"),
                "read_by": [],
                "created_at": _dt(n.get("createdAt")) or now,
                "updated_at": _dt(n.get("createdAt")) or now,
                "created_by": None,
                "deleted_at": None,
                "deleted_by": None,
            }
        )
    counts["notifications"] = await _bulk_insert(session, Notification, notifications)

    for branch_id, seq in branch_seq.items():
        await session.execute(update(Branch).where(Branch.id == branch_id, Branch.order_seq < seq).values(order_seq=seq))
    return counts


async def seed_demo(with_transactions: bool) -> dict[str, int]:
    """Load the demo dataset. Idempotent per company slug; transactions can be added later."""
    core = _load_json(DEMO_CORE_FILE)
    default_company = core["companies"][0]["id"]
    async with session_scope() as session:
        await seed_reference(session)
        existing = {c["slug"]: await _company_by_slug(session, c["slug"]) for c in core["companies"]}
        present = [c for c in existing.values() if c is not None]
        counts: dict[str, int] = {}
        if present and len(present) != len(existing):
            names = ", ".join(c.slug for c in present)
            raise CliError(f"Demo partially present ({names}) — remove/rename those companies or restore the DB before seeding again")

        if not present:
            ids = IdMap()
            _map_geo(core, await GeoIndex.load(session), ids)
            counts.update(await _seed_core(session, core, ids))
            core_map = ids.as_dict()
            print(f"core: {counts}")
        else:
            main = existing[core["companies"][0]["slug"]]
            assert main is not None
            saved = (main.settings or {}).get(SEED_SETTINGS_KEY) or {}
            if not with_transactions:
                raise CliError(
                    f"Demo company '{main.slug}' already exists — nothing to do. "
                    "Hint: pass --with-transactions to add the transactional dataset, or drop the company rows to reseed."
                )
            if saved.get("transactions"):
                raise CliError(f"Demo transactions already loaded into '{main.slug}' — refusing to duplicate")
            if not saved.get("idMap"):
                raise CliError(f"Company '{main.slug}' was not created by seed-demo (no id map) — cannot attach transactions")
            core_map = dict(saved["idMap"])
            ids = IdMap(core_map)
            _map_geo(core, await GeoIndex.load(session), ids)
            print(f"core: already present ({main.slug}); attaching transactions")

        if with_transactions:
            tx = _load_json(DEMO_TX_FILE)
            counts.update(await _seed_transactions(session, tx, ids, default_company))
            print("transactions: " + ", ".join(f"{k}={counts[k]}" for k in ("patients", "orders", "items", "payments", "documents", "outbox", "notifications")))

        # persist the core id map on the primary company so a later --with-transactions run can attach
        main_id = ids.get(default_company)
        main_company = await session.get(Company, main_id)
        assert main_company is not None
        main_company.settings = {
            **(main_company.settings or {}),
            SEED_SETTINGS_KEY: {"source": "seed/demo/core.json", "idMap": core_map, "transactions": with_transactions},
        }
        await audit(session, actor_type="system", actor_id=None, company_id=main_id, action="seed", entity="company", entity_id=main_id, after={"counts": counts, "withTransactions": with_transactions})
    return counts


# --------------------------------------------------------------------------- admin commands


async def create_superadmin(*, login: str, password: str, company_slug: str, company_name: str | None, full_name: str) -> str:
    """Create a platform superadmin employee (and its company when the slug is new). Returns employee id."""
    async with session_scope() as session:
        taken = (await session.execute(select(Employee).where(func.lower(Employee.login) == login.lower(), alive(Employee)))).scalar_one_or_none()
        if taken is not None:
            raise CliError(f"Login '{login}' already exists (use set-password to reset its password)")
        company = await _company_by_slug(session, company_slug)
        if company is None:
            company = Company(name=company_name or company_slug, slug=slugify(company_slug), settings={})
            session.add(company)
            await session.flush()
            session.add(Role(company_id=company.id, key="admin", name="Administrator", description="Klinika administratori", permissions=list(COMPANY_ADMIN_PERMISSIONS), is_system=True))
            await audit(session, actor_type="system", actor_id=None, company_id=company.id, action="create", entity="company", entity_id=company.id, after={"name": company.name, "slug": company.slug})
        role = await _platform_superadmin_role(session)
        employee = Employee(company_id=company.id, full_name=full_name, login=login, password_hash=hash_password(password), role_id=role.id, is_super_admin=True)
        session.add(employee)
        await session.flush()
        await audit(session, actor_type="system", actor_id=None, company_id=company.id, action="create", entity="employee", entity_id=employee.id, after={"login": login, "isSuperAdmin": True})
        return str(employee.id)


async def set_password(*, login: str, password: str) -> None:
    """Reset a staff password and clear the lock-out counters."""
    async with session_scope() as session:
        employee = (await session.execute(select(Employee).where(func.lower(Employee.login) == login.lower(), alive(Employee)))).scalar_one_or_none()
        if employee is None:
            raise CliError(f"Employee with login '{login}' not found")
        employee.password_hash = hash_password(password)
        employee.failed_logins = 0
        employee.locked_until = None
        await audit(session, actor_type="system", actor_id=None, company_id=employee.company_id, action="password_reset", entity="employee", entity_id=employee.id)


async def ensure_partitions(months_ahead: int = 3) -> None:
    """Create the next ``months_ahead`` monthly audit_log partitions (function from the initial migration)."""
    async with session_scope() as session:
        await session.execute(text("SELECT ensure_audit_partitions(:m)"), {"m": months_ahead})


# --------------------------------------------------------------------------- entrypoint


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="TibDaftari operations CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("seed-reference", help="upsert regions/districts of Uzbekistan")

    demo = sub.add_parser("seed-demo", help="load the demo dataset (companies, catalog, templates, staff)")
    demo.add_argument("--with-transactions", action="store_true", help="also load patients/orders/items/payments/documents/outbox")

    sa = sub.add_parser("create-superadmin", help="create a platform superadmin employee")
    sa.add_argument("--login", required=True)
    sa.add_argument("--password", required=True)
    sa.add_argument("--company-slug", required=True)
    sa.add_argument("--company-name", default=None, help="used only when the company is created")
    sa.add_argument("--name", required=True, help="employee full name")

    sp = sub.add_parser("set-password", help="reset a staff password")
    sp.add_argument("--login", required=True)
    sp.add_argument("--password", required=True)

    ep = sub.add_parser("ensure-partitions", help="create upcoming audit_log partitions")
    ep.add_argument("--months", type=int, default=3)
    return parser


async def _run(args: argparse.Namespace) -> None:
    try:
        if args.command == "seed-reference":
            async with session_scope() as session:
                regions, districts = await seed_reference(session)
            print(f"reference: {regions} regions, {districts} districts upserted")
        elif args.command == "seed-demo":
            counts = await seed_demo(with_transactions=args.with_transactions)
            print("done: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
        elif args.command == "create-superadmin":
            employee_id = await create_superadmin(login=args.login, password=args.password, company_slug=args.company_slug, company_name=args.company_name, full_name=args.name)
            print(f"superadmin created: {args.login} ({employee_id})")
        elif args.command == "set-password":
            await set_password(login=args.login, password=args.password)
            print(f"password updated for {args.login}")
        elif args.command == "ensure-partitions":
            await ensure_partitions(args.months)
            print(f"audit_log partitions ensured for the next {args.months} months")
    finally:
        await dispose_engine()


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint; returns the process exit code."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    args = build_parser().parse_args(argv)
    try:
        asyncio.run(_run(args))
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
