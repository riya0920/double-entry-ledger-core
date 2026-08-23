"""Ledger HTTP API.

The reason this exists rather than leaving `Ledger.post()` as a library call:
idempotency is a PROTOCOL property, not a function property. The interesting
cases only appear once there is a wire between the caller and the ledger --
a retried POST, two concurrent POSTs with the same key, a key reused with a
different body. Those are the cases this service exposes and the tests drive.

The `Idempotency-Key` header follows the convention Stripe popularised and the
IETF draft (draft-ietf-httpapi-idempotency-key-header) describes:

  * The key is supplied by the CLIENT, because only the client knows which
    retries are the same logical request.
  * Replaying a key returns the ORIGINAL response, byte for byte, with the
    original status code.
  * Reusing a key with a different body is a 409 -- NOT the original response.
    Returning the first result would tell caller two that its different request
    succeeded, which is worse than an error.

Run:  uvicorn serve:app --port 8100
"""
from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from ledger import invariants
from ledger.core import (FloorBreach, IdempotencyConflict, Ledger, LedgerError,
                         Unbalanced, credit, debit)
from ledger.payments import (PaymentError, authorize, bootstrap_accounts, capture,
                             refund, void)

_state: dict = {}


@asynccontextmanager
async def lifespan(_app):
    path = _state.get("db_path", ":memory:")
    lg = Ledger(path)
    bootstrap_accounts(lg, "m1")
    _state["ledger"] = lg
    yield
    _state.pop("ledger", None)


app = FastAPI(title="Ledger API", version="0.3.0", lifespan=lifespan)


class EntryIn(BaseModel):
    account_id: str
    direction: str = Field(pattern="^[DC]$")
    # STRICT int. A float amount is rejected by the schema rather than
    # truncated, because silent truncation of money is how a cent goes missing
    # in a place nobody thinks to look.
    amount_minor: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)


class PostingIn(BaseModel):
    entries: list[EntryIn] = Field(min_length=2)
    actor: str
    reason: str
    request_id: str


class AuthorizeIn(BaseModel):
    payment_id: str
    merchant_id: str = "m1"
    amount_minor: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)


class CaptureIn(BaseModel):
    amount_minor: int = Field(gt=0)
    fee_minor: int = Field(ge=0, default=0)


class RefundIn(BaseModel):
    amount_minor: int = Field(gt=0)
    refund_fee: bool = False


def _ledger() -> Ledger:
    lg = _state.get("ledger")
    if lg is None:
        raise HTTPException(503, "ledger not initialised")
    return lg


@app.get("/health")
def health() -> dict:
    lg = _ledger()
    violations = invariants.check_all(lg._conn())
    return {
        "status": "ok" if not violations else "INVARIANT_VIOLATION",
        "violations": [{"invariant": v.invariant, "detail": v.detail}
                       for v in violations],
    }


@app.get("/accounts/{account_id}/balance")
def balance(account_id: str, as_of: str | None = None) -> dict:
    lg = _ledger()
    if as_of:
        return {"account_id": account_id, "as_of": as_of,
                "balance_minor": lg.balance_as_of(account_id, as_of),
                "source": "reconstructed from the journal alone"}
    return {"account_id": account_id, "balance_minor": lg.balance(account_id),
            "source": "materialised cache (invariant I3 proves it matches)"}


@app.post("/postings", status_code=201)
def create_posting(response: Response, body: PostingIn = Body(...),
                   idempotency_key: str | None = Header(None)) -> dict:
    """Post one balanced transaction.

    The Idempotency-Key header is REQUIRED. A mutating money-movement endpoint
    without one cannot be safely retried, and every client eventually retries.
    """
    lg = _ledger()
    if not idempotency_key:
        raise HTTPException(
            400, "Idempotency-Key header is required on mutating endpoints: a "
                 "money-movement request that cannot be safely retried is a "
                 "request that will eventually be applied twice")

    entries = [(debit if e.direction == "D" else credit)(
        e.account_id, e.amount_minor, e.currency) for e in body.entries]
    payload = body.model_dump()

    try:
        result, status, replayed = lg.post_idempotent(
            key=idempotency_key, payload=payload, entries=entries,
            actor=body.actor, reason=body.reason, request_id=body.request_id)
    except IdempotencyConflict as exc:
        raise HTTPException(409, str(exc))
    except Unbalanced as exc:
        raise HTTPException(422, str(exc))
    except FloorBreach as exc:
        raise HTTPException(409, str(exc))
    except LedgerError as exc:
        raise HTTPException(400, str(exc))

    response.status_code = status
    # Tell the caller whether this was a fresh effect or a replay. Without it a
    # client cannot distinguish "my retry worked" from "my retry was ignored",
    # and those need different handling in a reconciliation.
    response.headers["Idempotent-Replay"] = "true" if replayed else "false"
    return {**result, "replayed": replayed}


