"""Re-runnable demo of the joint package tender and its guarantee desk.

Run from the repository root:

    python3 -m examples.run_tender_demo            # fresh temporary database
    python3 -m examples.run_tender_demo --db tender-demo.sqlite3

A fictional exhibition rents out booth-model slots (models A and B, one
unit each) in one joint sealed solicitation.  Buyers file menus of mutually
exclusive package options; the margin warehouse covers each menu with a
single guarantee of contract kind PACKAGE_TENDER.  The worked instance is
the one from the specification:

    jia : {A,B} pays 9  XOR  {A} pays 7
    yi  : {B} pays 5
    bing: {A} pays 4

The three scenarios are alternative timelines, so each uses its own set of
buyer accounts (jia1/yi1/bing1, jia2/..., jia3/...).  Everything is
synthetic; nothing touches real payment rails.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime
from pathlib import Path

from sealed_auction import (
    ClearingService,
    Database,
    GuaranteeDesk,
    MarginLedger,
    TenderService,
)
from sealed_auction.core import InsufficientFunds
from sealed_auction.reconcile import reconcile

OPTS_JIA = [
    {"label": "J-AB", "resources": {"A": 1, "B": 1}, "price_cents": 9},
    {"label": "J-A", "resources": {"A": 1}, "price_cents": 7},
]
OPTS_JIA_SWAPPED = [
    {"label": "J-AB", "resources": {"A": 1, "B": 1}, "price_cents": 9},
]
OPTS_YI = [{"label": "Y-B", "resources": {"B": 1}, "price_cents": 5}]
OPTS_BING = [{"label": "C-A", "resources": {"A": 1}, "price_cents": 4}]


def show(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def balances(margin: MarginLedger, buyers) -> None:
    print("\n  margin balances")
    for b in buyers:
        bal = margin.balance(b)
        print(
            f"    {b:>6}: deposited={bal['deposited_cents']:>4} "
            f"available={bal['available_cents']:>4} "
            f"frozen={bal['frozen_cents']:>4} charged={bal['charged_cents']:>4}"
        )


def print_frozen(frozen) -> None:
    print(f"    total={frozen['total_cents']} usage={frozen['usage']} "
          f"labels={frozen['labels']}")
    for w in frozen["winners"]:
        print(f"    WIN  {w['buyer']:>6} option {w['label']:<5} "
              f"pays {w['price_cents']} for {w['resources']}")
    for x in frozen["excluded"]:
        print(f"    OUT  {x['buyer']:>6} menu {x['menu_id']}: {x['reason']}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=None, help="SQLite path (default: temp file)")
    args = parser.parse_args()

    tmp = None
    if args.db is None:
        tmp = tempfile.TemporaryDirectory()
        db_path = Path(tmp.name) / "tender-demo.sqlite3"
    else:
        db_path = Path(args.db)
    run_tag = datetime.now().strftime("%H%M%S%f")
    print(f"database: {db_path}  run_tag: {run_tag}")

    db = Database(db_path)
    margin = MarginLedger(db)
    desk = GuaranteeDesk(db, margin)
    tenders = TenderService(db, margin)
    clearing = ClearingService(db, margin, auctions=None, tenders=tenders)
    ephemeral = tmp is not None
    tag = "r1" if ephemeral else run_tag

    def ns(name: str) -> str:
        return name if ephemeral else f"{name}#{tag}"

    counter = {"n": 0}

    def req(name: str) -> str:
        counter["n"] += 1
        return f"demo-{tag}-{name}-{counter['n']:03d}"

    def guarantee(tender, buyer, menu, revision, options, amount=None):
        return desk.freeze_guarantee(
            req("g"), tender, buyer, menu, revision, options, amount_cents=amount
        )

    def submit(tender, buyer, menu, options, amount=None):
        g = guarantee(tender, buyer, menu, 1, options, amount)
        tenders.submit_menu(req("m"), menu, tender, buyer, options, g["hold_id"])
        print(f"    {buyer:>6} menu {menu:<6} submitted "
              f"(guarantee {g['amount_cents']} on hold {g['hold_id']})")

    try:
        # ------------------------------------------------ scenario 1: 12 total
        show("0. open the joint solicitation EXPO-1 (A x1, B x1)")
        expo1 = ns("EXPO-1")
        tenders.create_tender(req("t"), expo1, {"A": 1, "B": 1})
        print("    resources: A=1, B=1 -- packages are all-or-nothing, "
              "one win per buyer")
        jia, yi, bing = "jia1", "yi1", "bing1"
        buyers = [jia, yi, bing]

        show("1. deposits")
        for buyer, amount in [(jia, 9), (yi, 8), (bing, 4)]:
            margin.deposit(req("dep"), buyer, amount)
        balances(margin, buyers)

        show("2. warehouse pre-audit of jia's two-option menu")
        audit = desk.preaudit_menu(expo1, jia, ns("M-J1"), 1, OPTS_JIA)
        print(f"    menu valid: {audit['menu_valid']}  "
              f"required guarantee: {audit['required_cents']} "
              f"(max single payment)  available: {audit['available_cents']}  "
              f"coverable: {audit['coverable']}")

        show("3. guarantees frozen, menus submitted")
        submit(expo1, jia, ns("M-J1"), OPTS_JIA)          # guarantee 9
        submit(expo1, yi, ns("M-Y1"), OPTS_YI, amount=8)  # over-covered: 8 >= 5
        submit(expo1, bing, ns("M-B1"), OPTS_BING)        # guarantee 4
        balances(margin, buyers)

        show("4. what does jia's visa cover?")
        jia_hold = margin.balance(jia)["holds"][0]["hold_id"]
        cov = desk.guarantee_coverage(jia_hold)
        for o in cov["options"] or []:
            print(f"    option {o['label']:<5} price {o['price_cents']} "
                  f"-> covered: {o['covered']}")

        show("5. cutoff: only guarantee-backed menus are frozen, then optimum")
        frozen = tenders.freeze_tender(req("close"), expo1)
        print_frozen(frozen)
        print("    -> jia wins A alone (7) and yi wins B (5): total 12 beats "
              "jia's own {A,B}=9")

        show("6. settle: pay-as-bid charges, remainders and loser released")
        settled = clearing.settle_tender(req("settle"), expo1)
        for w in settled["winners"]:
            print(f"    {w['buyer']:>6} charged {w['charged_cents']} "
                  f"(guarantee remainder {w['released_cents']} released)")
        balances(margin, buyers)

        # ------------------------- scenario 2: swap to a single option, tie
        show("7. EXPO-2: jia formally swaps to a single option {A,B}@9")
        expo2 = ns("EXPO-2")
        tenders.create_tender(req("t"), expo2, {"A": 1, "B": 1})
        jia, yi, bing = "jia2", "yi2", "bing2"
        buyers = [jia, yi, bing]
        margin.deposit(req("dep"), jia, 18)  # old guarantee stays frozen until swap
        margin.deposit(req("dep"), yi, 5)
        margin.deposit(req("dep"), bing, 4)
        first = guarantee(expo2, jia, ns("M-J2"), 1, OPTS_JIA)
        tenders.submit_menu(req("m"), ns("M-J2"), expo2, jia, OPTS_JIA, first["hold_id"])
        submit(expo2, yi, ns("M-Y2"), OPTS_YI)
        submit(expo2, bing, ns("M-B2"), OPTS_BING)
        g2 = guarantee(expo2, jia, ns("M-J2"), 2, OPTS_JIA_SWAPPED)
        revised = tenders.revise_menu(
            req("rev"), ns("M-J2"), jia, OPTS_JIA_SWAPPED, g2["hold_id"]
        )
        print(f"    menu now revision {revised['revision']}; old guarantee "
              f"{revised['replaced_hold_id']} -> "
              f"{margin.get_hold(revised['replaced_hold_id'])['status']}")
        frozen2 = tenders.freeze_tender(req("close"), expo2)
        print_frozen(frozen2)
        print("    -> {J-AB}=9 ties {C-A, Y-B}=9; sorted label tuples "
              "('C-A','Y-B') < ('J-AB',) break the tie")
        settled2 = clearing.settle_tender(req("settle"), expo2)
        for w in settled2["winners"]:
            print(f"    {w['buyer']:>6} charged {w['charged_cents']}")
        balances(margin, buyers)

        # ----------------------------- scenario 3: balance 6, no guarantee
        show("8. EXPO-3: jia's free balance is only 6 -- menu cannot be guaranteed")
        expo3 = ns("EXPO-3")
        tenders.create_tender(req("t"), expo3, {"A": 1, "B": 1})
        jia, yi, bing = "jia3", "yi3", "bing3"
        buyers = [jia, yi, bing]
        margin.deposit(req("dep"), jia, 6)
        margin.deposit(req("dep"), yi, 5)
        margin.deposit(req("dep"), bing, 4)
        audit = desk.preaudit_menu(expo3, jia, ns("M-J3"), 1, OPTS_JIA)
        print(f"    pre-audit: required {audit['required_cents']}, "
              f"available {audit['available_cents']}, coverable={audit['coverable']}")
        try:
            guarantee(expo3, jia, ns("M-J3"), 1, OPTS_JIA)
            print("    UNEXPECTED: guarantee issued")
        except InsufficientFunds as exc:
            print(f"    guarantee refused: {exc}")
        submit(expo3, yi, ns("M-Y3"), OPTS_YI)
        submit(expo3, bing, ns("M-B3"), OPTS_BING)
        frozen3 = tenders.freeze_tender(req("close"), expo3)
        print_frozen(frozen3)
        print("    -> bing and yi still close their books")
        clearing.settle_tender(req("settle"), expo3)
        balances(margin, buyers)

        show("9. re-running a settle request replays the receipt, never doubles")
        again = clearing.settle_tender(req("settle"), expo1)
        print(f"    replayed settle total: {again['total_cents']} "
              f"(winners unchanged: "
              f"{[w['buyer'] for w in again['winners']]})")
        with db.read() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM ledger_events "
                "WHERE auction_id=? AND kind='CHARGE'",
                (expo1,),
            ).fetchone()["n"]
        print(f"    raw CHARGE events for EXPO-1: {n} (one per winner)")

        show("10. independent reconciliation over both contracts")
        report = reconcile(db)
        print(json.dumps({
            "ok": report["ok"],
            "violations": report["violations"],
            "tenders": {
                k.split("#")[0]: {
                    "total_cents": v.get("total_cents"),
                    "labels": v.get("labels"),
                    "usage": v.get("usage"),
                }
                for k, v in report["tenders"].items()
            },
        }, indent=2, ensure_ascii=False))
        return 0 if report["ok"] else 1
    finally:
        if tmp is not None:
            tmp.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
