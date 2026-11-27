"""What a new employee practises, and what they may do while practising.

A task belongs to an employee when their role holds one of its permissions; it is done when the request that stands
for it succeeds IN THE SANDBOX (the gate records it after the commit). A task that needs something to work on — a
laboratory worker cannot open a cheque to get an analysis — names the sample the service puts there for it.

While in training a write is allowed only when it is listed here: the daily operations, which all land in the
sandbox tables. Everything else — the admin panel, settings, the catalogue, staff, SMS accounts, the assistant —
would change shared, real things and is refused (`training_mode`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

API_PREFIX = "/api/v1"

#: what a task needs to be possible at all in the facility the employee practises in. Without it the task is
#: reported as unavailable and does not hold the training back (a clinic whose catalogue is still empty must not
#: lock its first employees in the training for ever):
#: a service the facility offers / one that costs something (there is nothing to pay for otherwise)
SERVICE = "service"
PAID_SERVICE = "paid_service"
#: … and the samples the service puts into the sandbox: an analysis waiting for its result / a result waiting for approval
LAB_ITEM = "lab_item"
SUBMITTED_ITEM = "submitted_item"
#: samples for a plan that starts in the middle of the desk work («only how to take a payment»): a cheque to add a
#: service to / one with something to pay. Never a condition of a task — only something to begin with.
OPEN_ORDER = "open_order"
UNPAID_ORDER = "unpaid_order"
#: how the practice rows the service itself puts there are told apart (on the screen, and from the trainee's own work)
SAMPLE_NOTE = "O‘quv namunasi"


@dataclass(frozen=True, slots=True)
class Task:
    key: str
    #: holding any of these makes the task the employee's
    perms: tuple[str, ...]
    method: str
    #: the request that proves it (path under /api/v1)
    path: re.Pattern[str]
    needs: str | None = None


def _p(pattern: str) -> re.Pattern[str]:
    return re.compile(rf"^{pattern}/?$")


_ID = r"[^/]+"

TASKS: tuple[Task, ...] = (
    Task("patient_create", ("reception.patient.write",), "POST", _p(rf"/units/{_ID}/patients")),
    Task("order_create", ("reception.order.create",), "POST", _p(rf"/units/{_ID}/orders"), SERVICE),
    Task("order_add_item", ("reception.order.create",), "POST", _p(rf"/orders/{_ID}/items"), SERVICE),
    Task("payment_take", ("reception.payment.create",), "POST", _p(rf"/orders/{_ID}/pay"), PAID_SERVICE),
    Task("result_enter", ("lab.result.write",), "PUT", _p(rf"/items/{_ID}/values"), LAB_ITEM),
    Task("result_submit", ("lab.result.submit",), "POST", _p(rf"/items/{_ID}/submit"), LAB_ITEM),
    Task("result_approve", ("confirm.result.approve",), "POST", _p(rf"/(items|orders)/{_ID}/approve"), SUBMITTED_ITEM),
)
TASK_BY_KEY = {t.key: t for t in TASKS}

#: every write a trainee may make — the sandbox takes them all; anything not here is refused
ALLOWED_WRITES: tuple[tuple[str, re.Pattern[str]], ...] = (
    *((t.method, t.path) for t in TASKS),
    ("POST", _p(r"/auth/logout")),
    ("POST", _p(r"/notifications/read")),
    ("POST", _p(r"/training/complete")),
    ("POST", _p(r"/training/restart")),
    ("POST", _p(r"/training/leave")),
    ("POST", _p(rf"/units/{_ID}/patients/duplicates")),
    ("PUT", _p(rf"/patients/{_ID}")),
    ("PUT", _p(rf"/patients/{_ID}/portal-access")),
    ("DELETE", _p(rf"/patients/{_ID}/portal-access")),
    ("DELETE", _p(rf"/orders/{_ID}/items/{_ID}")),
    ("POST", _p(rf"/orders/{_ID}/cancel")),
    ("DELETE", _p(rf"/orders/{_ID}")),
    ("PUT", _p(rf"/payments/{_ID}/method")),
    ("POST", _p(rf"/payments/{_ID}/refund")),
    ("POST", _p(rf"/items/{_ID}/(reject|revoke)")),
    ("POST", _p(rf"/documents/{_ID}/(printed|sms)")),
    ("POST", _p(rf"/units/{_ID}/messages/send")),
    ("POST", _p(rf"/templates/{_ID}/preview\.pdf")),
)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def api_path(raw: str) -> str:
    """The request path as the patterns here see it (without the API prefix)."""
    return raw[len(API_PREFIX) :] if raw.startswith(API_PREFIX) else raw


def tasks_for(permissions: frozenset[str] | set[str]) -> list[Task]:
    """The tasks a role with these permissions has to do (in the order they are practised)."""
    return [t for t in TASKS if any(p in permissions for p in t.perms)]


def task_done_by(method: str, path: str) -> str | None:
    """The task a successful `method path` stands for, if any."""
    return next((t.key for t in TASKS if t.method == method and t.path.match(path)), None)


def write_allowed(method: str, path: str) -> bool:
    return any(m == method and p.match(path) for m, p in ALLOWED_WRITES)
