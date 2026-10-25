"""Reports → Excel (.xlsx): patients × analyses with amounts, and the lab / service / branch /
employee / daily breakdowns. One sheet per cut; numbers stay numbers so Excel can sum them."""

from __future__ import annotations

import io
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

import xlsxwriter

from app.core.timeutil import DEFAULT_TZ

EXPORT_LIMIT = 20_000


@dataclass(slots=True)
class DetailRow:
    date: datetime | None
    number: str
    patient: str
    phone: str
    service: str
    laboratory: str
    branch: str
    price: int
    status: str


@dataclass(slots=True)
class AggRow:
    name: str
    count: int
    revenue: int


@dataclass(slots=True)
class DayRow:
    date: str
    orders: int
    revenue: int


LABELS: dict[str, dict[str, str]] = {
    "uz": {
        "patients_sheet": "Bemorlar", "lab_sheet": "Laboratoriya", "service_sheet": "Analiz turlari", "branch_sheet": "Filiallar",
        "employee_sheet": "Xodimlar", "daily_sheet": "Kunlik",
        "no": "№", "date": "Sana", "order": "Chek", "patient": "Bemor", "phone": "Telefon", "service": "Analiz turi",
        "lab": "Laboratoriya", "branch": "Filial", "price": "Summa", "status": "Holat", "name": "Nomi", "count": "Soni",
        "revenue": "Tushum", "orders": "Buyurtmalar", "totals": "Jami", "generated": "Yaratildi: {at} · davr: {from_} – {to}",
    },
    "ru": {
        "patients_sheet": "Пациенты", "lab_sheet": "Лаборатория", "service_sheet": "Виды анализов", "branch_sheet": "Филиалы",
        "employee_sheet": "Сотрудники", "daily_sheet": "По дням",
        "no": "№", "date": "Дата", "order": "Чек", "patient": "Пациент", "phone": "Телефон", "service": "Вид анализа",
        "lab": "Лаборатория", "branch": "Филиал", "price": "Сумма", "status": "Статус", "name": "Название", "count": "Кол-во",
        "revenue": "Выручка", "orders": "Заказы", "totals": "Итого", "generated": "Создано: {at} · период: {from_} – {to}",
    },
    "en": {
        "patients_sheet": "Patients", "lab_sheet": "Laboratory", "service_sheet": "Analyses", "branch_sheet": "Branches",
        "employee_sheet": "Employees", "daily_sheet": "Daily",
        "no": "#", "date": "Date", "order": "Order", "patient": "Patient", "phone": "Phone", "service": "Analysis",
        "lab": "Laboratory", "branch": "Branch", "price": "Amount", "status": "Status", "name": "Name", "count": "Count",
        "revenue": "Revenue", "orders": "Orders", "totals": "Total", "generated": "Generated: {at} · period: {from_} – {to}",
    },
}
ITEM_STATUS: dict[str, dict[str, str]] = {
    "uz": {"pending": "Navbatda", "entered": "Kiritildi", "submitted": "Yuborilgan", "approved": "Tasdiqlangan", "rejected": "Qaytarilgan", "cancelled": "Bekor qilingan"},
    "ru": {"pending": "В очереди", "entered": "Введено", "submitted": "Отправлено", "approved": "Подтверждено", "rejected": "Возвращено", "cancelled": "Отменено"},
    "en": {"pending": "Pending", "entered": "Entered", "submitted": "Submitted", "approved": "Approved", "rejected": "Rejected", "cancelled": "Cancelled"},
}


def local_naive(dt: datetime | None) -> datetime | None:
    return dt.astimezone(DEFAULT_TZ).replace(tzinfo=None) if dt else None


