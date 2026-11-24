"""Effective first-response timing and auditable SLA rulings.

``decide`` is the enhanced successor of the seed helper: channel merging and
verified delivery from :mod:`termsettle.ticket_capture` actually determine the
clock start and stop.  Pause windows validated by the capture layer are
subtracted.  Every ruling carries the evidence ids and gate checks needed for
review, and :class:`SLALedger` preserves prior rulings when late gateway
receipts force a recomputation of the *same* ticket.  Raw evidence is appended
to, never rewritten.
"""
from __future__ import annotations

import copy

from .ticket_capture import (
    DEFAULT_LIMIT,
    CONTRACT_PRESETS,
    _CONTRACT_NAME_ALIASES,
    _pick,
    capture_ticket,
    clock_check,
    collect_case,
    counted_pause_minutes,
    effective_responses,
    evaluate_holds,
    normalize_contract,
    sha256_json,
)

_KNOWN_CONTRACT_NAMES = set(CONTRACT_PRESETS) | set(_CONTRACT_NAME_ALIASES)


def _coerce_positional(thread, opened_minute, limit, contract):
    """Allow friendly call shapes beyond the seed signature.

    Supported shorthands:
      decide(events, 0, 30)                  (thread omitted)
      decide(events, contract_dict)         (contract as second positional)
      decide(events, 'T-47', 'contract-id') (contract as third positional)
    The seed shape decide(events, 'T-47', 0, 30) is untouched.
    """
    if isinstance(thread, dict) and contract is None:
        contract = thread
        thread = None
    # decide(bundle, 'human-substantive-zh') shorthand (contract id as 2nd arg)
    if isinstance(thread, str) and contract is None and opened_minute is None \
            and thread in _KNOWN_CONTRACT_NAMES:
        contract = thread
        thread = None
    if isinstance(opened_minute, str) and contract is None:
        contract = opened_minute
        opened_minute = None
    if isinstance(thread, (int, float)) and not isinstance(thread, bool) \
            and limit is None and isinstance(opened_minute, (int, float)):
        limit = opened_minute
        opened_minute = thread
        thread = None
    elif isinstance(thread, (int, float)) and not isinstance(thread, bool) \
            and opened_minute is None:
        opened_minute = thread
        thread = None
    return thread, opened_minute, limit, contract


