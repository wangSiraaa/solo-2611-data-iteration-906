"""SQLite storage shared by the two services.

The auction service and margin warehouse are separate Python services, but
they may use a shared transaction (explicitly allowed by the problem) so a
bid revision and its collateral adjustment commit atomically.  Durable
intents/receipts are still used at the settlement boundary, where each
margin operation is its own idempotent transaction.
"""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS auctions (
    auction_id       TEXT PRIMARY KEY,
    units            INTEGER NOT NULL CHECK (units BETWEEN 1 AND 10),
    status           TEXT NOT NULL CHECK (status IN ('OPEN','FROZEN','SETTLED')),
    created_at       TEXT NOT NULL,
    frozen_at        TEXT,
    settled_at       TEXT
);

CREATE TABLE IF NOT EXISTS holds (
    hold_id          TEXT PRIMARY KEY,
    buyer            TEXT NOT NULL,
    auction_id       TEXT NOT NULL,
    amount_cents     INTEGER NOT NULL CHECK (amount_cents >= 0),
    status           TEXT NOT NULL CHECK (status IN ('ACTIVE','RELEASED','CONSUMED')),
    charged_cents    INTEGER NOT NULL DEFAULT 0 CHECK (charged_cents >= 0),
    -- Explicit contract kind separates the two operation versions:
    -- UNIT_AUCTION holds back per-auction bids; PACKAGE_TENDER guarantees
    -- back one menu revision of the joint tender and record that binding.
    contract_kind    TEXT NOT NULL DEFAULT 'UNIT_AUCTION'
                     CHECK (contract_kind IN ('UNIT_AUCTION','PACKAGE_TENDER')),
    menu_id          TEXT,
    menu_revision    INTEGER,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_holds_buyer ON holds(buyer);
CREATE INDEX IF NOT EXISTS idx_holds_auction ON holds(auction_id);

CREATE TABLE IF NOT EXISTS bids (
    bid_id            TEXT PRIMARY KEY,
    auction_id        TEXT NOT NULL REFERENCES auctions(auction_id),
    buyer             TEXT NOT NULL,
    qty               INTEGER NOT NULL CHECK (qty > 0),
    unit_price_cents  INTEGER NOT NULL CHECK (unit_price_cents > 0),
    revision          INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    status            TEXT NOT NULL CHECK (status IN ('ACTIVE','CANCELLED')),
    hold_id           TEXT NOT NULL REFERENCES holds(hold_id),
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
-- At most one valid bid per buyer per auction.
CREATE UNIQUE INDEX IF NOT EXISTS ux_bid_active_buyer
    ON bids(auction_id, buyer) WHERE status = 'ACTIVE';
-- A hold can back at most one valid bid at a time.
CREATE UNIQUE INDEX IF NOT EXISTS ux_bid_active_hold
    ON bids(hold_id) WHERE status = 'ACTIVE';
CREATE INDEX IF NOT EXISTS idx_bids_auction ON bids(auction_id);

CREATE TABLE IF NOT EXISTS bid_events (
    seq               INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id        TEXT NOT NULL,
    bid_id            TEXT NOT NULL,
    auction_id        TEXT NOT NULL,
    buyer             TEXT NOT NULL,
    action            TEXT NOT NULL CHECK (action IN ('PLACE','REVISE','CANCEL')),
    qty               INTEGER NOT NULL,
    unit_price_cents  INTEGER NOT NULL,
    revision          INTEGER NOT NULL,
    hold_id           TEXT NOT NULL,
    at                TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bid_events_auction ON bid_events(auction_id);

CREATE TABLE IF NOT EXISTS ledger_events (
    seq               INTEGER PRIMARY KEY AUTOINCREMENT,
    buyer             TEXT NOT NULL,
    kind              TEXT NOT NULL CHECK (kind IN ('DEPOSIT','FREEZE','RELEASE','CHARGE')),
    amount_cents      INTEGER NOT NULL CHECK (amount_cents > 0),
    hold_id           TEXT,
    auction_id        TEXT,
    request_id        TEXT NOT NULL,
    at                TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_buyer ON ledger_events(buyer);
CREATE INDEX IF NOT EXISTS idx_ledger_hold ON ledger_events(hold_id);
CREATE INDEX IF NOT EXISTS idx_ledger_auction ON ledger_events(auction_id);

CREATE TABLE IF NOT EXISTS receipts (
    request_id        TEXT PRIMARY KEY,
    operation         TEXT NOT NULL,
    fingerprint       TEXT NOT NULL,
    response_json     TEXT NOT NULL,
    at                TEXT NOT NULL
);

-- One hold may be charged at most once per auction, independent of retries.
CREATE UNIQUE INDEX IF NOT EXISTS ux_charge_once
    ON ledger_events(auction_id, hold_id) WHERE kind = 'CHARGE';

CREATE TABLE IF NOT EXISTS settlements (
    auction_id             TEXT PRIMARY KEY REFERENCES auctions(auction_id),
    clearing_price_cents   INTEGER,
    allocations_json       TEXT NOT NULL,
    status                 TEXT NOT NULL CHECK (status IN ('FROZEN','CLEARING','CLEARED')),
    created_at             TEXT NOT NULL,
    cleared_at             TEXT
);

-- ---------------------------------------------------------------------------
-- Joint package tender (联合密封征集), parallel to the unit auctions above.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS tenders (
    tender_id        TEXT PRIMARY KEY,
    resources_json   TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('OPEN','FROZEN','SETTLED')),
    created_at       TEXT NOT NULL,
    frozen_at        TEXT,
    settled_at       TEXT
);

-- One menu = one buyer's set of mutually exclusive package options (XOR).
-- menu_id is the stable label; revisions replace options + guarantee atomically.
CREATE TABLE IF NOT EXISTS menus (
    menu_id          TEXT PRIMARY KEY,
    tender_id        TEXT NOT NULL REFERENCES tenders(tender_id),
    buyer            TEXT NOT NULL,
    revision         INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    options_json     TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('ACTIVE','CANCELLED')),
    hold_id          TEXT NOT NULL REFERENCES holds(hold_id),
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
-- At most one valid menu per buyer per tender.
CREATE UNIQUE INDEX IF NOT EXISTS ux_menu_active_buyer
    ON menus(tender_id, buyer) WHERE status = 'ACTIVE';
-- A guarantee can back at most one valid menu at a time.
CREATE UNIQUE INDEX IF NOT EXISTS ux_menu_active_hold
    ON menus(hold_id) WHERE status = 'ACTIVE';
CREATE INDEX IF NOT EXISTS idx_menus_tender ON menus(tender_id);

CREATE TABLE IF NOT EXISTS menu_events (
    seq               INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id        TEXT NOT NULL,
    menu_id           TEXT NOT NULL,
    tender_id         TEXT NOT NULL,
    buyer             TEXT NOT NULL,
    action            TEXT NOT NULL CHECK (action IN ('SUBMIT','REVISE','WITHDRAW')),
    revision          INTEGER NOT NULL,
    options_json      TEXT NOT NULL,
    hold_id           TEXT NOT NULL,
    at                TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_menu_events_tender ON menu_events(tender_id);

CREATE TABLE IF NOT EXISTS tender_settlements (
    tender_id          TEXT PRIMARY KEY REFERENCES tenders(tender_id),
    result_json        TEXT NOT NULL,
    status             TEXT NOT NULL CHECK (status IN ('FROZEN','CLEARING','CLEARED')),
    created_at         TEXT NOT NULL,
    cleared_at         TEXT
);
"""


class Database:
    """Thin SQLite wrapper using one connection per transaction/read.

    BEGIN IMMEDIATE serialises writers.  Two concurrent freeze attempts for
    the last available cent therefore cannot both observe the same balance.
    """

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def _init_schema(self) -> None:
        conn = self.connect()
        try:
            if self.path != ":memory:":
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA synchronous = FULL")
            conn.executescript(SCHEMA)
            self._migrate(conn)
        finally:
            conn.close()

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Add contract-kind/binding columns to pre-existing hold tables."""
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(holds)")}
        if "contract_kind" not in cols:
            conn.execute(
                "ALTER TABLE holds ADD COLUMN contract_kind "
                "TEXT NOT NULL DEFAULT 'UNIT_AUCTION'"
            )
        if "menu_id" not in cols:
            conn.execute("ALTER TABLE holds ADD COLUMN menu_id TEXT")
        if "menu_revision" not in cols:
            conn.execute("ALTER TABLE holds ADD COLUMN menu_revision INTEGER")

    @contextlib.contextmanager
    def tx(self):
        conn = self.connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @contextlib.contextmanager
    def read(self):
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()
