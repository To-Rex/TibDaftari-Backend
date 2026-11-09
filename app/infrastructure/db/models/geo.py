"""Geo reference: countries → regions → districts (pure reference data, shared by every unit).
Countries also carry the per-country operating settings (currency, locale, phone format, timezone)."""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, String, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.db.base import Base, PKMixin


class Country(PKMixin, Base):
    """ISO 3166-1 country; `name` is the Uzbek name, `name_ru`/`name_en` the localised ones."""

    __tablename__ = "countries"

    code: Mapped[str] = mapped_column(String(2), nullable=False, unique=True)  # ISO 3166-1 alpha-2
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    name_ru: Mapped[str | None] = mapped_column(String(120))
    name_en: Mapped[str | None] = mapped_column(String(120))
    phone_code: Mapped[str | None] = mapped_column(String(8))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # per-country operating settings
    currency_code: Mapped[str | None] = mapped_column(String(3))
    default_locale: Mapped[str] = mapped_column(String(2), nullable=False, default="uz")
    locales: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=lambda: ["uz", "ru", "en"], server_default=text("'{uz,ru,en}'"))
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Tashkent")
    phone_mask: Mapped[str | None] = mapped_column(String(40))
    #: the system is live in this country (an org tree may be opened under it)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Region(PKMixin, Base):
    """First-level division (viloyat / oblast / city of republican status) of a country."""

    __tablename__ = "regions"

    country_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("countries.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    code: Mapped[str | None] = mapped_column(String(20))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class District(PKMixin, Base):
    """Tuman or shahar of a region (`kind` tells which)."""

    __tablename__ = "districts"

    region_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("regions.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    code: Mapped[str | None] = mapped_column(String(20))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    kind: Mapped[str] = mapped_column(String(10), nullable=False, default="district")  # district | city
