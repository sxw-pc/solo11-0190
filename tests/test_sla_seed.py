"""Auditable SLA first-response tests for T-47 and regression cases.

Covers:
* two contract-grounded readings of the original T-47 three events;
* email/portal conversation merging without restarting the clock;
* same-customer / different-dispute non-merging;
* send attempts, invalid/forged receipts and wrong-delivery retries;
* customer-signed, actually-received, contract-allowed pause windows and the
  exact 30-minute boundary;
* late gateway receipts preserving old rulings while recomputing the ticket;
* timezone/clock normalisation and content (not kind) adjudication.
"""
import copy
import json
import unittest
from pathlib import Path

from termsettle.ticket_capture import (
    assess_content,
    clock_check,
    counted_pause_minutes,
    first_response,
    merge_conversations,
    verify_delivery,
)
from termsettle.sla_decision import (
    SLALedger,
    adjudicate_all_contracts,
    apply_late_receipt,
    decide,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "service-thread.json"
DATA = json.loads(FIXTURE.read_text(encoding="utf-8"))
AUTO_CONTRACT = "appendix-a-auto-diagnostic"
HUMAN_CONTRACT = "human-substantive-zh"


def scenario(name):
    return next(s for s in DATA["scenarios"] if s["name"] == name)


def boundary_case(answer_minute, pause=(10, 21), request_minute=8,
                  request_received=9, answer_received=None, hold=None,
                  contract=HUMAN_CONTRACT):
    """Minimal auditable case for pause boundary arithmetic."""
    answer_received = answer_minute if answer_received is None else answer_received
    events = [
        {"id": "in", "thread": "TB", "direction": "inbound", "minute": 0,
         "kind": "client", "customer": "C-B"},
        {"id": "req", "thread": "TB", "direction": "outbound",
         "minute": request_minute, "kind": "supplement-request",
         "sender": "R1", "author_type": "human", "authorized": True,
         "body": "请补充上下文材料。"},
        {"id": "ans", "thread": "TB", "direction": "outbound",
         "minute": answer_minute, "kind": "substantive",
         "sender": "R1", "author_type": "human", "authorized": True,
         "term_answer": {"term_key": "t", "translation": "译名"},
         "body": "标准译法：译名。"},
    ]
    receipts = [
        {"id": "rcpt-req", "message_id": "req", "type": "client_inbox",
         "status": "received", "minute": request_received, "verified": True},
        {"id": "rcpt-ans", "message_id": "ans", "type": "client_inbox",
         "status": "received", "minute": answer_received, "verified": True},
    ]
    if hold is None:
        hold = {"id": "hold-b", "ticket": "TB", "request_message_id": "req",
                "start": pause[0], "end": pause[1], "signed": True,
                "signed_by": "C-B", "signature_verified": True}
    return {"thread": "TB", "opened_minute": 0, "limit": 30,
            "contract": contract, "events": events,
            "delivery_receipts": receipts, "holds": [hold]}


class OriginalTerminologyFixtureTests(unittest.TestCase):
    """The seed T-47 spine must remain the fixed 0/2/45 three events."""

    def test_original_three_events_unchanged(self):
        self.assertEqual(DATA["thread"], "T-47")
        spine = [(e["id"], e["direction"], e["minute"], e["kind"])
                 for e in DATA["events"]]
        self.assertEqual(spine, [
            ("in-email", "inbound", 0, "client"),
            ("auto", "outbound", 2, "bot-receipt"),
            ("human", "outbound", 45, "substantive"),
        ])

    def test_seed_imports_and_core_keys_still_exist(self):
        events = DATA["events"]
        self.assertEqual(first_response.__module__, "termsettle.ticket_capture")
        report = decide(DATA, contract=HUMAN_CONTRACT)
        self.assertIn("elapsed", report)
        self.assertIn("met", report)
        self.assertEqual(set(["elapsed", "met"]).issubset(report), True)
        # The raw primitive still exposes the earliest outbound event.
        from termsettle.ticket_capture import earliest_outbound
        self.assertEqual(earliest_outbound(events, "T-47")["id"], "auto")

    def test_optionless_first_response_keeps_seed_raw_contract(self):
        # Backwards-compatible raw mode: exactly the seed's earliest outbound.
        bare = [
            {"id": "in", "thread": "T", "direction": "inbound", "minute": 0},
            {"id": "a", "thread": "T", "direction": "outbound", "minute": 2},
            {"id": "b", "thread": "T", "direction": "outbound", "minute": 9},
        ]
        self.assertEqual(first_response(bare, "T")["id"], "a")
        # Explicit effective mode with no delivery evidence selects nothing.
        self.assertIsNone(first_response(bare, "T", verified=True))

    def test_bot_receipt_looks_like_response_but_contract_decides(self):
        # The seed name captures the trap: raw earliest outbound is "auto".
        self.assertEqual(
            first_response(DATA["events"], "T-47")["id"], "auto")
        # It only stops the clock when delivery+content+contract all agree.
        self.assertEqual(decide(DATA, contract=AUTO_CONTRACT)["elapsed"], 2)
        strict = decide(DATA["events"], "T-47", 0, 30, HUMAN_CONTRACT)
        self.assertEqual(strict["first_response_id"], "human")
        self.assertFalse(strict["met"])


class DualContractT47Tests(unittest.TestCase):
    def test_route_a_preapproved_diagnostic_meets_sla_at_2(self):
        report = decide(DATA, contract=AUTO_CONTRACT)
        self.assertEqual(report["first_response_id"], "auto")
        self.assertEqual(report["elapsed"], 2)
        self.assertTrue(report["met"])
        self.assertEqual(report["status"], "met")
        self.assertEqual(report["overdue_minutes"], 0)
        self.assertEqual(report["qualification_basis"],
                         "approved_auto_diagnostic")
        self.assertIn("rcpt-auto-client", report["evidence"])
        self.assertIn("contract:appendix-a-auto-diagnostic", report["evidence"])
        self.assertTrue(any("DG-API-SAFETY-01" in e for e in report["evidence"]))

    def test_route_b_human_answer_is_45_and_overdue_by_15(self):
        report = decide(DATA, contract=HUMAN_CONTRACT)
        self.assertEqual(report["first_response_id"], "human")
        self.assertEqual(report["elapsed"], 45)
        self.assertFalse(report["met"])
        self.assertEqual(report["status"], "breached")
        self.assertEqual(report["overdue_minutes"], 15)
        self.assertEqual(report["wall_elapsed"], 45)
        self.assertEqual(report["paused_minutes"], 0)

    def test_bundle_default_governing_contract_is_human_route(self):
        report = decide(DATA)
        self.assertEqual(report["contract_id"], HUMAN_CONTRACT)
        self.assertEqual(report["first_response_id"], "human")
        self.assertEqual(report["elapsed"], 45)
        self.assertFalse(report["met"])

    def test_two_contract_readings_of_same_three_events(self):
        rulings = adjudicate_all_contracts(DATA)
        self.assertEqual(set(rulings), {AUTO_CONTRACT, HUMAN_CONTRACT})
        self.assertEqual(
            (rulings[AUTO_CONTRACT]["elapsed"], rulings[AUTO_CONTRACT]["met"]),
            (2, True))
        self.assertEqual(
            (rulings[HUMAN_CONTRACT]["elapsed"],
             rulings[HUMAN_CONTRACT]["met"],
             rulings[HUMAN_CONTRACT]["overdue_minutes"]),
            (45, False, 15))

    def test_first_response_entrypoint_is_contract_driven(self):
        self.assertEqual(
            first_response(DATA, "T-47", contract=AUTO_CONTRACT)["id"], "auto")
        self.assertEqual(
            first_response(DATA, "T-47", contract=HUMAN_CONTRACT)["id"], "human")

    def test_friendly_positional_signatures(self):
        self.assertEqual(decide(DATA, AUTO_CONTRACT)["elapsed"], 2)
        self.assertEqual(
            decide(DATA, "T-47", HUMAN_CONTRACT)["elapsed"], 45)
        self.assertEqual(
            decide(DATA["events"], "T-47", 0, 30,
                   HUMAN_CONTRACT)["elapsed"], 45)
        self.assertEqual(
            first_response(DATA, AUTO_CONTRACT)["id"], "auto")

    def test_events_list_with_catalog_and_question(self):
        # Seed-style list input still works when content catalog is supplied.
        auto = first_response(
            DATA["events"], "T-47", contract=AUTO_CONTRACT,
            catalog=DATA["diagnostic_catalog"], question=DATA["question"])
        human = first_response(
            DATA["events"], "T-47", contract=HUMAN_CONTRACT,
            question=DATA["question"])
        self.assertEqual(auto["id"], "auto")
        self.assertEqual(human["id"], "human")

    def test_client_inbox_delivery_and_timezone_checked_for_both_routes(self):
        from termsettle.ticket_capture import capture_ticket
        captured = capture_ticket(DATA, "T-47")
        auto_delivery = captured["delivery"]["auto"]
        self.assertTrue(auto_delivery["delivered"])
        self.assertTrue(auto_delivery["client_inbox_verified"])
        self.assertEqual(auto_delivery["minute"], 2)
        for contract in (AUTO_CONTRACT, HUMAN_CONTRACT):
            report = decide(DATA, contract=contract)
            self.assertTrue(report["timezone_consistent"])
            self.assertTrue(report["clock_checks"])
            self.assertTrue(all(c["ok"] for c in report["clock_checks"]))
            delivery_gates = [c for c in report["checks"]
                              if c.get("gate") == "delivery"
                              and c.get("id") == "rcpt-auto-client"]
            self.assertTrue(delivery_gates)
            self.assertTrue(delivery_gates[0].get("client_side"))

    def test_kind_relabel_cannot_flip_ruling(self):
        # A kind whitelist would flip when labels change; content/contract does not.
        relabelled = copy.deepcopy(DATA)
        for event in relabelled["events"]:
            if event["id"] == "auto":
                event["kind"] = "substantive"  # tempt a whitelist
            if event["id"] == "human":
                event["kind"] = "bot-receipt"  # tempt a blacklist
        auto_route = decide(relabelled, contract=AUTO_CONTRACT)
        human_route = decide(relabelled, contract=HUMAN_CONTRACT)
        self.assertEqual(auto_route["first_response_id"], "auto")
        self.assertEqual(auto_route["elapsed"], 2)
        self.assertEqual(human_route["first_response_id"], "human")
        self.assertEqual(human_route["elapsed"], 45)

    def test_gateway_detail_confirms_no_concrete_translation_in_auto(self):
        gw = next(r for r in DATA["delivery_receipts"]
                  if r["id"] == "gw-auto-detail")
        self.assertFalse(gw["term_decision_present"])
        self.assertFalse(gw["substantive_translation_content"])
        verdict = assess_content(
            DATA["events"][1], question=DATA["question"],
            contract={"auto_diagnostic_counts": False},
            catalog=DATA["diagnostic_catalog"])
        self.assertTrue(verdict["has_diagnostic_guidance"])
        self.assertFalse(verdict["has_term_answer"])


class ConversationMergeTests(unittest.TestCase):
    def test_portal_repost_keeps_email_clock(self):
        sc = scenario("portal-repost-merge")
        groups = merge_conversations(sc)
        self.assertEqual(len(groups), 1)
        ticket = groups[0]
        self.assertEqual(ticket["ticket_id"], "T-61")
        self.assertEqual(ticket["opened_minute"], 0)
        self.assertEqual(set(ticket["channels"]), {"email", "portal"})
        self.assertEqual(len(ticket["inbound_ids"]), 2)
        report = decide(sc)
        self.assertEqual(report["first_response_id"], "s1-reply")
        self.assertEqual(report["response_minute"], 17)  # delivery, not send 16
        self.assertEqual(report["elapsed"], 17)
        self.assertTrue(report["met"])
        # Naive per-channel portal clock would have started at minute 4.
        self.assertEqual(sc["expected"]["naive_portal_elapsed"], 13)
        self.assertNotEqual(report["elapsed"], 13)

    def test_different_channel_labels_still_merge_on_dispute_key(self):
        events = [
            {"id": "e1", "thread": "email-thread-1", "direction": "inbound",
             "minute": 0, "channel": "email", "customer": "C",
             "dispute_key": "D-X", "body": "q1"},
            {"id": "e2", "thread": "portal-session-9", "direction": "inbound",
             "minute": 5, "channel": "portal", "customer": "C",
             "dispute_key": "D-X", "body": "q1 repost"},
        ]
        groups = merge_conversations(events)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["opened_minute"], 0)

    def test_same_customer_different_disputes_do_not_merge(self):
        s1 = scenario("portal-repost-merge")
        s2 = scenario("same-customer-other-dispute")
        groups = merge_conversations({"events": s1["events"] + s2["events"]})
        by_id = {g["ticket_id"]: g for g in groups}
        self.assertIn("T-61", by_id)
        self.assertIn("T-62", by_id)
        self.assertEqual(by_id["T-61"]["opened_minute"], 0)
        self.assertEqual(by_id["T-62"]["opened_minute"], 10)
        # The keyless same-customer orphan must not attach to T-61.
        t61_ids = set(by_id["T-61"]["event_ids"])
        self.assertNotIn("s2-orphan", t61_ids)
        report_62 = decide(s2)
        self.assertEqual(report_62["elapsed"], 12)
        self.assertTrue(report_62["met"])

    def test_customer_identity_alone_never_merges(self):
        events = [
            {"id": "a", "thread": "A", "direction": "inbound", "minute": 0,
             "customer": "C-9", "body": "question one"},
            {"id": "b", "thread": "B", "direction": "inbound", "minute": 3,
             "customer": "C-9", "body": "unrelated question two"},
        ]
        groups = merge_conversations(events)
        self.assertEqual(len(groups), 2)
        self.assertEqual({g["ticket_id"] for g in groups}, {"A", "B"})


