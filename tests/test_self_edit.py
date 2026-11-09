"""No self-escalation: an administrator or installer edits their own name, contacts and password, never their own
role, permission overrides, units, categories or status; the role one holds is edited by someone else. Superadmins
are exempt. Fixture rows are prefixed `T-`."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from tests.helpers import (
    API,
    app_instance,
    bearer,
    make_employee,
    make_role,
    make_tree,
    seed,
    staff_session,
    staff_token,
)


@pytest.fixture(scope="module")
def ctx() -> Iterator[dict]:
    async def rows(s):
        a = await make_tree(s)
        sup = await make_employee(s, None, super_admin=True)
        admin = await make_employee(s, a.region, "admin")
        installer = await make_employee(s, a.region, "installer")
        # a unit role under the region with the admin keys, held by one employee
        mgr_role = await make_role(s, a.region, f"t-mgr-{a.tag.lower()}", f"T-{a.tag} Menejer", ["admin.role.write", "admin.employee.read", "admin.employee.write", "admin.unit.read"])
        other_role = await make_role(s, a.region, f"t-oth-{a.tag.lower()}", f"T-{a.tag} Boshqa", ["reception.patient.read"])
        mgr = await make_employee(s, a.region, "admin", role=mgr_role)
        return {
            "region": str(a.region.id),
            "facility": str(a.facility.id),
            "tag": a.tag,
            "ids": {"super": str(sup.id), "admin": str(admin.id), "inst": str(installer.id), "mgr": str(mgr.id)},
            "roles": {"mgr": str(mgr_role.id), "other": str(other_role.id)},
            "logins": {"super": sup.login, "admin": admin.login, "inst": installer.login, "mgr": mgr.login},
        }

    data = seed(rows)
    with TestClient(app_instance()) as client:
        data["client"] = client
        data["tokens"] = {who: staff_token(client, login) for who, login in data["logins"].items()}
        yield data


def h(ctx: dict, who: str) -> dict[str, str]:
    return bearer(ctx["tokens"][who])


def test_session_names_the_held_role(ctx: dict) -> None:
    s = staff_session(ctx["client"], ctx["logins"]["mgr"])
    assert s["roleId"] == ctx["roles"]["mgr"]
    assert staff_session(ctx["client"], ctx["logins"]["super"])["roleId"] is not None


@pytest.mark.parametrize("who", ["admin", "inst"])
def test_own_profile_yes_own_power_no(ctx: dict, who: str) -> None:
    c: TestClient = ctx["client"]
    me = ctx["ids"][who]
    hh = h(ctx, who)
    # profile fields are fine
    r = c.put(f"{API}/employees/{me}", json={"fullName": f"T-{ctx['tag']} {who} yangi", "phone": "+998901112233"}, headers=hh)
    assert r.status_code == 200, r.text
    # role, units, categories, status, overrides — refused with the field names
    for body in ({"roleId": ctx["roles"]["other"]}, {"unitId": ctx["facility"]}, {"unitIds": [ctx["facility"]]}, {"categoryIds": []}, {"status": "inactive"}):
        r = c.put(f"{API}/employees/{me}", json=body, headers=hh)
        assert r.status_code == 403 and r.json()["error"]["code"] == "self_edit", (body, r.text)
    r = c.put(f"{API}/employees/{me}", json={"overrides": {"allow": ["platform.unit.manage"], "deny": []}}, headers=hh)
    assert r.status_code == 403 and r.json()["error"]["code"] == "self_edit"
    r = c.put(f"{API}/employees/{me}/overrides", json={"allow": ["admin.unit.reset"], "deny": []}, headers=hh)
    assert r.status_code == 403 and r.json()["error"]["code"] == "self_edit"
    # the account is unchanged
    s = staff_session(c, ctx["logins"][who])
    assert s["unitId"] == ctx["region"] and s["roleKey"] == who.replace("inst", "installer")


def test_the_held_role_is_not_edited_by_its_holder(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    mine, other = ctx["roles"]["mgr"], ctx["roles"]["other"]
    r = c.put(f"{API}/roles/{mine}", json={"permissions": ["admin.role.write", "admin.unit.reset"]}, headers=h(ctx, "mgr"))
    assert r.status_code == 403 and r.json()["error"]["code"] == "own_role"
    assert c.delete(f"{API}/roles/{mine}", headers=h(ctx, "mgr")).status_code == 403
    # another role of the unit is still theirs to edit; the region administrator edits the held one
    assert c.put(f"{API}/roles/{other}", json={"description": "T edited"}, headers=h(ctx, "mgr")).status_code == 200
    assert c.put(f"{API}/roles/{mine}", json={"description": "T edited by admin"}, headers=h(ctx, "admin")).status_code == 200


def test_superadmin_is_exempt(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    me = ctx["ids"]["super"]
    assert c.put(f"{API}/employees/{me}", json={"fullName": f"T-{ctx['tag']} Super"}, headers=h(ctx, "super")).status_code == 200
    assert c.put(f"{API}/employees/{me}/overrides", json={"allow": [], "deny": []}, headers=h(ctx, "super")).status_code == 200
