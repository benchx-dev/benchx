"""The shared work-order builder behind bx bench and bx compare."""

from pathlib import Path

import pytest

from benchx import core, harnesses, orders, session

SUITE = {"adapter": "google-benchmark", "suite": "demo-bench"}
TARGET = {"kind": "build", "path": "/b", "source_dir": "/s",
          "source": {"uri": "https://example.org/demo.git", "type": "git"}}


def test_minimal_order_is_valid_and_leaves_optionals_out():
    order = orders.make_order(suite=SUITE, target=TARGET, precision={"repetitions": 5}, run_key="rk")
    assert core.check(order, "work-order") == []
    assert order["requester"] == {"kind": "workbench", "name": "bx"}
    assert order["timeouts"] == {"case_s": orders.CASE_TIMEOUT_S, "order_s": orders.ORDER_TIMEOUT_S}
    for absent in ("round", "slot", "labels", "project", "environment_variables"):
        assert absent not in order


def test_round_and_slot_zero_are_kept():
    order = orders.make_order(suite=SUITE, target=TARGET, precision={"repetitions": 1}, run_key="rk",
                              round_=0, slot=0)
    assert (order["round"], order["slot"]) == (0, 0)


def test_quantities_set_quantities_and_plan():
    order = orders.make_order(suite=SUITE, target=TARGET, precision={"repetitions": 1}, run_key="rk",
                              quantities=["wall-time", "cpu-time"])
    assert order["quantities"] == ["wall-time", "cpu-time"]
    assert order["plan"] == [{"id": "p0", "case": "*", "quantity": "wall-time"},
                             {"id": "p1", "case": "*", "quantity": "cpu-time"}]


def test_each_order_gets_its_own_id():
    make = lambda: orders.make_order(suite=SUITE, target=TARGET, precision={"repetitions": 1}, run_key="rk")
    assert make()["work_order_id"] != make()["work_order_id"]


def test_invalid_order_is_refused():
    with pytest.raises(orders.OrderError):
        orders.make_order(suite=SUITE, target=TARGET, precision={"repetitions": 0}, run_key="rk")


def test_default_run_key():
    assert orders.default_run_key("bench").startswith("bench-")


def test_compare_orders_are_now_validated():
    side = {"kind": "build", "path": Path("/b"), "source": "/s", "identity": {}, "uri": None, "text": {}, "labels": {}}
    kwargs = dict(harness=harnesses.GOOGLE_BENCHMARK, suite="demo-bench", filter=None, quantities=["wall-time"],
                  environment_variables=None, project=None, run_key="rk", source_uri="https://example.org/demo.git",
                  round_=0, slot=0, labels=None)
    assert core.check(session._order(side, precision={"repetitions": 5}, **kwargs), "work-order") == []
    with pytest.raises(session.SessionError, match="round 0"):
        session._order(side, precision={"repetitions": 0}, **kwargs)
