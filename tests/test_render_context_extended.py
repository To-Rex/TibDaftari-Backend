"""Pure checks for the extended render-context keys (no DB)."""

from app.modules.templates.renderer import build_render_context
from app.modules.templates.renderer import expressions as ex
from app.modules.templates.renderer.context import to_render_item


def test_extended_context_keys() -> None:
    item = to_render_item(
        code="LG-1",
        service_type_id="s1",
        service_name="Xizmat",
        status="approved",
        status_label="Tasdiqlangan",
        values={"hb": 12},
        schema={"fields": [{"key": "hb", "label": "Gemoglobin", "type": "number"}]},
        entered_at="2026-08-16T05:00:00Z",
        submitted_at="2026-08-16T06:00:00Z",
        approved_at="2026-08-16T07:00:00Z",
        technician="Lab",
        doctor="Doc",
        price=100000,
        final_price=90000,
        category="Lab",
        schema_version=2,
    )
    ctx = build_render_context(
        patient={
            "fullName": "Test Bemor",
            "phone": "998901234567",
            "birthDate": "1990-01-15",
            "gender": "female",
            "pinfl": "12345678901234",
            "workplace": "Klinika",
            "discountPercent": 5,
            "contractNumber": "C-1",
            "note": "Izoh",
            "tags": ["VIP", "Shartnoma"],
            "street": "Ko‘cha 1",
        },
        order={
            "number": "UR-000001",
            "createdAt": "2026-08-16T05:00:00Z",
            "status": "completed",
            "payment": "paid",
            "subtotal": 100000,
            "discountPercent": 0,
            "discountAmount": 0,
            "total": 100000,
            "paidAmount": 100000,
            "itemCount": 1,
            "note": "Chek izohi",
        },
        item={
            "serviceName": "Xizmat",
            "serviceCode": "LG-1",
            "categoryName": "Laboratoriya",
            "status": "approved",
            "price": 100000,
            "finalPrice": 90000,
            "enteredAt": "2026-08-16T05:00:00Z",
            "submittedAt": "2026-08-16T06:00:00Z",
            "approvedAt": "2026-08-16T07:00:00Z",
            "technicianName": "Lab",
            "doctorName": "Doc",
            "labNote": "Sifatli",
            "rejectReason": "—",
            "schemaVersion": 2,
            "values": {"hb": 12},
        },
        company={"name": "Klinika", "legalName": "Klinika MChJ", "slug": "klinika", "phone": "+998 90 000-00-00", "email": "info@example.uz", "address": "Manzil"},
        branch={"name": "Markaziy", "code": "M", "address": "Urganch", "phone": "+998 90 111-11-11", "timezone": "Asia/Tashkent"},
        category={"name": "Laboratoriya", "code": "LAB", "phone": "97-000-00-00"},
        district_name="Urganch shahri",
        region_name="Xorazm viloyati",
        items=[item],
        language="uz",
    )

    assert ctx["patient"]["pinfl"] == "12345678901234"
    assert ctx["patient"]["region"] == "Xorazm viloyati"
    assert ctx["patient"]["district"] == "Urganch shahri"
    assert ctx["patient"]["street"] == "Ko‘cha 1"
    assert ctx["patient"]["tags"] == "VIP, Shartnoma"
    assert ctx["order"]["time"] == "10:00"
    assert ctx["order"]["statusLabel"] == "Yakunlangan"
    assert ctx["order"]["paymentLabel"] == "To‘langan"
    assert ctx["item"]["serviceCode"] == "LG-1"
    assert ctx["item"]["statusLabel"] == "Tasdiqlangan"
    assert ctx["item"]["price"] == "100,000"
    assert ctx["company"]["legalName"] == "Klinika MChJ"
    assert ctx["branch"]["code"] == "M"
    assert ctx["category"]["code"] == "LAB"
    assert ctx["now"]

    assert ex.service_value(ctx, "LG-1.price") == "100,000"
    assert ex.service_value(ctx, "LG-1.statusLabel") == "Tasdiqlangan"
    assert ex.service_value(ctx, "LG-1.category") == "Lab"
    row = ex.item_rows(ctx)[0]
    assert row["statusLabel"] == "Tasdiqlangan"
    assert row["price"] == "100,000"
    assert row["approvedAt"] == "16.08.2026 12:00"
    assert ex.interpolate("{patient.pinfl}|{order.statusLabel}|{item.serviceCode}|{company.legalName}|{branch.code}|{category.code}", ctx) == "12345678901234|Yakunlangan|LG-1|Klinika MChJ|M|LAB"