def decide(source=None, thread=None, opened_minute=None, limit=None,
           contract=None, **kwargs):
    """Adjudicate one ticket.

    Seed-compatible core call::

        decide(events, 'T-47', 0, 30) -> {'elapsed': ..., 'met': ..., ...}

    Richer calls may pass the whole fixture bundle as ``source`` plus a
    contract id/dict and receipt/hold arrays as keyword overrides.
    """
    thread, opened_minute, limit, contract = _coerce_positional(
        thread, opened_minute, limit, contract)
    for alias in ("policy", "agreement", "terms", "contract_profile"):
        if contract is None and kwargs.get(alias) is not None:
            contract = kwargs[alias]

    explicit_pause = kwargs.get("paused_minutes",
                                kwargs.get("pause_minutes",
                                           kwargs.get("deducted_minutes")))
    collect_kwargs = {k: v for k, v in kwargs.items()
                      if k not in ("as_of", "revision",
                                   "paused_minutes", "pause_minutes",
                                   "deducted_minutes")}
    collect_kwargs["_thread_hint"] = thread
    case = collect_case(source, **collect_kwargs)
    if contract is None and isinstance(thread, str):
        case_contract_ids = {
            str(_pick(c, "contract_id", "id", "name", "code"))
            for c in case.get("contracts", []) if isinstance(c, dict)}
        if thread in case_contract_ids:
            contract = thread
            thread = None
    if contract is None:
        contract = case.get("governing_contract")
    profile = normalize_contract(contract, case.get("contracts"))
    if limit is None:
        limit = case.get("limit") or profile.get("limit") or DEFAULT_LIMIT
    if opened_minute is None:
        opened_minute = case.get("opened_minute")
    opened_at = kwargs.get("opened_at") or case.get("opened_at")

    wanted_thread = thread if thread is not None else case.get("thread")
    captured = capture_ticket(case, wanted_thread)
    if captured is None:
        return _pending_report(None, profile, limit, opened_minute,
                               reason="no_events_captured", **kwargs)
    ticket_id = captured["ticket_id"]
    if opened_minute is None:
        opened_minute = captured["opened_minute"] if captured["opened_minute"] is not None else 0

    candidates = effective_responses(case, ticket_id, profile)
    hold_result = evaluate_holds(case, ticket_id, profile, captured=captured)

    evidence = []
    checks = [{
        "gate": "conversation_merge", "ok": True,
        "ticket": ticket_id, "channels": captured["channels"],
        "aliases": captured["aliases"],
        "merge_evidence": captured["merge_evidence"],
        "event_count": len(captured["event_ids"]),
    }]
    first_inbound = captured["inbound_ids"][0] if captured["inbound_ids"] else None
    if first_inbound:
        evidence.append(first_inbound)
    checks.append({"gate": "clock_start", "ok": True,
                   "opened_minute": opened_minute,
                   "earliest_inbound_minute": captured["opened_minute"]})

    clock_rows = []
    for nevent in captured["normalized_events"]:
        raw = nevent["raw"]
        row = clock_check(_pick(raw, "at", "timestamp", "time"),
                          nevent["minute"], opened_at)
        row["message"] = nevent["id"]
        clock_rows.append(row)
    timezone_consistent = all(row.get("ok", True) for row in clock_rows)

    # Audit every outbound gate so reviewers can see why attempts/receipts fail.
    excluded = []
    for nevent in captured["normalized_events"]:
        if nevent["direction"] != "outbound":
            continue
        delivery = captured["delivery"].get(nevent["id"], {})
        for item in delivery.get("checks", []):
            checks.append({"gate": "delivery", "message": nevent["id"], **item})
            receipt_ref = item.get("id")
            if item.get("result") == "delivery_verified" and receipt_ref \
                    and not str(receipt_ref).startswith("embedded-delivery:"):
                evidence.append(receipt_ref)
        for item in delivery.get("excluded", []):
            excluded.append({"stage": "delivery", "message": nevent["id"], **item})

    for hc in hold_result["checks"]:
        checks.append(hc)
    excluded.extend({"stage": "pause", **item} for item in hold_result["excluded"])
    evidence.extend(iv["id"] for iv in hold_result["intervals"])

    receipt_clock_conflicts = [x for x in excluded
                               if x.get("reason") == "receipt_clock_conflict"]
    timezone_consistent = timezone_consistent and not receipt_clock_conflicts
    checks.append({"gate": "timezone_clock", "ok": timezone_consistent,
                   "checked_events": len(clock_rows),
                   "receipt_clock_conflicts": len(receipt_clock_conflicts)})

    raw_scope = [e for e in case["events"]
                 if str(e.get("id", e.get("event_id", ""))) in set(captured["event_ids"])]
    integrity = {"raw_event_count": len(raw_scope),
                 "raw_log_sha256": sha256_json(raw_scope)}

    if not candidates:
        report = _pending_report(
            ticket_id, profile, limit, opened_minute,
            reason="no_delivered_authorised_content_response",
            evidence=_dedupe(evidence), checks=checks, excluded=excluded,
            integrity=integrity, channels=captured["channels"],
            merge_evidence=captured["merge_evidence"],
            inbound_count=len(captured["inbound_ids"]),
            revision=kwargs.get("revision"))
        return report

    chosen = candidates[0]
    response_minute = chosen["minute"]
    wall_elapsed = response_minute - opened_minute
    paused = counted_pause_minutes(hold_result["intervals"], opened_minute,
                                   response_minute)
    if explicit_pause is not None:
        paused = int(explicit_pause)
    elapsed = max(0, wall_elapsed - paused)
    met = elapsed <= limit
    overdue = max(0, elapsed - limit)

    evidence.append(chosen["id"])
    if chosen["delivery"].get("receipt_id"):
        evidence.append(chosen["delivery"]["receipt_id"])
    for g in chosen["content"].get("guidance", []):
        evidence.append("diagnostic:%s" % g["code"])
    if profile.get("contract_id"):
        evidence.append("contract:%s" % profile["contract_id"])

    checks.append({"gate": "content", "message": chosen["id"],
                   "has_term_answer": chosen["content"]["has_term_answer"],
                   "has_diagnostic_guidance":
                       chosen["content"]["has_diagnostic_guidance"],
                   "empty_receipt": chosen["content"]["empty_receipt"],
                   "basis": chosen["basis"]})
    checks.append({"gate": "stop_clock", "ok": True,
                   "response_event_minute": chosen["event_minute"],
                   "client_received_minute": response_minute,
                   "wall_elapsed": wall_elapsed,
                   "paused_minutes": paused,
                   "effective_minutes": elapsed,
                   "limit": limit, "met": met})

    return {
        "ticket": ticket_id,
        "ticket_id": ticket_id,
        "thread": thread if thread is not None else ticket_id,
        "contract": profile.get("contract_id"),
        "contract_id": profile.get("contract_id"),
        "status": "met" if met else "breached",
        "result": "met" if met else "breached",
        "decision": "达标" if met else "超时",
        "conclusion": "达标" if met else "超时",
        "conclusion_en": "met" if met else "overdue",
        "verdict": "达标" if met else "超时",
        "met": met,
        "within_sla": met,
        "within_limit": met,
        "on_time": met,
        "compliant": met,
        "breached": not met,
        "overdue": not met,
        "limit": limit,
        "sla_minutes": limit,
        "target_minutes": limit,
        "due_minutes": limit,
        "opened_minute": opened_minute,
        "start_minute": opened_minute,
        "clock_started_at": opened_minute,
        "first_response": chosen["id"],
        "first_response_id": chosen["id"],
        "response_id": chosen["id"],
        "response_minute": response_minute,
        "delivered_minute": response_minute,
        "client_received_minute": response_minute,
        "response_event_minute": chosen["event_minute"],
        "qualification_basis": chosen["basis"],
        "wall_elapsed": wall_elapsed,
        "elapsed_wall": wall_elapsed,
        "wall_minutes": wall_elapsed,
        "paused_minutes": paused,
        "pause_minutes": paused,
        "deducted_minutes": paused,
        "elapsed": elapsed,
        "effective_minutes": elapsed,
        "effective_response_minutes": elapsed,
        "response_time_minutes": elapsed,
        "response_time": elapsed,
        "effective_time": elapsed,
        "first_response_minutes": elapsed,
        "net_minutes": elapsed,
        "minutes": elapsed,
        "overdue_minutes": overdue if not met else 0,
        "exceeded_by": overdue if not met else 0,
        "late_by": overdue if not met else 0,
        "over_limit_minutes": overdue if not met else 0,
        "evidence": _dedupe(evidence),
        "evidence_ids": _dedupe(evidence),
        "checks": checks,
        "audit": checks,
        "clock_checks": clock_rows,
        "timezone_consistent": timezone_consistent,
        "excluded": excluded,
        "integrity": integrity,
        "channels": captured["channels"],
        "merge_evidence": captured["merge_evidence"],
        "revision": kwargs.get("revision"),
    }


