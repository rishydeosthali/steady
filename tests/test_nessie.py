import pytest

from backend.nessie.client import CustomerNotFound, NessieClient, NessieError
from tests.conftest import DUMMY_NESSIE_KEY, FakeResponse, FakeSession

BASE = "https://nessie.invalid"


def client(routes):
    session = FakeSession(routes)
    return NessieClient(BASE, DUMMY_NESSIE_KEY, session=session), session


def test_no_margaret_reports_names_that_exist_and_creates_nothing():
    c, s = client({"/customers": FakeResponse(200, [
        {"_id": "1", "first_name": "Bob", "last_name": "Ross"}])})
    with pytest.raises(CustomerNotFound) as exc:
        c.resolve("Margaret")
    assert exc.value.existing == ["Bob Ross"]
    assert {m for m, _, _ in s.requests} == {"GET"}


def test_empty_customer_list():
    c, _ = client({"/customers": FakeResponse(200, [])})
    with pytest.raises(CustomerNotFound) as exc:
        c.resolve("Margaret")
    assert exc.value.existing == []


def test_discovers_ids_and_uses_checking_account_with_key_param_only_get():
    c, s = client({
        "/customers": FakeResponse(200, [{"_id": "cust9", "first_name": "Margaret",
                                          "last_name": "X"}]),
        "/customers/cust9/accounts": FakeResponse(200, [
            {"_id": "sav", "type": "Savings"}, {"_id": "chk", "type": "Checking"}]),
        "/accounts/chk": FakeResponse(200, {"_id": "chk", "balance": 321.0}),
    })
    account = c.resolve("Margaret")
    assert account["_id"] == "chk"
    assert c.get_balance("chk") == 321.0
    assert all(m == "GET" and p == {"key": DUMMY_NESSIE_KEY} for m, _, p in s.requests)


def test_404_on_lists_means_empty():
    c, _ = client({})
    assert c.get_bills("chk") == [] and c.get_deposits("chk") == []


def test_errors_never_contain_the_key():
    class Boom(FakeSession):
        def get(self, url, params=None, timeout=None):
            import requests
            raise requests.ConnectionError(f"failed for {url}?key={params['key']}")

        def request(self, method, url, params=None, json=None, timeout=None):
            import requests
            raise requests.ConnectionError(f"failed for {url}?key={params['key']}")

    c = NessieClient(BASE, DUMMY_NESSIE_KEY, session=Boom({}))
    with pytest.raises(NessieError) as exc:
        c.get_bills("chk")
    assert DUMMY_NESSIE_KEY not in str(exc.value)


def test_http_error_message_has_no_key():
    c, _ = client({"/accounts/chk/bills": FakeResponse(500, {})})
    with pytest.raises(NessieError) as exc:
        c.get_bills("chk")
    assert DUMMY_NESSIE_KEY not in str(exc.value)


def test_create_bill_posts_body_and_returns_object():
    created = {"_id": "bill-9", "payee": "Austin Energy", "payment_amount": 130.0,
               "payment_date": "2026-10-31", "status": "pending"}
    c, s = client({"/accounts/chk/bills": FakeResponse(201, {"objectCreated": created})})
    body = {"status": "pending", "payee": "Austin Energy", "payment_amount": 130.0,
            "payment_date": "2026-10-31", "recurring_date": 31, "nickname": "Electric"}
    assert c.create_bill("chk", body) == created
    assert s.requests[-1][0] == "POST"
    assert s.bodies[-1] == body


def test_update_bill_puts_body():
    c, s = client({"/bills/bill-ae": FakeResponse(202, {"message": "Accepted"})})
    body = {"status": "pending", "payee": "Austin Energy", "payment_amount": 130.0,
            "payment_date": "2026-10-31", "recurring_date": 31, "nickname": "Electric"}
    got = c.update_bill("bill-ae", body)
    assert got["_id"] == "bill-ae"
    assert got["payment_amount"] == 130.0
    assert s.requests[-1][0] == "PUT"
    assert s.bodies[-1] == body
