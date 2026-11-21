"""End-to-end scenario tests for the joint package tender + guarantee desk."""

from __future__ import annotations

import itertools
import random
import tempfile
import threading
import unittest
from pathlib import Path

from sealed_auction import (
    AuctionService,
    ClearingService,
    Database,
    GuaranteeDesk,
    MarginLedger,
    TenderService,
)
from sealed_auction.core import (
    DomainError,
    InsufficientFunds,
    InvalidState,
    LimitExceeded,
    ResponseLost,
)
from sealed_auction.reconcile import assert_reconciled, reconcile
from sealed_auction.solver import solve_package_tender

# The worked instance from the specification: models A and B, one unit each.
OPTS_JIA = [
    {"label": "J-AB", "resources": {"A": 1, "B": 1}, "price_cents": 9},
    {"label": "J-A", "resources": {"A": 1}, "price_cents": 7},
]
OPTS_JIA_SWAPPED = [
    {"label": "J-AB", "resources": {"A": 1, "B": 1}, "price_cents": 9},
]
OPTS_YI = [{"label": "Y-B", "resources": {"B": 1}, "price_cents": 5}]
OPTS_BING = [{"label": "C-A", "resources": {"A": 1}, "price_cents": 4}]


class Harness:
    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "tender.sqlite3")
        self.margin = MarginLedger(self.db)
        self.desk = GuaranteeDesk(self.db, self.margin)
        self.tenders = TenderService(self.db, self.margin)
        self.auctions = AuctionService(self.db, self.margin)
        self.clearing = ClearingService(
            self.db, self.margin, self.auctions, self.tenders
        )
        self._req = 0

    def req(self, prefix="req"):
        self._req += 1
        return f"{prefix}-{self._req:04d}"

    def deposit(self, buyer, amount):
        return self.margin.deposit(self.req("dep"), buyer, amount)

    def create_tender(self, tender_id, resources):
        return self.tenders.create_tender(self.req("new"), tender_id, resources)

    def guarantee(self, tender_id, buyer, menu_id, revision, options, amount=None):
        return self.desk.freeze_guarantee(
            self.req("gfr"), tender_id, buyer, menu_id, revision, options,
            amount_cents=amount,
        )

    def submit(self, menu_id, tender_id, buyer, options, amount=None, revision=1):
        g = self.guarantee(tender_id, buyer, menu_id, revision, options, amount)
        return self.tenders.submit_menu(
            self.req("sub"), menu_id, tender_id, buyer, options, g["hold_id"]
        )

    def expo(self, tender_id="EXPO", jia_deposit=9, yi_deposit=8, bing_deposit=4):
        """The worked instance: A/B one unit each, jia/yi/bing menus filed."""
        self.deposit("jia", jia_deposit)
        self.deposit("yi", yi_deposit)
        self.deposit("bing", bing_deposit)
        self.create_tender(tender_id, {"A": 1, "B": 1})
        self.submit("M-J", tender_id, "jia", OPTS_JIA)            # guarantee 9
        self.submit("M-Y", tender_id, "yi", OPTS_YI, amount=8)    # over-covered 8 >= 5
        self.submit("M-B", tender_id, "bing", OPTS_BING)          # guarantee 4
        return tender_id

    def close(self):
        self._tmp.cleanup()


class TenderScenarioTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    # --------------------------------------- 1. worked instance: 12 total

    def test_instance_jia_gets_a_yi_gets_b_total_12(self):
        h = self.h
        h.expo()
        frozen = h.tenders.freeze_tender(h.req("close"), "EXPO")
        self.assertEqual(frozen["total_cents"], 12)
        self.assertEqual(frozen["labels"], ["J-A", "Y-B"])
        self.assertEqual(frozen["usage"], {"A": 1, "B": 1})
        winners = {w["buyer"]: w for w in frozen["winners"]}
        self.assertEqual(winners["jia"]["label"], "J-A")
        self.assertEqual(winners["jia"]["price_cents"], 7)
        self.assertEqual(winners["yi"]["label"], "Y-B")
        self.assertEqual([l["buyer"] for l in frozen["losers"]], ["bing"])

        settled = h.clearing.settle_tender(h.req("settle"), "EXPO")
        charged = {w["buyer"]: w["charged_cents"] for w in settled["winners"]}
        # Pay-as-bid: each winner pays their own package price, not a uniform one.
        self.assertEqual(charged, {"jia": 7, "yi": 5})
        # Guarantee remainders are released (9-2=7 for jia, 8-3=5 for yi).
        released = {w["buyer"]: w["released_cents"] for w in settled["winners"]}
        self.assertEqual(released, {"jia": 2, "yi": 3})
        self.assertEqual(h.margin.balance("jia")["charged_cents"], 7)
        self.assertEqual(h.margin.balance("yi")["charged_cents"], 5)
        # Loser's guarantee is fully released.
        self.assertEqual(h.margin.balance("bing")["available_cents"], 4)
        self.assertEqual(h.margin.balance("bing")["frozen_cents"], 0)
        assert_reconciled(h.db)

    # ----------------------------- 2. formal swap creates a label-broken tie

    def test_swap_to_single_option_ties_and_labels_decide(self):
        h = self.h
        h.deposit("jia", 18)  # old guarantee stays frozen until the swap commits
        h.deposit("yi", 5)
        h.deposit("bing", 4)
        h.create_tender("EXPO2", {"A": 1, "B": 1})
        first = h.submit("M-J", "EXPO2", "jia", OPTS_JIA)
        old_hold = first["hold_id"]
        h.submit("M-Y", "EXPO2", "yi", OPTS_YI)
        h.submit("M-B", "EXPO2", "bing", OPTS_BING)

        # Formal swap: a fresh guarantee bound to revision 2, then revise.
        g2 = h.guarantee("EXPO2", "jia", "M-J", 2, OPTS_JIA_SWAPPED)
        revised = h.tenders.revise_menu(
            h.req("rev"), "M-J", "jia", OPTS_JIA_SWAPPED, g2["hold_id"]
        )
        self.assertEqual(revised["revision"], 2)
        self.assertEqual(revised["replaced_hold_id"], old_hold)
        self.assertEqual(h.margin.get_hold(old_hold)["status"], "RELEASED")
        self.assertEqual(h.margin.get_hold(g2["hold_id"])["status"], "ACTIVE")

        frozen = h.tenders.freeze_tender(h.req("close"), "EXPO2")
        # {J-AB}=9 ties {C-A, Y-B}=9; the sorted label tuple decides:
        # ("C-A","Y-B") < ("J-AB",), so bing+yi win this instance's tie.
        self.assertEqual(frozen["total_cents"], 9)
        self.assertEqual(frozen["labels"], ["C-A", "Y-B"])
        self.assertEqual({w["buyer"] for w in frozen["winners"]}, {"bing", "yi"})

        settled = h.clearing.settle_tender(h.req("settle"), "EXPO2")
        charged = {w["buyer"]: w["charged_cents"] for w in settled["winners"]}
        self.assertEqual(charged, {"bing": 4, "yi": 5})
        self.assertEqual(h.margin.balance("jia")["charged_cents"], 0)
        self.assertEqual(h.margin.balance("jia")["available_cents"], 18)
        assert_reconciled(h.db)

    def test_retransmitted_swap_mints_no_second_voucher(self):
        h = self.h
        h.deposit("jia", 18)
        h.create_tender("EXPO2", {"A": 1, "B": 1})
        first = h.submit("M-J", "EXPO2", "jia", OPTS_JIA)
        old_hold = first["hold_id"]
        g2 = h.guarantee("EXPO2", "jia", "M-J", 2, OPTS_JIA_SWAPPED)

        fired = {"done": False}

        def injector(operation, request_id):
            if operation == "tender.revise_menu" and not fired["done"]:
                fired["done"] = True
                return True
            return False

        h.tenders.fault_injector = injector
        rid = h.req("rev")
        with self.assertRaises(ResponseLost):
            h.tenders.revise_menu(rid, "M-J", "jia", OPTS_JIA_SWAPPED, g2["hold_id"])
        h.tenders.fault_injector = None
        # The swap committed before the response was lost; the retry replays it.
        replayed = h.tenders.revise_menu(
            rid, "M-J", "jia", OPTS_JIA_SWAPPED, g2["hold_id"]
        )
        self.assertEqual(replayed["revision"], 2)
        self.assertEqual(replayed["hold_id"], g2["hold_id"])
        with h.db.read() as conn:
            releases = conn.execute(
                "SELECT COUNT(*) AS n FROM ledger_events "
                "WHERE hold_id=? AND kind='RELEASE'",
                (old_hold,),
            ).fetchone()["n"]
            jia_holds = conn.execute(
                "SELECT COUNT(*) AS n FROM holds WHERE buyer='jia'"
            ).fetchone()["n"]
        self.assertEqual(releases, 1)          # old guarantee released exactly once
        self.assertEqual(jia_holds, 2)         # no third voucher appeared
        self.assertEqual(h.margin.get_hold(old_hold)["status"], "RELEASED")
        self.assertEqual(h.margin.balance("jia")["frozen_cents"], 9)
        assert_reconciled(h.db)

    # ------------------------------------ 3. balance 6 cannot be guaranteed

    def test_balance_6_cannot_cover_two_option_menu(self):
        h = self.h
        h.deposit("jia", 6)   # menu max payment is 9
        h.deposit("yi", 5)
        h.deposit("bing", 4)
        h.create_tender("EXPO3", {"A": 1, "B": 1})

        audit = h.desk.preaudit_menu("EXPO3", "jia", "M-J", 1, OPTS_JIA)
        self.assertTrue(audit["menu_valid"])
        self.assertEqual(audit["required_cents"], 9)
        self.assertEqual(audit["available_cents"], 6)
        self.assertFalse(audit["coverable"])
        with self.assertRaises(InsufficientFunds):
            h.guarantee("EXPO3", "jia", "M-J", 1, OPTS_JIA)

        # bing and yi still close their books without jia.
        h.submit("M-Y", "EXPO3", "yi", OPTS_YI)
        h.submit("M-B", "EXPO3", "bing", OPTS_BING)
        frozen = h.tenders.freeze_tender(h.req("close"), "EXPO3")
        self.assertEqual(frozen["total_cents"], 9)
        self.assertEqual(frozen["labels"], ["C-A", "Y-B"])
        settled = h.clearing.settle_tender(h.req("settle"), "EXPO3")
        charged = {w["buyer"]: w["charged_cents"] for w in settled["winners"]}
        self.assertEqual(charged, {"bing": 4, "yi": 5})
        assert_reconciled(h.db)

    # -------------------------------------- 4. contract kinds stay separate

    def test_contract_kinds_are_not_interchangeable(self):
        h = self.h
        h.deposit("b1", 1_000)
        h.create_tender("T-K", {"A": 2})
        h.auctions.create_auction(h.req("new"), "a-old", 2)

        # Old-style single-model bid still works through the uniform-price path.
        unit_hold = h.margin.freeze(h.req("frz"), "b1", "a-old", 200)
        h.auctions.place_bid(
            h.req("bid"), "u-01", "a-old", "b1", 2, 100, unit_hold["hold_id"]
        )

        # A UNIT_AUCTION hold cannot back a tender menu.
        with self.assertRaises(DomainError) as ctx:
            h.tenders.submit_menu(
                h.req("sub"), "M-K", "T-K", "b1",
                [{"label": "K-A", "resources": {"A": 1}, "price_cents": 100}],
                unit_hold["hold_id"],
            )
        self.assertIn("contract kind mismatch", str(ctx.exception))

        # A PACKAGE_TENDER guarantee cannot back an old-style bid.
        h.deposit("b2", 1_000)
        g = h.desk.freeze_guarantee(
            h.req("gfr"), "T-K", "b2", "M-K2", 1,
            [{"label": "K2-A", "resources": {"A": 1}, "price_cents": 100}],
        )
        with self.assertRaises(DomainError) as ctx:
            h.auctions.place_bid(
                h.req("bid"), "u-02", "a-old", "b2", 1, 100, g["hold_id"]
            )
        self.assertIn("contract kind mismatch", str(ctx.exception))

        # Both versions settle side by side against the same warehouse.
        h.submit("M-K", "T-K", "b1",
                 [{"label": "K-A", "resources": {"A": 1}, "price_cents": 100}])
        h.auctions.freeze_auction(h.req("fz"), "a-old")
        h.tenders.freeze_tender(h.req("fz"), "T-K")
        h.clearing.settle(h.req("st"), "a-old")
        h.clearing.settle_tender(h.req("st"), "T-K")
        self.assertEqual(h.margin.balance("b1")["charged_cents"], 200 + 100)
        assert_reconciled(h.db)

    def test_cash_frozen_elsewhere_cannot_be_reused(self):
        h = self.h
        h.deposit("b1", 9)
        h.auctions.create_auction(h.req("new"), "a-old", 1)
        h.margin.freeze(h.req("frz"), "b1", "a-old", 9)  # all cash frozen elsewhere
        h.create_tender("T-R", {"A": 1})
        with self.assertRaises(InsufficientFunds):
            h.guarantee("T-R", "b1", "M-R", 1,
                        [{"label": "R-A", "resources": {"A": 1}, "price_cents": 9}])
        self.assertEqual(reconcile(h.db)["ok"], True)

    # -------------------------------------------- 5. preaudit and coverage

    def test_preaudit_and_coverage_views(self):
        h = self.h
        h.deposit("jia", 20)
        h.create_tender("T-C", {"A": 1, "B": 1})

        audit = h.desk.preaudit_menu("T-C", "jia", "M-J", 1, OPTS_JIA)
        self.assertTrue(audit["menu_valid"])
        self.assertTrue(audit["coverable"])
        self.assertEqual(audit["required_cents"], 9)

        bad = h.desk.preaudit_menu(
            "T-C", "jia", "M-J", 1,
            [{"label": "X", "resources": {"A": 5}, "price_cents": 3}],
        )
        self.assertFalse(bad["menu_valid"])
        self.assertTrue(bad["errors"])

        g = h.guarantee("T-C", "jia", "M-J", 1, OPTS_JIA)
        # Before submission the desk reports the binding but no menu options.
        cov = h.desk.guarantee_coverage(g["hold_id"])
        self.assertEqual(cov["menu_revision"], 1)
        self.assertEqual(cov["max_coverable_cents"], 9)
        self.assertIsNone(cov["options"])
        # A caller-supplied option list is evaluated per option.
        cov2 = h.desk.guarantee_coverage(
            g["hold_id"],
            options=[
                {"label": "ok", "resources": {"A": 1}, "price_cents": 9},
                {"label": "dear", "resources": {"A": 1}, "price_cents": 10},
            ],
        )
        self.assertEqual(
            {o["label"]: o["covered"] for o in cov2["options"]},
            {"ok": True, "dear": False},
        )
        # After submission the visa reports the menu's own options.
        h.tenders.submit_menu(h.req("sub"), "M-J", "T-C", "jia", OPTS_JIA, g["hold_id"])
        cov3 = h.desk.guarantee_coverage(g["hold_id"])
        self.assertEqual(cov3["options_source"], "menu")
        self.assertTrue(all(o["covered"] for o in cov3["options"]))

    # -------------------------------------- 6. failed swap keeps both halves

    def test_failed_swap_leaves_old_menu_and_guarantee_intact(self):
        h = self.h
        h.deposit("jia", 20)
        h.create_tender("T-F", {"A": 1, "B": 1})
        first = h.submit("M-J", "T-F", "jia", OPTS_JIA)
        old_hold = first["hold_id"]

        # A guarantee bound to a *different* menu cannot ride on this swap.
        other = h.guarantee("T-F", "jia", "M-OTHER", 1, OPTS_JIA_SWAPPED)
        with self.assertRaises(DomainError):
            h.tenders.revise_menu(
                h.req("rev"), "M-J", "jia", OPTS_JIA_SWAPPED, other["hold_id"]
            )
        # A made-up credential is rejected too.
        with self.assertRaises(DomainError):
            h.tenders.revise_menu(
                h.req("rev"), "M-J", "jia", OPTS_JIA_SWAPPED, "hold-ghost"
            )
        # Reusing the current guarantee is not a revision.
        with self.assertRaises(DomainError):
            h.tenders.revise_menu(
                h.req("rev"), "M-J", "jia", OPTS_JIA_SWAPPED, old_hold
            )

        menu = h.tenders.read_result("T-F")  # still open; inspect via db
        self.assertEqual(menu["status"], "OPEN")
        with h.db.read() as conn:
            row = conn.execute(
                "SELECT revision, hold_id, status FROM menus WHERE menu_id='M-J'"
            ).fetchone()
        self.assertEqual((row["revision"], row["hold_id"], row["status"]),
                         (1, old_hold, "ACTIVE"))
        self.assertEqual(h.margin.get_hold(old_hold)["status"], "ACTIVE")
        self.assertEqual(h.margin.balance("jia")["frozen_cents"], 9 + 9)
        assert_reconciled(h.db)

    def test_interrupted_swap_rolls_back_completely(self):
        h = self.h
        h.deposit("jia", 18)
        h.create_tender("T-I", {"A": 1, "B": 1})
        first = h.submit("M-J", "T-I", "jia", OPTS_JIA)
        old_hold = first["hold_id"]
        g2 = h.guarantee("T-I", "jia", "M-J", 2, OPTS_JIA_SWAPPED)

        # Crash the transaction halfway: the new options are installed but
        # the old guarantee release fails, so the whole swap must roll back.
        original = h.margin.release_in_tx

        def boom(*args, **kwargs):
            raise RuntimeError("simulated mid-transaction crash")

        h.margin.release_in_tx = boom
        with self.assertRaises(RuntimeError):
            h.tenders.revise_menu(
                h.req("rev"), "M-J", "jia", OPTS_JIA_SWAPPED, g2["hold_id"]
            )
        h.margin.release_in_tx = original

        with h.db.read() as conn:
            row = conn.execute(
                "SELECT revision, hold_id, status FROM menus WHERE menu_id='M-J'"
            ).fetchone()
        self.assertEqual((row["revision"], row["hold_id"], row["status"]),
                         (1, old_hold, "ACTIVE"))
        # Old guarantee untouched; the separately frozen new one survives and
        # can be reused for the retry.
        self.assertEqual(h.margin.get_hold(old_hold)["status"], "ACTIVE")
        self.assertEqual(h.margin.get_hold(g2["hold_id"])["status"], "ACTIVE")
        retried = h.tenders.revise_menu(
            h.req("rev2"), "M-J", "jia", OPTS_JIA_SWAPPED, g2["hold_id"]
        )
        self.assertEqual(retried["revision"], 2)
        self.assertEqual(h.margin.get_hold(old_hold)["status"], "RELEASED")
        assert_reconciled(h.db)

    # ------------------------------------- 7. two connections, real races

    def test_two_connections_race_for_last_balance(self):
        h = self.h
        h.deposit("b1", 9)
        h.create_tender("T-RACE", {"A": 1})
        results = []
        barrier = threading.Barrier(2)

        def worker(menu_id):
            barrier.wait()
            try:
                h.desk.freeze_guarantee(
                    f"race-{menu_id}", "T-RACE", "b1", menu_id, 1,
                    [{"label": f"L-{menu_id}", "resources": {"A": 1},
                      "price_cents": 9}],
                )
                results.append(("ok", menu_id))
            except DomainError as exc:
                results.append(("rejected", str(exc)))

        threads = [threading.Thread(target=worker, args=(f"M-{i}",))
                   for i in (1, 2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        outcomes = sorted(r[0] for r in results)
        self.assertEqual(outcomes, ["ok", "rejected"])
        self.assertEqual(h.margin.balance("b1")["frozen_cents"], 9)
        self.assertEqual(h.margin.balance("b1")["available_cents"], 0)
        assert_reconciled(h.db)

    # ------------------------------------- 8. cutoff only freezes good menus

    def test_guarantee_released_before_cutoff_excludes_the_menu(self):
        h = self.h
        h.expo()
        jia_hold = h.margin.balance("jia")["holds"][0]["hold_id"]
        h.margin.release(h.req("rel"), jia_hold)  # jia pulls the guarantee early

        frozen = h.tenders.freeze_tender(h.req("close"), "EXPO")
        self.assertEqual([x["menu_id"] for x in frozen["excluded"]], ["M-J"])
        self.assertEqual(frozen["total_cents"], 9)
        self.assertEqual(frozen["labels"], ["C-A", "Y-B"])

        settled = h.clearing.settle_tender(h.req("settle"), "EXPO")
        charged = {w["buyer"]: w["charged_cents"] for w in settled["winners"]}
        self.assertEqual(charged, {"bing": 4, "yi": 5})
        self.assertEqual(h.margin.balance("jia")["charged_cents"], 0)
        assert_reconciled(h.db)

    def test_cutoff_immutability_and_lost_settle_response(self):
        h = self.h
        h.expo()
        frozen = h.tenders.freeze_tender(h.req("close"), "EXPO")

        # No menu mutations after the deadline.
        with self.assertRaises(InvalidState):
            h.submit("M-LATE", "EXPO", "jia", OPTS_JIA)
        with self.assertRaises(InvalidState):
            h.tenders.withdraw_menu(h.req("wd"), "M-J")
        with self.assertRaises(InvalidState):
            h.tenders.revise_menu(h.req("rev"), "M-J", "jia", OPTS_JIA_SWAPPED, "any")

        # Re-reads always return the frozen result.
        again = h.tenders.freeze_tender(h.req("close2"), "EXPO")
        self.assertEqual(again["labels"], frozen["labels"])
        self.assertEqual(h.tenders.read_result("EXPO")["labels"], frozen["labels"])

        # A lost charge response is resumed, not doubled.
        fired = {"count": 0}

        def injector(operation, request_id):
            if operation == "margin.charge_capped" and fired["count"] == 0:
                fired["count"] += 1
                return True
            return False

        h.margin.fault_injector = injector
        rid = h.req("settle")
        with self.assertRaises(ResponseLost):
            h.clearing.settle_tender(rid, "EXPO")
        h.margin.fault_injector = None
        resumed = h.clearing.settle_tender(rid, "EXPO")
        self.assertEqual(resumed["total_cents"], 12)
        with h.db.read() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM ledger_events "
                "WHERE auction_id='EXPO' AND kind='CHARGE'"
            ).fetchone()["n"]
        self.assertEqual(n, 2)  # one per winner, never more
        dup = h.clearing.settle_tender(h.req("settle"), "EXPO")
        self.assertEqual(dup["total_cents"], 12)
        assert_reconciled(h.db)

    def test_withdraw_releases_guarantee_atomically(self):
        h = self.h
        h.deposit("jia", 9)
        h.create_tender("T-W", {"A": 1, "B": 1})
        first = h.submit("M-J", "T-W", "jia", OPTS_JIA)
        h.tenders.withdraw_menu(h.req("wd"), "M-J")
        self.assertEqual(h.margin.get_hold(first["hold_id"])["status"], "RELEASED")
        self.assertEqual(h.margin.balance("jia")["available_cents"], 9)
        frozen = h.tenders.freeze_tender(h.req("close"), "T-W")
        self.assertEqual(frozen["total_cents"], 0)
        self.assertEqual(frozen["winners"], [])
        h.clearing.settle_tender(h.req("settle"), "T-W")
        assert_reconciled(h.db)

    # ------------------------------------------------------- 9. hard limits

    def test_limits(self):
        h = self.h
        h.deposit("b1", 100_000)
        with self.assertRaises(LimitExceeded):
            h.create_tender("T-BIG", {f"R{i}": 1 for i in range(9)})
        with self.assertRaises(LimitExceeded):
            h.create_tender("T-MANY", {"A": 4})
        h.create_tender("T-L", {"A": 3, "B": 1})

        too_many_options = [
            {"label": f"o{i}", "resources": {"A": 1}, "price_cents": 1}
            for i in range(5)
        ]
        with self.assertRaises(DomainError):
            h.guarantee("T-L", "b1", "M-5", 1, too_many_options)
        with self.assertRaises(DomainError):  # asks more than the supply
            h.guarantee("T-L", "b1", "M-Q", 1,
                        [{"label": "q", "resources": {"A": 4}, "price_cents": 1}])
        with self.assertRaises(DomainError):  # unknown resource
            h.guarantee("T-L", "b1", "M-U", 1,
                        [{"label": "u", "resources": {"ZZ": 1}, "price_cents": 1}])
        with self.assertRaises(DomainError):  # non-integer price
            h.guarantee("T-L", "b1", "M-P", 1,
                        [{"label": "p", "resources": {"A": 1}, "price_cents": 1.5}])
        with self.assertRaises(DomainError):  # guarantee below the menu maximum
            h.guarantee("T-L", "b1", "M-G", 1, OPTS_JIA, amount=8)

        # 20 buyers may join; the 21st may not.
        for i in range(20):
            buyer = f"bk{i:02d}"
            h.deposit(buyer, 10)
            h.submit(f"M-{i:02d}", "T-L", buyer,
                     [{"label": f"L{i:02d}", "resources": {"B": 1}, "price_cents": 1}])
        h.deposit("bk20", 10)
        with self.assertRaises(LimitExceeded):
            h.submit("M-20", "T-L", "bk20",
                     [{"label": "L20", "resources": {"B": 1}, "price_cents": 1}])

        # Duplicate option labels across live menus are rejected.
        h.deposit("dup", 10)
        with self.assertRaises(DomainError):
            h.submit("M-DUP", "T-L", "dup",
                     [{"label": "L00", "resources": {"B": 1}, "price_cents": 1}])
        assert_reconciled(h.db)

    # ----------------------------- 10. solver cross-checked by brute force

    def test_solver_matches_brute_force_on_random_small_instances(self):
        rng = random.Random(20261121)
        for case in range(40):
            resources = {f"R{i}": rng.randint(1, 2) for i in range(rng.randint(2, 4))}
            names = sorted(resources)
            menus = []
            for b in range(rng.randint(2, 6)):
                options = []
                for o in range(rng.randint(1, 3)):
                    picked = rng.sample(names, rng.randint(1, len(names)))
                    options.append(
                        {
                            "label": f"c{case}b{b}o{o}",
                            "resources": {r: 1 for r in picked},
                            "price_cents": rng.randint(1, 30),
                        }
                    )
                menus.append({"menu_id": f"m{b}", "buyer": f"b{b}", "options": options})

            got = solve_package_tender(resources, menus)

            best_value, best_labels = 0, ()
            for combo in itertools.product(
                *[[None] + m["options"] for m in menus]
            ):
                usage = {r: 0 for r in names}
                total, labels = 0, []
                ok = True
                for opt in combo:
                    if opt is None:
                        continue
                    for r, q in opt["resources"].items():
                        usage[r] += q
                        if usage[r] > resources[r]:
                            ok = False
                            break
                    if not ok:
                        break
                    total += opt["price_cents"]
                    labels.append(opt["label"])
                if not ok:
                    continue
                key = tuple(sorted(labels))
                if total > best_value or (total == best_value and key < best_labels):
                    best_value, best_labels = total, key
            self.assertEqual(got["total_cents"], best_value, f"case {case}")
            self.assertEqual(tuple(got["labels"]), best_labels, f"case {case}")
            for r, used in got["usage"].items():
                self.assertLessEqual(used, resources[r])

    # ------------------------------ 11. scale: 8 resources, 20 buyers x 4

    def test_scale_twenty_buyers_eight_resources(self):
        h = self.h
        rng = random.Random(7)
        resources = {f"R{i}": 3 for i in range(1, 9)}
        h.create_tender("T-SCALE", resources)
        menus = {}
        for i in range(1, 21):
            buyer = f"bk{i:02d}"
            options = []
            for j in range(4):
                if i <= 8:
                    # Eight buyers all chase the scarce model R1 (3 units).
                    res = {"R1": 1}
                    if j % 2:
                        res[rng.choice(["R2", "R3"])] = 1
                else:
                    # The rest mostly take disjoint models.
                    res = {f"R{2 + (i - 9) % 7}": 1}
                    if j == 3:
                        res[f"R{2 + i % 7}"] = 1
                options.append(
                    {"label": f"L{i:02d}-{j}", "resources": res,
                     "price_cents": rng.randint(5, 60)}
                )
            menus[buyer] = options
            h.deposit(buyer, 250)
            h.submit(f"M-{i:02d}", "T-SCALE", buyer, options)

        frozen = h.tenders.freeze_tender(h.req("close"), "T-SCALE")
        for r, used in frozen["usage"].items():
            self.assertLessEqual(used, 3, r)
        r1_winners = [w for w in frozen["winners"] if "R1" in w["resources"]]
        self.assertLessEqual(len(r1_winners), 3)
        self.assertGreater(len(r1_winners), 0)  # the scarce model is contested
        self.assertEqual(
            len({w["buyer"] for w in frozen["winners"]}), len(frozen["winners"])
        )

        settled = h.clearing.settle_tender(h.req("settle"), "T-SCALE")
        for w in settled["winners"]:
            self.assertEqual(w["charged_cents"], w["price_cents"])
        total_charged = sum(w["charged_cents"] for w in settled["winners"])
        self.assertEqual(total_charged, frozen["total_cents"])
        for bal in h.clearing.all_balances():
            self.assertEqual(
                bal["deposited_cents"],
                bal["available_cents"] + bal["frozen_cents"] + bal["charged_cents"],
            )
        report = reconcile(h.db)
        self.assertTrue(report["ok"], report["violations"])

    # ------------------------------------------------- 12. tamper detection

    def test_reconcile_detects_a_deleted_tender_charge(self):
        h = self.h
        h.expo()
        h.tenders.freeze_tender(h.req("close"), "EXPO")
        h.clearing.settle_tender(h.req("settle"), "EXPO")
        self.assertTrue(reconcile(h.db)["ok"])
        with h.db.tx() as conn:
            conn.execute(
                "DELETE FROM ledger_events WHERE auction_id='EXPO' AND kind='CHARGE' "
                "AND buyer='jia'"
            )
        report = reconcile(h.db)
        self.assertFalse(report["ok"])
        self.assertTrue(any("EXPO" in v for v in report["violations"]))

    # ------------------------------------------------------- 13. statements

    def test_statements_cover_both_contract_kinds(self):
        h = self.h
        h.expo()
        h.tenders.freeze_tender(h.req("close"), "EXPO")
        h.clearing.settle_tender(h.req("settle"), "EXPO")

        st = h.clearing.tender_statement("EXPO")
        self.assertEqual(st["total_charged_cents"], 12)
        self.assertEqual(st["tender"]["settlement_status"], "CLEARED")

        acct = h.clearing.account_statement("jia")
        self.assertEqual(acct["derived"]["charged_cents"], 7)
        self.assertEqual(acct["derived"]["available_cents"], 2)

        explain = h.tenders.explain_tender("EXPO")
        self.assertEqual(explain["total_cents"], 12)
        self.assertEqual([e["action"] for e in explain["menu_events"]],
                         ["SUBMIT", "SUBMIT", "SUBMIT"])
        assert_reconciled(h.db)


if __name__ == "__main__":
    unittest.main(verbosity=2)
