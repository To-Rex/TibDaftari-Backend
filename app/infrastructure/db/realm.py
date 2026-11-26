"""Which data a request works with: the REAL one, or the TRAINING sandbox.

An employee who is still learning the system («o‘quv rejimi», see `app.modules.training`) must not write into the
real data. Their requests therefore run on a second connection pool whose `search_path` is `training, public`: the
operational tables (patients, cheques, items, payments, documents, counters, outbox, notifications, audit) exist
once more in the `training` schema and shadow the real ones, while everything else — the organisation tree, staff,
the catalogue, templates — resolves to `public` as usual. Nothing in the code has to know: the same ORM models and
the same SQL read and write the sandbox. Workers, the bots, the patient portal and public links never use it.

`is_training()` is the one thing application code may ask (to skip a side effect that would leave the sandbox:
keeping a PDF, calling an SMS provider) — it is a context variable set per request by `get_session`.
"""

from __future__ import annotations

from contextvars import ContextVar

#: the schema that shadows the operational tables for a trainee
SANDBOX_SCHEMA = "training"
#: the database role every sandbox connection takes: it may read the real tables and write only the sandbox ones,
#: so a mistake in the code cannot put practice data into the real data — the database itself refuses
SANDBOX_ROLE = "tibdaftari_trainee"

_training: ContextVar[bool] = ContextVar("db_realm_training", default=False)


def is_training() -> bool:
    """True while the current request belongs to an employee in training (their data is the sandbox)."""
    return _training.get()


def enter(training: bool) -> object:
    """Mark the current context; returns the token for `leave`."""
    return _training.set(training)


def leave(token: object) -> None:
    try:
        _training.reset(token)  # type: ignore[arg-type]
    except ValueError:  # reset from another context (a generator finalised elsewhere): just fall back to real
        _training.set(False)