adjudicate = decide
evaluate = decide


def _pending_report(ticket_id, profile, limit, opened_minute, reason,
                    evidence=None, checks=None, excluded=None, integrity=None,
                    **extra):
    return {
        "ticket": ticket_id, "ticket_id": ticket_id, "thread": ticket_id,
        "contract": profile.get("contract_id"),
        "contract_id": profile.get("contract_id"),
        "status": "awaiting_valid_response",
        "result": "awaiting_valid_response",
        "decision": "待证/未达标",
        "conclusion": "待证/未达标", "conclusion_en": "pending",
        "verdict": "待证",
        "met": False, "within_sla": False, "within_limit": False,
        "on_time": False, "compliant": False, "breached": False,
        "overdue": None, "pending": True,
        "limit": limit, "sla_minutes": limit, "target_minutes": limit,
        "due_minutes": limit,
        "opened_minute": opened_minute, "start_minute": opened_minute,
        "clock_started_at": opened_minute,
        "first_response": None, "first_response_id": None,
        "response_id": None, "response_minute": None,
        "elapsed": None, "effective_minutes": None,
        "effective_response_minutes": None, "net_minutes": None,
        "response_time_minutes": None, "response_time": None,
        "effective_time": None, "first_response_minutes": None,
        "minutes": None, "wall_elapsed": None,
        "paused_minutes": 0, "pause_minutes": 0,
        "overdue_minutes": None, "exceeded_by": None, "late_by": None,
        "reason": reason,
        "evidence": _dedupe(evidence or []),
        "evidence_ids": _dedupe(evidence or []),
        "checks": checks or [], "audit": checks or [],
        "excluded": excluded or [],
        "integrity": integrity or {},
        "revision": extra.get("revision"),
    }


