"""Habits: what a household buys again and again, read from its Silpo history.

Three pure stages, none of which touches the network or a database:

* `purchases` — the two history payloads become `PurchaseEvent`s, with every rule the
  2026-09-13 capture taught (reference §9, Plan 3 Task 0).
* `engine` — events become `Habit`s, or nothing: silence below confidence is the rule.
* `draft` — due habits become `KnownLine`s for a basket built without a model request.

`importer` is the one stage that reads Silpo, through the `SilpoClient` protocol, so
it is still tested with a fake. Persistence lives in `db/repo.py`.
"""
