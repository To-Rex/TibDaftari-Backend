"""Text helpers shared by search (fold), phones and money formatting — mirror of the frontend."""

from __future__ import annotations

import re

_CYR = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "j", "з": "z", "и": "i", "й": "y",
    "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
    "х": "x", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sh", "ъ": "", "ы": "i", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    "ў": "o", "қ": "q", "ғ": "g", "ҳ": "h",
}
_APOS = re.compile(r"[‘’'`ʻ]")
_WS = re.compile(r"\s+")
_PHONE_RE = re.compile(r"^998\d{9}$")


def fold(s: str | None) -> str:
    """Search normalisation identical to the frontend `fold()`."""
    if not s:
        return ""
    out = "".join(_CYR.get(ch, ch) for ch in s.lower())
    out = _APOS.sub("", out).replace("h", "x")
    return _WS.sub(" ", out).strip()


def matches(hay: str | None, needle: str | None) -> bool:
    if not needle:
        return True
    if hay is None:
        return False
    return fold(needle) in fold(hay)


def digits(s: str | None) -> str:
    return re.sub(r"\D", "", s or "")


def norm_phone(p: str | None) -> str:
    """'+998 90 123-45-67' | '901234567' → '998901234567' (no validation)."""
    d = digits(p)
    return "998" + d if len(d) == 9 else d


def is_valid_uz_phone(normalized: str) -> bool:
    return bool(_PHONE_RE.match(normalized))


def fmt_phone(p: str | None) -> str:
    """'998901234567' → '+998 90 123-45-67'; empty → '—'; other → unchanged."""
    if not p:
        return "—"
    d = digits(p)
    if len(d) == 12 and d.startswith("998"):
        return f"+998 {d[3:5]} {d[5:8]}-{d[8:10]}-{d[10:]}"
    return p


def fmt_money_ru(v: int | float) -> str:
    """JS `toLocaleString('ru-RU')` — thousands grouped with U+00A0."""
    n = int(round(v))
    sign = "-" if n < 0 else ""
    return sign + f"{abs(n):,}".replace(",", " ")


def slugify(s: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", fold(s)).strip("-")
    return base or "item"


# The fold() pipeline as an IMMUTABLE SQL function (installed by the initial migration) so that
# search filters can run in Postgres with a trigram index: fold_text(col) LIKE '%' || fold_text(:q) || '%'
FOLD_SQL_FUNCTION = r"""
CREATE OR REPLACE FUNCTION fold_text(src text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT AS $fold$
  SELECT btrim(regexp_replace(replace(regexp_replace(
    translate(
      replace(replace(replace(replace(replace(replace(replace(lower(src),
        'ё','yo'),'ц','ts'),'ч','ch'),'ш','sh'),'щ','sh'),'ю','yu'),'я','ya'),
      'абвгдежзийклмнопрстуфхыэўқғҳъь', 'abvgdejziyklmnoprstufxieoqgh'),
    '[‘’''`ʻ]', '', 'g'), 'h', 'x'), '\s+', ' ', 'g'))
$fold$;
"""
