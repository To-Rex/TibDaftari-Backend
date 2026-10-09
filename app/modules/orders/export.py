"""Cheque list → Excel (.xlsx): exactly the cheques the list's filters and sort select (up to EXPORT_LIMIT).

One sheet: a title, the filters the user had on (`caption`, already in their language), then one row per cheque
with the money split by payment method, and a totals row. Numbers stay numbers (#,##0) and dates real dates, so
Excel can sort, filter and sum them; the header row has an autofilter and stays frozen.
"""

from __future__ import annotations

import io
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

import xlsxwriter

from app.core.timeutil import DEFAULT_TZ

EXPORT_LIMIT = 20_000
METHODS = ("cash", "card", "transfer", "insurance")

LABELS: dict[str, dict[str, str]] = {
    "uz": {
        "sheet": "Cheklar", "title": "Cheklar", "no": "№", "number": "Chek", "date": "Sana", "branch": "Filial", "patient": "Bemor",
        "phone": "Telefon", "items": "Xizmatlar soni", "services": "Xizmatlar", "subtotal": "Summa", "discountPercent": "Chegirma %",
        "discount": "Chegirma", "total": "Jami", "paid": "To‘langan", "debt": "Qarz", "cash": "Naqd", "card": "Karta",
        "transfer": "O‘tkazma", "insurance": "Sug‘urta", "payment": "To‘lov holati", "status": "Holat", "createdBy": "Chekni ochgan",
        "totals": "Jami", "generated": "Yaratildi: {at} · {n} ta chek", "truncated": "Faqat birinchi {n} ta chek eksport qilindi — filtrlarni toraytiring",
    },
    "ru": {
        "sheet": "Чеки", "title": "Чеки", "no": "№", "number": "Чек", "date": "Дата", "branch": "Филиал", "patient": "Пациент",
        "phone": "Телефон", "items": "Кол-во услуг", "services": "Услуги", "subtotal": "Сумма", "discountPercent": "Скидка %",
        "discount": "Скидка", "total": "Итого", "paid": "Оплачено", "debt": "Долг", "cash": "Наличные", "card": "Карта",
        "transfer": "Перевод", "insurance": "Страховка", "payment": "Оплата", "status": "Статус", "createdBy": "Кто открыл",
        "totals": "Итого", "generated": "Создано: {at} · чеков: {n}", "truncated": "Выгружены только первые {n} чеков — сузьте фильтры",
    },
    "en": {
        "sheet": "Orders", "title": "Orders", "no": "#", "number": "Order", "date": "Date", "branch": "Branch", "patient": "Patient",
        "phone": "Phone", "items": "Services", "services": "Service names", "subtotal": "Subtotal", "discountPercent": "Discount %",
        "discount": "Discount", "total": "Total", "paid": "Paid", "debt": "Debt", "cash": "Cash", "card": "Card",
        "transfer": "Transfer", "insurance": "Insurance", "payment": "Payment", "status": "Status", "createdBy": "Opened by",
        "totals": "Total", "generated": "Generated: {at} · {n} orders", "truncated": "Only the first {n} orders were exported — narrow the filters",
    },
}
ORDER_STATUS: dict[str, dict[str, str]] = {
    "uz": {"draft": "Qoralama", "open": "Ochiq", "in_progress": "Jarayonda", "completed": "Yakunlangan", "cancelled": "Bekor qilingan"},
    "ru": {"draft": "Черновик", "open": "Открыт", "in_progress": "В работе", "completed": "Завершён", "cancelled": "Отменён"},
    "en": {"draft": "Draft", "open": "Open", "in_progress": "In progress", "completed": "Completed", "cancelled": "Cancelled"},
}
PAYMENT_STATUS: dict[str, dict[str, str]] = {
    "uz": {"unpaid": "To‘lanmagan", "partial": "Qisman", "paid": "To‘langan", "refunded": "Qaytarilgan"},
    "ru": {"unpaid": "Не оплачен", "partial": "Частично", "paid": "Оплачен", "refunded": "Возвращён"},
    "en": {"unpaid": "Unpaid", "partial": "Partial", "paid": "Paid", "refunded": "Refunded"},
}


def fmt_phone(p: str | None) -> str:
    d = "".join(ch for ch in (p or "") if ch.isdigit())
    if len(d) == 12 and d.startswith("998"):
        return f"+998 {d[3:5]} {d[5:8]}-{d[8:10]}-{d[10:]}"
    return p or ""


def local_naive(dt: datetime | None) -> datetime | None:
    """Excel has no time zones: the clinic's wall-clock time."""
    return dt.astimezone(DEFAULT_TZ).replace(tzinfo=None) if dt else None


