"""Built-in documents: the standard cashier cheque every company starts with — seeded when a company gets
its first branch, offered as the "Standart chek" start option on the receipt-template page, and created on
demand through POST /companies/{id}/templates/default-receipt.

Pure functions (no DB, no request context) so migrations can build the same document."""

from __future__ import annotations

from typing import Any

DEFAULT_RECEIPT_NAME: dict[str, str] = {"uz": "Standart chek", "ru": "Стандартный чек", "en": "Standard receipt"}

_LABELS: dict[str, dict[str, str]] = {
    "uz": {
        "phone": "Tel",
        "number": "Chek",
        "patient": "Bemor",
        "cashier": "Kassir",
        "service": "Xizmat",
        "amount": "Summa",
        "subtotal": "Xizmatlar summasi",
        "discount": "Chegirma",
        "total": "JAMI",
        "paid": "To‘langan",
        "remaining": "Qoldiq",
        "payDate": "Sana",
        "payMethod": "To‘lov turi",
        "footer": "Murojaatingiz uchun rahmat!",
    },
    "ru": {
        "phone": "Тел",
        "number": "Чек",
        "patient": "Пациент",
        "cashier": "Кассир",
        "service": "Услуга",
        "amount": "Сумма",
        "subtotal": "Сумма услуг",
        "discount": "Скидка",
        "total": "ИТОГО",
        "paid": "Оплачено",
        "remaining": "Остаток",
        "payDate": "Дата",
        "payMethod": "Способ оплаты",
        "footer": "Спасибо за обращение!",
    },
    "en": {
        "phone": "Tel",
        "number": "Receipt",
        "patient": "Patient",
        "cashier": "Cashier",
        "service": "Service",
        "amount": "Amount",
        "subtotal": "Subtotal",
        "discount": "Discount",
        "total": "TOTAL",
        "paid": "Paid",
        "remaining": "Balance due",
        "payDate": "Date",
        "payMethod": "Method",
        "footer": "Thank you for visiting!",
    },
}


def _style(size: float, weight: int = 400, align: str = "left") -> dict[str, Any]:
    return {"fontFamily": "sans", "fontSize": size, "fontWeight": weight, "color": "#000000", "align": align, "lineHeight": 1.2}


def default_receipt_doc(paper: str = "Receipt80", language: str = "uz") -> dict[str, Any]:
    """The standard cheque as a TemplateDoc: header (company, branch, phone), cheque number and time, patient,
    cashier, the services table, totals (discount and balance only when there is one), the payments table
    (only when something was paid) and a thank-you line. Both tables `grow` with their rows, so everything
    below them moves along."""
    lang = language if language in _LABELS else "uz"
    lb = _LABELS[lang]
    narrow = paper == "Receipt58"
    width = 219 if narrow else 302
    m = 6 if narrow else 8
    cw = width - 2 * m
    fs = 9.5 if narrow else 10.5
    row = 16 if narrow else 18
    els: list[dict[str, Any]] = []

    def text(id_: str, y: float, h: float, txt: str, *, size: float = fs, weight: int = 400, align: str = "left", show_if: str | None = None) -> None:
        el: dict[str, Any] = {"id": id_, "type": "text", "x": m, "y": y, "w": cw, "h": h, "text": txt, "style": _style(size, weight, align)}
        if show_if:
            el["showIf"] = show_if
        els.append(el)

    def line(id_: str, y: float) -> None:
        els.append({"id": id_, "type": "line", "x": m, "y": y, "w": cw, "h": 1, "orientation": "horizontal", "color": "#000000", "width": 1})

    def table(id_: str, y: float, h: float, field_key: str, columns: list[dict[str, Any]], *, show_if: str | None = None) -> None:
        el: dict[str, Any] = {
            "id": id_,
            "type": "table",
            "x": m,
            "y": y,
            "w": cw,
            "h": h,
            "fieldKey": field_key,
            "columns": columns,
            "headerStyle": _style(fs - 0.5, 700),
            "cellStyle": _style(fs - 0.5),
            "rowHeight": row,
            "borderColor": "#000000",
            "borderWidth": 1,
            "showHeader": True,
            "showRowNumber": False,
            "highlightAbnormal": False,
            "grow": True,
        }
        if show_if:
            el["showIf"] = show_if
        els.append(el)

    # column widths are relative — the № column must still fit two digits on the narrow strip
    num_w, name_w, sum_w = (26, 129, 52) if narrow else (30, 172, 84)
    y = m
    text("hdr", y, 34, "{company.name}", size=fs + 3.5, weight=700, align="center")  # two lines for long names
    y += 36
    text("br", y, 14, "{branch.name}", align="center")
    y += 14
    text("adr", y, 14, "{branch.address}", size=fs - 1, align="center", show_if="{branch.address}")
    y += 14
    text("tel", y, 14, f"{lb['phone']}: {{branch.phone}}", align="center", show_if="{branch.phone}")
    y += 18
    line("l1", y)
    y += 6
    text("no", y, 16, f"{lb['number']}: {{order.number}}", size=fs + 1, weight=700)
    text("dt", y, 16, "{order.dateTime}", align="right")
    y += 18
    text("pt", y, 16, f"{lb['patient']}: {{patient.fullName}}")
    y += 16
    text("ph", y, 16, f"{lb['phone']}: {{patient.phone}}", show_if="{patient.phone}")
    y += 16
    text("cs", y, 16, f"{lb['cashier']}: {{cashier.name}}", show_if="{cashier.name}")
    y += 20
    table(
        "items",
        y,
        row * 4,
        "items",
        [
            {"id": "c1", "header": "№", "bind": "i", "width": num_w, "align": "left"},
            {"id": "c2", "header": lb["service"], "bind": "name", "width": name_w, "align": "left"},
            {"id": "c3", "header": lb["amount"], "bind": "finalPrice", "width": sum_w, "align": "right"},
        ],
    )
    y += row * 4 + 6
    text("sub", y, 16, f"{lb['subtotal']}: {{order.subtotal}}", align="right")
    y += 16
    text("dsc", y, 16, f"{lb['discount']} {{order.discountPercent}}%: -{{order.discountAmount}}", align="right", show_if="{order.hasDiscount}")
    y += 18
    text("tot", y, 20, f"{lb['total']}: {{order.total}}", size=fs + 2.5, weight=700, align="right")
    y += 22
    text("pd", y, 16, f"{lb['paid']}: {{order.paidAmount}}", align="right")
    y += 16
    text("rm", y, 16, f"{lb['remaining']}: {{order.remaining}}", weight=700, align="right", show_if="{order.hasRemaining}")
    y += 20
    line("l2", y)
    y += 6
    table(
        "payments",
        y,
        row * 3,
        "payments",
        [
            {"id": "p1", "header": lb["payDate"], "bind": "date", "width": 120, "align": "left"},
            {"id": "p2", "header": lb["payMethod"], "bind": "method", "width": 80, "align": "left"},
            {"id": "p3", "header": lb["amount"], "bind": "amount", "width": 86, "align": "right"},
        ],
        show_if="{order.hasPayments}",
    )
    y += row * 3 + 10
    text("ft", y, 16, lb["footer"], align="center")
    return {"paper": paper, "orientation": "portrait", "background": "#ffffff", "margin": m, "elements": els}
