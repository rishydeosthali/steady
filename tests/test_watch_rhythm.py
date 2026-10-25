import ast
from pathlib import Path

from backend.watch.demo import bill, deposit, live_shape, purchase, snapshot
from backend.watch.rhythm import build_group, build_rhythm
from backend.watch.units import key_of, money_full, money_short, next_month_on_day, to_cents
from datetime import date


def test_cents_use_decimal_and_reject_bools():
    assert to_cents(118.6) == 11860
    assert to_cents("1,900.00") == 190000
    assert to_cents(45.25) == 4525
    assert to_cents(True) is None and to_cents(None) is None and to_cents("abc") is None


def test_keys_are_normalized_exactly_never_fuzzy():
    assert key_of("H-E-B") == key_of("HEB") == "heb"
    assert key_of("  Barnes & Noble ") == "barnes and noble"
    assert key_of("Social Security") == "social security"
    assert key_of("Walgreen") != key_of("Walgreens")


def test_money_formats():
    assert money_full(14775) == "$147.75"
    assert money_full(221300) == "$2,213.00"
    assert money_short(6500) == "$65" and money_short(1499) == "$14.99"


def test_live_shape_statuses():
    r = build_rhythm(live_shape())
    heb = r["purchases"]["heb"]
    assert heb["status"] == "uneven"
    assert (heb["low_cents"], heb["high_cents"]) == (4700, 6200)
    for key in ("shell", "cvs", "walgreens"):
        assert r["purchases"][key]["status"] == "sketch"
        assert r["purchases"][key]["median_cents"] is None
    ae = r["bills"]["austin energy"]
    assert ae["status"] == "sketch" and ae["completed_count"] == 1 and ae["previous_cents"] == 11860
    ss = r["deposits"]["social security"]
    assert ss["status"] == "steady" and ss["day_of_month"] == 3 and ss["completed_count"] == 3


def test_pending_bills_are_not_history():
    r = build_rhythm(live_shape())
    assert "water co" not in r["bills"] and "spectrum" not in r["bills"]


def test_steady_band_is_100_cents_or_2_percent():
    near = build_group("purchases", [purchase("a", "Gym", "30.00", "2026-09-01"),
                                     purchase("b", "Gym", "31.00", "2026-10-01")])
    assert near["gym"]["status"] == "steady"
    wide = build_group("purchases", [purchase("a", "Gym", "30.00", "2026-09-01"),
                                     purchase("b", "Gym", "31.01", "2026-10-01")])
    assert wide["gym"]["status"] == "uneven"


def test_fields_for_one_and_zero_records():
    r = build_group("bills", [bill("x", "Water Co", "41.80", "2026-09-03", "completed")])
    w = r["water co"]
    assert w["completed_count"] == 1 and w["median_cents"] is None and w["low_cents"] is None
    assert w["previous_cents"] == 4180 and w["gaps_days"] == [] and w["day_of_month"] == 3


def test_display_is_most_recent_raw_name_and_gaps_in_order():
    r = build_group("purchases", [purchase("a", "HEB", "47.00", "2026-10-06"),
                                  purchase("b", "H-E-B", "62.00", "2026-10-18")])
    assert r["heb"]["display"] == "H-E-B"
    assert r["heb"]["gaps_days"] == [12]


def test_day_of_month_null_when_days_differ():
    r = build_group("deposits", [deposit("a", "10.00", "2026-08-03"), deposit("b", "10.00", "2026-09-04")])
    assert r["social security"]["day_of_month"] is None


def test_next_month_clamps_short_months():
    assert next_month_on_day(date(2026, 10, 3), 3) == date(2026, 11, 3)
    assert next_month_on_day(date(2026, 1, 31), 31) == date(2026, 2, 28)
    assert next_month_on_day(date(2026, 12, 3), 3) == date(2027, 1, 3)


def test_pure_modules_import_no_nessie_llm_or_flask():
    root = Path(__file__).resolve().parent.parent / "backend" / "watch"
    banned = ("nessie", "llm", "flask", "openai", "twilio", "requests", "agent")
    for name in ("rhythm.py", "pocket.py", "judge.py", "units.py"):
        tree = ast.parse((root / name).read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                for mod in mods:
                    assert not any(b in mod.lower() for b in banned), f"{name} imports {mod}"


def test_empty_snapshot():
    assert build_rhythm(snapshot("10.00")) == {"bills": {}, "deposits": {}, "purchases": {}}
