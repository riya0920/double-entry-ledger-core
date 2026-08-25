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
    # Every mutating payment endpoint now REQUIRES Idempotency-Key. It was
    # required on /postings and not on capture/refund/void, which is backwards:
    # an authorize holds funds and a capture MOVES them.
    client.post("/payments/authorize", json={
        "payment_id": "p1", "amount_minor": 10_000, "currency": "USD"},
        headers={"Idempotency-Key": "p1-auth"})
    client.post("/payments/p1/capture", json={"amount_minor": 4_000,
                                              "fee_minor": 120},
                headers={"Idempotency-Key": "p1-cap"})
    state = client.get("/payments/p1").json()
    assert state["state"] == "partially_captured"
    assert state["captured_minor"] == 4_000

    client.post("/payments/p1/refund", json={"amount_minor": 4_000},
                headers={"Idempotency-Key": "p1-ref"})
    assert client.get("/payments/p1").json()["state"] == "refunded"
    assert client.get("/invariants").json()["ok"] is True


def test_illegal_transition_over_http_is_422(client):
    client.post("/payments/authorize", json={
        "payment_id": "p2", "amount_minor": 5_000, "currency": "USD"},
        headers={"Idempotency-Key": "p2-auth"})
    r = client.post("/payments/p2/refund", json={"amount_minor": 100},
                    headers={"Idempotency-Key": "p2-ref"})
    assert r.status_code == 422
    assert "illegal transition" in r.json()["detail"]


# ------------------------------- the header, on the endpoints that move money
def test_capture_without_an_idempotency_key_is_refused(client):
    """It was required on /postings and OPTIONAL on the endpoints that actually
    move money -- the most dangerous operations were the only unprotected
    ones."""
    client.post("/payments/authorize", json={
        "payment_id": "p3", "amount_minor": 10_000, "currency": "USD"},
        headers={"Idempotency-Key": "p3-auth"})
    r = client.post("/payments/p3/capture", json={"amount_minor": 1_000,
                                                  "fee_minor": 0})
    assert r.status_code == 400
    assert "more dangerous" in r.json()["detail"]


def test_a_retried_capture_does_not_move_the_money_twice(client):
    """The failure the header exists to prevent, and the one that hides: partial
    capture is legal, so the second call usually SUCCEEDS -- there is remaining
    authorization for it to consume, and nothing about it looks wrong."""
    client.post("/payments/authorize", json={
        "payment_id": "p4", "amount_minor": 10_000, "currency": "USD"},
        headers={"Idempotency-Key": "p4-auth"})
    h = {"Idempotency-Key": "p4-cap"}
    first = client.post("/payments/p4/capture",
                        json={"amount_minor": 4_000, "fee_minor": 100},
                        headers=h)
    retry = client.post("/payments/p4/capture",
                        json={"amount_minor": 4_000, "fee_minor": 100},
                        headers=h)

    assert first.headers["Idempotent-Replay"] == "false"
    assert retry.headers["Idempotent-Replay"] == "true"
    assert first.json()["txn_id"] == retry.json()["txn_id"]
    assert client.get("/payments/p4").json()["captured_minor"] == 4_000


def test_reusing_a_key_for_a_different_amount_is_409(client):
    """A bare key says "you have seen this request"; without the payload it
    cannot say "you have seen THIS request". Returning the first result would
    tell the caller their second capture succeeded when it never ran."""
    client.post("/payments/authorize", json={
        "payment_id": "p5", "amount_minor": 10_000, "currency": "USD"},
        headers={"Idempotency-Key": "p5-auth"})
    h = {"Idempotency-Key": "p5-cap"}
    client.post("/payments/p5/capture", json={"amount_minor": 4_000,
                                              "fee_minor": 0}, headers=h)
    r = client.post("/payments/p5/capture", json={"amount_minor": 9_000,
                                                  "fee_minor": 0}, headers=h)
    assert r.status_code == 409


def test_refund_and_void_require_the_header_too(client):
    client.post("/payments/authorize", json={
        "payment_id": "p6", "amount_minor": 10_000, "currency": "USD"},
        headers={"Idempotency-Key": "p6-auth"})
    assert client.post("/payments/p6/refund",
                       json={"amount_minor": 100}).status_code == 400
    assert client.post("/payments/p6/void").status_code == 400


def test_a_retried_void_replays_rather_than_returning_422(client):
    """A retried void is the mildest of the three -- the state machine rejects
    the second because the payment is already `voided`. But "the guard happens
    to reject it" is a different guarantee from "this is idempotent", and the
    caller gets a 422 for a retry that in fact succeeded."""
    client.post("/payments/authorize", json={
        "payment_id": "p7", "amount_minor": 10_000, "currency": "USD"},
        headers={"Idempotency-Key": "p7-auth"})
    h = {"Idempotency-Key": "p7-void"}
    first = client.post("/payments/p7/void", headers=h)
    retry = client.post("/payments/p7/void", headers=h)

    assert first.status_code == 200
    assert retry.status_code == 200, "a retried void returned an error"
    assert first.json()["txn_id"] == retry.json()["txn_id"]


# ------------------------------------- the connection bug the header exposed
def test_every_thread_sees_the_same_in_memory_database():
    """A REAL BUG, and a total one under any threaded server.

    `Ledger` holds a thread-local connection. SQLite's ":memory:" names a
    database PRIVATE TO THE CONNECTION, so thread A ran the schema and thread B
    opened a fresh blank database with the same name. The constructor's schema
    write landed in whichever thread built the Ledger and was invisible to every
    request thread after it.

    In-process tests never saw it because they construct and use the Ledger on
    one thread. It surfaced the moment a TestClient request touched the
    idempotency table: `no such table: idempotency_key`, on a schema that
    plainly creates it.
    """
    import threading

    from ledger.core import Ledger

    lg = Ledger(":memory:")
    seen = {}

    def look():
        seen["tables"] = {
            r[0] for r in lg._conn().execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    t = threading.Thread(target=look)
    t.start()
    t.join()

    assert "idempotency_key" in seen["tables"], (
        "a second thread saw a different (empty) database")
    assert "journal_txn" in seen["tables"]


def test_a_write_on_one_thread_is_visible_on_another():
    """The consequence that matters: not just the schema, the DATA."""
    import threading

    from ledger.core import Ledger
    from ledger.payments import bootstrap_accounts

    lg = Ledger(":memory:")
    # Uses the module's own account setup rather than inventing account ids --
    # a foreign-key failure here would make the test fail for a reason that has
    # nothing to do with cross-thread visibility.
    bootstrap_accounts(lg, "m1")

    seen = {}

    def look():
        seen["n"] = lg._conn().execute(
            "SELECT COUNT(*) FROM account").fetchone()[0]

    t = threading.Thread(target=look)
    t.start()
    t.join()
    assert seen["n"] >= 1, "a write on one thread was invisible on another"


def test_a_file_backed_ledger_is_unaffected_by_the_fix():
    """The shared-cache URI applies only to ":memory:". A path-backed ledger
    already shared one database across threads, and must keep doing so."""
    import tempfile
    from pathlib import Path

    from ledger.core import Ledger

    d = tempfile.mkdtemp()
    lg = Ledger(Path(d) / "l.db")
    assert lg._memory_uri is None
    tables = {r[0] for r in lg._conn().execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "idempotency_key" in tables
    # Deliberately NOT cleaning the temp dir: sqlite holds the file open on
    # Windows and rmtree then fails, which would make this test fail for a
    # reason that has nothing to do with what it checks.
