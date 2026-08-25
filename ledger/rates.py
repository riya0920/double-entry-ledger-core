"""Where a rate comes from, and why that is a control rather than a lookup.

`RateSet` takes closing and average rates as constructor arguments. Somebody has
to supply them, and the README listed that as open. The interesting part is not
fetching a number -- it is that a consolidation is only as defensible as the
provenance of the rate that produced it.

FIVE PROPERTIES A RATE SOURCE NEEDS, and each is a way a consolidation goes
wrong without it:

  DATED. A rate is a fact about an instant. "The USD/EUR rate" is not a value;
  "the USD/EUR closing rate on 2026-03-31" is. A store keyed only by currency
  silently reuses yesterday's rate for today's close, and the error is a
  plausible-looking CTA rather than a crash.

  IMMUTABLE ONCE PUBLISHED. A rate used in a filed consolidation cannot be
  edited afterwards. If the provider restates, that is a NEW rate with a new
  effective date and the old one stays -- otherwise last quarter's report
  becomes unreproducible and nobody can say when it changed.

  SOURCED. ECB, the provider, a manual override. A manual override is
  legitimate and it is the one an auditor asks about, so it is recorded as such
  rather than looking identical to a fetched rate.

  DECIMAL, NEVER FLOAT. Same bug as a float amount: 0.1 + 0.2 in the middle of a
  consolidation produces a CTA that is pure representation error and looks
  exactly like a real one. `RateSet` says so; this enforces it at the boundary,
  which is where a float actually gets in.

  IT FAILS. A missing rate must raise. The alternative -- defaulting to 1.0, or
  carrying the last known rate forward silently -- produces a consolidation that
  balances and is wrong, and a consolidation that balances is one nobody checks.

WHAT THIS DELIBERATELY DOES NOT DO: fetch from the internet at import time, or
at all by default. A reporting run whose numbers depend on whether a web request
succeeded is not reproducible, which is the property a consolidation most needs.
`fetch_ecb` exists, is explicit, and writes into the store -- the store is the
source of truth, not the network.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .reporting import RateSet

SCHEMA = """
CREATE TABLE IF NOT EXISTS fx_rate (
    quote_date   TEXT NOT NULL,
    base         TEXT NOT NULL,      -- the currency being converted FROM
    quote        TEXT NOT NULL,      -- the reporting currency
    kind         TEXT NOT NULL,      -- closing | average
    rate         TEXT NOT NULL,      -- Decimal as a string. Never a float.
    source       TEXT NOT NULL,      -- ecb | provider | manual
    recorded_at  TEXT NOT NULL,
    note         TEXT,
    PRIMARY KEY (quote_date, base, quote, kind)
);
"""


class RateError(Exception):
    pass


class RateNotFound(RateError):
    """No rate for this currency on this date. Deliberately not a default."""


class RateImmutable(RateError):
    """A published rate may not be edited in place."""


VALID_KINDS = ("closing", "average")
VALID_SOURCES = ("ecb", "provider", "manual")


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Rate:
    quote_date: str
    base: str
    quote: str
    kind: str
    rate: Decimal
    source: str
    note: str = ""


class RateStore:
    def __init__(self, path: str | Path = ":memory:"):
        self.con = sqlite3.connect(str(path))
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)

    # ------------------------------------------------------------ writing
    def publish(self, quote_date: str, base: str, quote: str, kind: str,
                rate, source: str, note: str = "") -> Rate:
        """Record a rate. Refuses to overwrite one that already exists.

        The refusal is the control. A rate used in a filed consolidation cannot
        be edited afterwards -- if the provider restates, that is a NEW rate
        with a new effective date, and the old one stays so last quarter's
        report still reproduces.
        """
        if kind not in VALID_KINDS:
            raise RateError("kind must be one of {}".format(VALID_KINDS))
        if source not in VALID_SOURCES:
            raise RateError("source must be one of {}".format(VALID_SOURCES))

        # Decimal at the boundary. Accepting a float here and converting is how
        # a float gets in: Decimal(0.1) is 0.1000000000000000055511151231257827,
        # so the conversion preserves the error rather than removing it.
        if isinstance(rate, float):
            raise RateError(
                "rate must be a Decimal or a string, never a float. "
                "Decimal(0.1) preserves the binary error rather than removing "
                "it, and a float rate produces a CTA that is representation "
                "error wearing the shape of a real one.")
        try:
            value = Decimal(str(rate))
        except InvalidOperation as exc:
            raise RateError("unparseable rate {!r}".format(rate)) from exc
        if value <= 0:
            raise RateError("a rate must be positive, got {}".format(value))

        existing = self.con.execute(
            "SELECT rate, source FROM fx_rate WHERE quote_date=? AND base=?"
            " AND quote=? AND kind=?",
            (quote_date, base, quote, kind)).fetchone()
        if existing is not None:
            if Decimal(existing["rate"]) == value:
                return self.get(quote_date, base, quote, kind)
            raise RateImmutable(
                "{} {}/{} {} on {} is already published as {} (source {}). A "
                "published rate is not editable: a filed consolidation used it, "
                "and changing it in place makes that report unreproducible with "
                "no record of when it moved.".format(
                    kind, base, quote, kind, quote_date, existing["rate"],
                    existing["source"]))

        self.con.execute(
            "INSERT INTO fx_rate (quote_date, base, quote, kind, rate, source,"
            " recorded_at, note) VALUES (?,?,?,?,?,?,?,?)",
            (quote_date, base, quote, kind, str(value), source, _now(), note))
        self.con.commit()
        return Rate(quote_date, base, quote, kind, value, source, note)

    # ------------------------------------------------------------ reading
    def get(self, quote_date: str, base: str, quote: str, kind: str) -> Rate:
        row = self.con.execute(
            "SELECT * FROM fx_rate WHERE quote_date=? AND base=? AND quote=?"
            " AND kind=?", (quote_date, base, quote, kind)).fetchone()
        if row is None:
            raise RateNotFound(
                "no {} rate for {}/{} on {}. NOT defaulting to 1.0 and NOT "
                "carrying the last known rate forward: either would produce a "
                "consolidation that balances and is wrong, and one that "
                "balances is one nobody checks.".format(
                    kind, base, quote, quote_date))
        return Rate(row["quote_date"], row["base"], row["quote"], row["kind"],
                    Decimal(row["rate"]), row["source"], row["note"] or "")

    def rate_set(self, quote_date: str, reporting_currency: str,
                 currencies) -> RateSet:
        """Build the `RateSet` a consolidation consumes, for ONE date.

        Every currency must have both a closing and an average rate for that
        date. A partial set is refused rather than filled in, because the
        missing half is exactly where a silent default would hide.
        """
        closing, average = {}, {}
        missing = []
        for ccy in currencies:
            if ccy == reporting_currency:
                continue
            for kind, table in (("closing", closing), ("average", average)):
                try:
                    table[ccy] = self.get(quote_date, ccy, reporting_currency,
                                          kind).rate
                except RateNotFound:
                    missing.append("{} {}".format(kind, ccy))
        if missing:
            raise RateNotFound(
                "incomplete rate set for {} on {}: missing {}".format(
                    reporting_currency, quote_date, ", ".join(sorted(missing))))
        return RateSet(reporting_currency, closing, average)

    def history(self, base: str, quote: str, kind: str) -> list:
        rows = self.con.execute(
            "SELECT * FROM fx_rate WHERE base=? AND quote=? AND kind=?"
            " ORDER BY quote_date", (base, quote, kind)).fetchall()
        return [Rate(r["quote_date"], r["base"], r["quote"], r["kind"],
                     Decimal(r["rate"]), r["source"], r["note"] or "")
                for r in rows]

    def sources_used(self, quote_date: str) -> dict:
        """Which source produced each rate on this date.

        The question an auditor asks first, because a manual override is
        legitimate and is the one worth explaining.
        """
        rows = self.con.execute(
            "SELECT source, COUNT(*) n FROM fx_rate WHERE quote_date=?"
            " GROUP BY source", (quote_date,)).fetchall()
        return {r["source"]: r["n"] for r in rows}


def fetch_ecb(store: RateStore, quote_date: str, reporting_currency: str = "EUR",
              currencies=("USD", "GBP"), timeout: float = 10.0) -> dict:
    """Populate the store from the ECB's public reference rates.

    EXPLICIT, and never called at import or by a reporting run. A consolidation
    whose numbers depend on whether a web request succeeded is not reproducible,
    which is the property it most needs -- so the STORE is the source of truth
    and this is one way to fill it.

    The ECB publishes rates as EUR per unit, so a USD->EUR conversion is the
    reciprocal. That inversion is the kind of detail that silently doubles or
    halves a consolidation, so it is done in one place and tested.
    """
    import ssl
    from urllib import request
    from xml.etree import ElementTree

    # An explicit trust store. Python on Windows does not use the OS one, so the
    # default context fails ECB's certificate with CERTIFICATE_VERIFY_FAILED --
    # which reads like the site being down and is a local trust problem.
    #
    # NOT solved by disabling verification. A rate that arrives over an
    # unverified connection is a rate an attacker can choose, and it lands in a
    # consolidation that balances.
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()

    url = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
    with request.urlopen(url, timeout=timeout, context=ctx) as r:
        tree = ElementTree.fromstring(r.read())

    ns = {"gesmes": "http://www.gesmes.org/xml/2002-08-01",
          "ecb": "http://www.ecb.int/vocabulary/2002-08-01/eurofxref"}
    published = {}
    for cube in tree.iter():
        if cube.get("currency") and cube.get("rate"):
            published[cube.get("currency")] = Decimal(cube.get("rate"))

    written = {}
    for ccy in currencies:
        if ccy not in published:
            continue
        # ECB quotes EUR->ccy. We need ccy->EUR, so invert.
        per_eur = published[ccy]
        into_eur = Decimal(1) / per_eur
        for kind in VALID_KINDS:
            # The same rate for both kinds is a STAND-IN, and it is recorded in
            # the note rather than passed off as an average. A real average rate
            # is a period mean the ECB daily file does not contain.
            store.publish(quote_date, ccy, reporting_currency, kind,
                          into_eur, "ecb",
                          note=("ECB daily reference, inverted from EUR/{}. "
                                "Used as {} -- the daily file has no period "
                                "average, so this is a stand-in and not an "
                                "average rate.".format(ccy, kind)))
        written[ccy] = into_eur
    return written
