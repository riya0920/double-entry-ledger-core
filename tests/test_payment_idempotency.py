"""Idempotency of the PAYMENTS endpoints, not just of raw postings.

The distinction this file exists for: `/postings` was idempotent from the start
and `/payments/authorize` was not. It passed a request id to `ledger.post`,
which records the id on the journal row and creates no idempotency record, so a
client that timed out and retried placed a second hold on the card.

`run_api_load.py` is what surfaced it -- 3,200 authorizes over HTTP produced
3,200 journal transactions and zero idempotency keys. The repository's headline
guarantee was real and was being demonstrated on a path the payments API did not
take.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

import serve
from ledger import invariants
from ledger.payments import HOLD_ASSET


@pytest.fixture
def client(tmp_path):
    serve._state["db_path"] = str(tmp_path / "pay.db")
    with TestClient(serve.app) as c:
        yield c


def _auth(client, payment_id="p1", amount=10_000, key=None):
    headers = {"Idempotency-Key": key} if key else {}
    return client.post("/payments/authorize", headers=headers, json={
        "payment_id": payment_id, "merchant_id": "m1",
        "amount_minor": amount, "currency": "USD"})


def _txn_count(client):
    lg = serve._state["ledger"]
    return lg._conn().execute(
        "SELECT COUNT(*) c FROM journal_txn").fetchone()["c"]


def test_a_retried_authorize_replays_instead_of_holding_twice(client):
    """The failure this prevents is a double hold on a customer's card after a
    network timeout, which is the single most common payment API bug."""
    first = _auth(client, key="k-1")
    assert first.status_code == 201
    before, held = _txn_count(client), serve._state["ledger"].balance(HOLD_ASSET)

    second = _auth(client, key="k-1")
    assert second.status_code in (200, 201)
    assert second.json()["txn_id"] == first.json()["txn_id"]
    assert _txn_count(client) == before, "a retry created a second transaction"
    assert serve._state["ledger"].balance(HOLD_ASSET) == held


def test_every_authorize_records_an_idempotency_key(client):
    """The regression that started this: keys and payment transactions have to
    move together, or invariant I4 holds only on the paths nobody calls."""
    for i in range(5):
        assert _auth(client, payment_id="p{}".format(i)).status_code == 201
    lg = serve._state["ledger"]
    keys = lg._conn().execute(
        "SELECT COUNT(*) c FROM idempotency_key").fetchone()["c"]
    assert keys == 5


def test_the_payment_id_is_the_default_key(client):
    """No header still protects the caller. A default that quietly does not is
    worse than no default, because it reads as protection."""
    first = _auth(client, payment_id="p9")
    second = _auth(client, payment_id="p9")
    assert second.status_code in (200, 201)
    assert second.json()["txn_id"] == first.json()["txn_id"]


def test_same_key_different_amount_is_a_conflict_not_a_replay(client):
    """Returning the first caller's result would tell caller two that its
    DIFFERENT request succeeded."""
    assert _auth(client, payment_id="p2", amount=10_000, key="k-2").status_code == 201
    clash = _auth(client, payment_id="p2", amount=99_999, key="k-2")
    assert clash.status_code == 409


def test_a_duplicate_payment_id_under_a_new_key_is_still_rejected(client):
    """A fresh key must not launder a constraint violation into a replay. The
    integrity error came from the work, not from a concurrent duplicate, and
    conflating the two would let a second authorize through on the same
    payment."""
    assert _auth(client, payment_id="p3", key="k-3").status_code == 201
    assert _auth(client, payment_id="p3", key="k-4").status_code == 409


def test_the_book_still_balances_after_replays(client):
    for i in range(4):
        _auth(client, payment_id="q{}".format(i), key="qk{}".format(i))
        _auth(client, payment_id="q{}".format(i), key="qk{}".format(i))
    assert not invariants.check_all(serve._state["ledger"]._conn())


def test_the_library_call_without_a_key_is_unchanged(tmp_path):
    """Opting in is a parameter. The library-level tests exercise the plain
    path and must keep working, or this becomes a breaking change dressed up as
    a fix."""
    from ledger.core import Ledger
    from ledger.payments import authorize, bootstrap_accounts

    lg = Ledger(tmp_path / "lib.db")
    bootstrap_accounts(lg, "m1")
    txn = authorize(lg, "x1", "m1", 5_000, "USD", "r1")
    assert isinstance(txn, int)
    keys = lg._conn().execute(
        "SELECT COUNT(*) c FROM idempotency_key").fetchone()["c"]
    assert keys == 0
