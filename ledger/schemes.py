"""Scheme-specific adjustment rules, and why they belong outside the mechanism.

`ledger/adjustments.py` implements the MECHANISM -- an authorization can change
size while it is open, an increment holds more, a decrement releases some, and a
decrement may never fall below what has been captured. That rule is universal
and belongs in the ledger.

Everything below is NOT universal. Whether a hotel may increment a stay, by how
much, how many times, and for how long the original authorization survives are
rules a CARD SCHEME sets, and they differ between schemes and between merchant
category codes. The README listed this as open: "the mechanism is there; the
card [scheme rules are not]".

WHY THIS IS A SEPARATE MODULE RATHER THAN BRANCHES INSIDE `adjust`. A limit
inside the ledger is a limit the ledger cannot be operated without. Schemes
publish rule changes on their own timetable, an acquirer runs several at once,
and a merchant in a monitoring programme gets different limits from one that is
not. Putting any of that inside `adjust` makes the ledger's correctness depend
on a table that changes quarterly.

So: `adjust` enforces what is true of double-entry. This enforces what is true
of Visa in 2026, and says which is which.

THE NUMBERS BELOW ARE PLAUSIBLE STAND-INS, NOT THE RULEBOOKS. Real limits come
from scheme documentation that is licensed, versioned and not public, and the
values differ by region and MCC. They are named `ASSUMED_` for the same reason
SE-2's win rates are: a number quoted from memory and presented as a rule is the
kind of thing this project exists not to do.

WHAT IS REAL HERE regardless of the numbers: the SHAPE of the constraints. Every
scheme limits incremental authorizations by count, by cumulative size relative
to the original, and by age; every scheme treats some MCCs as
incremental-eligible and others as not; and every scheme's expiry clock runs
from the ORIGINAL authorization, which is the rule an implementation is most
likely to get wrong.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal

from .payments import PaymentError


class SchemeViolation(PaymentError):
    """The ledger would accept this; the scheme will not.

    A subclass of PaymentError so a caller that already handles payment errors
    does not silently miss it -- but a distinct type, because "your books would
    be wrong" and "the network will decline this" need different handling and
    different messages to the merchant.
    """


# MCCs that are incremental-eligible in every scheme, because the amount is
# genuinely unknown at authorization time. This list is the concept, not the
# rulebook.
INCREMENTAL_MCC = {
    "3501": "hotel",
    "7011": "lodging",
    "5812": "restaurant",
    "5813": "bar",
    "7512": "vehicle rental",
    "5542": "automated fuel dispenser",
}


@dataclass
class SchemeRules:
    """One scheme's adjustment limits.

    `max_cumulative_ratio` is expressed against the ORIGINAL authorization
    rather than the running total, because "may increase by 15%" compounds
    dangerously if read the other way: three 15% increments on a running total
    is 52%, not 45%.
    """
    name: str
    max_increments: int
    max_cumulative_ratio: Decimal
    authorization_valid_days: int
    incremental_mcc_only: bool = True
    allows_decrement: bool = True


# ASSUMED, not quoted. See the module docstring.
ASSUMED_RULES = {
    "visa": SchemeRules("visa", max_increments=5,
                        max_cumulative_ratio=Decimal("1.15"),
                        authorization_valid_days=30),
    "mastercard": SchemeRules("mastercard", max_increments=4,
                              max_cumulative_ratio=Decimal("1.20"),
                              authorization_valid_days=30),
    "amex": SchemeRules("amex", max_increments=3,
                        max_cumulative_ratio=Decimal("1.10"),
                        authorization_valid_days=7),
    # Domestic debit schemes commonly do not support incremental authorization
    # at all, which is a different answer from "a small limit" and has to be
    # representable.
    "interac": SchemeRules("interac", max_increments=0,
                           max_cumulative_ratio=Decimal("1.00"),
                           authorization_valid_days=1,
                           allows_decrement=False),
}


@dataclass
class AuthorizationContext:
    """What the scheme check needs to know that the ledger does not track."""
    scheme: str
    mcc: str
    original_minor: int
    current_minor: int
    increments_so_far: int
    authorized_on: str                 # ISO date of the ORIGINAL authorization
    captured_minor: int = 0


def _days_since(authorized_on: str, as_of: str) -> int:
    return (date.fromisoformat(as_of) - date.fromisoformat(authorized_on)).days


def check_adjustment(ctx: AuthorizationContext, delta_minor: int, as_of: str,
                     rules: dict | None = None) -> dict:
    """May the scheme accept this adjustment? Raises SchemeViolation if not.

    Returns the headroom remaining, because a caller that has been refused wants
    to know by how much -- "declined" with no number sends a hotel back to try
    again at a smaller amount by guessing.
    """
    table = rules or ASSUMED_RULES
    scheme = ctx.scheme.lower()
    if scheme not in table:
        raise SchemeViolation(
            "unknown scheme {!r}. NOT falling back to a permissive default: an "
            "unknown scheme's limits are unknown, and guessing them upward is "
            "how an authorization gets declined at capture time when the money "
            "is already spent.".format(ctx.scheme))
    r = table[scheme]

    # ---- the expiry clock runs from the ORIGINAL authorization ----------
    #
    # The rule an implementation is most likely to get wrong. Restarting it on
    # each increment is exactly the bug `adjustments.py` was written to avoid by
    # not re-authorizing -- and re-introducing it here would undo that.
    age = _days_since(ctx.authorized_on, as_of)
    if age > r.authorization_valid_days:
        raise SchemeViolation(
            "the original authorization is {} days old and {} allows {}. The "
            "clock runs from the ORIGINAL authorization, not from the last "
            "increment -- otherwise a hold could be extended indefinitely by "
            "incrementing it.".format(age, r.name, r.authorization_valid_days))

    if delta_minor == 0:
        raise SchemeViolation("an adjustment of zero is not an adjustment")

    # ---- decrement -------------------------------------------------------
    if delta_minor < 0:
        if not r.allows_decrement:
            raise SchemeViolation(
                "{} does not support decremental authorization. The remedy is "
                "to void and re-authorize, accepting that the expiry clock "
                "restarts -- which is a business decision, not a fallback this "
                "should make silently.".format(r.name))
        # The ledger's own rule (never below captured) stays in `adjust`. This
        # deliberately does not re-check it: two implementations of one
        # invariant drift, and the ledger's is the one that must hold.
        return {"allowed": True, "scheme": r.name,
                "increments_remaining": r.max_increments - ctx.increments_so_far,
                "headroom_minor": 0}

    # ---- increment -------------------------------------------------------
    if r.max_increments == 0:
        raise SchemeViolation(
            "{} does not support incremental authorization at all. That is a "
            "different answer from a small limit, and treating it as one would "
            "produce an authorization the network never accepted.".format(r.name))

    if ctx.increments_so_far >= r.max_increments:
        raise SchemeViolation(
            "{} allows {} increments and this authorization has had {}".format(
                r.name, r.max_increments, ctx.increments_so_far))

    if r.incremental_mcc_only and ctx.mcc not in INCREMENTAL_MCC:
        raise SchemeViolation(
            "MCC {} is not incremental-eligible. Incremental authorization "
            "exists for merchants whose final amount is genuinely unknown at "
            "authorization time -- a retailer that knows the total is expected "
            "to authorize it.".format(ctx.mcc))

    ceiling = int(Decimal(ctx.original_minor) * r.max_cumulative_ratio)
    proposed = ctx.current_minor + delta_minor
    if proposed > ceiling:
        raise SchemeViolation(
            "{} would take the authorization to {} against a {} ceiling of {} "
            "({}x the ORIGINAL {}). The ratio is against the original and not "
            "the running total -- three 15% increments on a running total is "
            "52%, not 45%.".format(
                delta_minor, proposed, r.name, ceiling,
                r.max_cumulative_ratio, ctx.original_minor))

    return {"allowed": True, "scheme": r.name,
            "increments_remaining": r.max_increments - ctx.increments_so_far - 1,
            "headroom_minor": ceiling - proposed}


def headroom(ctx: AuthorizationContext, as_of: str,
             rules: dict | None = None) -> dict:
    """How much more could be held, without attempting it.

    A merchant should be able to ask before trying, rather than discovering the
    limit by being declined in front of a customer.
    """
    table = rules or ASSUMED_RULES
    r = table.get(ctx.scheme.lower())
    if r is None:
        return {"known": False, "reason": "unknown scheme"}
    ceiling = int(Decimal(ctx.original_minor) * r.max_cumulative_ratio)
    return {
        "known": True,
        "scheme": r.name,
        "ceiling_minor": ceiling,
        "headroom_minor": max(0, ceiling - ctx.current_minor),
        "increments_remaining": max(0, r.max_increments - ctx.increments_so_far),
        "days_remaining": max(
            0, r.authorization_valid_days - _days_since(ctx.authorized_on, as_of)),
        "mcc_eligible": (not r.incremental_mcc_only
                         or ctx.mcc in INCREMENTAL_MCC),
    }