class DeliveryAndInvalidReceiptTests(unittest.TestCase):
    def test_wrong_delivery_retry_and_rejected_receipts(self):
        sc = scenario("wrong-delivery-retry")
        report = decide(sc)
        self.assertEqual(report["first_response_id"], "s3-retry")
        self.assertEqual(report["response_minute"], 10)
        self.assertEqual(report["elapsed"], 10)
        self.assertTrue(report["met"])
        excluded_ids = {x.get("id") for x in report["excluded"]}
        self.assertIn("s3-rcpt-forged", excluded_ids)
        self.assertIn("s3-rcpt-bounce1", excluded_ids)
        reasons = {x.get("id"): (x.get("reasons") or x.get("reason"))
                   for x in report["excluded"]}
        self.assertEqual(reasons["s3-rcpt-forged"],
                         "receipt_failed_authenticity_check")
        self.assertEqual(reasons["s3-rcpt-bounce1"],
                         "delivery_failed_or_bounced")
        # The first attempt must never be selected.
        self.assertNotEqual(report["first_response_id"], "s3-attempt1")

    def test_gateway_acceptance_alone_does_not_stop_clock(self):
        events = [
            {"id": "in", "thread": "T", "direction": "inbound", "minute": 0},
            {"id": "out", "thread": "T", "direction": "outbound", "minute": 2,
             "sender": "R1", "author_type": "human", "authorized": True,
             "term_answer": {"term_key": "t", "translation": "x"},
             "body": "标准译法：x。"},
        ]
        verdict = verify_delivery(
            events[1], [{"id": "gw", "message_id": "out", "status": "accepted",
                         "minute": 2}])
        self.assertFalse(verdict["delivered"])
        self.assertTrue(verdict["attempt_only"])
        report = decide({"events": events, "delivery_receipts": [
            {"id": "gw", "message_id": "out", "status": "accepted",
             "minute": 2}]}, "T", 0, 30)
        self.assertIsNone(report["first_response_id"])
        self.assertFalse(report["met"])
        self.assertEqual(report["status"], "awaiting_valid_response")

    def test_forged_favorable_receipt_is_ignored(self):
        verdict = verify_delivery(
            {"id": "out", "recipient": "client@example"},
            [{"id": "bad", "message_id": "out", "type": "client_inbox",
              "status": "received", "minute": 1, "verified": False}])
        self.assertFalse(verdict["delivered"])
        self.assertTrue(any(x["reason"] == "receipt_failed_authenticity_check"
                            for x in verdict["excluded"]))

    def test_invalid_clock_receipt_rejected(self):
        bad = clock_check("2026-11-24T10:99:00+08:00", 2,
                          "2026-11-24T09:00:00+08:00")
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["status"], "unparseable_timestamp")
        conflict = clock_check("2026-11-24T11:40:00+08:00", 2,
                               "2026-11-24T09:00:00+08:00")
        self.assertEqual(conflict["status"], "clock_conflict")
        self.assertFalse(conflict["ok"])