def build_xlsx(
    detail: Sequence[DetailRow],
    labs: Sequence[AggRow],
    services: Sequence[AggRow],
    branches: Sequence[AggRow],
    employees: Sequence[AggRow],
    daily: Sequence[DayRow],
    *,
    lang: str,
    d_from: date,
    d_to: date,
    generated_at: datetime,
) -> bytes:
    lang = lang if lang in LABELS else "uz"
    labels = LABELS[lang]
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})

    title_f = wb.add_format({"bold": True, "font_size": 14})
    note_f = wb.add_format({"italic": True, "font_color": "#555555"})
    head_f = wb.add_format({"bold": True, "bg_color": "#E3F1EE", "border": 1, "text_wrap": True, "valign": "vcenter"})
    text_f = wb.add_format({"border": 1, "valign": "top"})
    wrap_f = wb.add_format({"border": 1, "text_wrap": True, "valign": "top"})
    int_f = wb.add_format({"border": 1, "valign": "top"})
    money_f = wb.add_format({"border": 1, "num_format": "#,##0", "valign": "top"})
    date_f = wb.add_format({"border": 1, "num_format": "dd.mm.yyyy hh:mm", "valign": "top"})
    day_f = wb.add_format({"border": 1, "num_format": "dd.mm.yyyy", "valign": "top"})
    tot_label_f = wb.add_format({"bold": True, "top": 2, "bg_color": "#F4F6F5"})
    tot_f = wb.add_format({"bold": True, "top": 2, "num_format": "#,##0", "bg_color": "#F4F6F5"})

    note = labels["generated"].format(at=local_naive(generated_at).strftime("%d.%m.%Y %H:%M") if generated_at else "", from_=d_from.isoformat(), to=d_to.isoformat())

    def head_row(ws: xlsxwriter.worksheet.Worksheet, keys: list[tuple[str, int]], title: str) -> int:
        ws.write(0, 0, title, title_f)
        ws.write(1, 0, note, note_f)
        r = 3
        ws.set_row(r, 26)
        for i, (key, width) in enumerate(keys):
            ws.write(r, i, labels[key], head_f)
            ws.set_column(i, i, width)
        return r

    # --- patients × analyses (detail)
    ws = wb.add_worksheet(labels["patients_sheet"])
    cols: list[tuple[str, int]] = [
        ("no", 6), ("date", 17), ("order", 13), ("patient", 26), ("phone", 18), ("service", 28), ("lab", 22), ("branch", 16), ("price", 12), ("status", 14),
    ]
    head = head_row(ws, cols, labels["patients_sheet"])
    ci = {key: i for i, (key, _) in enumerate(cols)}
    total = 0
    for n, row in enumerate(detail, start=1):
        r = head + n
        ws.write_number(r, ci["no"], n, int_f)
        when = local_naive(row.date)
        if when:
            ws.write_datetime(r, ci["date"], when, date_f)
        ws.write_string(r, ci["order"], row.number, text_f)
        ws.write_string(r, ci["patient"], row.patient, text_f)
        ws.write_string(r, ci["phone"], row.phone, text_f)
        ws.write_string(r, ci["service"], row.service, wrap_f)
        ws.write_string(r, ci["lab"], row.laboratory, wrap_f)
        ws.write_string(r, ci["branch"], row.branch, text_f)
        ws.write_number(r, ci["price"], row.price, money_f)
        ws.write_string(r, ci["status"], ITEM_STATUS[lang].get(row.status, row.status), text_f)
        total += row.price
    if detail:
        ws.autofilter(head, 0, head + len(detail), len(cols) - 1)
        t = head + len(detail) + 1
        ws.write(t, ci["service"], labels["totals"], tot_label_f)
        ws.write_number(t, ci["price"], total, tot_f)
    ws.freeze_panes(head + 1, 2)

    # --- aggregated cuts
    for sheet, rows in (
        (labels["lab_sheet"], labs),
        (labels["service_sheet"], services),
        (labels["branch_sheet"], branches),
        (labels["employee_sheet"], employees),
    ):
        ws = wb.add_worksheet(sheet)
        keys: list[tuple[str, int]] = [("name", 34), ("count", 10), ("revenue", 14)]
        head = head_row(ws, keys, sheet)
        c_name, c_count, c_rev = 0, 1, 2
        n_count = n_rev = 0
        for n, row in enumerate(rows, start=1):
            r = head + n
            ws.write_string(r, c_name, row.name, text_f)
            ws.write_number(r, c_count, row.count, int_f)
            ws.write_number(r, c_rev, row.revenue, money_f)
            n_count += row.count
            n_rev += row.revenue
        if rows:
            ws.autofilter(head, 0, head + len(rows), len(keys) - 1)
            t = head + len(rows) + 1
            ws.write(t, c_name, labels["totals"], tot_label_f)
            ws.write_number(t, c_count, n_count, tot_f)
            ws.write_number(t, c_rev, n_rev, tot_f)
        ws.freeze_panes(head + 1, 0)

    # --- daily trend
    ws = wb.add_worksheet(labels["daily_sheet"])
    keys = [("date", 14), ("orders", 12), ("revenue", 14)]
    head = head_row(ws, keys, labels["daily_sheet"])
    t_orders = t_rev = 0
    for n, row in enumerate(daily, start=1):
        r = head + n
        ws.write_datetime(r, 0, datetime.fromisoformat(row.date), day_f)
        ws.write_number(r, 1, row.orders, int_f)
        ws.write_number(r, 2, row.revenue, money_f)
        t_orders += row.orders
        t_rev += row.revenue
    if daily:
        t = head + len(daily) + 1
        ws.write(t, 0, labels["totals"], tot_label_f)
        ws.write_number(t, 1, t_orders, tot_f)
        ws.write_number(t, 2, t_rev, tot_f)
    ws.freeze_panes(head + 1, 0)

    wb.close()
    return buf.getvalue()
