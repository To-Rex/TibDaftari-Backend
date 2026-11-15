"""One phone, a whole family (test database, rows prefixed `T-family-`): undocumented patients may share a number
when the newcomer says who they are to the people on it; the same name + birth date on a number is the same person;
the family is listed for the card, the portal and Telegram."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from app.infrastructure.db.models import Patient
from app.modules.patients import service as patients_svc
from fastapi.testclient import TestClient

from tests.helpers import API, app_instance, bearer, db, make_employee, make_tree, phone, seed, staff_token

RUN = uuid.uuid4().hex[:6]


@pytest.fixture(scope="module")
def env() -> Iterator[dict[str, Any]]:
    async def rows(s):
        a = await make_tree(s)
        emp = await make_employee(s, a.facility, "registrator", login=f"t-family-{RUN}")
        return {"facility": str(a.facility.id), "login": emp.login, "tag": a.tag}

    data = seed(rows)
    with TestClient(app_instance()) as client:
        data["client"] = client
        data["h"] = bearer(staff_token(client, data["login"]))
        data["url"] = f"{API}/units/{data['facility']}/patients"
        data["phone"] = phone()
        yield data


def post(env: dict[str, Any], **body: Any):
    return env["client"].post(env["url"], json={"phone": env["phone"], **body}, headers=env["h"])


def test_a_family_shares_one_number(env: dict[str, Any]) -> None:
    # the owner: nobody else on the number, no relation needed (asking for one is refused)
    r = post(env, fullName=f"T-family Aziza {RUN}", gender="female", birthDate="1988-04-02", relation="mother")
    assert r.status_code == 422 and r.json()["error"]["code"] == "relation_target"
    r = post(env, fullName=f"T-family Aziza {RUN}", gender="female", birthDate="1988-04-02")
    assert r.status_code == 201, r.text
    mother = r.json()
    assert mother["relation"] is None and mother["relatedPatientId"] is None and mother["phoneShared"] == 0
    env["mother"] = mother
    # an undocumented second person without a relation → 409 with the people on the number
    r = post(env, fullName=f"T-family Ali {RUN}", gender="male", birthDate="2016-09-01")
    assert r.status_code == 409 and r.json()["error"]["code"] == "phone_relation_required"
    assert [x["id"] for x in r.json()["error"]["details"]["patients"]] == [mother["id"]]
    # with a relation: linked to the number's owner by default
    r = post(env, fullName=f"T-family Ali {RUN}", gender="male", birthDate="2016-09-01", relation="child")
    assert r.status_code == 201, r.text
    son = r.json()
    assert (son["relation"], son["relatedPatientId"], son["relatedPatientName"], son["phoneShared"]) == ("child", mother["id"], mother["fullName"], 1)
    env["son"] = son
    # a third person, related to someone other than the owner; a bad target / kind is refused
    r = post(env, fullName=f"T-family Vali {RUN}", gender="male", relation="brother_older", relatedPatientId=str(uuid.uuid4()))
    assert r.status_code == 422 and r.json()["error"]["code"] == "relation_target"
    r = post(env, fullName=f"T-family Vali {RUN}", gender="male", relation="cousin")
    assert r.status_code == 422
    r = post(env, fullName=f"T-family Vali {RUN}", gender="male", relatedPatientId=son["id"])
    assert r.status_code == 422 and r.json()["error"]["code"] == "relation_required"
    r = post(env, fullName=f"T-family Vali {RUN}", gender="male", birthDate="2012-01-15", relation="brother_older", relatedPatientId=son["id"])
    assert r.status_code == 201 and r.json()["relatedPatientId"] == son["id"] and r.json()["phoneShared"] == 2, r.text
    env["brother"] = r.json()


def test_the_same_person_is_not_registered_twice(env: dict[str, Any]) -> None:
    son = env["son"]
    # the same name (folded: case / Cyrillic) and birth date on the number → the existing person
    r = post(env, fullName=son["fullName"].upper(), birthDate="2016-09-01", relation="child")
    assert r.status_code == 409 and r.json()["error"]["code"] == "duplicate_person" and r.json()["error"]["details"]["patientId"] == son["id"]
    # an unknown birth date on either side still counts as the same name
    r = post(env, fullName=son["fullName"], relation="child")
    assert r.status_code == 409 and r.json()["error"]["code"] == "duplicate_person"
    # same name, another birth date: a different person (a namesake grandfather)
    r = post(env, fullName=son["fullName"], birthDate="1950-03-03", gender="male", relation="grandparent")
    assert r.status_code == 201, r.text
    env["grandpa"] = r.json()
    # a documented person may share the number without a relation (as before) — or with one
    pinfl = ("5" + uuid.uuid4().hex.translate(str.maketrans("abcdef", "123456")))[:14]
    r = post(env, fullName=f"T-family Dilshod {RUN}", pinfl=pinfl)
    assert r.status_code == 201 and r.json()["relation"] is None, r.text
    env["documented"] = r.json()


def test_the_family_list_and_the_cards(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    mother, son = env["mother"], env["son"]
    r = c.get(f"{API}/patients/{son['id']}/family", headers=env["h"])
    assert r.status_code == 200, r.text
    fam = r.json()
    ids = [m["id"] for m in fam["members"]]
    assert fam["patientId"] == son["id"] and son["id"] not in ids
    assert ids[0] == mother["id"] and fam["members"][0]["isOwner"] is True
    assert {env["brother"]["id"], env["grandpa"]["id"], env["documented"]["id"]} <= set(ids)
    by_id = {m["id"]: m for m in fam["members"]}
    assert by_id[env["brother"]["id"]]["relation"] == "brother_older" and by_id[env["brother"]["id"]]["relatedPatientId"] == son["id"]
    # the card counts the people on the number; search and duplicates carry the relation
    card = c.get(f"{API}/patients/{mother['id']}", headers=env["h"]).json()
    assert card["phoneShared"] == 4 and card["relation"] is None
    found = c.get(f"{API}/units/{env['facility']}/patients/search", params={"q": env["phone"]}, headers=env["h"]).json()
    assert {mother["id"], son["id"]} <= {p["id"] for p in found}
    assert next(p for p in found if p["id"] == son["id"])["relatedPatientName"] == mother["fullName"]
    dupes = c.post(f"{API}/units/{env['facility']}/patients/duplicates", json={"phone": env["phone"]}, headers=env["h"]).json()
    assert len(dupes) == 5


def test_editing_the_relation_and_the_number(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    son, mother = env["son"], env["mother"]
    # the relation is editable; an unrelated edit leaves it alone
    r = c.put(f"{API}/patients/{son['id']}", json={"relation": "grandchild", "relatedPatientId": env["grandpa"]["id"]}, headers=env["h"])
    assert r.status_code == 200 and (r.json()["relation"], r.json()["relatedPatientId"]) == ("grandchild", env["grandpa"]["id"]), r.text
    r = c.put(f"{API}/patients/{son['id']}", json={"note": "T-family note"}, headers=env["h"])
    assert r.status_code == 200 and r.json()["relation"] == "grandchild"
    # dropping the relation of an undocumented person on a shared number is refused
    r = c.put(f"{API}/patients/{son['id']}", json={"relation": None, "relatedPatientId": None}, headers=env["h"])
    assert r.status_code == 409 and r.json()["error"]["code"] == "phone_relation_required"
    # moving to an own number clears the relation (it was about the old number)
    own = phone()
    r = c.put(f"{API}/patients/{son['id']}", json={"phone": own}, headers=env["h"])
    assert r.status_code == 200 and r.json()["relation"] is None and r.json()["phoneShared"] == 0, r.text
    # …and back: the number is shared again, so the relation is asked for once more
    r = c.put(f"{API}/patients/{son['id']}", json={"phone": env["phone"]}, headers=env["h"])
    assert r.status_code == 409 and r.json()["error"]["code"] == "phone_relation_required"
    r = c.put(f"{API}/patients/{son['id']}", json={"phone": env["phone"], "relation": "child"}, headers=env["h"])
    assert r.status_code == 200 and r.json()["relatedPatientId"] == mother["id"], r.text


def test_telegram_reaches_the_chat_linked_to_the_number(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]

    async def flow(s):
        mother = await s.get(Patient, uuid.UUID(env["mother"]["id"]))
        son = await s.get(Patient, uuid.UUID(env["son"]["id"]))
        before = await patients_svc.telegram_chat_for(s, son)
        mother.telegram_chat_id = f"77{RUN[:4].translate(str.maketrans('abcdef', '123456'))}"
        await s.flush()
        after = await patients_svc.telegram_chat_for(s, son)
        mother.telegram_chat_id = None
        await s.flush()
        return before, after, await patients_svc.telegram_chat_for(s, None)

    before, after, none = db(c, flow)
    assert before is None and after is not None and after.startswith("77") and none is None
