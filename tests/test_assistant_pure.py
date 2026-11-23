"""Assistant pieces that need no database: the tool registry filtered by the employee's keys, the secret-stripping of
tool outputs, the one-line summaries, the history window, and the two provider message shapes (Anthropic merges every
tool result of a turn into one user message; OpenAI-compatible uses `tool` messages)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.modules.assistant import service, tools
from app.modules.assistant.providers import AccountConfig, AnthropicProvider, OpenAICompatProvider, ToolCall, Turn


def principal(perms: list[str], *, super_admin: bool = False):
    perm_set = frozenset(perms)
    return SimpleNamespace(
        is_super_admin=super_admin,
        permissions=list(perms),
        has=lambda *ps: super_admin or any(p in perm_set for p in ps),
    )


def test_a_leader_gets_the_read_tools_only() -> None:
    from app.core.permissions import LEADER_PERMISSIONS

    mine = tools.tools_for(principal(list(LEADER_PERMISSIONS)))
    assert {t.name for t in mine} >= {"whoami", "list_units", "dashboard", "breakdown", "list_orders", "search_patients", "list_employees", "export_data", "export_chart"}
    # nothing that changes the clinic's data — the only writes a leader holds are their OWN scheduled tasks
    assert all(t.kind == "read" or t.group == "schedule" for t in mine)
    assert {t.name for t in mine if t.kind == "write"} == {"create_schedule", "update_schedule", "delete_schedule"}
    assert not ({"update_unit", "create_employee", "create_patient", "send_sms", "set_template_status"} & {t.name for t in mine})


def test_tools_follow_the_keys_of_the_role() -> None:
    names = {
        t.name
        for t in tools.tools_for(principal(["assistant.use", "reception.patient.read", "reception.order.create"]))
    }
    assert {
        "whoami",
        "list_units",
        "get_unit",
        "search_patients",
        "get_patient",
        "list_orders",
        "get_order",
        "list_service_types",
    } <= names
    assert not ({"list_employees", "update_unit", "create_employee", "send_sms", "dashboard"} & names)
    assert tools.tools_for(principal(["reception.patient.read"])) == []  # no assistant.use → nothing at all
    everything = {t.name for t in tools.tools_for(principal([], super_admin=True))}
    assert everything == set(tools.REGISTRY)
    writes = {t.name for t in tools.REGISTRY.values() if t.kind == "write"}
    assert {
        "update_unit",
        "create_unit",
        "quick_start",
        "create_employee",
        "update_employee",
        "update_service_type",
        "create_service_type",
        "update_category",
        "set_template_status",
        "send_sms",
    } <= writes
    # the reception sees its own writes and nobody else's; a laborant theirs
    reception = {t.name for t in tools.tools_for(principal(["assistant.use", "reception.patient.read", "reception.patient.write", "reception.order.create", "reception.payment.create"]))}
    assert {"create_patient", "update_patient", "create_order", "add_order_items", "pay_order", "list_patients"} <= reception
    assert not ({"refund_payment", "cancel_order", "delete_order", "save_result_values", "approve_result", "create_role", "reset_unit", "copy_working_set", "update_sms_templates"} & reception)
    lab = {t.name for t in tools.tools_for(principal(["assistant.use", "lab.worklist.read", "lab.result.write", "lab.result.submit"]))}
    assert {"worklist", "worklist_counts", "get_order_item", "save_result_values", "submit_result"} <= lab
    assert not ({"approve_result", "revoke_result", "create_order", "pay_order", "list_patients", "update_unit"} & lab)
    leader = {t.name for t in tools.tools_for(principal(["assistant.use", "reports.operations.read", "reports.finance.read", "admin.unit.read", "admin.employee.read", "admin.catalog.read", "admin.template.read"]))}
    assert {"dashboard", "patients_report", "results_report", "sms_report", "services_report", "territory_stats", "get_template", "list_schemas"} <= leader
    assert all(tools.REGISTRY[n].kind == "read" or tools.REGISTRY[n].group == "schedule" for n in leader)


def test_definitions_are_json_schema_objects() -> None:
    for d in tools.definitions(tools.REGISTRY.values()):
        assert d["parameters"]["type"] == "object" and d["parameters"]["additionalProperties"] is False
        assert d["description"] and d["name"]
    qs = tools.REGISTRY["quick_start"].params
    assert qs["required"] == ["parentId", "facility"] and qs["properties"]["facility"]["required"] == [
        "name",
        "kind",
        "code",
    ]


def test_secrets_never_reach_the_model() -> None:
    dirty = {
        "id": "1",
        "login": "x",
        "passwordHash": "argon",
        "password": "123456",
        "sms": {"apiKeyMasked": "xx"},
        "nested": [{"token": "t", "name": "ok"}],
    }
    assert tools._clean(dirty) == {"id": "1", "login": "x", "nested": [{"name": "ok"}]}
    t = tools.REGISTRY["create_employee"]
    assert (
        tools.summarize(t, {"fullName": "Ali", "login": "ali", "password": "123456"})
        == "fullName=Ali · login=ali · password=••••"
    )


def test_history_window_starts_at_a_user_turn() -> None:
    def mk(role, content="", **kw):
        return SimpleNamespace(
            role=role,
            content=content,
            tool_calls=kw.get("tool_calls"),
            tool_call_id=kw.get("tool_call_id"),
            is_error=False,
        )

    rows = [
        mk("tool", "orphan", tool_call_id="c0"),
        mk("user", "hi"),
        mk("assistant", "", tool_calls=[{"id": "c1", "name": "whoami", "args": {}}]),
        mk("tool", "{}", tool_call_id="c1"),
        mk("assistant", "done"),
    ]
    turns = service._turns(rows)
    assert [t.role for t in turns] == ["user", "assistant", "tool", "assistant"]
    assert turns[1].tool_calls[0].name == "whoami" and turns[2].tool_call_id == "c1"
    # the char budget keeps the newest turns, still starting at a user turn
    half = int(service.HISTORY_CHARS * 0.7)
    big = [mk("user", "x" * half), mk("assistant", "a"), mk("user", "y" * half), mk("assistant", "b")]
    kept = service._turns(big)
    assert kept[0].role == "user" and kept[0].content.startswith("y")


def test_provider_message_shapes() -> None:
    turns = [
        Turn(role="user", content="salom"),
        Turn(
            role="assistant",
            content="qarayman",
            tool_calls=[
                ToolCall(id="c1", name="list_units", args={"search": "x"}),
                ToolCall(id="c2", name="whoami", args={}),
            ],
        ),
        Turn(role="tool", content='{"items": []}', tool_call_id="c1"),
        Turn(role="tool", content='{"error": "forbidden"}', tool_call_id="c2", is_error=True),
        Turn(role="assistant", content="tayyor"),
    ]
    a = AnthropicProvider._messages(turns)
    assert [m["role"] for m in a] == ["user", "assistant", "user", "assistant"]
    assert [b["type"] for b in a[1]["content"]] == ["text", "tool_use", "tool_use"]
    assert [b["tool_use_id"] for b in a[2]["content"]] == ["c1", "c2"] and a[2]["content"][1]["is_error"] is True
    o = OpenAICompatProvider._messages("SYS", turns)
    assert o[0] == {"role": "system", "content": "SYS"}
    assert (
        o[2]["tool_calls"][0]["function"]["name"] == "list_units"
        and '"search": "x"' in o[2]["tool_calls"][0]["function"]["arguments"]
    )
    assert o[3] == {"role": "tool", "tool_call_id": "c1", "content": '{"items": []}'}


def test_account_config_falls_back_to_the_preset() -> None:
    acc = SimpleNamespace(provider="deepseek", model=None, base_url=None, api_key_enc=None, settings={"maxTokens": 512})
    cfg = service._config(acc)
    assert (
        cfg.model == "deepseek-chat"
        and cfg.base_url == "https://api.deepseek.com"
        and cfg.max_tokens == 512
        and cfg.api_key == ""
    )
    assert isinstance(AccountConfig(provider="anthropic", model="claude-opus-5-5", api_key="k").max_tokens, int)
    assert uuid.UUID(str(uuid.uuid4()))
