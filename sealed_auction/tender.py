"""Joint sealed package tender service (联合密封征集).

Parallel to :class:`AuctionService`: one tender rents out several
booth-model slots at once.  A buyer submits a *menu* of mutually exclusive
package options (XOR); packages are all-or-nothing and a buyer wins at
most one option.  Winners pay their own package price (pay-as-bid), never
a uniform price.

Menus carry stable ids and are revised/withdrawn like bids; a revision
installs a new guarantee and releases the old one in the same transaction,
so a failed swap can never leave the old menu valid while its margin is
gone.  At cutoff only menus whose guarantee still verifies are frozen into
the immutable result; after that the result is re-read verbatim and
settlement only ever clears the original winners.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .core import (
    DataIntegrityError,
    DomainError,
    InvalidState,
    LimitExceeded,
    NotFound,
    get_receipt,
    now_utc,
    put_receipt,
    require_int,
    require_nonempty,
)
from .guarantee import GUARANTEE_KIND
from .margin import MarginLedger
from .solver import MAX_OPTIONS_PER_MENU, solve_package_tender, validate_options

MAX_RESOURCES = 8
MAX_UNITS_PER_RESOURCE = 3
MAX_BUYERS_PER_TENDER = 20
MAX_TENDERS = 3  # total joint tenders in this synthetic deployment


class TenderService:
    def __init__(self, db, margin: MarginLedger, fault_injector=None):
        self.db = db
        self.margin = margin
        # fault_injector(operation, request_id) -> raise ResponseLost after commit
        self.fault_injector = fault_injector

    # ------------------------------------------------------------- lifecycle

    def create_tender(
        self, request_id: str, tender_id: str, resources: dict[str, int]
    ) -> dict[str, Any]:
        require_nonempty(request_id, "request_id")
        require_nonempty(tender_id, "tender_id")
        if not isinstance(resources, dict) or not 1 <= len(resources) <= MAX_RESOURCES:
            raise LimitExceeded(
                f"a tender offers between 1 and {MAX_RESOURCES} resources"
            )
        normalized: dict[str, int] = {}
        for name, units in resources.items():
            require_nonempty(name, "resource")
            require_int(units, f"resources[{name!r}]", minimum=1)
            if units > MAX_UNITS_PER_RESOURCE:
                raise LimitExceeded(
                    f"resource {name!r}: at most {MAX_UNITS_PER_RESOURCE} units"
                )
            normalized[name] = units
        normalized = {r: normalized[r] for r in sorted(normalized)}
        payload = {"tender_id": tender_id, "resources": normalized}
        operation = "tender.create"

        with self.db.tx() as conn:
            prior = get_receipt(conn, request_id, operation, payload)
            if prior is not None:
                return prior
            existing = conn.execute(
                "SELECT * FROM tenders WHERE tender_id=?", (tender_id,)
            ).fetchone()
            if existing is not None:
                if json.loads(existing["resources_json"]) != normalized:
                    raise InvalidState(
                        f"tender {tender_id} already exists with resources="
                        f"{existing['resources_json']}"
                    )
                response = {
                    "tender_id": tender_id,
                    "resources": normalized,
                    "status": existing["status"],
                    "created_at": existing["created_at"],
                }
                put_receipt(conn, request_id, operation, payload, response)
                return response
            total = conn.execute("SELECT COUNT(*) AS n FROM tenders").fetchone()["n"]
            if total >= MAX_TENDERS:
                raise LimitExceeded(f"at most {MAX_TENDERS} tenders may be created")
            at = now_utc()
            conn.execute(
                "INSERT INTO tenders(tender_id, resources_json, status, created_at) "
                "VALUES (?, ?, 'OPEN', ?)",
                (tender_id, json.dumps(normalized, sort_keys=True), at),
            )
            response = {"tender_id": tender_id, "resources": normalized,
                        "status": "OPEN", "created_at": at}
            put_receipt(conn, request_id, operation, payload, response)
        return response

    def _tender(self, conn, tender_id: str) -> dict:
        row = conn.execute(
            "SELECT * FROM tenders WHERE tender_id=?", (tender_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"tender {tender_id!r} not found")
        return dict(row)

    def _require_open(self, conn, tender_id: str) -> dict:
        tender = self._tender(conn, tender_id)
        if tender["status"] != "OPEN":
            raise InvalidState(
                f"tender {tender_id} is {tender['status']}; solicitation is closed"
            )
        return tender

    # ------------------------------------------------------------ validation

    def _check_buyer_capacity(self, conn, tender_id: str, buyer: str) -> None:
        rows = conn.execute(
            "SELECT DISTINCT buyer FROM menus WHERE tender_id=? AND status='ACTIVE'",
            (tender_id,),
        ).fetchall()
        buyers = {r["buyer"] for r in rows}
        if buyer not in buyers and len(buyers) >= MAX_BUYERS_PER_TENDER:
            raise LimitExceeded(
                f"tender {tender_id} already has {MAX_BUYERS_PER_TENDER} buyers"
            )

    @staticmethod
    def _check_label_uniqueness(
        conn, tender_id: str, labels: list[str], exclude_menu_id: str | None = None
    ) -> None:
        """Option labels must be unique across all live menus of a tender,
        so the frozen winner list unambiguously names the chosen options."""
        rows = conn.execute(
            "SELECT menu_id, options_json FROM menus "
            "WHERE tender_id=? AND status='ACTIVE'",
            (tender_id,),
        ).fetchall()
        taken: dict[str, str] = {}
        for row in rows:
            if row["menu_id"] == exclude_menu_id:
                continue
            for opt in json.loads(row["options_json"]):
                taken[opt["label"]] = row["menu_id"]
        for label in labels:
            if label in taken:
                raise DomainError(
                    f"option label {label!r} is already used by menu "
                    f"{taken[label]!r} in this tender"
                )

    def _verify_guarantee(
        self,
        conn,
        hold_id: str,
        buyer: str,
        tender_id: str,
        menu_id: str,
        revision: int,
        required_cents: int,
    ) -> dict:
        """Tender-side verification of a presented guarantee, inside the
        menu transaction.  Caller assertions are never trusted: the
        warehouse row must match on contract kind, buyer, tender, amount,
        status and the (menu, revision) binding."""
        hold = self.margin.verify_credential(
            conn, hold_id, buyer, tender_id, required_cents,
            contract_kind=GUARANTEE_KIND,
        )
        errors = []
        if hold["menu_id"] != menu_id:
            errors.append(f"bound to menu {hold['menu_id']!r}, not {menu_id!r}")
        if hold["menu_revision"] != revision:
            errors.append(
                f"bound to menu revision {hold['menu_revision']}, not {revision}"
            )
        if errors:
            raise DomainError("invalid guarantee: " + "; ".join(errors))
        return hold

    # ----------------------------------------------------------------- menus

    def submit_menu(
        self,
        request_id: str,
        menu_id: str,
        tender_id: str,
        buyer: str,
        options: Any,
        hold_id: str,
    ) -> dict[str, Any]:
        require_nonempty(request_id, "request_id")
        require_nonempty(menu_id, "menu_id")
        require_nonempty(tender_id, "tender_id")
        require_nonempty(buyer, "buyer")
        require_nonempty(hold_id, "hold_id")
        payload = {"menu_id": menu_id, "tender_id": tender_id, "buyer": buyer,
                   "options": options, "hold_id": hold_id}
        operation = "tender.submit_menu"

        with self.db.tx() as conn:
            prior = get_receipt(conn, request_id, operation, payload)
            if prior is not None:
                return prior
            tender = self._require_open(conn, tender_id)
            supply = json.loads(tender["resources_json"])
            normalized, required = validate_options(
                supply, options, max_options=MAX_OPTIONS_PER_MENU
            )
            self._check_buyer_capacity(conn, tender_id, buyer)
            existing = conn.execute(
                "SELECT 1 FROM menus WHERE menu_id=?", (menu_id,)
            ).fetchone()
            if existing is not None:
                raise InvalidState(
                    f"menu {menu_id} already exists; use revise_menu for a live menu"
                )
            other = conn.execute(
                "SELECT 1 FROM menus WHERE tender_id=? AND buyer=? AND status='ACTIVE'",
                (tender_id, buyer),
            ).fetchone()
            if other is not None:
                raise InvalidState(
                    f"buyer {buyer} already has an active menu in {tender_id}"
                )
            self._check_label_uniqueness(conn, tender_id, [o["label"] for o in normalized])
            self._verify_guarantee(
                conn, hold_id, buyer, tender_id, menu_id, revision=1,
                required_cents=required,
            )
            at = now_utc()
            options_json = json.dumps(normalized, sort_keys=True, ensure_ascii=False)
            try:
                conn.execute(
                    "INSERT INTO menus(menu_id, tender_id, buyer, revision, "
                    "options_json, status, hold_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, 1, ?, 'ACTIVE', ?, ?, ?)",
                    (menu_id, tender_id, buyer, options_json, hold_id, at, at),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError(
                    f"menu rejected: {menu_id} already exists, or buyer {buyer} "
                    "already has an active menu, or the guarantee already backs one"
                ) from exc
            conn.execute(
                "INSERT INTO menu_events(request_id, menu_id, tender_id, buyer, "
                "action, revision, options_json, hold_id, at) "
                "VALUES (?, ?, ?, ?, 'SUBMIT', 1, ?, ?, ?)",
                (request_id, menu_id, tender_id, buyer, options_json, hold_id, at),
            )
            response = {
                "request_id": request_id,
                "menu_id": menu_id,
                "tender_id": tender_id,
                "buyer": buyer,
                "revision": 1,
                "status": "ACTIVE",
                "options": normalized,
                "hold_id": hold_id,
                "required_cents": required,
                "at": at,
            }
            put_receipt(conn, request_id, operation, payload, response)
        self._maybe_lose("tender.submit_menu", request_id, response)
        return response

    def revise_menu(
        self,
        request_id: str,
        menu_id: str,
        buyer: str,
        options: Any,
        new_hold_id: str,
    ) -> dict[str, Any]:
        """Replace a live menu wholesale with the next revision.

        The new guarantee must already exist and be bound to
        ``(menu, revision+1)``.  The old guarantee is released **in the
        same transaction** that installs the new options: a failed swap
        leaves the old menu valid *and* the old margin frozen, and a
        retransmitted request replays the receipt instead of minting a
        second spendable voucher.
        """
        require_nonempty(request_id, "request_id")
        require_nonempty(menu_id, "menu_id")
        require_nonempty(buyer, "buyer")
        require_nonempty(new_hold_id, "new_hold_id")
        payload = {"menu_id": menu_id, "buyer": buyer, "options": options,
                   "new_hold_id": new_hold_id}
        operation = "tender.revise_menu"

        with self.db.tx() as conn:
            prior = get_receipt(conn, request_id, operation, payload)
            if prior is not None:
                return prior
            row = conn.execute(
                "SELECT * FROM menus WHERE menu_id=?", (menu_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"menu {menu_id!r} not found")
            menu = dict(row)
            if menu["status"] != "ACTIVE":
                raise InvalidState(f"menu {menu_id} is {menu['status']}")
            if menu["buyer"] != buyer:
                raise DomainError(
                    f"menu {menu_id} belongs to {menu['buyer']}, not {buyer}"
                )
            tender = self._require_open(conn, menu["tender_id"])
            supply = json.loads(tender["resources_json"])
            normalized, required = validate_options(
                supply, options, max_options=MAX_OPTIONS_PER_MENU
            )
            revision = menu["revision"] + 1
            if new_hold_id == menu["hold_id"]:
                raise DomainError("revision requires a distinct guarantee")
            self._check_label_uniqueness(
                conn, menu["tender_id"], [o["label"] for o in normalized],
                exclude_menu_id=menu_id,
            )
            self._verify_guarantee(
                conn, new_hold_id, buyer, menu["tender_id"], menu_id, revision,
                required_cents=required,
            )
            at = now_utc()
            old_hold_id = menu["hold_id"]
            options_json = json.dumps(normalized, sort_keys=True, ensure_ascii=False)
            try:
                conn.execute(
                    "UPDATE menus SET revision=?, options_json=?, hold_id=?, "
                    "updated_at=? WHERE menu_id=?",
                    (revision, options_json, new_hold_id, at, menu_id),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError(
                    f"guarantee {new_hold_id} already backs another active menu"
                ) from exc
            # Atomic release of the superseded guarantee (no independent
            # receipt: the outer revision receipt replays the whole swap).
            self.margin.release_in_tx(
                conn, old_hold_id, f"{request_id}:release",
                auction_id=menu["tender_id"],
            )
            conn.execute(
                "INSERT INTO menu_events(request_id, menu_id, tender_id, buyer, "
                "action, revision, options_json, hold_id, at) "
                "VALUES (?, ?, ?, ?, 'REVISE', ?, ?, ?, ?)",
                (request_id, menu_id, menu["tender_id"], buyer, revision,
                 options_json, new_hold_id, at),
            )
            response = {
                "request_id": request_id,
                "menu_id": menu_id,
                "tender_id": menu["tender_id"],
                "buyer": buyer,
                "revision": revision,
                "status": "ACTIVE",
                "options": normalized,
                "hold_id": new_hold_id,
                "replaced_hold_id": old_hold_id,
                "required_cents": required,
                "at": at,
            }
            put_receipt(conn, request_id, operation, payload, response)
        self._maybe_lose("tender.revise_menu", request_id, response)
        return response

    def withdraw_menu(self, request_id: str, menu_id: str) -> dict[str, Any]:
        require_nonempty(request_id, "request_id")
        require_nonempty(menu_id, "menu_id")
        payload = {"menu_id": menu_id}
        operation = "tender.withdraw_menu"

        with self.db.tx() as conn:
            prior = get_receipt(conn, request_id, operation, payload)
            if prior is not None:
                return prior
            row = conn.execute(
                "SELECT * FROM menus WHERE menu_id=?", (menu_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"menu {menu_id!r} not found")
            menu = dict(row)
            if menu["status"] != "ACTIVE":
                raise InvalidState(f"menu {menu_id} is already {menu['status']}")
            self._require_open(conn, menu["tender_id"])
            at = now_utc()
            conn.execute(
                "UPDATE menus SET status='CANCELLED', updated_at=? WHERE menu_id=?",
                (at, menu_id),
            )
            self.margin.release_in_tx(
                conn, menu["hold_id"], f"{request_id}:release",
                auction_id=menu["tender_id"],
            )
            conn.execute(
                "INSERT INTO menu_events(request_id, menu_id, tender_id, buyer, "
                "action, revision, options_json, hold_id, at) "
                "VALUES (?, ?, ?, ?, 'WITHDRAW', ?, ?, ?, ?)",
                (request_id, menu_id, menu["tender_id"], menu["buyer"],
                 menu["revision"], menu["options_json"], menu["hold_id"], at),
            )
            response = {
                "request_id": request_id,
                "menu_id": menu_id,
                "tender_id": menu["tender_id"],
                "buyer": menu["buyer"],
                "status": "CANCELLED",
                "released_hold_id": menu["hold_id"],
                "at": at,
            }
            put_receipt(conn, request_id, operation, payload, response)
        self._maybe_lose("tender.withdraw_menu", request_id, response)
        return response

    # ---------------------------------------------------------------- cutoff

    def freeze_tender(self, request_id: str, tender_id: str) -> dict[str, Any]:
        """Close the solicitation and persist the immutable optimal result.

        Only menus whose guarantee still verifies are frozen into the
        allocation; the rest are recorded as excluded.  No money moves here.
        """
        require_nonempty(request_id, "request_id")
        require_nonempty(tender_id, "tender_id")
        payload = {"tender_id": tender_id}
        operation = "tender.freeze"

        with self.db.tx() as conn:
            prior = get_receipt(conn, request_id, operation, payload)
            if prior is not None:
                return prior
            tender = self._tender(conn, tender_id)
            if tender["status"] in ("FROZEN", "SETTLED"):
                snapshot = self._load_snapshot(conn, tender_id)
                response = self._snapshot_response(tender_id, tender["status"], snapshot)
                put_receipt(conn, request_id, operation, payload, response)
                return response

            supply = json.loads(tender["resources_json"])
            menus = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM menus WHERE tender_id=? AND status='ACTIVE' "
                    "ORDER BY menu_id",
                    (tender_id,),
                )
            ]
            eligible: list[dict] = []
            excluded: list[dict] = []
            for menu in menus:
                options = json.loads(menu["options_json"])
                required = max(o["price_cents"] for o in options)
                try:
                    self._verify_guarantee(
                        conn, menu["hold_id"], menu["buyer"], tender_id,
                        menu["menu_id"], menu["revision"], required,
                    )
                    eligible.append(menu)
                except DomainError as exc:
                    excluded.append(
                        {
                            "menu_id": menu["menu_id"],
                            "buyer": menu["buyer"],
                            "hold_id": menu["hold_id"],
                            "reason": str(exc),
                        }
                    )

            result = solve_package_tender(
                supply,
                [
                    {
                        "menu_id": m["menu_id"],
                        "buyer": m["buyer"],
                        "options": json.loads(m["options_json"]),
                    }
                    for m in eligible
                ],
            )
            by_menu = {m["menu_id"]: m for m in eligible}
            winners = [
                {**w, "hold_id": by_menu[w["menu_id"]]["hold_id"],
                 "revision": by_menu[w["menu_id"]]["revision"]}
                for w in result["winners"]
            ]
            winner_ids = {w["menu_id"] for w in winners}
            losers = [
                {"menu_id": m["menu_id"], "buyer": m["buyer"], "hold_id": m["hold_id"]}
                for m in eligible
                if m["menu_id"] not in winner_ids
            ]
            snapshot = {
                "tender_id": tender_id,
                "resources": supply,
                "total_cents": result["total_cents"],
                "usage": result["usage"],
                "labels": result["labels"],
                "winners": winners,
                "losers": losers,
                "excluded": excluded,
                "eligible_menu_ids": sorted(m["menu_id"] for m in eligible),
            }
            at = now_utc()
            conn.execute(
                "UPDATE tenders SET status='FROZEN', frozen_at=? WHERE tender_id=?",
                (at, tender_id),
            )
            conn.execute(
                "INSERT INTO tender_settlements(tender_id, result_json, status, "
                "created_at) VALUES (?, ?, 'FROZEN', ?)",
                (tender_id, json.dumps(snapshot, sort_keys=True, ensure_ascii=False), at),
            )
            response = self._snapshot_response(tender_id, "FROZEN", snapshot)
            put_receipt(conn, request_id, operation, payload, response)
        self._maybe_lose("tender.freeze", request_id, response)
        return response

    def _load_snapshot(self, conn, tender_id: str) -> dict:
        row = conn.execute(
            "SELECT result_json FROM tender_settlements WHERE tender_id=?",
            (tender_id,),
        ).fetchone()
        if row is None:
            raise DataIntegrityError(
                f"tender {tender_id} is frozen but has no stored result"
            )
        return json.loads(row["result_json"])

    @staticmethod
    def _snapshot_response(tender_id: str, status: str, snapshot: dict) -> dict:
        return {
            "tender_id": tender_id,
            "status": status,
            "resources": snapshot["resources"],
            "total_cents": snapshot["total_cents"],
            "usage": snapshot["usage"],
            "labels": snapshot["labels"],
            "winners": snapshot["winners"],
            "losers": snapshot["losers"],
            "excluded": snapshot["excluded"],
        }

    # ---------------------------------------------------------------- settle

    def settle_tender(self, request_id: str, tender_id: str) -> dict[str, Any]:
        """Clear the frozen result against real guarantees.

        Winners are charged their own package price (pay-as-bid, capped by
        the guarantee, remainder released); losers' and excluded menus'
        guarantees are released.  Every warehouse operation carries a
        deterministic request_id, so a lost receipt is resumed against the
        original frozen trades, never a recomputed ones.
        """
        require_nonempty(request_id, "request_id")
        require_nonempty(tender_id, "tender_id")
        payload = {"tender_id": tender_id}
        operation = "tender.settle"

        # Phase 1: fix the settlement intent (resumable).
        with self.db.tx() as conn:
            prior = get_receipt(conn, request_id, operation, payload)
            if prior is not None:
                return prior
            tender = self._tender(conn, tender_id)
            if tender["status"] == "OPEN":
                raise InvalidState(
                    f"tender {tender_id} is still OPEN; freeze it before settling"
                )
            snapshot = self._load_snapshot(conn, tender_id)
            settlement = conn.execute(
                "SELECT status FROM tender_settlements WHERE tender_id=?",
                (tender_id,),
            ).fetchone()
            if settlement["status"] != "CLEARED":
                conn.execute(
                    "UPDATE tender_settlements SET status='CLEARING' WHERE tender_id=?",
                    (tender_id,),
                )
                conn.execute(
                    "UPDATE tenders SET status='SETTLED' WHERE tender_id=?",
                    (tender_id,),
                )

        # Phase 2: idempotent warehouse side effects, in snapshot order.
        for winner in snapshot["winners"]:
            self.margin.charge_capped(
                request_id=f"settle:{tender_id}:{winner['menu_id']}",
                hold_id=winner["hold_id"],
                auction_id=tender_id,
                charge_cents=winner["price_cents"],
            )
        for item in snapshot["losers"]:
            self._release_if_active(
                f"settle:{tender_id}:release:{item['menu_id']}", item["hold_id"]
            )
        for item in snapshot["excluded"]:
            self._release_if_active(
                f"settle:{tender_id}:excluded:{item['menu_id']}", item["hold_id"]
            )

        # Phase 3: mark cleared and record the receipt.
        with self.db.tx() as conn:
            prior = get_receipt(conn, request_id, operation, payload)
            if prior is not None:
                return prior
            at = now_utc()
            conn.execute(
                "UPDATE tender_settlements SET status='CLEARED', cleared_at=? "
                "WHERE tender_id=?",
                (at, tender_id),
            )
            conn.execute(
                "UPDATE tenders SET settled_at=? WHERE tender_id=?",
                (at, tender_id),
            )
            result = self._settlement_result(conn, tender_id, snapshot, at)
            put_receipt(conn, request_id, operation, payload, result)
        return result

    def _release_if_active(self, request_id: str, hold_id: str) -> dict[str, Any]:
        """Release a guarantee unless it is already terminal.

        A buyer may have released their own guarantee between cutoff and
        settlement; that must not wedge clearing of the frozen trades.
        """
        hold = self.margin.get_hold(hold_id)
        if hold["status"] == "ACTIVE":
            return self.margin.release(request_id, hold_id)
        return {
            "request_id": request_id,
            "hold_id": hold_id,
            "status": hold["status"],
            "released_cents": 0,
            "note": f"hold already {hold['status']}",
        }

    def _settlement_result(self, conn, tender_id, snapshot, cleared_at) -> dict:
        winners = []
        for w in snapshot["winners"]:
            hold = conn.execute(
                "SELECT status, amount_cents, charged_cents FROM holds "
                "WHERE hold_id=?",
                (w["hold_id"],),
            ).fetchone()
            winners.append(
                {
                    **w,
                    "hold_status": hold["status"],
                    "charged_cents": hold["charged_cents"],
                    "released_cents": hold["amount_cents"] - hold["charged_cents"],
                }
            )
        others = []
        for item in snapshot["losers"] + snapshot["excluded"]:
            hold = conn.execute(
                "SELECT status FROM holds WHERE hold_id=?", (item["hold_id"],)
            ).fetchone()
            others.append(
                {
                    "menu_id": item["menu_id"],
                    "buyer": item["buyer"],
                    "hold_id": item["hold_id"],
                    "hold_status": hold["status"] if hold else None,
                }
            )
        return {
            "tender_id": tender_id,
            "status": "SETTLED",
            "total_cents": snapshot["total_cents"],
            "usage": snapshot["usage"],
            "labels": snapshot["labels"],
            "winners": winners,
            "others": others,
            "settled_at": cleared_at,
        }

    # ----------------------------------------------------------------- reads

    def read_result(self, tender_id: str) -> dict[str, Any]:
        """Re-open after cutoff/settlement: always the same frozen result."""
        with self.db.read() as conn:
            tender = self._tender(conn, tender_id)
            if tender["status"] == "OPEN":
                return {
                    "tender_id": tender_id,
                    "status": "OPEN",
                    "result": None,
                    "note": "solicitation is open; no frozen result yet",
                }
            snapshot = self._load_snapshot(conn, tender_id)
            settlement = conn.execute(
                "SELECT status, cleared_at FROM tender_settlements WHERE tender_id=?",
                (tender_id,),
            ).fetchone()
            holds = {
                r["hold_id"]: dict(r)
                for r in conn.execute(
                    "SELECT hold_id, status, charged_cents FROM holds "
                    "WHERE auction_id=?",
                    (tender_id,),
                )
            }
        winners = []
        for w in snapshot["winners"]:
            h = holds.get(w["hold_id"], {})
            winners.append(
                {**w, "hold_status": h.get("status"),
                 "charged_cents": h.get("charged_cents")}
            )
        return {
            **self._snapshot_response(tender_id, tender["status"], snapshot),
            "winners": winners,
            "settlement_status": settlement["status"] if settlement else None,
            "cleared_at": settlement["cleared_at"] if settlement else None,
        }

    def explain_tender(self, tender_id: str) -> dict[str, Any]:
        """Human-readable derivation of the frozen allocation."""
        with self.db.read() as conn:
            tender = self._tender(conn, tender_id)
            if tender["status"] == "OPEN":
                raise InvalidState("tender is still OPEN; freeze it first")
            snapshot = self._load_snapshot(conn, tender_id)
            events = [
                dict(r)
                for r in conn.execute(
                    "SELECT seq, action, menu_id, buyer, revision, options_json, "
                    "hold_id, at FROM menu_events WHERE tender_id=? ORDER BY seq",
                    (tender_id,),
                )
            ]
        return {
            "tender_id": tender_id,
            "status": tender["status"],
            "rule": (
                "choose at most one option per buyer maximising the sum of "
                "package prices; per-resource usage may not exceed supply; "
                "packages are all-or-nothing"
            ),
            "tie_break": (
                "among max-total allocations, the lexicographically smallest "
                "ascending tuple of selected option labels wins"
            ),
            "pricing": "pay-as-bid: each winner is charged their own package price",
            "resources": snapshot["resources"],
            "usage": snapshot["usage"],
            "total_cents": snapshot["total_cents"],
            "labels": snapshot["labels"],
            "winners": snapshot["winners"],
            "excluded": snapshot["excluded"],
            "menu_events": events,
        }

    # ------------------------------------------------------------ fault hook

    def _maybe_lose(self, operation: str, request_id: str, response: dict) -> None:
        if self.fault_injector is not None:
            from .core import ResponseLost

            if self.fault_injector(operation, request_id):
                raise ResponseLost(operation, request_id, response)
