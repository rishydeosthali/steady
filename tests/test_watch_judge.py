from datetime import date

from backend.watch.demo import (LIVE_CLOCK, bill, bill_increase_shape, deposit, deposit_missing_shape,
                                live_shape, purchase, run_pair, snapshot)
from backend.watch.judge import judge_contemplated, safety_view
from backend.watch.rhythm import build_rhythm
from backend.watch.units import to_cents
from backend.watch.wording import fallback

OCT6 = date(2026, 10, 6)


def kinds(verdicts):
    return [v["kind"] for v in verdicts]


def tape_row(payee_key, decision, payment_date=None):
    return {"payee_key": payee_key, "decision": decision, "payment_date": payment_date}


# -- live shape -------------------------------------------------------------

def test_live_first_look_hands_bills_to_the_planner_and_stays_quiet():
    v = run_pair(None, live_shape(), LIVE_CLOCK)
    assert kinds(v) == ["bill_due", "bill_due"]
    assert [x["facts"]["payee"] for x in v] == ["Austin Energy", "Water Co"]
    assert all(x["consumer"] == "planner" and x["notify"] is False for x in v)
    assert all(x["facts"]["hold_scheduling"] is False for x in v)


def test_spectrum_outside_the_14_day_window_is_not_due_yet():
    v = run_pair(None, live_shape(), LIVE_CLOCK)
    assert "Spectrum" not in [x["facts"]["payee"] for x in v]
    later = run_pair(None, live_shape(), date(2026, 10, 29))
    assert "Spectrum" in [x["facts"]["payee"] for x in later]


def test_no_alerts_on_live_shape_when_nothing_changed():
    assert run_pair(live_shape(), live_shape(), LIVE_CLOCK) == run_pair(None, live_shape(), LIVE_CLOCK)


def test_new_grocery_trip_inside_the_pocket_is_silent():
    after = live_shape()
    after["purchases"].append(purchase("p-heb3", "H-E-B", "40.00", "2026-10-24"))
    v = run_pair(live_shape(), after, LIVE_CLOCK)
    assert kinds(v) == ["bill_due", "bill_due"]


def test_one_new_charge_somewhere_new_is_memory_only():
    after = live_shape()
    after["purchases"].append(purchase("p-new", "Joann", "22.00", "2026-10-24"))
    v = [x for x in run_pair(live_shape(), after, LIVE_CLOCK) if x["kind"] != "bill_due"]
    assert kinds(v) == ["new_merchant"]
    assert v[0]["notify"] is False and v[0]["consumer"] == "memory_only"


def test_never_alert_because_a_purchase_is_above_average():
    after = live_shape()
    after["purchases"].append(purchase("p-heb3", "H-E-B", "180.00", "2026-10-24"))
    assert [x for x in run_pair(live_shape(), after, LIVE_CLOCK) if x["kind"] != "bill_due"] == []


# -- bill increase ------------------------------------------------------------

def test_bill_increase_fires_with_the_spec_sentence():
    v = run_pair(None, bill_increase_shape("85.00"), LIVE_CLOCK)
    inc = [x for x in v if x["kind"] == "bill_increase"]
    assert len(inc) == 1
    assert fallback(inc[0]) == ("Your internet bill increased from $65 to $85. "
                                "Is that normal? Reply yes or no.")
    assert inc[0]["dedupe_key"] == "bill_increase:b-net-pending:8500"


def test_bill_increase_inside_the_band_does_not_fire():
    assert "bill_increase" not in kinds(run_pair(None, bill_increase_shape("67.00"), LIVE_CLOCK))


def test_single_completed_bill_never_fires_an_increase():
    assert "bill_increase" not in kinds(run_pair(None, bill_increase_shape("85.00", ("65.00",)),
                                                 LIVE_CLOCK))


def test_a_decrease_is_never_an_increase():
    assert "bill_increase" not in kinds(run_pair(None, bill_increase_shape("40.00"), LIVE_CLOCK))


def test_austin_energy_118_60_to_120_is_not_an_increase():
    assert "bill_increase" not in kinds(run_pair(None, live_shape(), LIVE_CLOCK))


# -- duplicate ----------------------------------------------------------------

def dentist(*rows):
    return snapshot("2213.00", purchases=[purchase(i, "dentist", amt, d) for i, amt, d in rows])


def test_duplicate_charge_today():
    v = run_pair(dentist(("p1", "120.00", "2026-10-24")),
                 dentist(("p1", "120.00", "2026-10-24"), ("p2", "120.00", "2026-10-24")), LIVE_CLOCK)
    assert kinds(v) == ["duplicate"]
    assert fallback(v[0]) == "There are two $120 dentist charges today. Were you expecting both?"
    assert v[0]["dedupe_key"] == "duplicate:dentist:12000:2026-10-24"


