from datetime import date

from backend.watch.demo import (LIVE_CLOCK, THURSDAY_CLOCK, bill, deposit, live_shape, snapshot,
                                thursday_shape)
from backend.watch.judge import judge_contemplated
from backend.watch.pocket import compute_pocket
from backend.watch.rhythm import build_rhythm
from backend.watch.units import to_cents
from backend.watch.wording import fallback


def contemplate(shape, clock, amount, merchant="Dentist", tape=()):
    return judge_contemplated(shape, build_rhythm(shape), list(tape), clock, merchant, to_cents(amount))


def pocket_for(shape, clock, amount, missing=False):
    return compute_pocket(shape["balance_cents"], shape["bills"], shape["deposits"], clock,
                          to_cents(amount), missing)


def test_reserved_bills_stop_at_the_next_deposit():
    p = pocket_for(live_shape(), LIVE_CLOCK, "0")
    assert [b["payee"] for b in p.reserved] == ["Austin Energy", "Water Co"]   # Spectrum is next cycle
    assert p.reserved_cents == 16525
    assert p.horizon_date == date(2026, 11, 3) and p.horizon_reason == "next_deposit"
    assert p.horizon_label == "Social Security"


def test_1900_leaves_147_75_with_the_spec_sheet():
    v = contemplate(live_shape(), LIVE_CLOCK, "1900.00").verdict
    assert v["kind"] == "pocket_shortfall" and v["over_threshold"] is True
    assert v["dedupe_key"] == "pocket_shortfall:contemplate:190000"
    f = v["facts"]
    assert f["left_after"] == "$147.75" and f["balance"] == "$2,213.00" and f["buffer"] == "$200.00"
    assert f["when"] == "contemplated" and f["same_day_as_clock"] is False
    assert f["reserved_bills"] == [
        {"payee": "Austin Energy", "amount": "$120.00", "date": "Oct 31", "weekday": "Saturday"},
        {"payee": "Water Co", "amount": "$45.25", "date": "Nov 3", "weekday": "Tuesday"},
    ]
    assert fallback(v) == "This would leave $147.75 after your upcoming bills."


def test_exactly_200_left_is_a_shortfall():
    v = contemplate(live_shape(), LIVE_CLOCK, "1847.75").verdict
    assert v and v["facts"]["left_after"] == "$200.00"


def test_one_cent_above_buffer_holds():
    r = contemplate(live_shape(), LIVE_CLOCK, "1847.74")
    assert r.verdict is None and r.pocket.left_after_cents == 20001


def test_groceries_inside_the_pocket_are_silent():
    r = contemplate(live_shape(), LIVE_CLOCK, "40.00", merchant="H-E-B")
    assert r.verdict is None and r.pocket.holds


def test_thursday_shape():
    v = contemplate(thursday_shape(), THURSDAY_CLOCK, "100.00").verdict
    assert v["source"] == "fixture"
    assert v["facts"]["left_after"] == "$180.00"
    assert v["facts"]["reserved_bills"][0]["weekday"] == "Thursday"
    assert fallback(v) == "This would leave $180.00 after your upcoming bills."
    assert THURSDAY_CLOCK.strftime("%A") == "Monday"


def test_bill_due_on_deposit_day_is_reserved():
    shape = snapshot("500.00", bills=[bill("b", "Water Co", "45.25", "2026-11-03")],
                     deposits=[deposit("d", "1874.00", "2026-11-03", "pending")])
    assert pocket_for(shape, LIVE_CLOCK, "0").reserved_cents == 4525


def test_no_deposit_on_file_uses_14_days():
    shape = snapshot("500.00", bills=[bill("a", "Austin Energy", "120.00", "2026-11-07"),
                                      bill("b", "Spectrum", "89.99", "2026-11-08")])
    p = pocket_for(shape, LIVE_CLOCK, "0")
    assert p.horizon_reason == "no_deposit_on_file" and p.horizon_date == date(2026, 11, 7)
    assert p.reserved_cents == 12000


def test_missing_deposit_does_not_set_the_horizon():
    p = pocket_for(live_shape(), LIVE_CLOCK, "0", missing=True)
    assert p.horizon_reason == "deposit_missing" and p.horizon_date == date(2026, 11, 7)
    assert p.horizon_label is None


def test_balance_is_used_as_returned_not_reduced_by_history():
    p = pocket_for(live_shape(), LIVE_CLOCK, "0")
    assert p.balance_cents == 221300            # five purchases on file are not subtracted


def test_unknown_balance_has_no_pocket():
    shape = live_shape()
    shape["balance_cents"] = None
    r = contemplate(shape, LIVE_CLOCK, "1900.00")
    assert r.verdict is None and r.pocket is None
