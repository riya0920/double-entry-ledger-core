"""HTTP API tests -- idempotency as a protocol property, not a function property."""
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

import serve


@pytest.fixture
def client(tmp_path):
    serve._state["db_path"] = str(tmp_path / "api.db")
    with TestClient(serve.app) as c:
        lg = serve._state["ledger"]
        lg.open_account("cash", "asset", "USD", floor_minor=-10**12,
                        overdraft_allowed=True)
        lg.open_account("revenue", "revenue", "USD", floor_minor=-10**12,
                        overdraft_allowed=True)
        yield c


def _posting(amount=1000, actor="test", reason="sale", request_id="r1"):
    return {"entries": [
        {"account_id": "cash", "direction": "D", "amount_minor": amount,
         "currency": "USD"},
        {"account_id": "revenue", "direction": "C", "amount_minor": amount,
         "currency": "USD"}],
        "actor": actor, "reason": reason, "request_id": request_id}


def test_posting_requires_an_idempotency_key(client):
    """A money-movement endpoint that cannot be safely retried will eventually
    be applied twice."""
    r = client.post("/postings", json=_posting())
    assert r.status_code == 400
    assert "Idempotency-Key" in r.json()["detail"]


def test_posting_moves_money_and_reports_not_replayed(client):
    r = client.post("/postings", json=_posting(),
                    headers={"Idempotency-Key": "k1"})
    assert r.status_code == 201
    assert r.json()["replayed"] is False
    assert r.headers["Idempotent-Replay"] == "false"
    assert client.get("/accounts/cash/balance").json()["balance_minor"] == 1000


def test_retry_returns_the_original_response_and_no_second_effect(client):
    first = client.post("/postings", json=_posting(),
                        headers={"Idempotency-Key": "k2"})
    second = client.post("/postings", json=_posting(),
                         headers={"Idempotency-Key": "k2"})
    assert first.json()["txn_id"] == second.json()["txn_id"]
    assert second.json()["replayed"] is True
    assert second.headers["Idempotent-Replay"] == "true"
    assert client.get("/accounts/cash/balance").json()["balance_minor"] == 1000


def test_same_key_different_body_is_409_not_a_replay(client):
    """Returning the first result would tell caller two that its DIFFERENT
    request succeeded, which is worse than an error."""
    client.post("/postings", json=_posting(amount=1000),
                headers={"Idempotency-Key": "k3"})
    r = client.post("/postings", json=_posting(amount=9999),
                    headers={"Idempotency-Key": "k3"})
    assert r.status_code == 409
    assert client.get("/accounts/cash/balance").json()["balance_minor"] == 1000


def test_concurrent_duplicates_produce_exactly_one_effect(client):
    """Two in-flight requests with the same key: one effect, both callers get
    the winner's result."""
    results = []
    barrier = threading.Barrier(8)

    def send():
        barrier.wait()
        r = client.post("/postings", json=_posting(amount=500),
                        headers={"Idempotency-Key": "concurrent"})
        results.append(r.json())

    threads = [threading.Thread(target=send) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    txn_ids = {r["txn_id"] for r in results if "txn_id" in r}
    assert len(txn_ids) == 1, "more than one transaction was created"
    assert client.get("/accounts/cash/balance").json()["balance_minor"] == 500


def test_unbalanced_posting_is_422(client):
    body = _posting()
    body["entries"][1]["amount_minor"] = 999
    r = client.post("/postings", json=body, headers={"Idempotency-Key": "k4"})
    assert r.status_code == 422


def test_float_amount_is_rejected_at_the_schema(client):
    body = _posting()
    body["entries"][0]["amount_minor"] = 10.5
    r = client.post("/postings", json=body, headers={"Idempotency-Key": "k5"})
    assert r.status_code == 422


def test_as_of_balance_is_reconstructed_from_the_journal(client):
    client.post("/postings", json=_posting(amount=100),
                headers={"Idempotency-Key": "a"})
    lg = serve._state["ledger"]
    cutoff = lg._conn().execute(
        "SELECT created_at FROM journal_txn ORDER BY id LIMIT 1").fetchone()[0]
    client.post("/postings", json=_posting(amount=700),
                headers={"Idempotency-Key": "b"})

    assert client.get("/accounts/cash/balance").json()["balance_minor"] == 800
    r = client.get("/accounts/cash/balance", params={"as_of": cutoff}).json()
    assert r["balance_minor"] == 100
    assert "journal" in r["source"]


def test_invariants_endpoint_is_a_health_check_with_teeth(client):
    client.post("/postings", json=_posting(), headers={"Idempotency-Key": "inv"})
    r = client.get("/invariants").json()
    assert r["ok"] is True and r["violations"] == []


def test_payment_lifecycle_over_http(client):
    client.post("/payments/authorize", json={
        "payment_id": "p1", "amount_minor": 10_000, "currency": "USD"})
    client.post("/payments/p1/capture", json={"amount_minor": 4_000,
                                              "fee_minor": 120})
    state = client.get("/payments/p1").json()
    assert state["state"] == "partially_captured"
    assert state["captured_minor"] == 4_000

    client.post("/payments/p1/refund", json={"amount_minor": 4_000})
    assert client.get("/payments/p1").json()["state"] == "refunded"
    assert client.get("/invariants").json()["ok"] is True


def test_illegal_transition_over_http_is_422(client):
    client.post("/payments/authorize", json={
        "payment_id": "p2", "amount_minor": 5_000, "currency": "USD"})
    r = client.post("/payments/p2/refund", json={"amount_minor": 100})
    assert r.status_code == 422
    assert "illegal transition" in r.json()["detail"]