class PauseBoundaryTests(unittest.TestCase):
    def test_fixture_valid_signed_pause_crosses_30(self):
        sc = scenario("pause-valid-boundary")
        report = decide(sc)
        self.assertEqual(report["wall_elapsed"], 48)
        self.assertEqual(report["paused_minutes"], 20)
        self.assertEqual(report["elapsed"], 28)
        self.assertTrue(report["met"])
        self.assertEqual(report["overdue_minutes"], 0)
        pause_gates = [c for c in report["checks"] if c.get("gate") == "pause"]
        self.assertTrue(pause_gates and all(c["ok"] for c in pause_gates))

    def test_exact_30_is_met_and_31_is_breached(self):
        met_report = decide(boundary_case(41))   # 41 - 11 = 30
        late_report = decide(boundary_case(42))  # 42 - 11 = 31
        self.assertEqual(met_report["paused_minutes"], 11)
        self.assertEqual(met_report["elapsed"], 30)
        self.assertTrue(met_report["met"])  # <= limit inclusive
        self.assertEqual(late_report["elapsed"], 31)
        self.assertFalse(late_report["met"])
        self.assertEqual(late_report["overdue_minutes"], 1)

    def test_pause_after_response_is_clamped(self):
        case = boundary_case(40, pause=(25, 60))
        report = decide(case)
        # Only [25, 40) can count: 15 minutes, not 35.
        self.assertEqual(report["paused_minutes"], 15)
        self.assertEqual(report["elapsed"], 25)
        self.assertTrue(report["met"])

    def test_pause_before_supplement_receipt_clamped_to_receipt(self):
        # Signed window says [5,25) but the request is not received until 9.
        case = boundary_case(40, pause=(5, 25), request_received=9)
        report = decide(case)
        self.assertEqual(report["paused_minutes"], 16)  # [9,25)
        self.assertEqual(report["elapsed"], 24)

    def test_counted_pause_helper_union_and_bounds(self):
        intervals = [{"start": 10, "end": 20}, {"start": 18, "end": 25}]
        self.assertEqual(counted_pause_minutes(intervals, 0, 40), 15)
        self.assertEqual(counted_pause_minutes(
            [{"start": -5, "end": 10}], 0, 40), 10)

    def test_invalid_pause_fixtures_do_not_stop_clock_deduction(self):
        for name in ("pause-unsigned", "pause-request-not-received",
                     "pause-contract-forbidden"):
            with self.subTest(name=name):
                report = decide(scenario(name))
                self.assertEqual(report["paused_minutes"], 0)
                self.assertEqual(report["elapsed"], 40)
                self.assertFalse(report["met"])
                self.assertEqual(report["overdue_minutes"], 10)
                self.assertTrue(any(c.get("gate") == "pause" and not c["ok"]
                                    for c in report["checks"]))


