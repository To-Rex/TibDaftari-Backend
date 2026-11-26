"""Mandatory training of a new employee («o‘quv rejimi»).

Until an employee has done the tasks of their role once, everything they do happens in a sandbox: the same screens,
the same code, but the patients, cheques, results and payments live in the `training` schema and never reach the
real tables, reports, counters, SMS or Telegram. When every task is done they press «finish» and start working with
the real data; a manager may let an experienced person through, or send someone back.

* `tasks`    — what there is to practise, by permission, and which writes a trainee may make at all
* `sandbox`  — builds the `training` schema from the live definitions of the real tables (and rebuilds it when
               they change)
* `gate`     — per request: is the caller a trainee (→ the sandbox pool), may this write happen, was a task done
* `service`  — the employee's state, the task list with sample data to practise on, finish / skip / reset
"""
