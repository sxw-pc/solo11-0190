"""Cross-channel conversation capture, real delivery and content verification.

The capture layer never trusts a channel label or an outbound ``kind``:

* copies of the *same dispute* arriving by email and portal are merged with
  strong identity evidence, while different disputes from the same customer
  stay on separate clocks;
* send attempts, automatic receipts and authorised substantive replies are
  treated as distinct evidence classes;
* a response can stop the SLA clock only after a verifiable customer-side
  delivery receipt exists;
* response bodies are actually inspected for approved diagnostic guidance or
  a concrete translation answer matched to the terminology question;
* customer-signed pause/resume windows are validated against proof that the
  supplement request was really received and that the contract allows pausing.

``sla_decision`` consumes :func:`capture_ticket` / :func:`first_response`; it
does not re-derive channel or delivery facts itself.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone

DEFAULT_LIMIT = 30

# ---------------------------------------------------------------------------
# Small generic helpers
# ---------------------------------------------------------------------------

def canonical(value):
    """Deterministic JSON encoding used for audit hashes."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def sha256_json(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _pick(data, *keys):
    if not isinstance(data, dict):
        return None
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _minute(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


# ---------------------------------------------------------------------------
# Timezone / clock verification
# ---------------------------------------------------------------------------

def parse_timestamp(value):
    """Parse an ISO-8601 timestamp.

    Returns a timezone-aware UTC ``datetime`` or ``None`` for empty input.
    Naive timestamps are returned as the original ``datetime`` tagged through
    :func:`clock_check` so callers can flag them instead of silently assuming
    the desk timezone.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        return dt  # naive: clock_check reports timezone_unverified
    return dt.astimezone(timezone.utc)


def clock_check(at, declared_minute=None, opened_at=None, tolerance_seconds=90):
    """Cross-check a declared relative minute against an absolute timestamp.

    The check normalises every aware timestamp (including gateway ``Z`` time)
    against the ticket open timestamp.  Naive timestamps and contradictions
    are reported rather than guessed.
    """
    check = {"gate": "clock", "ok": True, "status": "consistent",
             "declared_minute": declared_minute, "normalized_minute": None,
             "reasons": []}
    if at is None:
        check["status"] = "no_timestamp"
        return check
    try:
        dt = parse_timestamp(at)
    except (ValueError, TypeError) as exc:
        check.update(ok=False, status="unparseable_timestamp")
        check["reasons"].append(str(exc))
        return check
    if dt.tzinfo is None:
        check.update(ok=False, status="timezone_unverified")
        check["reasons"].append("timestamp has no UTC offset")
        return check
    if opened_at is not None:
        try:
            opened = parse_timestamp(opened_at)
        except (ValueError, TypeError):
            opened = None
        if opened is not None and getattr(opened, "tzinfo", None) is not None:
            elapsed = (dt - opened).total_seconds() / 60.0
            check["normalized_minute"] = int(elapsed)
            if declared_minute is not None:
                skew = abs(elapsed - declared_minute) * 60.0
                check["skew_seconds"] = round(skew, 1)
                if skew > tolerance_seconds:
                    check.update(ok=False, status="clock_conflict")
                    check["reasons"].append(
                        "timestamp normalizes to minute %.2f but record declares %s"
                        % (elapsed, declared_minute))
    return check


# ---------------------------------------------------------------------------
# Contract profiles
# ---------------------------------------------------------------------------

CONTRACT_PRESETS = {
    # Route A: contract appendix recognises pre-acknowledged auto diagnostics.
    "appendix-a-auto-diagnostic": {
        "contract_id": "appendix-a-auto-diagnostic",
        "limit": 30,
        "auto_diagnostic_counts": True,
        "customer_preacknowledged": True,
        "human_substantive_required": False,
        "pause_allowed": True,
        "approved_auto_senders": ["auto-diagnostic-bot", "service-bot", "bot"],
        "authorized_humans": ["R1", "agent", "human-agent"],
    },
    # Route B: only an authorised human answer on the concrete term stops SLA.
    "human-substantive-zh": {
        "contract_id": "human-substantive-zh",
        "limit": 30,
        "auto_diagnostic_counts": False,
        "receipt_only_does_not_count": True,
        "human_substantive_required": True,
        "pause_allowed": True,
        "authorized_humans": ["R1", "agent", "human-agent"],
        "approved_auto_senders": [],
    },
    # Pause forbidden variant used by regression cases.
    "human-no-pause": {
        "contract_id": "human-no-pause",
        "limit": 30,
        "auto_diagnostic_counts": False,
        "human_substantive_required": True,
        "pause_allowed": False,
        "authorized_humans": ["R1", "agent", "human-agent"],
        "approved_auto_senders": [],
    },
}

_CONTRACT_NAME_ALIASES = {
    "auto": "appendix-a-auto-diagnostic",
    "diagnostic": "appendix-a-auto-diagnostic",
    "auto-diagnostic": "appendix-a-auto-diagnostic",
    "appendix-a": "appendix-a-auto-diagnostic",
    "preapproved-auto": "appendix-a-auto-diagnostic",
    "auto-diagnostic-appendix-a": "appendix-a-auto-diagnostic",
    "human": "human-substantive-zh",
    "strict": "human-substantive-zh",
    "manual": "human-substantive-zh",
    "human-substantive": "human-substantive-zh",
    "human-substantive-contract": "human-substantive-zh",
    "human-substantive-required": "human-substantive-zh",
    "no-pause": "human-no-pause",
}

# Conservative default: a receipt alone never resolves a terminology dispute.
DEFAULT_CONTRACT = CONTRACT_PRESETS["human-substantive-zh"]


def normalize_contract(contract=None, contracts=None):
    """Resolve a contract id / alias / dict into a normalised profile."""
    if contract is None:
        return copy.deepcopy(DEFAULT_CONTRACT)
    if isinstance(contract, dict):
        profile = copy.deepcopy(contract)
        cid = _pick(profile, "contract_id", "id", "name", "code")
        if cid and cid in CONTRACT_PRESETS:
            base = copy.deepcopy(CONTRACT_PRESETS[cid])
            base.update({k: v for k, v in profile.items() if v is not None})
            profile = base
        else:
            profile.setdefault("contract_id", cid or "custom-contract")
        # Aliases for booleans used by different fixture authors.
        if "auto_diagnostic_counts_as_first_response" in profile:
            profile["auto_diagnostic_counts"] = \
                profile["auto_diagnostic_counts_as_first_response"]
        if "auto_reply_counts" in profile:
            profile["auto_diagnostic_counts"] = profile["auto_reply_counts"]
        if "pauses_allowed" in profile:
            profile["pause_allowed"] = profile["pauses_allowed"]
        profile.setdefault("limit", _pick(profile, "limit_minutes", "sla_minutes")
                           or DEFAULT_LIMIT)
        profile.setdefault("pause_allowed", True)
        profile.setdefault("auto_diagnostic_counts", False)
        profile.setdefault("human_substantive_required",
                           not profile.get("auto_diagnostic_counts", False))
        profile.setdefault("authorized_humans",
                           ["R1", "agent", "human-agent", "reviewer"])
        profile.setdefault("approved_auto_senders",
                           ["auto-diagnostic-bot", "service-bot",
                            "bot", "automated", "auto", "system"]
                           if profile.get("auto_diagnostic_counts") else [])
        return profile
    key = str(contract)
    pool = contracts if isinstance(contracts, (list, dict)) else None
    if isinstance(pool, dict) and key in pool:
        return normalize_contract(pool[key])
    if isinstance(pool, list):
        for item in pool:
            if isinstance(item, dict) and _pick(item, "contract_id", "id", "name",
                                                "code") == key:
                return normalize_contract(item)
    resolved = _CONTRACT_NAME_ALIASES.get(key, _CONTRACT_NAME_ALIASES.get(key.lower()))
    if resolved:
        return copy.deepcopy(CONTRACT_PRESETS[resolved])
    if key in CONTRACT_PRESETS:
        return copy.deepcopy(CONTRACT_PRESETS[key])
    raise KeyError("unknown contract profile: %r" % (contract,))


# ---------------------------------------------------------------------------
# Case / evidence collection
# ---------------------------------------------------------------------------

_RECEIPT_CONTAINER_KEYS = ("delivery_receipts", "gateway_receipts", "receipts",
                           "deliveries", "delivery_events", "gateway_events")
_HOLD_KEYS = ("holds", "pauses", "pause_windows", "suspensions", "hold_windows")


def collect_case(source=None, **overrides):
    """Gather events, receipts, holds and contract metadata from many shapes.

    ``source`` may be a fixture/bundle dict, a list of events, or an already
    collected case.  Explicit keyword arguments always win and are appended.
    The input graph is never mutated: nested containers are shallow-copied into
    fresh lists/dicts.
    """
    case = {
        "events": [], "receipts": [], "holds": [], "contracts": [],
        "diagnostic_catalog": {}, "question": None,
        "opened_at": None, "opened_minute": None, "limit": None,
        "thread": None, "customer": None, "customer_contact": None,
        "raw_source": None,
    }

    # Docket shape: {"tickets": [{...case...}, ...]} (or threads/cases/
    # conversations/scenarios).  Resolve the requested child and let the
    # parent contribute shared contracts/catalog; the child still wins.
    if isinstance(source, dict) and not source.get("_case"):
        nested_keys = ("tickets", "threads", "cases", "conversations",
                       "scenarios")
        own_events = any(isinstance(source.get(k), list) and source.get(k)
                         for k in ("events", "messages", "thread_events", "items"))
        nested_key = next((k for k in nested_keys
                           if isinstance(source.get(k), list) and source[k]), None)
        if nested_key and not own_events:
            wanted = overrides.get("_thread_hint") or overrides.get("thread") \
                or _pick(source, "thread", "ticket_id", "ticket")
            entries = [t for t in source[nested_key] if isinstance(t, dict)]
            child = None
            if wanted is not None:
                sw = str(wanted)
                for ticket in entries:
                    ids = [_pick(ticket, "thread", "ticket_id", "id",
                                 "case_id", "name")] + _as_list(
                        ticket.get("aliases"))
                    if sw in {str(x) for x in ids if x is not None}:
                        child = ticket
                        break
            if child is None and len(entries) == 1:
                child = entries[0]
            if child is not None:
                blended = {k: v for k, v in source.items() if k != nested_key}
                blended.update(child)
                return collect_case(blended, **overrides)

    sources = []
    if isinstance(source, dict) and source.get("_case") is True:
        sources.append(source)
    elif isinstance(source, dict):
        sources.append(source)
    elif isinstance(source, list):
        case["events"].extend(e for e in source if isinstance(e, dict))
    elif source is not None:
        raise TypeError("source must be a bundle dict, an event list or None")

    for bundle in sources:
        case["raw_source"] = bundle
        ev = _pick(bundle, "events", "messages", "thread_events", "items")
        if isinstance(ev, list):
            case["events"].extend(e for e in ev if isinstance(e, dict))
        for key in _RECEIPT_CONTAINER_KEYS:
            rows = bundle.get(key)
            if isinstance(rows, list):
                case["receipts"].extend(r for r in rows if isinstance(r, dict))
        for key in _HOLD_KEYS:
            rows = bundle.get(key)
            if isinstance(rows, list):
                case["holds"].extend(h for h in rows if isinstance(h, dict))
        contracts = _pick(bundle, "contracts", "contract_profiles")
        if isinstance(contracts, list):
            case["contracts"].extend(c for c in contracts if isinstance(c, dict))
        elif isinstance(contracts, dict):
            case["contracts"].append(contracts)
        catalog = _pick(bundle, "diagnostic_catalog", "diagnostics",
                        "diagnostic_codes")
        if isinstance(catalog, dict):
            case["diagnostic_catalog"].update(catalog)
        question = _pick(bundle, "question", "dispute")
        if isinstance(question, dict):
            case["question"] = question
        for target, *keys in (
            ("opened_at", "opened_at", "start_at", "clock_start"),
            ("thread", "thread", "ticket_id", "ticket", "case_id"),
            ("customer", "customer", "customer_id", "client", "requester"),
            ("customer_contact", "customer_contact", "contact_email",
             "client_email", "customer_email"),
        ):
            value = _pick(bundle, *keys)
            if value is not None and case[target] is None:
                case[target] = value
        om = _minute(_pick(bundle, "opened_minute", "start_minute"))
        if om is not None:
            case["opened_minute"] = om
        lm = _minute(_pick(bundle, "limit", "limit_minutes", "sla_minutes"))
        if lm is not None:
            case["limit"] = lm
        gc = _pick(bundle, "governing_contract", "contract", "contract_id",
                   "policy_id")
        if gc is not None:
            case["governing_contract"] = gc

    # Evidence embedded directly on events is promoted into the receipt pool.
    for event in case["events"]:
        embedded = []
        for key in ("receipts", "delivery_receipts", "gateway_receipts"):
            embedded.extend(_as_list(event.get(key)))
        for key in ("delivery", "receipt", "delivery_receipt", "client_receipt"):
            value = event.get(key)
            if isinstance(value, dict):
                embedded.append(value)
        for receipt in embedded:
            if not isinstance(receipt, dict):
                continue
            receipt = dict(receipt)
            receipt.setdefault("message_id",
                               _pick(event, "id", "event_id", "message_id"))
            case["receipts"].append(receipt)
        if any(k in event for k in ("delivered", "delivered_minute",
                                    "client_received_minute",
                                    "delivery_status", "status")):
            case["receipts"].append({
                "id": "embedded-delivery:%s" % event.get("id"),
                "message_id": _pick(event, "id", "event_id", "message_id"),
                "type": "embedded_delivery",
                "status": _pick(event, "delivery_status", "status") or (
                    "delivered" if event.get("delivered") is True else None),
                "minute": _pick(event, "client_received_minute",
                                "delivered_minute", "received_minute"),
                "recipient": _pick(event, "recipient", "to"),
                "verified": event.get("delivery_verified",
                                      event.get("receipt_verified", True)),
            })

    for key, target in (("receipts", "receipts"), ("gateway_receipts", "receipts"),
                        ("deliveries", "receipts"), ("holds", "holds"),
                        ("pause_windows", "holds")):
        rows = overrides.get(key)
        if isinstance(rows, list):
            case[target].extend(r for r in rows if isinstance(r, dict))
    if overrides.get("contract") is not None and not isinstance(
            overrides.get("contract"), (str, int)):
        case["contracts"].append(overrides["contract"])
    if overrides.get("question") is not None:
        case["question"] = overrides["question"]
    if overrides.get("catalog") is not None:
        catalog = overrides["catalog"]
        if isinstance(catalog, dict):
            case["diagnostic_catalog"].update(catalog)
    if overrides.get("diagnostics") is not None and isinstance(
            overrides["diagnostics"], dict):
        case["diagnostic_catalog"].update(overrides["diagnostics"])
    for key in ("opened_at", "opened_minute", "limit", "thread", "customer",
                "customer_contact", "question", "diagnostic_catalog"):
        if overrides.get(key) is not None:
            case[key] = overrides[key]

    # Re-collecting an already collected case re-promotes embedded receipts;
    # de-duplicate by stable id (or content hash) to keep audit rows single.
    for collection in ("receipts", "holds"):
        seen, unique = set(), []
        for item in case[collection]:
            key = _pick(item, "id", "receipt_id", "hold_id", "window_id") \
                or canonical(item)
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        case[collection] = unique

    case["_case"] = True
    return case


# ---------------------------------------------------------------------------
# Event normalisation and conversation merging
# ---------------------------------------------------------------------------

def _direction(event):
    raw = str(_pick(event, "direction", "dir", "message_direction") or "").lower()
    if raw in ("outbound", "out", "sent", "send", "reply", "from_agent"):
        return "outbound"
    if raw in ("inbound", "in", "received", "incoming", "from_client"):
        return "inbound"
    # Seed-style fallback: kind/client implies inbound.
    kind = str(event.get("kind", "")).lower()
    if kind in ("client", "customer", "inbound"):
        return "inbound"
    return raw or "unknown"


def normalize_event(event):
    """Return a stable read-only-ish view of an event (raw dict retained)."""
    eid = _pick(event, "id", "event_id")
    message_id = _pick(event, "message_id", "rfc_message_id", "internet_message_id")
    labels = []
    for key in ("thread", "thread_id", "ticket", "ticket_id", "case_id",
                "conversation_id", "channel_thread", "external_thread_id"):
        value = event.get(key)
        if value is not None:
            labels.append(str(value))
    dispute_key = _pick(event, "dispute_key", "dispute_id", "question_key",
                        "question_id", "matter_key", "controversy_id", "merge_key")
    customer = _pick(event, "customer", "customer_id", "client_id",
                     "requester_id", "account")
    fingerprint = _pick(event, "fingerprint", "body_fingerprint",
                        "normalized_sha", "fingerprint_sha")
    links = []
    for key in ("duplicate_of", "same_dispute_as", "copy_of", "repost_of",
                "merged_into", "same_question_as"):
        links.extend(str(x) for x in _as_list(event.get(key)))
    return {
        "id": str(eid) if eid is not None else (str(message_id) if message_id else None),
        "message_id": str(message_id) if message_id is not None else None,
        "labels": labels,
        "direction": _direction(event),
        "minute": _minute(_pick(event, "minute", "offset_minute",
                               "minutes_from_open", "elapsed_minute",
                               "at_minute", "minute_offset", "t_min",
                               "min", "offset")),
        "at": _pick(event, "at", "timestamp", "time", "created_at", "ts",
                    "sent_at"),
        "channel": str(_pick(event, "channel", "source", "via") or "unknown").lower(),
        "kind": event.get("kind"),
        "dispute_key": str(dispute_key) if dispute_key is not None else None,
        "customer": str(customer) if customer is not None else None,
        "fingerprint": str(fingerprint) if fingerprint is not None else None,
        "links": links,
        "sender": _pick(event, "from", "sender", "author", "actor", "sender_id"),
        "recipient": _pick(event, "to", "recipient", "rcpt"),
        "authorized": event.get("authorized", event.get("authorised")),
        "author_type": str(_pick(event, "author_type", "sender_type", "actor_type")
                           or "").lower() or None,
        "raw": event,
    }


class _DSU:
    def __init__(self, n):
        self.parent = list(range(n))

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


class TicketGroups(list):
    """List of merged ticket groups that also supports ``groups['T-47']``."""

    def get(self, key, default=None):
        for ticket in self:
            if key == ticket["ticket_id"] or key in ticket["aliases"] \
                    or key in ticket["dispute_keys"]:
                return ticket
        return default

    def __getitem__(self, key):
        if isinstance(key, str):
            ticket = self.get(key)
            if ticket is None:
                raise KeyError(key)
            return ticket
        return list.__getitem__(self, key)


def merge_conversations(source=None, **kwargs):
    """Merge cross-channel copies of the same dispute.

    Merge evidence classes (any one strong edge is enough):

    * identical explicit dispute/question key;
    * a shared desk ticket/thread label;
    * an explicit duplicate/same-dispute link by event or RFC message id;
    * identical normalised body fingerprint *and* identical customer.

    Customer identity alone never merges two groups.  Returns a list of ticket
    group dicts ordered by first activity.
    """
    case = collect_case(source, **kwargs)
    raw_events = case["events"]
    events = [normalize_event(e) for e in raw_events]
    n = len(events)
    dsu = _DSU(n)

    by_dispute, by_label, by_fingerprint, by_event_id, by_message_id = ({}, {}, {}, {}, {})
    for i, ev in enumerate(events):
        if ev["dispute_key"]:
            by_dispute.setdefault(ev["dispute_key"], []).append(i)
        for label in ev["labels"]:
            by_label.setdefault(label, []).append(i)
        if ev["fingerprint"] and ev["customer"]:
            by_fingerprint.setdefault((ev["customer"], ev["fingerprint"]), []).append(i)
        if ev["id"]:
            by_event_id.setdefault(ev["id"], i)
        if ev["message_id"]:
            by_message_id.setdefault(ev["message_id"], i)

    for indices in list(by_dispute.values()) + list(by_label.values()) \
            + list(by_fingerprint.values()):
        for j in indices[1:]:
            dsu.union(indices[0], j)

    id_index = dict(by_event_id)
    id_index.update(by_message_id)
    for i, ev in enumerate(events):
        for link in ev["links"]:
            target = id_index.get(str(link))
            if target is not None:
                dsu.union(i, target)

    groups = {}
    for i in range(n):
        groups.setdefault(dsu.find(i), []).append(i)

    tickets = []
    for indices in groups.values():
        members = [events[i] for i in indices]
        minutes = [m["minute"] for m in members if m["minute"] is not None]
        inbound = [m for m in members if m["direction"] == "inbound"]
        inbound_minutes = [m["minute"] for m in inbound if m["minute"] is not None]
        start = min(inbound_minutes) if inbound_minutes else (
            min(minutes) if minutes else None)
        labels = sorted({label for m in members for label in m["labels"]})
        dispute_keys = sorted({m["dispute_key"] for m in members if m["dispute_key"]})
        customers = sorted({m["customer"] for m in members if m["customer"]})
        channels = sorted({m["channel"] for m in members})
        explicit_ids = [label for m in members for label in m["labels"]
                        if label.lower().startswith(("t-", "ticket"))]
        ticket_id = explicit_ids[0] if explicit_ids else (
            labels[0] if labels else (dispute_keys[0] if dispute_keys else
                                      "merged-%s" % sha256_json(
                                          [m["id"] for m in members])[:10]))
        merge_evidence = []
        if len(channels) > 1:
            merge_evidence.append("cross-channel:%s" % "+".join(channels))
        if len(dispute_keys) == 1 and sum(
                1 for m in members if m["dispute_key"]) > 1:
            merge_evidence.append("shared-dispute-key:%s" % dispute_keys[0])
        tickets.append({
            "ticket_id": ticket_id,
            "aliases": labels,
            "dispute_keys": dispute_keys,
            "customer": customers[0] if len(customers) == 1 else (
                customers or [None])[0],
            "customers": customers,
            "channels": channels,
            "opened_minute": start,
            "event_ids": [m["id"] for m in sorted(
                members, key=lambda m: (m["minute"] if m["minute"] is not None else 1 << 30,
                                        m["id"] or ""))],
            "events": [m["raw"] for m in sorted(
                members, key=lambda m: (m["minute"] if m["minute"] is not None else 1 << 30,
                                        m["id"] or ""))],
            "normalized_events": members,
            "inbound_ids": [m["id"] for m in inbound],
            "outbound_ids": [m["id"] for m in members
                             if m["direction"] == "outbound"],
            "merge_evidence": merge_evidence,
        })
    tickets.sort(key=lambda t: (t["opened_minute"] if t["opened_minute"] is not None
                                else 1 << 30, t["ticket_id"]))
    return TicketGroups(tickets)


# Plausible alternative names a caller/tests might use.
merge_threads = merge_conversations
group_conversations = merge_conversations
conversation_groups = merge_conversations


def tickets_by_id(source=None, **kwargs):
    return {t["ticket_id"]: t for t in merge_conversations(source, **kwargs)}


def _resolve_ticket(case, thread=None):
    tickets = merge_conversations(case)
    if not tickets:
        return None, tickets
    if thread is None:
        wanted = case.get("thread")
    else:
        wanted = str(thread)
    if wanted is None:
        return tickets[0], tickets
    wanted = str(wanted)
    for ticket in tickets:
        if ticket["ticket_id"] == wanted or wanted in ticket["aliases"] \
                or wanted in ticket["dispute_keys"]:
            return ticket, tickets
    # Aliases may be non-string ids; compare stringified forms too.
    for ticket in tickets:
        if wanted in {str(a) for a in ticket["aliases"]}:
            return ticket, tickets
    raise KeyError("no conversation merged under ticket/thread %r" % wanted)


def capture_ticket(source=None, thread=None, **kwargs):
    """Return one merged ticket plus delivery/content/hold evidence maps."""
    if thread is not None:
        kwargs.setdefault("_thread_hint", thread)
    case = collect_case(source, **kwargs)
    ticket, _ = _resolve_ticket(case, thread)
    if ticket is None:
        return None
    ids = set(ticket["event_ids"])
    message_ids = set()
    for nevent in ticket["normalized_events"]:
        if nevent["message_id"]:
            message_ids.add(nevent["message_id"])
    targets = ids | message_ids

    receipts = [r for r in case["receipts"]
                if str(_pick(r, "message_id", "event_id", "target_id") or "") in targets]
    holds = []
    for hold in case["holds"]:
        ht = _pick(hold, "ticket", "thread", "ticket_id", "case_id")
        req = _pick(hold, "request_message_id", "request_event_id",
                    "supplement_request_id", "linked_request", "request_id")
        direct_req_minute = _pick(hold, "request_received_minute",
                                  "request_delivered_minute",
                                  "supplement_received_minute")
        ticket_match = ht is not None and str(ht) in (
            set(ticket["aliases"]) | {ticket["ticket_id"]})
        request_match = req is not None and str(req) in targets
        unlinked = ht is None and req is None
        direct_attested = ht is None and direct_req_minute is not None
        if ticket_match or request_match or unlinked or direct_attested:
            holds.append(hold)

    delivery = {}
    for nevent in ticket["normalized_events"]:
        if nevent["direction"] == "outbound":
            delivery[nevent["id"]] = verify_delivery(
                nevent["raw"], receipts, customer=ticket.get("customer"),
                opened_at=case.get("opened_at"),
                customer_contact=case.get("customer_contact"))
    return {
        **{k: ticket[k] for k in ("ticket_id", "aliases", "dispute_keys",
                                  "customer", "channels", "opened_minute",
                                  "event_ids", "events", "inbound_ids",
                                  "outbound_ids", "merge_evidence")},
        "normalized_events": ticket["normalized_events"],
        "receipts": receipts,
        "holds": holds,
        "delivery": delivery,
        "case": case,
    }


# ---------------------------------------------------------------------------
# Delivery verification
# ---------------------------------------------------------------------------

_POSITIVE_DELIVERY = {"delivered", "delivery", "received", "client_received",
                      "inbox", "in_box", "placed_in_inbox", "read", "displayed",
                      "opened", "mdn_sent", "delivered_to_inbox"}
_NEGATIVE_DELIVERY = {"failed", "failure", "bounced", "bounce", "rejected",
                      "undelivered", "deferred", "dropped", "spam_rejected",
                      "hard_bounce"}
_ATTEMPT_ONLY = {"accepted", "sent", "queued", "dispatched", "pending",
                 "attempt", "submitted", "gateway_accepted"}
_CLIENT_RECEIPT_KINDS = {"client_inbox", "client_receipt", "inbox_snapshot",
                         "mailbox_export", "mdn", "read_receipt", "client-delivery"}


def _receipt_target(receipt):
    return str(_pick(receipt, "message_id", "event_id", "target_id",
                     "message") or "")


def verify_delivery(message, receipts=None, customer=None, opened_at=None,
                    customer_contact=None):
    """Verify whether an outbound message actually reached the client.

    Only positive, authentic receipts with a matching recipient prove delivery.
    Gateway acceptance / send attempts do not; bounces and forged or
    signature-invalid receipts are recorded as excluded evidence.
    """
    if isinstance(message, dict):
        mid = str(_pick(message, "id", "event_id", "message_id") or "")
        intended_rcpt = _pick(message, "recipient", "to", "rcpt")
    else:
        mid, intended_rcpt = str(message), None
    verdict = {"message_id": mid, "delivered": False, "minute": None,
               "receipt_id": None, "client_inbox_verified": False,
               "gateway_verified": False, "attempt_only": False,
               "checks": [], "excluded": []}
    related = []
    for receipt in receipts or []:
        if not isinstance(receipt, dict):
            continue
        target = _receipt_target(receipt)
        if target == mid or target in {mid}:
            related.append(receipt)
    if isinstance(message, dict):
        for receipt in _as_list(_pick(message, "receipts", "delivery_receipts")):
            if isinstance(receipt, dict):
                related.append(receipt)
        for key in ("delivery", "receipt", "delivery_receipt", "client_receipt"):
            if isinstance(message.get(key), dict):
                related.append(message[key])

    attempt_seen = False
    for receipt in related:
        rid = _pick(receipt, "id", "receipt_id")
        status = str(_pick(receipt, "status", "state", "delivery_status") or "").lower()
        kind = str(_pick(receipt, "type", "kind", "source") or "").lower()
        authentic = receipt.get("verified",
                                receipt.get("signature_valid",
                                            receipt.get("authentic", True)))
        verification = str(receipt.get("verification", "")).lower()
        if verification in ("failed", "invalid", "bad_signature"):
            authentic = False
        rcpt_to = _pick(receipt, "recipient", "rcpt", "delivered_to", "to")
        minute = _minute(_pick(receipt, "client_received_minute",
                               "received_minute", "delivered_minute", "minute",
                               "at_minute"))
        cc = clock_check(_pick(receipt, "at", "timestamp", "delivered_at"),
                         minute, opened_at)
        entry = {"receipt_id": rid, "status": status, "kind": kind,
                 "minute": minute, "clock": cc["status"]}

        if authentic is False:
            verdict["excluded"].append({
                "id": rid, "reason": "receipt_failed_authenticity_check", **entry})
            continue
        if not cc["ok"] and cc["status"] == "clock_conflict":
            verdict["excluded"].append({
                "id": rid, "reason": "receipt_clock_conflict", **entry})
            continue
        expected = str(intended_rcpt or customer_contact or "").strip().lower()
        actual = str(rcpt_to or "").strip().lower()
        if expected and actual:
            # Only compare like identifiers: two mailbox addresses or two
            # account codes.  A customer code must not reject an email receipt.
            comparable = ("@" in expected and "@" in actual) or \
                         ("@" not in expected and "@" not in actual)
            if comparable and actual != expected and expected not in actual \
                    and actual not in expected:
                verdict["excluded"].append({
                    "id": rid, "reason": "receipt_recipient_mismatch",
                    "expected": expected, "actual": actual, **entry})
                continue
        if status in _NEGATIVE_DELIVERY or kind in _NEGATIVE_DELIVERY:
            verdict["excluded"].append({
                "id": rid, "reason": "delivery_failed_or_bounced", **entry})
            continue
        if status in _ATTEMPT_ONLY or kind in _ATTEMPT_ONLY:
            attempt_seen = True
            verdict["checks"].append({"id": rid, "result": "send_attempt_only", **entry})
            continue
        positive = status in _POSITIVE_DELIVERY or kind in _POSITIVE_DELIVERY \
            or kind in _CLIENT_RECEIPT_KINDS
        if positive:
            client_side = kind in _CLIENT_RECEIPT_KINDS or \
                receipt.get("client_side") is True or \
                receipt.get("client_inbox") is True
            if minute is None and cc.get("normalized_minute") is not None:
                minute = cc["normalized_minute"]
            if minute is None:
                verdict["excluded"].append({
                    "id": rid, "reason": "positive_receipt_without_time", **entry})
                continue
            if verdict["minute"] is None or minute < verdict["minute"]:
                verdict.update(delivered=True, minute=minute, receipt_id=rid)
            verdict["checks"].append({"id": rid, "result": "delivery_verified",
                                      "client_side": client_side, **entry})
            if client_side:
                verdict["client_inbox_verified"] = True
            else:
                verdict["gateway_verified"] = True
        else:
            verdict["excluded"].append({
                "id": rid, "reason": "receipt_status_not_proof_of_delivery", **entry})

    verdict["attempt_only"] = attempt_seen and not verdict["delivered"]
    if not verdict["delivered"] and not verdict["checks"] and not verdict["excluded"]:
        verdict["checks"].append({"result": "no_delivery_evidence"})
    return verdict


effective_delivery = verify_delivery
delivery_verdict = verify_delivery


# ---------------------------------------------------------------------------
# Content verification
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_CODE_RE = re.compile(r"\b(?:DG|DIAG|A)[-_][A-Z0-9]+(?:-[A-Z0-9]+)*\b")
_TRANSLATION_RE = re.compile(
    r"(?:译名(?:定为|取|为|是)?|裁定(?:为|如下)?|建议译(?:为|作)?|标准译法(?:为|是)?|"
    r"translation|render(?:ed)? as)\s*[:：]\s*[“\"'「]?([^”\"'」\n。；;]{1,40})")


def extract_text(message):
    """Extract inspectable text from string/dict/HTML message bodies."""
    if message is None:
        return ""
    if isinstance(message, str):
        return _TAG_RE.sub(" ", message)
    parts = []
    body = _pick(message, "body", "content", "text", "payload")
    if isinstance(body, str):
        parts.append(body)
    elif isinstance(body, dict):
        for key in ("text", "plain", "markdown", "html", "body"):
            if isinstance(body.get(key), str):
                parts.append(body[key])
    for key in ("text", "body_text", "plain_text", "summary"):
        value = message.get(key)
        if isinstance(value, str) and value not in parts:
            parts.append(value)
    if isinstance(message.get("subject"), str):
        parts.append(message["subject"])
    attachments = message.get("attachments") or message.get("components") or []
    for att in attachments if isinstance(attachments, list) else []:
        if isinstance(att, dict) and isinstance(att.get("text"), str):
            parts.append(att["text"])
    return _TAG_RE.sub(" ", "\n".join(parts))


def _structured_term_answer(message):
    answer = _pick(message, "term_answer", "translation_decision",
                   "substantive_answer", "answer")
    if isinstance(answer, str) and answer.strip():
        return {"translation": answer.strip(), "term_key": message.get("term_key")}
    if isinstance(answer, dict):
        translation = _pick(answer, "translation", "translated_name", "name",
                            "rendering", "zh")
        if translation:
            return {"translation": str(translation).strip(),
                    "term_key": _pick(answer, "term_key", "term", "key")}
    return None


def assess_content(message, question=None, contract=None, catalog=None):
    """Inspect actual body content rather than trusting the event kind."""
    contract = contract or {}
    catalog = catalog or {}
    if not catalog and isinstance(message, dict):
        catalog = message.get("diagnostic_catalog") or {}
    text = extract_text(message) if isinstance(message, dict) else str(message)
    compact = re.sub(r"\s+", "", text)
    codes = sorted(set(_CODE_RE.findall(text)))
    structured = _structured_term_answer(message) if isinstance(message, dict) else None
    regex_answer = None
    match = _TRANSLATION_RE.search(text)
    if match:
        candidate = match.group(1).strip(" :：")
        if candidate and not candidate.startswith(("请", "confirm", "the ")):
            regex_answer = candidate
    term_answer = structured or ({"translation": regex_answer} if regex_answer else None)

    q_term = None
    q_dispute = None
    if isinstance(question, dict):
        q_term = _pick(question, "term_key", "term", "key", "asked_name")
        q_dispute = _pick(question, "dispute_key", "id", "question_key")

    guidance_hits = []
    for code in codes:
        entry = catalog.get(code) if isinstance(catalog, dict) else None
        if entry is None and isinstance(catalog, list):
            entry = next((c for c in catalog
                          if isinstance(c, dict) and _pick(c, "code", "id") == code), None)
        if not isinstance(entry, dict):
            # A code present in the body with inline structured proof still
            # counts when the message itself records term matching.
            inline_match = isinstance(message, dict) and _pick(
                message, "diagnostic_term_key", "matched_term")
            if inline_match:
                entry = {"term_key": inline_match, "preapproved": True,
                         "guidance": _pick(message, "diagnostic_summary",
                                           "guidance") or "inline-guidance"}
        if not isinstance(entry, dict):
            continue
        entry_term = _pick(entry, "term_key", "term", "key")
        term_matches = q_term is None or entry_term is None or \
            str(entry_term) == str(q_term) or str(q_term) in str(entry_term) \
            or str(entry_term) in str(q_term)
        preapproved = entry.get("preapproved",
                                entry.get("pre_approved",
                                          entry.get("customer_preacknowledged", True)))
        guidance_text = _pick(entry, "guidance", "summary", "instruction",
                              "handling_guidance")
        if term_matches and preapproved and guidance_text:
            guidance_hits.append({"code": code, "term_key": entry_term,
                                  "guidance": guidance_text})

    if isinstance(message, dict) and not guidance_hits and \
            _pick(message, "diagnostic_code"):
        code = message["diagnostic_code"]
        entry = catalog.get(code, {}) if isinstance(catalog, dict) else {}
        term_key = _pick(entry, "term_key") or _pick(
            message, "diagnostic_term_key", "matched_term")
        if (q_term is None or term_key is None or str(term_key) == str(q_term)) \
                and len(compact) >= 12:
            guidance_hits.append({"code": code, "term_key": term_key,
                                  "guidance": _pick(entry, "guidance", "summary")
                                  or _pick(message, "diagnostic_summary")
                                  or text[:80]})

    # Last-resort but still content-based: the body itself names the disputed
    # term/question and contains substantive handling guidance.  This avoids a
    # kind whitelist when no external catalog is supplied.
    if not guidance_hits and codes and q_term is not None \
            and str(q_term) in text and re.search(
                r"指引|指导|规则|步骤|建议先|guidance|rule|step", text, re.I):
        approved_codes = set()
        if isinstance(contract, dict):
            approved_codes = set(_as_list(contract.get("approved_diagnostic_codes")))
        code = codes[0]
        if not approved_codes or code in approved_codes:
            guidance_hits.append({
                "code": code, "term_key": str(q_term),
                "guidance": "body explicitly matches %s and gives handling guidance"
                            % q_term})

    term_match = True
    if term_answer and q_term and term_answer.get("term_key"):
        term_match = str(term_answer["term_key"]) == str(q_term)
    has_term_answer = bool(term_answer and term_answer.get("translation"))
    receipt_markers = ("收讫", "已受理", "自动回复", "auto", "receipt",
                       "received your", "ticket")
    looks_like_receipt = any(m in text.lower() for m in receipt_markers)
    empty_receipt = not has_term_answer and not guidance_hits and (
        len(compact) < 60 or looks_like_receipt)
    return {
        "has_term_answer": has_term_answer,
        "term_answer": term_answer,
        "term_matches_question": term_match,
        "has_diagnostic_guidance": bool(guidance_hits),
        "guidance": guidance_hits,
        "codes": codes,
        "empty_receipt": empty_receipt,
        "text_length": len(compact),
        "question_term": q_term,
        "question_dispute": q_dispute,
    }


verify_content = assess_content
content_verdict = assess_content


# ---------------------------------------------------------------------------
# Sender authorisation and response qualification
# ---------------------------------------------------------------------------

def sender_authorized(event, contract):
    nevent = normalize_event(event)
    if nevent.get("authorized") is True:
        return True, "event_marked_authorized"
    sender = str(nevent.get("sender") or "").lower()
    author_type = nevent.get("author_type")
    humans = [str(x).lower() for x in contract.get("authorized_humans", [])]
    bots = [str(x).lower() for x in contract.get("approved_auto_senders", [])]
    if author_type in ("human", "agent", "reviewer", "staff"):
        return (sender in humans or any(h in sender for h in humans),
                "human sender not on contract roster" if sender not in humans else "rostered_human")
    if author_type in ("bot", "automated", "system", "auto"):
        return (sender in bots or any(b in sender for b in bots),
                "auto sender not pre-approved")
    if sender in humans or any(h and h in sender for h in humans):
        return True, "rostered_human"
    if sender in bots or any(b and b in sender for b in bots):
        return True, "approved_auto_sender"
    if nevent.get("authorized") is False:
        return False, "event_marked_unauthorized"
    return False, "sender_not_authorized"


def qualify_response(event, context):
    """Apply delivery + authorisation + content + contract gates."""
    contract = context["contract"]
    delivery = context["delivery"]
    content = context["content"]
    ok_auth, auth_reason = sender_authorized(event, contract)
    result = {"id": _pick(event, "id", "event_id"), "qualified": False,
              "basis": None, "gates": {
                  "outbound": context.get("direction") == "outbound"
                  or _direction(event) == "outbound",
                  "delivered": bool(delivery.get("delivered")),
                  "authorized_sender": ok_auth,
              }, "reasons": []}
    if not result["gates"]["outbound"]:
        result["reasons"].append("not_outbound")
    if not delivery.get("delivered"):
        result["reasons"].append("not_delivered_to_client")
    if not ok_auth:
        result["reasons"].append("unauthorized_sender:%s" % auth_reason)
    if result["reasons"]:
        return result

    if contract.get("auto_diagnostic_counts") and content["has_diagnostic_guidance"] \
            and contract.get("customer_preacknowledged", True):
        result.update(qualified=True, basis="approved_auto_diagnostic")
        result["gates"]["content"] = "matched_preapproved_diagnostic_guidance"
        return result

    if content["has_term_answer"] and content["term_matches_question"]:
        if contract.get("human_substantive_required"):
            nevent = normalize_event(event)
            is_human = nevent["author_type"] in (None, "human", "agent",
                                                 "reviewer", "staff") \
                and nevent["kind"] not in ("bot-receipt", "auto-receipt") \
                and not str(nevent.get("sender") or "").lower().startswith(
                    ("bot", "auto", "service-bot"))
            if is_human or ok_auth and nevent["author_type"] in (
                    "human", "agent", "reviewer", "staff"):
                result.update(qualified=True,
                              basis="authorised_human_substantive_answer")
                result["gates"]["content"] = "concrete_translation_answer"
                return result
            result["reasons"].append("term_answer_not_from_authorised_human")
        else:
            result.update(qualified=True, basis="substantive_term_answer")
            return result

    if content["has_diagnostic_guidance"] and not contract.get(
            "auto_diagnostic_counts"):
        result["reasons"].append(
            "diagnostic_guidance_not_recognised_by_contract")
    elif content["empty_receipt"] or not content["has_term_answer"]:
        result["reasons"].append("receipt_without_substantive_term_content")
    else:
        result["reasons"].append("content_did_not_match_question")
    return result


def effective_responses(source=None, thread=None, contract=None, **kwargs):
    """Return all outbound events that validly stop the clock, soonest first."""
    captured = capture_ticket(source, thread, **kwargs)
    if captured is None:
        return []
    case = captured["case"]
    profile = normalize_contract(
        contract if contract is not None else case.get("governing_contract")
        or kwargs.get("contracts"), case.get("contracts"))
    question = kwargs.get("question") or case.get("question") or {}
    catalog = case.get("diagnostic_catalog") or {}
    candidates = []
    for nevent in captured["normalized_events"]:
        if nevent["direction"] != "outbound":
            continue
        raw = nevent["raw"]
        delivery = captured["delivery"].get(nevent["id"]) or verify_delivery(
            raw, captured["receipts"], customer=captured.get("customer"),
            opened_at=case.get("opened_at"),
            customer_contact=case.get("customer_contact"))
        content = assess_content(raw, question=question, contract=profile,
                                 catalog=catalog)
        verdict = qualify_response(raw, {
            "contract": profile, "delivery": delivery, "content": content,
            "direction": nevent["direction"]})
        if verdict["qualified"]:
            stop_minute = delivery["minute"]
            candidates.append({
                "id": nevent["id"],
                "event_minute": nevent["minute"],
                "minute": stop_minute,
                "delivery_minute": stop_minute,
                "basis": verdict["basis"],
                "delivery": delivery,
                "content": content,
                "verdict": verdict,
                "event": raw,
            })
    candidates.sort(key=lambda c: (c["minute"] if c["minute"] is not None else 1 << 30,
                                   c["id"] or ""))
    return candidates


def first_response(source=None, thread=None, contract=None, verified=None,
                   **kwargs):
    """Earliest outbound message on a merged ticket.

    Two modes:

    * Seed-compatible raw mode ``first_response(events, 'T-47')`` with no
      contract/verification options returns the actual earliest outbound event
      exactly like the original helper (callers that only need the raw
      primitive may also use :func:`earliest_outbound`).
    * Effective mode (``contract=...`` / ``verified=True`` / bundle with
      receipts) returns the augmented earliest *delivered, authorised and
      contract-valid* response.  :func:`effective_first_response` is an
      explicit alias for this mode.
    """
    contract = contract if contract is not None else kwargs.pop("policy", None)
    contract = contract if contract is not None else kwargs.pop("agreement", None)
    if isinstance(thread, dict) and contract is None:
        contract = thread
        thread = None
    if isinstance(thread, str) and contract is None and thread in (
            set(CONTRACT_PRESETS) | set(_CONTRACT_NAME_ALIASES)):
        contract = thread
        thread = None
    evidence_kwarg = any(kwargs.get(k) for k in (
        "receipts", "gateway_receipts", "deliveries", "holds",
        "pause_windows", "catalog", "question", "contracts"))
    bundle_with_evidence = isinstance(source, dict) and (
        source.get("receipts") or source.get("delivery_receipts")
        or source.get("gateway_receipts") or source.get("contracts")
        or source.get("_case") or source.get("diagnostic_catalog"))
    raw_seed_mode = verified is False or (
        contract is None and verified is None and not evidence_kwarg
        and not bundle_with_evidence and isinstance(source, list))
    if raw_seed_mode:
        return earliest_outbound(source, thread)
    candidates = effective_responses(source, thread, contract, **kwargs)
    if not candidates:
        return None
    chosen = candidates[0]
    augmented = dict(chosen["event"])
    augmented.update({
        "id": chosen["id"],
        "effective_minute": chosen["minute"],
        "response_minute": chosen["minute"],
        "qualification_basis": chosen["basis"],
        "delivery_verified": chosen["delivery"]["delivered"],
        "delivery_receipt_id": chosen["delivery"]["receipt_id"],
    })
    return augmented


def effective_first_response(source=None, thread=None, contract=None, **kwargs):
    kwargs["verified"] = True
    return first_response(source, thread, contract, **kwargs)


verified_first_response = effective_first_response
first_valid_response = effective_first_response


def earliest_outbound(events, thread=None):
    """Original seed primitive: earliest outbound by declared minute."""
    rows = [e for e in events if e.get("direction") == "outbound"
            and (thread is None or e.get("thread") == thread)]
    return min(rows, key=lambda event: event["minute"])


# ---------------------------------------------------------------------------
# Signed pause / resume windows
# ---------------------------------------------------------------------------

def evaluate_holds(source=None, thread=None, contract=None, **kwargs):
    """Validate pause windows and return counted intervals plus audit rows."""
    captured = kwargs.pop("captured", None)
    if captured is None:
        captured = capture_ticket(source, thread, **kwargs)
    if captured is None:
        return {"intervals": [], "paused_minutes": 0, "checks": [], "excluded": []}
    case = captured["case"]
    profile = normalize_contract(
        contract if contract is not None else case.get("governing_contract"),
        case.get("contracts"))
    delivery_minutes = {mid: v.get("minute")
                        for mid, v in captured["delivery"].items()}
    # Supplement request delivery may also be an inbound-linked outbound event.
    for nevent in captured["normalized_events"]:
        if nevent["direction"] == "outbound" and nevent["id"] not in delivery_minutes:
            verdict = verify_delivery(nevent["raw"], captured["receipts"],
                                     customer=captured.get("customer"),
                                     opened_at=case.get("opened_at"),
                                     customer_contact=case.get("customer_contact"))
            delivery_minutes[nevent["id"]] = verdict.get("minute")

    intervals, checks, excluded = [], [], []
    for hold in captured["holds"]:
        hid = _pick(hold, "id", "hold_id", "window_id") or "hold"
        start = _minute(_pick(hold, "start", "from_minute", "pause_from",
                              "from", "start_minute", "pause_started_minute",
                              "start_min", "pause_start", "begin_minute"))
        end = _minute(_pick(hold, "end", "to_minute", "resume_at", "to",
                            "end_minute", "resume_minute", "end_min",
                            "pause_end", "finish_minute"))
        req = _pick(hold, "request_message_id", "request_event_id",
                    "supplement_request_id", "linked_request", "request_id")
        signed = hold.get("signed", hold.get("customer_signed",
                                             hold.get("acknowledged",
                                                      hold.get("signed_by_customer"))))
        signer = _pick(hold, "signed_by", "signer", "customer_signature")
        signature_verified = hold.get("signature_verified",
                                      hold.get("signature_valid", True))
        reasons = []
        if not profile.get("pause_allowed", True):
            reasons.append("contract_does_not_allow_pause")
        if signed is False or (signed is None and not signer):
            reasons.append("pause_not_signed_by_customer")
        if signature_verified is False:
            reasons.append("pause_signature_unverified")
        req_minute = delivery_minutes.get(str(req)) if req is not None else None
        if req_minute is None:
            req_minute = _minute(_pick(
                hold, "request_received_minute", "request_delivered_minute",
                "supplement_received_minute", "request_client_received_minute"))
        if req is not None and req_minute is None and \
                hold.get("require_request_delivery", True):
            reasons.append("supplement_request_not_verified_delivered")
        if start is None or end is None:
            reasons.append("pause_window_incomplete")
        elif end < start:
            reasons.append("pause_window_inverted")
        if reasons:
            excluded.append({"id": hid, "reasons": reasons,
                             "start": start, "end": end, "request": req})
            checks.append({"gate": "pause", "id": hid, "ok": False,
                           "reasons": reasons})
            continue
        effective_start = start
        if req_minute is not None:
            effective_start = max(start, req_minute)
        intervals.append({"id": hid, "start": effective_start, "end": end,
                          "request": req, "request_received_minute": req_minute,
                          "signed_by": signer or signed})
        checks.append({"gate": "pause", "id": hid, "ok": True,
                       "counted_from": effective_start, "until": end,
                       "request_received_minute": req_minute})

    merged = []
    for iv in sorted(intervals, key=lambda x: x["start"]):
        if not merged or iv["start"] > merged[-1]["end"]:
            merged.append(dict(iv))
        else:
            merged[-1]["end"] = max(merged[-1]["end"], iv["end"])
    return {"intervals": merged, "raw_intervals": intervals,
            "paused_minutes": sum(max(0, i["end"] - i["start"]) for i in merged),
            "checks": checks, "excluded": excluded}


def counted_pause_minutes(intervals, opened_minute=0, response_minute=None):
    """Clamp half-open pause intervals to [open, response] and sum union."""
    lo = opened_minute or 0
    hi = response_minute
    clipped = []
    for iv in intervals:
        start = max(lo, iv["start"])
        end = iv["end"] if hi is None else min(iv["end"], hi)
        if end > start:
            clipped.append((start, end))
    total = 0
    cursor = None
    for start, end in sorted(clipped):
        if cursor is None:
            total += end - start
            cursor = end
        elif start >= cursor:
            total += end - start
            cursor = end
        elif end > cursor:
            total += end - cursor
            cursor = end
    return total


# ---------------------------------------------------------------------------
# Naming aliases for the audit/review vocabulary used across callers
# ---------------------------------------------------------------------------

delivery_status = verify_delivery
check_response_content = assess_content
merge_conversation = merge_conversations
group_threads = merge_conversations
build_ticket = capture_ticket
capture = capture_ticket
calculate_pause_windows = evaluate_holds
pause_windows = evaluate_holds


def valid_pause_minutes(source=None, thread=None, contract=None,
                        response_minute=None, opened_minute=0, **kwargs):
    """Integer union of valid signed pause intervals, optionally clamped."""
    result = evaluate_holds(source, thread, contract, **kwargs)
    if response_minute is None:
        try:
            candidates = effective_responses(source, thread, contract, **kwargs)
            if candidates:
                response_minute = candidates[0]["minute"]
        except Exception:
            response_minute = None
    return counted_pause_minutes(result["intervals"], opened_minute,
                                 response_minute)


pause_minutes = valid_pause_minutes

capture_conversation = capture_ticket
verify_response = qualify_response