class LateGatewayReceiptRevisionTests(unittest.TestCase):
    def setUp(self):
        late = DATA["late_receipt_revision"]
        self.late_receipt = copy.deepcopy(late["late_receipt"])
        self.bundle = {k: copy.deepcopy(v) for k, v in late.items()
                       if k not in ("late_receipt", "expected_initial",
                                    "expected_revised", "name", "title")}
        self.bundle["delivery_receipts"] = copy.deepcopy(late["initial_receipts"])
        self.contract = late["contract"]

    def test_late_receipt_preserves_old_ruling_and_recomputes_same_ticket(self):
        original_receipt_count = len(self.bundle["delivery_receipts"])
        outcome = apply_late_receipt(
            self.bundle, self.late_receipt, thread="T-66",
            contract=self.contract)

        old, new = outcome["previous"], outcome["ruling"]
        self.assertEqual((old["revision"], old["first_response_id"],
                          old["elapsed"], old["met"]),
                         (1, "s8-human", 45, False))
        self.assertEqual((new["revision"], new["first_response_id"],
                          new["elapsed"], new["met"]),
                         (2, "s8-auto", 2, True))
        self.assertEqual(old["ticket_id"], new["ticket_id"])
        # Old ruling object is preserved untouched.
        self.assertFalse(outcome["history"][0]["met"])
        self.assertTrue(outcome["history"][1]["met"])
        self.assertEqual(len(outcome["ledger"].rulings), 2)
        # Raw event log is never rewritten and caller bundle is not mutated.
        self.assertTrue(outcome["raw_log_unchanged"])
        self.assertEqual(len(self.bundle["delivery_receipts"]),
                         original_receipt_count)

    def test_ledger_class_workflow(self):
        ledger = SLALedger(self.bundle, thread="T-66", contract=self.contract)
        first = ledger.rule()
        self.assertEqual(first["elapsed"], 45)
        self.assertFalse(first["met"])
        ledger.add_receipt(self.late_receipt)
        self.assertEqual(ledger.current["elapsed"], 2)
        self.assertTrue(ledger.current["met"])
        self.assertEqual(ledger.previous["elapsed"], 45)
        self.assertTrue(ledger.raw_log_unchanged())

    def test_late_delivery_receipt_does_not_change_human_contract(self):
        revised_evidence = copy.deepcopy(self.bundle)
        revised_evidence["delivery_receipts"].append(self.late_receipt)
        report = decide(revised_evidence, "T-66",
                        contract=HUMAN_CONTRACT)
        self.assertEqual(report["first_response_id"], "s8-human")
        self.assertEqual(report["elapsed"], 45)
        self.assertFalse(report["met"])
        self.assertEqual(report["overdue_minutes"], 15)

    def test_late_receipt_ingestion_time_differs_from_delivery_minute(self):
        # Ingested at minute 58 but proves delivery at minute 2.
        self.assertEqual(self.late_receipt["minute"], 2)
        self.assertEqual(self.late_receipt["ingested_minute"], 58)
        report = apply_late_receipt(
            self.bundle, self.late_receipt, thread="T-66",
            contract=self.contract)["ruling"]
        self.assertEqual(report["response_minute"], 2)