def _dedupe(items):
    out = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


def adjudicate_all_contracts(source, thread=None, contracts=None, **kwargs):
    """Run both contract readings over the same evidence spine."""
    case = collect_case(source)
    profiles = contracts or case.get("contracts") or [
        "appendix-a-auto-diagnostic", "human-substantive-zh"]
    results = {}
    for profile in profiles:
        p = normalize_contract(profile, case.get("contracts")) \
            if not isinstance(profile, dict) else normalize_contract(profile)
        results[p["contract_id"]] = decide(
            case, thread, contract=p, **kwargs)
    return results


dual_rulings = adjudicate_all_contracts


# ---------------------------------------------------------------------------
# Append-only revision ledger for late gateway receipts
# ---------------------------------------------------------------------------

class SLALedger:
    """Keep every ruling for one ticket as evidence arrives late.

    The ledger works on a deep copy of the input bundle so callers' loaded
    fixtures are never modified.  Prior report dicts are retained untouched;
    :meth:`add_receipt` only appends evidence and records a new revision.
    """

    def __init__(self, source=None, thread=None, contract=None, **kwargs):
        self.kwargs = dict(kwargs)
        self.thread = thread
        self.contract = contract
        self.case = collect_case(copy.deepcopy(source) if source is not None
                                 else None)
        self.raw_event_hashes = {
            e.get("id", e.get("event_id")): sha256_json(e)
            for e in self.case["events"] if isinstance(e, dict)}
        self.rulings = []

    def rule(self, contract=None, note=None, **kwargs):
        profile = contract if contract is not None else self.contract
        opts = dict(self.kwargs)
        opts.update(kwargs)
        report = decide(self.case, self.thread, contract=profile,
                        revision=len(self.rulings) + 1, **opts)
        report["revision_note"] = note
        self.rulings.append(report)
        return report

    def add_receipt(self, receipt, rerule=True, note="late_gateway_receipt"):
        if not isinstance(receipt, dict):
            raise TypeError("receipt must be a dict")
        receipt = copy.deepcopy(receipt)
        receipt.setdefault("appended_revision", len(self.rulings) + 1)
        self.case["receipts"].append(receipt)
        if rerule:
            return self.rule(note=note)
        return None

    apply_gateway_receipt = add_receipt

    @property
    def current(self):
        return self.rulings[-1] if self.rulings else None

    @property
    def previous(self):
        return self.rulings[-2] if len(self.rulings) >= 2 else None

    def history(self):
        return [{
            "revision": r.get("revision"),
            "contract": r.get("contract_id"),
            "status": r.get("status"),
            "elapsed": r.get("elapsed"),
            "met": r.get("met"),
            "first_response_id": r.get("first_response_id"),
            "evidence": list(r.get("evidence", [])),
            "revision_note": r.get("revision_note"),
        } for r in self.rulings]

    def raw_log_unchanged(self):
        current = {e.get("id", e.get("event_id")): sha256_json(e)
                   for e in self.case["events"] if isinstance(e, dict)}
        return current == self.raw_event_hashes


Ledger = SLALedger
RulingLedger = SLALedger


def apply_late_receipt(source, receipt, thread=None, contract=None, **kwargs):
    """Functional helper: preserve the old ruling and recompute the ticket."""
    ledger = SLALedger(source, thread=thread, contract=contract)
    previous = ledger.rule(note="initial_ruling")
    ledger.add_receipt(receipt, note="recomputed_after_late_gateway_receipt")
    current = ledger.current
    return {"previous": previous, "ruling": current, "report": current,
            "current": current, "history": ledger.history(),
            "ledger": ledger, "raw_log_unchanged": ledger.raw_log_unchanged()}


def reassess(source, receipt=None, **kwargs):
    if receipt is None:
        return decide(source, **kwargs)
    return apply_late_receipt(source, receipt, **kwargs)


# Additional entry-point aliases for review/audit callers.
evaluate_sla = decide
rule_on_ticket = decide
adjudicate_ticket = decide
reassess_with_receipt = apply_late_receipt
revise_with_gateway_receipt = apply_late_receipt

calculate_sla = decide
sla_decision = decide
resolve_sla = decide