def test_duplicate_on_an_earlier_day_names_the_date():
    v = run_pair(dentist(("p1", "120.00", "2026-10-21")),
                 dentist(("p1", "120.00", "2026-10-21"), ("p2", "120.00", "2026-10-22")), LIVE_CLOCK)
    assert fallback(v[0]) == ("There are two $120 dentist charges on Oct 21 and Oct 22. "
                              "Were you expecting both?")
    assert v[0]["facts"]["same_day_as_clock"] is False


def test_duplicates_already_there_at_first_look_are_baseline():
    both = dentist(("p1", "120.00", "2026-10-24"), ("p2", "120.00", "2026-10-24"))
    assert run_pair(None, both, LIVE_CLOCK) == []


def test_thirty_days_later_is_a_subscription_not_a_duplicate():
    v = run_pair(dentist(("p1", "120.00", "2026-09-24")),
                 dentist(("p1", "120.00", "2026-09-24"), ("p2", "120.00", "2026-10-24")), LIVE_CLOCK)
    assert kinds(v) == ["subscription"]


def test_two_grocery_trips_of_different_amounts_are_not_duplicates():
    before = snapshot("2213.00", purchases=[purchase("a", "H-E-B", "47.00", "2026-10-23")])
    after = snapshot("2213.00", purchases=[purchase("a", "H-E-B", "47.00", "2026-10-23"),
                                           purchase("b", "H-E-B", "62.00", "2026-10-24")])
    assert "duplicate" not in kinds(run_pair(before, after, LIVE_CLOCK))


def test_duplicate_that_breaks_the_pocket_stays_a_duplicate():
    before = snapshot("300.00", purchases=[purchase("p1", "dentist", "120.00", "2026-10-24")])
    after = snapshot("300.00", purchases=[purchase("p1", "dentist", "120.00", "2026-10-24"),
                                          purchase("p2", "dentist", "120.00", "2026-10-24")])
    v = run_pair(before, after, LIVE_CLOCK)
    assert kinds(v) == ["duplicate"]
    assert v[0]["facts"]["pocket"]["left_after"] == "$180.00"
    assert fallback(v[0]).startswith("There are two $120 dentist charges today.")


# -- subscription ------------------------------------------------------------

def streambox(*rows, balance="2213.00"):
    return snapshot(balance, purchases=[purchase(i, "StreamBox", amt, d) for i, amt, d in rows])


def test_subscription_locks_on_the_new_second_charge():
    v = run_pair(streambox(("s1", "14.99", "2026-09-24")),
                 streambox(("s1", "14.99", "2026-09-24"), ("s2", "14.99", "2026-10-24")), LIVE_CLOCK)
    assert kinds(v) == ["subscription"]
    assert fallback(v[0]) == "StreamBox charged $14.99 again. This may be a monthly subscription."
    assert v[0]["dedupe_key"] == "subscription:streambox:1499"


def test_one_charge_alone_is_silent_to_mom():
    before = snapshot("2213.00", purchases=[purchase("x", "Shell", "34.00", "2026-10-11")])
    after = snapshot("2213.00", purchases=[purchase("x", "Shell", "34.00", "2026-10-11"),
                                           purchase("s1", "StreamBox", "14.99", "2026-10-24")])
    v = run_pair(before, after, LIVE_CLOCK)
    assert kinds(v) == ["new_merchant"] and v[0]["notify"] is False


def test_both_charges_at_first_look_are_silent_and_steady():
    both = streambox(("s1", "14.99", "2026-09-24"), ("s2", "14.99", "2026-10-24"))
    assert run_pair(None, both, LIVE_CLOCK) == []
    assert build_rhythm(both)["purchases"]["streambox"]["status"] == "steady"


def test_different_amount_or_short_gap_does_not_lock():
    assert run_pair(streambox(("s1", "14.99", "2026-09-24")),
                    streambox(("s1", "14.99", "2026-09-24"), ("s2", "40.00", "2026-10-24")),
                    LIVE_CLOCK) == []
    assert run_pair(streambox(("s1", "14.99", "2026-10-14")),
                    streambox(("s1", "14.99", "2026-10-14"), ("s2", "14.99", "2026-10-24")),
                    LIVE_CLOCK) == []


def test_third_month_does_not_lock_again():
    two = streambox(("s1", "14.99", "2026-08-24"), ("s2", "14.99", "2026-09-24"))
    three = streambox(("s1", "14.99", "2026-08-24"), ("s2", "14.99", "2026-09-24"),
                      ("s3", "14.99", "2026-10-24"))
    assert run_pair(two, three, LIVE_CLOCK) == []