def build_xlsx(
    rows: Sequence[Any],
    payments: Mapping[Any, Mapping[str, int]],
    services: Mapping[Any, Sequence[str]],
    *,
    lang: str = "uz",
    caption: str | None = None,
    truncated: bool = False,
    generated_at: datetime,
) -> bytes:
    """`rows`: objects with `.Order`, `.creator`, `.branch` (see repository.export_orders)."""
    lang = lang if lang in LABELS else "uz"
    labels = LABELS[lang]
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    ws = wb.add_worksheet(labels["sheet"])
    title_f = wb.add_format({"bold": True, "font_size": 14})
    note_f = wb.add_format({"italic": True, "font_color": "#555555"})
    warn_f = wb.add_format({"bold": True, "font_color": "#B45309"})
    head_f = wb.add_format({"bold": True, "bg_color": "#E3F1EE", "border": 1, "text_wrap": True, "valign": "vcenter"})
    text_f = wb.add_format({"border": 1, "valign": "top"})
    wrap_f = wb.add_format({"border": 1, "text_wrap": True, "valign": "top"})
    int_f = wb.add_format({"border": 1, "valign": "top"})
    money_f = wb.add_format({"border": 1, "num_format": "#,##0", "valign": "top"})
    date_f = wb.add_format({"border": 1, "num_format": "dd.mm.yyyy hh:mm", "valign": "top"})
    tot_label_f = wb.add_format({"bold": True, "top": 2, "bg_color": "#F4F6F5"})
    tot_f = wb.add_format({"bold": True, "top": 2, "num_format": "#,##0", "bg_color": "#F4F6F5"})

    cols: list[tuple[str, int]] = [
        ("no", 6), ("number", 13), ("date", 17), ("branch", 18), ("patient", 26), ("phone", 18), ("items", 9), ("services", 40),
        ("subtotal", 13), ("discountPercent", 10), ("discount", 12), ("total", 13), ("paid", 13), ("debt", 12),
        *[(m, 12) for m in METHODS], ("payment", 14), ("status", 15), ("createdBy", 22),
    ]
    ci = {key: i for i, (key, _) in enumerate(cols)}
    ws.write(0, 0, labels["title"], title_f)
    if caption:
        ws.write(1, 0, caption, note_f)
    ws.write(2, 0, labels["generated"].format(at=local_naive(generated_at).strftime("%d.%m.%Y %H:%M") if generated_at else "", n=len(rows)), note_f)
    if truncated:
        ws.write(3, 0, labels["truncated"].format(n=len(rows)), warn_f)
    head = 4
    ws.set_row(head, 30)
    for i, (key, width) in enumerate(cols):
        ws.write(head, i, labels[key], head_f)
        ws.set_column(i, i, width)

    sums = {k: 0 for k in ("subtotal", "discount", "total", "paid", "debt", *METHODS)}
    r = head
    for n, row in enumerate(rows, start=1):
        o = row.Order
        r = head + n
        live = o.status != "cancelled"
        debt = max(0, o.total - o.paid_amount) if live else 0
        by_method = payments.get(o.id, {})
        ws.write_number(r, ci["no"], n, int_f)
        ws.write_string(r, ci["number"], o.number, text_f)
        created = local_naive(o.created_at)
        if created:
            ws.write_datetime(r, ci["date"], created, date_f)
        ws.write_string(r, ci["branch"], row.branch or "", text_f)
        ws.write_string(r, ci["patient"], o.patient_name or "", text_f)
        ws.write_string(r, ci["phone"], fmt_phone(o.patient_phone), text_f)
        ws.write_number(r, ci["items"], o.item_count, int_f)
        ws.write_string(r, ci["services"], ", ".join(services.get(o.id, [])), wrap_f)
        ws.write_number(r, ci["subtotal"], o.subtotal, money_f)
        ws.write_number(r, ci["discountPercent"], o.discount_percent, int_f)
        ws.write_number(r, ci["discount"], o.discount_amount, money_f)
        ws.write_number(r, ci["total"], o.total, money_f)
        ws.write_number(r, ci["paid"], o.paid_amount, money_f)
        ws.write_number(r, ci["debt"], debt, money_f)
        for m in METHODS:
            amount = int(by_method.get(m, 0))
            if amount:
                ws.write_number(r, ci[m], amount, money_f)
            else:
                ws.write_blank(r, ci[m], None, money_f)
            sums[m] += amount
        ws.write_string(r, ci["payment"], PAYMENT_STATUS[lang].get(o.payment, o.payment), text_f)
        ws.write_string(r, ci["status"], ORDER_STATUS[lang].get(o.status, o.status), text_f)
        ws.write_string(r, ci["createdBy"], row.creator or "", text_f)
        if live:  # money totals leave cancelled cheques out (same as the list's summary)
            for k, v in (("subtotal", o.subtotal), ("discount", o.discount_amount), ("total", o.total), ("paid", o.paid_amount), ("debt", debt)):
                sums[k] += v
    if rows:
        ws.autofilter(head, 0, r, len(cols) - 1)
        t = r + 1
        ws.write(t, ci["number"], labels["totals"], tot_label_f)
        for i in range(len(cols)):
            key = cols[i][0]
            if key in sums:
                ws.write_number(t, i, sums[key], tot_f)
            elif i != ci["number"]:
                ws.write_blank(t, i, None, tot_label_f)
    ws.freeze_panes(head + 1, 2)
    wb.close()
    return buf.getvalue()
