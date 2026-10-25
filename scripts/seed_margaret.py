"""Create Margaret's Nessie sandbox data. Setup only; the agent never calls this.

Reads NESSIE_API_KEY and NESSIE_BASE_URL from .env. Does not print the key.
If Margaret already exists, it adds only records that are not already there.

Demo shape from docs/FEATURES_AND_GOALS.md:
  one checking account, Social Security deposits, and bills from three known
  payees (Austin Energy, Water Co, Spectrum), all under the $500 co-sign line.

Checking balance stays $2,213, the figure set when the account was created.
This API stores balances, deposits, and purchase amounts as whole dollars;
bill amounts keep cents. It also does not change the balance when later
purchases are added. October spending on file is $176. The three pending
bills total $255.24, so paying them still leaves her above the $200 buffer.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIRST_NAME = "Margaret"
TARGET_BALANCE = 2213

BILLS = (
    {"status": "completed", "payee": "Water Co", "nickname": "Water",
     "payment_date": "2026-09-03", "recurring_date": 3, "payment_amount": 41.80},
    {"status": "completed", "payee": "Spectrum", "nickname": "Internet",
     "payment_date": "2026-09-12", "recurring_date": 12, "payment_amount": 89.99},
    {"status": "completed", "payee": "Austin Energy", "nickname": "Electric",
     "payment_date": "2026-09-30", "recurring_date": 30, "payment_amount": 118.60},
    {"status": "pending", "payee": "Austin Energy", "nickname": "Electric",
     "payment_date": "2026-10-31", "recurring_date": 31, "payment_amount": 120.00},
    {"status": "pending", "payee": "Water Co", "nickname": "Water",
     "payment_date": "2026-11-03", "recurring_date": 3, "payment_amount": 45.25},
    {"status": "pending", "payee": "Spectrum", "nickname": "Internet",
     "payment_date": "2026-11-12", "recurring_date": 12, "payment_amount": 89.99},
)

DEPOSITS = (
    {"medium": "balance", "transaction_date": "2026-08-03", "status": "completed",
     "amount": 1874, "description": "Social Security"},
    {"medium": "balance", "transaction_date": "2026-09-03", "status": "completed",
     "amount": 1874, "description": "Social Security"},
    {"medium": "balance", "transaction_date": "2026-10-03", "status": "completed",
     "amount": 1874, "description": "Social Security"},
    {"medium": "balance", "transaction_date": "2026-11-03", "status": "pending",
     "amount": 1874, "description": "Social Security"},
)

MERCHANTS = (
    {"name": "H-E-B", "category": "grocery",
     "address": {"street_number": "2701", "street_name": "E 7th St",
                 "city": "Austin", "state": "TX", "zip": "78702"},
     "geocode": {"lat": 30.259, "lng": -97.714}},
    {"name": "CVS", "category": "pharmacy",
     "address": {"street_number": "1901", "street_name": "E Martin Luther King Jr Blvd",
                 "city": "Austin", "state": "TX", "zip": "78702"},
     "geocode": {"lat": 30.280, "lng": -97.720}},
    {"name": "Shell", "category": "gas",
     "address": {"street_number": "1401", "street_name": "E 12th St",
                 "city": "Austin", "state": "TX", "zip": "78702"},
     "geocode": {"lat": 30.272, "lng": -97.724}},
    {"name": "Walgreens", "category": "pharmacy",
     "address": {"street_number": "1111", "street_name": "E 12th St",
                 "city": "Austin", "state": "TX", "zip": "78702"},
     "geocode": {"lat": 30.273, "lng": -97.727}},
)

PURCHASES = (
    {"merchant": "H-E-B", "medium": "balance", "purchase_date": "2026-10-06",
     "amount": 47, "status": "completed", "description": "Groceries"},
    {"merchant": "Shell", "medium": "balance", "purchase_date": "2026-10-11",
     "amount": 34, "status": "completed", "description": "Gas"},
    {"merchant": "H-E-B", "medium": "balance", "purchase_date": "2026-10-18",
     "amount": 62, "status": "completed", "description": "Groceries"},
    {"merchant": "CVS", "medium": "balance", "purchase_date": "2026-10-20",
     "amount": 18, "status": "completed", "description": "Pharmacy"},
    {"merchant": "Walgreens", "medium": "balance", "purchase_date": "2026-10-22",
     "amount": 15, "status": "completed", "description": "Prescription"},
)


def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in (REPO_ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    missing = [name for name in ("NESSIE_API_KEY", "NESSIE_BASE_URL") if not env.get(name)]
    if missing:
        raise SystemExit("Missing in .env: " + ", ".join(missing))
    return env


class Nessie:
    def __init__(self, base: str, key: str):
        self._base = base.rstrip("/")
        self._key = key

    def request(self, method: str, path: str, body: dict | None = None):
        url = self._base + path + "?" + urllib.parse.urlencode({"key": self._key})
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = raw[:400]
            if isinstance(parsed, str):
                parsed = parsed.replace(self._key, "[redacted]")
            raise SystemExit(f"{method} {path} failed: HTTP {exc.code} {parsed}") from None


def _created(payload) -> dict:
    if isinstance(payload, dict) and isinstance(payload.get("objectCreated"), dict):
        return payload["objectCreated"]
    if isinstance(payload, dict) and payload.get("_id"):
        return payload
    raise SystemExit(f"Unexpected create response: {payload}")


def _list(api: Nessie, path: str) -> list:
    data = api.request("GET", path)[1]
    return data if isinstance(data, list) else []


def _day(value) -> str:
    return str(value or "")[:10]


def _fill(api: Nessie, account_id: str) -> None:
    """Post any demo records this account does not already have."""
    bills = _list(api, f"/accounts/{account_id}/bills")
    have_bills = {(_day(b.get("payment_date")), b.get("payee"), b.get("status")) for b in bills}
    added_bills = 0
    for bill in BILLS:
        key = (bill["payment_date"], bill["payee"], bill["status"])
        if key in have_bills:
            continue
        _created(api.request("POST", f"/accounts/{account_id}/bills", bill)[1])
        added_bills += 1

    deposits = _list(api, f"/accounts/{account_id}/deposits")
    have_deposits = {
        (_day(d.get("transaction_date")), d.get("description"), d.get("status")) for d in deposits
    }
    added_deposits = 0
    for deposit in DEPOSITS:
        key = (deposit["transaction_date"], deposit["description"], deposit["status"])
        if key in have_deposits:
            continue
        _created(api.request("POST", f"/accounts/{account_id}/deposits", deposit)[1])
        added_deposits += 1

    merchants = _list(api, "/merchants")
    merchant_ids = {m.get("name"): m.get("_id") for m in merchants if m.get("name") and m.get("_id")}
    added_merchants = 0
    for merchant in MERCHANTS:
        if merchant["name"] in merchant_ids:
            continue
        created = _created(api.request("POST", "/merchants", merchant)[1])
        merchant_ids[merchant["name"]] = created["_id"]
        added_merchants += 1

    purchases = _list(api, f"/accounts/{account_id}/purchases")
    have_purchases = {(_day(p.get("purchase_date")), p.get("description")) for p in purchases}
    added_purchases = 0
    for purchase in PURCHASES:
        key = (purchase["purchase_date"], purchase["description"])
        if key in have_purchases:
            continue
        body = {field: value for field, value in purchase.items() if field != "merchant"}
        body["merchant_id"] = merchant_ids[purchase["merchant"]]
        _created(api.request("POST", f"/accounts/{account_id}/purchases", body)[1])
        added_purchases += 1

    api.request("PUT", f"/accounts/{account_id}", {
        "nickname": "Everyday Checking",
        "rewards": 0,
        "balance": TARGET_BALANCE,
    })
    print(
        f"Added {added_bills} bills, {added_deposits} deposits, "
        f"{added_merchants} merchants, {added_purchases} purchases."
    )


def _show(api: Nessie, customer_id: str) -> None:
    customer = api.request("GET", f"/customers/{customer_id}")[1]
    accounts = api.request("GET", f"/customers/{customer_id}/accounts")[1]
    name = f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip()
    print(f"customer {name} {customer.get('_id')}")
    if not isinstance(accounts, list):
        print("accounts", accounts)
        return
    for account in accounts:
        account_id = account.get("_id")
        print(
            f"  {account.get('type')} {account.get('nickname')!r} "
            f"balance={account.get('balance')} {account_id}"
        )
        for kind, fields in (
            ("bills", ("payee", "payment_amount", "payment_date", "status")),
            ("deposits", ("description", "amount", "transaction_date", "status")),
            ("purchases", ("description", "amount", "purchase_date", "status")),
        ):
            rows = api.request("GET", f"/accounts/{account_id}/{kind}")[1]
            rows = rows if isinstance(rows, list) else []
            print(f"    {kind}: {len(rows)}")
            for row in rows:
                print("     ", {field: row.get(field) for field in fields})


def main() -> None:
    env = _load_env()
    api = Nessie(env["NESSIE_BASE_URL"], env["NESSIE_API_KEY"])
    customers = api.request("GET", "/customers")[1]
    if not isinstance(customers, list):
        raise SystemExit(f"GET /customers returned {customers!r}")

    existing = [c for c in customers
                if str(c.get("first_name", "")).strip().lower() == FIRST_NAME.lower()]
    if existing:
        customer = existing[0]
        print(f"{FIRST_NAME} already exists; adding only missing records.")
    else:
        customer = _created(api.request("POST", "/customers", {
            "first_name": FIRST_NAME,
            "last_name": "Alvarez",
            "address": {
                "street_number": "2200",
                "street_name": "Manor Rd",
                "city": "Austin",
                "state": "TX",
                "zip": "78722",
            },
        })[1])

    accounts = _list(api, f"/customers/{customer['_id']}/accounts")
    account = next((a for a in accounts if str(a.get("type", "")).lower() == "checking"), None)
    if account is None:
        account = _created(api.request("POST", f"/customers/{customer['_id']}/accounts", {
            "type": "Checking",
            "nickname": "Everyday Checking",
            "rewards": 0,
            "balance": TARGET_BALANCE,
        })[1])

    _fill(api, account["_id"])
    _show(api, customer["_id"])


if __name__ == "__main__":
    try:
        main()
    except urllib.error.URLError as exc:
        print(f"Nessie request failed: {type(exc.reason).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
