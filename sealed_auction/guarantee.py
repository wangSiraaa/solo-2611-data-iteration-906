"""Guarantee desk of the margin warehouse (托管仓担保台).

For the joint package tender the warehouse issues *guarantees*: ordinary
holds of contract kind ``PACKAGE_TENDER`` bound to
``(tender, buyer, menu, menu_revision)``.  Because a menu's options are
mutually exclusive, one guarantee covers the whole menu as long as its
amount is at least the menu's maximum possible single payment (the highest
option price).  Cash already frozen for any other contract -- an old-style
unit auction or another tender -- is invisible to the availability check,
so the same money can never back two contracts.

The desk answers three buyer-facing requests:

* ``preaudit_menu``       -- dry-run a menu: structure, required amount,
                            whether the buyer's free balance can cover it;
* ``freeze_guarantee``    -- actually freeze the guarantee (idempotent);
* ``guarantee_coverage``  -- show which options this visa can cover.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .core import (
    DomainError,
    InsufficientFunds,
    InvalidState,
    NotFound,
    get_receipt,
    now_utc,
    put_receipt,
    require_int,
    require_nonempty,
)
from .margin import MarginLedger
from .solver import validate_options

GUARANTEE_KIND = "PACKAGE_TENDER"


class GuaranteeDesk:
    def __init__(self, db, margin: MarginLedger, fault_injector=None):
        self.db = db
        self.margin = margin
        # fault_injector(operation, request_id) -> raise ResponseLost after commit
        self.fault_injector = fault_injector

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _tender_supply(conn, tender_id: str) -> dict[str, int]:
        row = conn.execute(
            "SELECT resources_json, status FROM tenders WHERE tender_id=?",
            (tender_id,),
        ).fetchone()
        if row is None:
            raise NotFound(f"tender {tender_id!r} not found")
        return json.loads(row["resources_json"])

    def _revision_verdict(self, conn, menu_id: str, revision: int) -> str | None:
        """A guarantee must name the menu revision it will back: revision 1
        for a menu that does not exist yet, or current+1 for a live one."""
        row = conn.execute(
            "SELECT revision, status FROM menus WHERE menu_id=?", (menu_id,)
        ).fetchone()
        if row is None:
            if revision != 1:
                return f"menu {menu_id!r} does not exist yet; first revision must be 1"
            return None
        if row["status"] != "ACTIVE":
            return f"menu {menu_id!r} is {row['status']}; it cannot be revised"
        if revision != row["revision"] + 1:
            return (
                f"guarantee revision {revision} does not match the next menu "
                f"revision {row['revision'] + 1}"
            )
        return None

    # --------------------------------------------------------------- preaudit

    def preaudit_menu(
        self,
        tender_id: str,
        buyer: str,
        menu_id: str,
        revision: int,
        options: Any,
    ) -> dict[str, Any]:
        """Read-only menu audit.  Nothing is frozen; the verdict tells the
        buyer exactly what a guarantee for this menu would require."""
        require_nonempty(tender_id, "tender_id")
        require_nonempty(buyer, "buyer")
        require_nonempty(menu_id, "menu_id")
        require_int(revision, "revision", minimum=1)

        with self.db.read() as conn:
            supply = self._tender_supply(conn, tender_id)
            errors: list[str] = []
            normalized: list[dict[str, Any]] = []
            required = 0
            try:
                normalized, required = validate_options(supply, options)
            except DomainError as exc:
                errors.append(str(exc))
            revision_error = self._revision_verdict(conn, menu_id, revision)
            if revision_error is not None:
                errors.append(revision_error)
            legs = self.margin.legs(conn, buyer)

        menu_valid = not errors
        coverable = menu_valid and legs["available_cents"] >= required
        return {
            "tender_id": tender_id,
            "buyer": buyer,
            "menu_id": menu_id,
            "revision": revision,
            "menu_valid": menu_valid,
            "errors": errors,
            "required_cents": required,
            "available_cents": legs["available_cents"],
            "coverable": coverable,
            "options": normalized,
        }

    # ---------------------------------------------------------------- freeze

    def freeze_guarantee(
        self,
        request_id: str,
        tender_id: str,
        buyer: str,
        menu_id: str,
        revision: int,
        options: Any,
        amount_cents: int | None = None,
        hold_id: str | None = None,
    ) -> dict[str, Any]:
        """Freeze one guarantee covering a whole menu of XOR options.

        The amount defaults to the menu's maximum possible single payment
        and may be higher, never lower.  Funds already frozen for any other
        contract cannot be reused.  Retrying with the same request_id
        replays the committed response; it never creates a second hold.
        """
        require_nonempty(request_id, "request_id")
        require_nonempty(tender_id, "tender_id")
        require_nonempty(buyer, "buyer")
        require_nonempty(menu_id, "menu_id")
        require_int(revision, "revision", minimum=1)
        hold_id = hold_id or f"ghold-{request_id}"
        require_nonempty(hold_id, "hold_id")
        payload = {
            "tender_id": tender_id,
            "buyer": buyer,
            "menu_id": menu_id,
            "revision": revision,
            "options": options,
            "amount_cents": amount_cents,
            "hold_id": hold_id,
        }
        operation = "guarantee.freeze"

        with self.db.tx() as conn:
            prior = get_receipt(conn, request_id, operation, payload)
            if prior is not None:
                return prior
            tender = conn.execute(
                "SELECT resources_json, status FROM tenders WHERE tender_id=?",
                (tender_id,),
            ).fetchone()
            if tender is None:
                raise NotFound(f"tender {tender_id!r} not found")
            if tender["status"] != "OPEN":
                raise InvalidState(
                    f"tender {tender_id} is {tender['status']}; "
                    "no new guarantees are issued"
                )
            supply = json.loads(tender["resources_json"])
            normalized, required = validate_options(supply, options)
            revision_error = self._revision_verdict(conn, menu_id, revision)
            if revision_error is not None:
                raise DomainError(revision_error)
            amount = required if amount_cents is None else amount_cents
            require_int(amount, "amount_cents", minimum=1)
            if amount < required:
                raise DomainError(
                    f"guarantee {amount} is below the menu's maximum possible "
                    f"single payment {required} cents"
                )
            legs = self.margin.legs(conn, buyer)
            if legs["available_cents"] < amount:
                raise InsufficientFunds(
                    f"insufficient available balance for {buyer}: need {amount}, "
                    f"available {legs['available_cents']} cents"
                )
            at = now_utc()
            try:
                conn.execute(
                    "INSERT INTO holds(hold_id, buyer, auction_id, amount_cents, "
                    "status, charged_cents, contract_kind, menu_id, menu_revision, "
                    "created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 'ACTIVE', 0, ?, ?, ?, ?, ?)",
                    (hold_id, buyer, tender_id, amount, GUARANTEE_KIND,
                     menu_id, revision, at, at),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError(
                    f"hold {hold_id!r} already exists; use a fresh hold_id"
                ) from exc
            conn.execute(
                "INSERT INTO ledger_events(buyer, kind, amount_cents, hold_id, "
                "auction_id, request_id, at) VALUES (?, 'FREEZE', ?, ?, ?, ?, ?)",
                (buyer, amount, hold_id, tender_id, request_id, at),
            )
            legs = self.margin.legs(conn, buyer)
            response = {
                "request_id": request_id,
                "hold_id": hold_id,
                "buyer": buyer,
                "tender_id": tender_id,
                "menu_id": menu_id,
                "menu_revision": revision,
                "contract_kind": GUARANTEE_KIND,
                "amount_cents": amount,
                "required_cents": required,
                "status": "ACTIVE",
                "covered_options": [o["label"] for o in normalized],
                **legs,
            }
            put_receipt(conn, request_id, operation, payload, response)
        self._maybe_lose("guarantee.freeze", request_id, response)
        return response

    # -------------------------------------------------------------- coverage

    def guarantee_coverage(
        self, hold_id: str, options: Any = None
    ) -> dict[str, Any]:
        """Show which choices this visa can cover.

        With ``options`` given, each is checked against the guarantee
        amount.  Otherwise the bound menu's current options are used when
        the menu exists at the bound revision.
        """
        require_nonempty(hold_id, "hold_id")
        with self.db.read() as conn:
            hold = self.margin._get_hold_conn(conn, hold_id)
            if hold["contract_kind"] != GUARANTEE_KIND:
                raise DomainError(
                    f"hold {hold_id!r} is a {hold['contract_kind']} hold, "
                    "not a package-tender guarantee"
                )
            result: dict[str, Any] = {
                "hold_id": hold_id,
                "buyer": hold["buyer"],
                "tender_id": hold["auction_id"],
                "menu_id": hold["menu_id"],
                "menu_revision": hold["menu_revision"],
                "contract_kind": hold["contract_kind"],
                "status": hold["status"],
                "amount_cents": hold["amount_cents"],
                "max_coverable_cents": hold["amount_cents"],
            }
            evaluated: list[dict[str, Any]] | None = None
            if options is not None:
                supply = self._tender_supply(conn, hold["auction_id"])
                normalized, _ = validate_options(supply, options)
                evaluated = normalized
                source = "provided"
            else:
                row = conn.execute(
                    "SELECT revision, options_json FROM menus WHERE menu_id=?",
                    (hold["menu_id"],),
                ).fetchone()
                if row is not None and row["revision"] == hold["menu_revision"]:
                    evaluated = json.loads(row["options_json"])
                    source = "menu"
                elif row is not None:
                    source = "stale (menu has moved to another revision)"
                else:
                    source = "unsubmitted (menu not on file; pass options to evaluate)"
            result["options_source"] = source
            if evaluated is not None:
                result["options"] = [
                    {
                        "label": o["label"],
                        "price_cents": o["price_cents"],
                        "covered": o["price_cents"] <= hold["amount_cents"],
                    }
                    for o in evaluated
                ]
            else:
                result["options"] = None
        return result

    # ------------------------------------------------------------ fault hook

    def _maybe_lose(self, operation: str, request_id: str, response: dict) -> None:
        if self.fault_injector is not None:
            from .core import ResponseLost

            if self.fault_injector(operation, request_id):
                raise ResponseLost(operation, request_id, response)