class TimezoneClockTests(unittest.TestCase):
    OPENED = "2026-11-24T09:00:00+08:00"

    def test_utc_gateway_and_local_agree_on_minute_2(self):
        utc = clock_check("2026-11-24T01:02:00Z", 2, self.OPENED)
        local = clock_check("2026-11-24T09:02:00+08:00", 2, self.OPENED)
        self.assertTrue(utc["ok"])
        self.assertTrue(local["ok"])
        self.assertEqual(utc["normalized_minute"], 2)
        self.assertEqual(local["normalized_minute"], 2)

    def test_naive_timestamp_is_flagged(self):
        result = clock_check("2026-11-24T09:02:00", 2, self.OPENED)
        self.assertEqual(result["status"], "timezone_unverified")
        self.assertFalse(result["ok"])


class FixtureExpectationMatrixTests(unittest.TestCase):
    """Every scenario's embedded expected block must match recomputation."""

    def test_all_scenario_expected_blocks(self):
        for sc in DATA["scenarios"]:
            with self.subTest(scenario=sc["name"]):
                report = decide(sc)
                expected = sc["expected"]
                for key in ("elapsed", "met", "paused_minutes",
                            "wall_elapsed", "overdue_minutes",
                            "first_response_id", "response_minute"):
                    if key in expected:
                        self.assertEqual(
                            report[key], expected[key],
                            msg="%s[%s]: %r != %r" % (
                                sc["name"], key, report[key], expected[key]))


if __name__ == "__main__":
    unittest.main()