@app.post("/payments/authorize", status_code=201)
def api_authorize(body: AuthorizeIn,
                  idempotency_key: str | None = Header(
                      default=None, alias="Idempotency-Key")) -> dict:
    """Authorize a payment. Send `Idempotency-Key` and a retry replays.

    This endpoint used to pass a request id straight through to `ledger.post`,
    which records the id on the journal row and creates NO idempotency record.
    `run_api_load.py` made that visible: 3,200 authorizes over HTTP produced
    3,200 journal transactions and **0 idempotency keys**, so a client that
    timed out and retried placed a second hold on the card. The repository's
    idempotency guarantee was real and was being demonstrated on a code path
    the payments API did not take.

    The key defaults to the payment id when the header is absent, because for
    an authorize the payment id already IS the natural idempotency key -- and a
    default that protects the caller beats a default that quietly does not.
    """
    lg = _ledger()
    key = idempotency_key or "auth:" + body.payment_id
    try:
        txn = authorize(lg, body.payment_id, body.merchant_id,
                        body.amount_minor, body.currency,
                        "req-" + body.payment_id, idempotency_key=key)
    except PaymentError as exc:
        raise HTTPException(422, str(exc))
    except IdempotencyConflict as exc:
        # Same key, DIFFERENT payload. Returning the first caller's result here
        # would tell caller two that its different request succeeded.
        raise HTTPException(409, str(exc))
    except Exception as exc:
        raise HTTPException(409, str(exc))
    return {"payment_id": body.payment_id, "txn_id": txn, "state": "authorized"}


@app.post("/payments/{payment_id}/capture")
def api_capture(payment_id: str, body: CaptureIn) -> dict:
    lg = _ledger()
    try:
        txn = capture(lg, payment_id, body.amount_minor, body.fee_minor,
                      "req-cap-" + payment_id)
    except PaymentError as exc:
        raise HTTPException(422, str(exc))
    return {"payment_id": payment_id, "txn_id": txn, **_payment_state(lg, payment_id)}


@app.post("/payments/{payment_id}/refund")
def api_refund(payment_id: str, body: RefundIn) -> dict:
    lg = _ledger()
    try:
        txn = refund(lg, payment_id, body.amount_minor, "req-ref-" + payment_id,
                     refund_fee=body.refund_fee)
    except PaymentError as exc:
        raise HTTPException(422, str(exc))
    return {"payment_id": payment_id, "txn_id": txn, **_payment_state(lg, payment_id)}


@app.post("/payments/{payment_id}/void")
def api_void(payment_id: str) -> dict:
    lg = _ledger()
    try:
        txn = void(lg, payment_id, "req-void-" + payment_id)
    except PaymentError as exc:
        raise HTTPException(422, str(exc))
    return {"payment_id": payment_id, "txn_id": txn, **_payment_state(lg, payment_id)}


@app.get("/payments/{payment_id}")
def api_payment(payment_id: str) -> dict:
    lg = _ledger()
    state = _payment_state(lg, payment_id)
    if not state:
        raise HTTPException(404, "unknown payment")
    return {"payment_id": payment_id, **state}


def _payment_state(lg: Ledger, payment_id: str) -> dict:
    row = lg._conn().execute(
        "SELECT * FROM payment WHERE id = ?", (payment_id,)).fetchone()
    if row is None:
        return {}
    return {"state": row["state"], "authorized_minor": row["authorized_minor"],
            "captured_minor": row["captured_minor"],
            "refunded_minor": row["refunded_minor"]}


@app.get("/invariants")
def check_invariants() -> dict:
    """The four invariants, exposed as an endpoint.

    This is a health check with teeth: it re-derives every balance from the
    journal and compares against the materialised cache. A monitoring system can
    poll it, which turns "the ledger is consistent" from an assertion into a
    continuously verified fact.
    """
    lg = _ledger()
    v = invariants.check_all(lg._conn())
    return {"ok": not v,
            "violations": [{"invariant": x.invariant, "detail": x.detail} for x in v]}