def test_subscription_that_breaks_the_pocket_is_a_shortfall_with_the_flag():
    v = run_pair(streambox(("s1", "14.99", "2026-09-24"), balance="210.00"),
                 streambox(("s1", "14.99", "2026-09-24"), ("s2", "14.99", "2026-10-24"),
                           balance="210.00"), LIVE_CLOCK)
    assert kinds(v) == ["pocket_shortfall"]
    assert v[0]["facts"]["subscription"] is True and v[0]["facts"]["when"] == "posted"
    assert fallback(v[0]) == "This leaves $195.01 after your upcoming bills."


# -- missing deposit ---------------------------------------------------------

def test_missing_october_social_security_fires_without_an_amount():
    v = run_pair(None, deposit_missing_shape(), OCT6)
    assert kinds(v) == ["deposit_missing"]
    sentence = fallback(v[0])
    assert sentence == ("Your usual deposit hasn't appeared yet. "
                        "Let's check before scheduling payments.")
    assert "$" not in sentence
    assert v[0]["facts"]["expected_date"] == "Oct 3" and v[0]["facts"]["usual_amount"] == "$1,874.00"
    assert v[0]["dedupe_key"] == "deposit_missing:social security:2026-10-03"


def test_not_missing_inside_the_arrival_window():
    assert run_pair(None, deposit_missing_shape(), date(2026, 10, 5)) == []


def test_seeded_november_pending_deposit_does_not_fire():
    assert "deposit_missing" not in kinds(run_pair(None, live_shape(), LIVE_CLOCK))


def test_one_completed_deposit_never_fires():
    shape = snapshot("2213.00", deposits=[deposit("d", "1874.00", "2026-09-03")])
    assert run_pair(None, shape, OCT6) == []


def test_missing_deposit_holds_scheduling_on_every_bill_due():
    shape = deposit_missing_shape(bills=[bill("b-ae", "Austin Energy", "120.00", "2026-10-15")])
    v = run_pair(None, shape, OCT6)
    assert kinds(v) == ["deposit_missing", "bill_due"]
    assert v[1]["facts"]["hold_scheduling"] is True


def test_missing_deposit_emits_alongside_a_purchase_verdict():
    before = deposit_missing_shape()
    after = deposit_missing_shape()
    after["purchases"] = [purchase("p", "Joann", "10.00", "2026-10-06")]
    assert sorted(kinds(run_pair(before, after, OCT6))) == ["deposit_missing", "new_merchant"]


# -- held payment -----------------------------------------------------------

def test_held_payee_returns_as_held_not_bill_due():
    roofer = snapshot("2213.00", bills=[bill("b-roof", "Roofer", "2400.00", "2026-10-30")])
    v = run_pair(None, roofer, LIVE_CLOCK, [tape_row("roofer", "sarah_denied")])
    assert kinds(v) == ["held_payment"]
    assert fallback(v[0]) == "You already put this payment on hold. It's still on hold."
    assert v[0]["over_threshold"] is True and v[0]["consumer"] == "mom"
    assert "Sarah" not in fallback(v[0])
    assert v[0]["dedupe_key"] == "held_payment:roofer:b-roof"


def test_held_matches_payee_even_at_a_new_amount():
    roofer = snapshot("2213.00", bills=[bill("b-roof2", "ROOFER", "1800.00", "2026-10-30")])
    assert kinds(run_pair(None, roofer, LIVE_CLOCK, [tape_row("roofer", "declined")])) == ["held_payment"]


def test_accepted_row_suppresses_that_bill_due():
    v = run_pair(None, live_shape(), LIVE_CLOCK, [tape_row("austin energy", "accepted", "2026-10-31")])
    assert [x["facts"]["payee"] for x in v] == ["Water Co"]


def test_contemplated_purchase_to_a_held_payee_is_held():
    shape = live_shape()
    r = judge_contemplated(shape, build_rhythm(shape), [tape_row("roofer", "sarah_denied")],
                           LIVE_CLOCK, "Roofer", to_cents("2400.00"))
    assert r.verdict["kind"] == "held_payment"


# -- safety ------------------------------------------------------------------

def test_safety_view_never_carries_balance_or_pocket():
    v = judge_contemplated(live_shape(), build_rhythm(live_shape()), [], LIVE_CLOCK, "Dentist",
                           to_cents("1900.00")).verdict
    hidden = safety_view(v["facts"])
    assert not {"balance", "left_after", "buffer", "reserved_bills"} & set(hidden)
    assert hidden["purchase_amount"] == "$1,900.00"
