"""Conservative forecasts in integer cents. Pending income is never cleared cash.

Balance already includes settled deposits and purchases. No recurring future
income or expenses are invented. Savings capacity is not a savings transfer.
"""

from datetime import date, timedelta

from ..payments.money import cents, money


def cash_cents(value):
    """Balances may be negative; payment amounts and goal values may not."""
    if value is None or isinstance(value, bool):
        raise ValueError("Missing balance")
    raw = str(value).strip()
    return -cents(raw[1:]) if raw.startswith("-") else cents(raw)


def forecast(goal, snapshot):
    today = date.fromisoformat(snapshot["today"])
    days = (date.fromisoformat(goal["deadline"]) - today).days + 1
    if not 1 <= days <= 367:
        raise ValueError("Goal deadline is outside the supported horizon")
    weekly = goal.get("weekly_essentials_cents")
    if weekly is None:
        return {"status": "needs_essentials", "goal_id": goal["id"]}
    balance = snapshot["available_cents"]
    minimum = balance
    daily = (weekly + 6) // 7
    pending = [d for d in snapshot["deposits"] if d.get("status") == "pending"
               and snapshot["today"] < str(d.get("transaction_date", ""))[:10] <= goal["deadline"]]
    bills_total = 0
    for offset in range(days):
        when = (today + timedelta(days=offset)).isoformat()
        for deposit in pending:
            if str(deposit["transaction_date"])[:10] == when:
                balance += cents(deposit.get("amount"))
        due = sum(b["amount_cents"] for b in snapshot["obligations"]
                  if max(b["due_date"], snapshot["today"]) == when)
        bills_total += due
        balance -= due + daily
        minimum = min(minimum, balance)
    buffer = max(20000, goal["buffer_cents"])
    capacity = max(0, balance - buffer)
    shortfall = max(0, goal["target_cents"] - capacity)
    buffer_shortfall = max(0, buffer - minimum)
    status = "at_risk" if shortfall or buffer_shortfall else "on_track"
    if snapshot.get("incomplete"):
        status = "incomplete"
    return dict(status=status, goal_id=goal["id"], deadline=goal["deadline"],
                target_cents=goal["target_cents"], projected_capacity_cents=capacity,
                goal_shortfall_cents=shortfall, buffer_shortfall_cents=buffer_shortfall,
                essentials_cents=daily * days, known_bills_cents=bills_total,
                conditional_on_pending_income=bool(pending), verified_saved_cents=None, days=days)


def render(goal, report):
    if report["status"] == "needs_essentials":
        return ("I need your weekly essentials allowance before I can make a useful budget. "
                "Include groceries, transport, and other costs outside your listed bills. "
                "Send a revised goal with that weekly amount; I will ask you to confirm it.")
    text = (f"For your {money(goal['target_cents'])} goal by {goal['deadline']}, "
            f"I project {money(report['projected_capacity_cents'])} available above your "
            f"{money(goal['buffer_cents'])} cushion after known bills and your essentials allowance. ")
    if report["status"] == "incomplete":
        text += "Some bill information is missing, so this plan needs checking. "
    if report["buffer_shortfall_cents"]:
        text += f"The forecast falls below your cushion by {money(report['buffer_shortfall_cents'])}. "
    if report["goal_shortfall_cents"]:
        text += (f"The goal has a projected gap of {money(report['goal_shortfall_cents'])}. "
                 "Would you prefer a later deadline or a smaller savings target? "
                 "I will keep your current goal until you confirm a change. ")
    if report["conditional_on_pending_income"]:
        text += "This assumes the listed pending income arrives; it is not cleared money. "
    return text + "This is a plan, not a savings transfer. Unlisted future bills and income are not included."
