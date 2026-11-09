"""Pure tests (no DB / Redis) for the patient portal login: login rules, generated passwords, token scope, DTOs."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from app.api.deps import PatientPrincipal
from app.core.config import settings
from app.core.security import decode_token, hash_password, issue_token, random_password
from app.infrastructure.db.models import Patient
from app.modules.auth.schemas import PatientPasswordChangeIn, PatientSessionOut
from app.modules.messaging import service as messaging
from app.modules.patients.portal_login import login_base, norm_login, portal_link, valid_login
from app.modules.patients.schemas import PortalAccessIn
from app.modules.patients.service import patient_out
from app.modules.portal.service import identity_ids
from pydantic import ValidationError


def test_logins_compare_case_insensitively() -> None:
    assert norm_login("  Aziz.Karimov ") == "aziz.karimov"
    assert norm_login(None) == ""


@pytest.mark.parametrize("login", ["aziz4821", "a.b", "0123", "ali_vali-7", "x" * 40])
def test_valid_logins(login: str) -> None:
    assert valid_login(login)


@pytest.mark.parametrize("login", ["", "ab", ".aziz", "-aziz", "aziz karimov", "азиз", "aziz@mail", "x" * 41, "Aziz"])
def test_invalid_logins(login: str) -> None:
    assert not valid_login(login)


@pytest.mark.parametrize(
    ("name", "base"),
    [
        ("Азизов Шахзод", "azizov"),
        ("Karimova Dilnoza", "karimova"),
        ("O‘ktamov Sardor", "oktamov"),
        ("Ғайратов", "gayratov"),
        ("Abdurahmonovaxonim Gulchehra", "abdurahmonov"),
        ("", "bemor"),
        ("  ", "bemor"),
        ("123", "bemor"),
        (None, "bemor"),
    ],
)
def test_suggested_login_starts_with_the_latin_first_word(name: str | None, base: str) -> None:
    assert login_base(name) == base
    assert valid_login(f"{base}1234")


def test_generated_passwords_are_readable() -> None:
    for _ in range(300):
        pw = random_password()
        assert len(pw) == 8
        assert any(c.isdigit() for c in pw) and any(c.isalpha() for c in pw)
        assert not set(pw) & set("01oli")  # no look-alikes
        assert pw == pw.lower()
    assert len({random_password() for _ in range(50)}) == 50


def test_login_token_carries_via_and_otp_token_does_not(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jwt_secret", "test-secret-for-pure-tests-only-0123456789")
    sub = uuid.uuid4()
    tok, _, _ = issue_token(sub, "patient", timedelta(minutes=5), via="login")
    assert decode_token(tok).get("via") == "login"
    tok, _, _ = issue_token(sub, "patient", timedelta(minutes=5))
    assert "via" not in decode_token(tok)


def _principal(via: str) -> PatientPrincipal:
    patient = SimpleNamespace(id=uuid.uuid4(), phone="998901234567")
    return PatientPrincipal(patient=patient, jti="j", token_exp=datetime.now(UTC), via=via)  # type: ignore[arg-type]


def test_login_session_sees_its_own_patient_only() -> None:
    p = _principal("login")
    assert p.own_only
    # no DB is touched: the phone's other patients are never looked up
    assert asyncio.run(identity_ids(None, p)) == [p.id]  # type: ignore[arg-type]
    assert not _principal("otp").own_only


def _patient(**kw: object) -> Patient:
    now = datetime.now(UTC)
    base: dict[str, object] = {
        "id": uuid.uuid4(), "country_id": uuid.uuid4(), "full_name": "Aziz", "phone": "998901234567", "tags": [],
        "stats_orders": 0, "stats_total_spent": 0, "discount_percent": 0, "portal_linked": False,
        "created_at": now, "updated_at": now,
    }
    return Patient(**{**base, **kw})


def test_staff_dto_shows_the_login_only_while_a_password_is_set() -> None:
    assert patient_out(_patient()).portal.login is None
    assert patient_out(_patient(portal_login="aziz1234")).portal.login is None  # half-cleared row
    set_at = datetime.now(UTC)
    out = patient_out(_patient(portal_login="aziz1234", portal_password_hash=hash_password("abc12345"), portal_password_set_at=set_at))
    assert out.portal.login == "aziz1234" and out.portal.login_set_at == set_at
    dumped = out.model_dump(by_alias=True)
    assert "loginSetAt" in dumped["portal"]
    assert not any("password" in k.lower() for k in dumped["portal"])


def test_access_input_rules() -> None:
    assert PortalAccessIn().send_sms is False
    with pytest.raises(ValidationError):
        PortalAccessIn(password="12345")
    with pytest.raises(ValidationError):
        PortalAccessIn(login="x" * 41)
    with pytest.raises(ValidationError):
        PatientPasswordChangeIn(current_password="a", new_password="short")


def test_session_dto_defaults_to_the_sms_code_way_in() -> None:
    s = PatientSessionOut(patient_id="p", phone="998901234567", full_name="A", access_token="t", expires_at=datetime.now(UTC))
    assert s.via == "otp" and s.login is None


def test_credentials_sms_text_and_its_outbox_mask() -> None:
    # the default portal-access text, rendered for the organisation "Temo Lab" (no unit texts of its own)
    text = messaging.render_text({}, "portal_access", org="Temo Lab", login="aziz1234", password="k7m2pqab", link="https://temo.uz/login")
    assert text == "Temo Lab shaxsiy kabinetiga kirish: https://temo.uz/login Login: aziz1234 Parol: k7m2pqab"
    masked = messaging.render_text({}, "portal_access", org="Temo Lab", login="aziz1234", password="••••••", link="https://temo.uz/login")
    assert "k7m2pqab" not in masked and "aziz1234" in masked
    # `{company}` keeps working as an alias of `{org}` in texts written before the tree
    assert messaging.render_text({"portal_access": "{company}: {login}"}, "portal_access", org="Temo Lab", login="aziz1234") == "Temo Lab: aziz1234"
    assert {"otp", "portal_access"} <= messaging.SECRET_KINDS


def test_sms_link_points_at_the_portal_sign_in() -> None:
    meta = SimpleNamespace(origin="https://temo.uz")
    assert portal_link(meta) == "https://temo.uz/login"  # type: ignore[arg-type]
    assert portal_link(None) == f"{settings.frontend_url.rstrip('/')}/login"
