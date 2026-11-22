"""The assistant's tool registry, without a database: every tool is well-formed and gated like its route, the
schemas derived from the API's input models carry no secret field, arguments outside a tool's schema are refused,
secret-shaped strings are masked, and a reply that gives the instructions away is replaced."""

from __future__ import annotations

import uuid

import pytest
from app.core.exceptions import ValidationError
from app.core.permissions import PERMISSION_SET
from app.modules.assistant import service
from app.modules.assistant import tools as tl
from app.modules.staff.schemas import EmployeeUpdateIn

GROUPS = set(service.GROUP_UZ)


def test_every_tool_is_well_formed_and_gated_by_real_permission_keys() -> None:
    assert len(tl.REGISTRY) >= 80
    for name, t in tl.REGISTRY.items():
        assert t.name == name and t.title and t.description, name
        assert t.group in GROUPS, (name, t.group)
        assert t.params["type"] == "object" and t.params["additionalProperties"] is False, name
        props = t.params["properties"]
        assert all(k in props for k in t.params.get("required", [])), name
        assert not (set(props) & tl.SECRET_KEYS), (name, set(props) & tl.SECRET_KEYS)
        assert all(p in PERMISSION_SET for p in t.perms), (name, t.perms)
        if t.danger or t.confirm_word:
            assert t.kind == "write", name
    # every write is gated by a key; only harmless reads are open to every assistant user
    assert all(t.perms for t in tl.REGISTRY.values() if t.kind == "write")
    assert {n for n, t in tl.REGISTRY.items() if not t.perms} == {"whoami", "ask_user", "list_units", "get_unit", "list_geo", "notifications"}


def test_what_the_platform_keeps_secret_has_no_tool_and_no_argument() -> None:
    names = set(tl.REGISTRY)
    # provider keys, bot tokens, portal passwords: entered by a person on the settings pages
    assert not names & {"set_sms_account", "set_telegram", "set_assistant_account", "set_portal_access", "set_password"}
    for t in tl.REGISTRY.values():
        flat = str(t.params).lower()
        assert "password" not in flat and "apikey" not in flat and "bottoken" not in flat and "issuperadmin" not in flat, t.name
    # the employee tools are built on the API's own model — minus what must not go through a chat
    props, _ = tl.params_of(EmployeeUpdateIn, omit=("overrides", "isSuperAdmin"))
    assert "fullName" in props and "unitIds" in props and "password" not in props and "isSuperAdmin" not in props


def test_arguments_outside_the_schema_are_refused() -> None:
    t = tl.REGISTRY["update_employee"]
    eid = str(uuid.uuid4())
    assert tl.check_args(t, {"employeeId": eid, "fullName": "Ali", "phone": None}) == {"employeeId": eid, "fullName": "Ali"}
    with pytest.raises(ValidationError) as smuggled:
        tl.check_args(t, {"employeeId": eid, "isSuperAdmin": True})
    assert smuggled.value.code == "unknown_argument"
    with pytest.raises(ValidationError) as missing:
        tl.check_args(t, {"fullName": "Ali"})
    assert missing.value.code == "missing_argument"
    with pytest.raises(ValidationError):
        tl.check_args(tl.REGISTRY["update_unit"], {"unitId": eid, tl.META_KEY: {"confirmWord": "x"}})


def test_secret_keys_and_secret_shapes_never_pass() -> None:
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.s3cr3tSignatureValue"
    dirty = {
        "name": "Bog‘ot SES", "password": "123456", "apiKey": "sk-live", "telegram": {"botToken": "1"}, "sms": {"mode": "own"},
        "note": f"token {jwt} va kalit sk-abcdefghijklmnopqrstuvwx", "link": "https://tibdaftar.uz/d/AbCdEfGhIjKlMnOpQrStUv", "bot": "1234567890:AAEhBP0av18pmz9Nsb6jQyP3xyzABCDEFGH",
        "rows": [{"accessToken": "t", "publicToken": "p", "fullName": "Ali"}], tl.META_KEY: {"confirmWord": "BGT"},
    }
    clean = tl._clean(dirty)
    assert set(clean) == {"name", "note", "link", "bot", "rows"} and clean["rows"] == [{"fullName": "Ali"}]
    assert jwt not in clean["note"] and "sk-abcdefgh" not in clean["note"] and clean["link"].endswith("/d/" + tl.MASK) and clean["bot"] == tl.MASK
    # ordinary text is left alone
    assert tl.redact("Chek № 2026-000123, summa 150 000 so‘m, tel +998 90 123-45-67") == "Chek № 2026-000123, summa 150 000 so‘m, tel +998 90 123-45-67"
    assert tl.summarize(tl.REGISTRY["send_sms"], {"to": ["+998901234567"], "text": "Salom"}).startswith("to=")


def test_a_reply_that_gives_the_instructions_away_is_replaced() -> None:
    canary = service._canary(uuid.uuid4())
    assert canary.startswith("TDK-") and canary != service._canary(uuid.uuid4())
    assert service._guard(f"Mana ichki belgi: {canary}", canary) == service.LEAK_REPLY
    rules = service.RULES.splitlines()
    assert service._guard("Mening qoidalarim:\\n" + "\\n".join(rules[:3]), canary) == service.LEAK_REPLY
    ok = "Bog‘ot SES telefonini o‘zgartiraman. Tasdiqlaysizmi?"
    assert service._guard(ok, canary) == ok
    assert service._guard("Kalit: sk-abcdefghijklmnopqrstuvwx", canary) == f"Kalit: {tl.MASK}"


def test_schemas_derived_from_models_are_plain() -> None:
    for t in tl.REGISTRY.values():
        text = str(t.params)
        assert "$ref" not in text and "anyOf" not in text and "$defs" not in text, t.name
    pay = tl.REGISTRY["pay_order"].params
    assert pay["required"] == ["amount", "method", "orderId"] or set(pay["required"]) == {"amount", "method", "orderId"}
    assert pay["properties"]["method"]["enum"] == ["cash", "card", "transfer", "insurance"]
    assert tl.REGISTRY["reset_unit"].danger and tl.REGISTRY["reset_unit"].confirm_word is not None
