"""The work orders the workbench writes (schemas/work-order/0.1.0).

`bx bench` and `bx compare` (measure mode) each work out the parts of an order
that depend on their harness and target: the suite entry, the target, the
precision. Everything else is the same envelope, so it is filled in here, and
every order is checked against its schema before anything runs it.
"""

import uuid
from datetime import datetime, timezone

from . import core

SCHEMA_VERSION = "benchx/work-order/0.1.0"
REQUESTER = {"kind": "workbench", "name": "bx"}
CASE_TIMEOUT_S, ORDER_TIMEOUT_S = 60, 600


class OrderError(Exception):
    """The order does not satisfy its schema or rules; the message says why."""


def default_run_key(prefix: str) -> str:
    """`PREFIX-<UTC timestamp>`, the run key when the user names none."""
    return f"{prefix}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"


def make_order(*, suite: dict, target: dict, precision: dict, run_key: str, quantities=("wall-time",),
               case_s: float = CASE_TIMEOUT_S, order_s: float = ORDER_TIMEOUT_S, project=None, labels=None,
               environment_variables=None, round_=None, slot=None, benchmark=None,
               collectors=None) -> dict:
    """One resolved work order. Optional fields that are None or empty are left out.

    Raises OrderError when the result is not a valid work order.
    """
    order = {
        "schema_version": SCHEMA_VERSION,
        "work_order_id": f"urn:uuid:{uuid.uuid4()}",
        "state": "resolved",
        "suites": [suite],
        "target": target,
        "quantities": list(quantities),
        "precision": precision,
        "timeouts": {"case_s": case_s, "order_s": order_s},
        "run_key": run_key,
        "requester": dict(REQUESTER),
        # Placeholder: the runner does not yet cross-check plan against the
        # case list it discovers (runner-schema.md §3.2).
        "plan": [{"id": f"p{i}", "case": "*", "quantity": quantity} for i, quantity in enumerate(quantities)],
    }
    if round_ is not None:
        order["round"] = round_
    if slot is not None:
        order["slot"] = slot
    if environment_variables:
        order["environment_variables"] = dict(environment_variables)
    if labels:
        order["labels"] = dict(labels)
    if project:
        order["project"] = project
    if benchmark:
        order["benchmark"] = benchmark
    if collectors:
        order["collectors"] = list(collectors)
    errors = core.check(order, "work-order")
    if errors:
        raise OrderError("; ".join(str(error) for error in errors))
    return order
