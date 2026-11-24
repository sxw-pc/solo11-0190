"""Capture delegated source-access evidence.

``unknown_accesses`` only finds candidate events whose account is absent from
the known-account list.  An unknown account is never, by itself, evidence of an
external person.  The classification below follows the chain that was
effective at the moment of each access:

* client-approved delegation (issuer, approval, effective window, revocation),
* token issuance and revocation,
* registered device and off-site handshake,
* active session on that device,
* released source version,
* actual download receipt (version and recipients).

Every event is judged separately, so one proxy account can carry a legitimate
session and a stolen session without either verdict contaminating the other.
A missing link yields ``pending`` rather than ``compliant``.  A receipt is
retained in the audit history even when a delegation or token is revoked after
the access: late revocation never erases an access that actually happened.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

KNOWN = 'known'
COMPLIANT = 'compliant'
PENDING = 'pending'
CONFLICT = 'conflict'

DOWNLOAD_ACTIONS = frozenset({'download', 'export', 'retrieve'})
HANDSHAKE_TOLERANCE = timedelta(minutes=15)

# The first reason present becomes the reported primary reason.
CONFLICT_PRIORITY = (
    'delegation_issued_after_access',
    'outside_authorization_window',
    'delegation_revoked_before_access',
    'token_revoked_before_access',
    'token_expired',
    'token_issued_after_access',
    'token_used_on_overlapping_devices',
    'offsite_device_handshake',
    'receipt_denied_by_reviewer',
    'device_unregistered',
    'device_registered_after_access',
    'device_deregistered_before_access',
    'device_account_mismatch',
    'session_not_active',
    'session_not_found',
    'session_account_mismatch',
    'session_token_mismatch',
    'session_device_mismatch',
    'delegation_account_mismatch',
    'delegation_packet_mismatch',
    'delegation_not_approved',
    'token_account_mismatch',
    'token_delegation_mismatch',
    'receipt_account_mismatch',
    'receipt_before_access',
    'receipt_version_mismatch',
    'version_not_released',
    'version_not_delegated',
    'version_packet_mismatch',
)

PENDING_PRIORITY = (
    'delegation_missing',
    'delegation_record_missing',
    'delegation_approval_unverified',
    'delegation_issue_time_missing',
    'delegation_window_missing',
    'token_missing',
    'token_record_missing',
    'token_issue_time_missing',
    'token_expiry_missing',
    'device_missing',
    'device_record_missing',
    'device_registration_missing',
    'session_missing',
    'session_record_missing',
    'session_time_missing',
    'receipt_missing',
    'version_missing',
    'version_record_missing',
    'version_release_missing',
)

NOTE_PRIORITY = (
    'delegation_revoked_after_access',
    'token_revoked_after_access',
    'receipt_retained_after_revocation',
    'prior_decision_withdrawn',
    'known_account_baseline',
)


def _time(value):
    """Parse an ISO timestamp; naive values are treated as UTC."""
    if value is None or value == '':
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _before(left, right):
    a, b = _time(left), _time(right)
    return a is not None and b is not None and a < b


def _at_or_before(left, right):
    a, b = _time(left), _time(right)
    return a is not None and b is not None and a <= b


def _within(moment, start, end):
    """Inclusive window check; a missing bound leaves that side open."""
    instant = _time(moment)
    if instant is None:
        return False
    lower, upper = _time(start), _time(end)
    return (lower is None or lower <= instant) and (upper is None or instant <= upper)


def _gap(a, b):
    left, right = _time(a), _time(b)
    if left is None or right is None:
        return None
    return abs(right - left)


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _pick(record, *keys):
    if not isinstance(record, dict):
        return None
    for key in keys:
        if key in record and record[key] is not None:
            return record[key]
    return None


def _records(evidence, *keys):
    evidence = evidence or {}
    for key in keys:
        value = evidence.get(key)
        if value:
            return list(value)
    return []


def _index(records):
    return {record.get('id'): record for record in records if record.get('id') is not None}


def _ordered(reasons, priority):
    reasons = list(dict.fromkeys(reasons))
    return sorted(reasons, key=lambda item: priority.index(item) if item in priority else len(priority))


def unknown_accesses(events, known_accounts):
    """Return events whose account is not on the known list.

    The result is a candidate set for evidence review, not a finding.  Use
    :func:`capture_evidence` to classify the candidates against delegation,
    token, device, session, version and receipt evidence.
    """
    known = set(known_accounts or ())
    return [event for event in (events or []) if event.get('account') not in known]


def _context(events, known_accounts, evidence):
    evidence = evidence or {}
    packet = evidence.get('packet')
    if packet is None:
        for event in events or []:
            if event.get('packet') is not None:
                packet = event['packet']
                break
    return {
        'packet': packet,
        'known': set(known_accounts or ()),
        'events': [deepcopy(event) for event in (events or [])],
        'delegations': _records(evidence, 'delegations', 'delegation', 'authorizations'),
        'tokens': _records(evidence, 'tokens', 'token_issues'),
        'devices': _records(evidence, 'devices', 'device_registry'),
        'sessions': _records(evidence, 'sessions', 'device_sessions'),
        'versions': _records(evidence, 'versions', 'source_versions'),
        'receipts': _records(evidence, 'receipts', 'download_receipts'),
        'handshakes': _records(evidence, 'handshakes', 'device_handshakes'),
        'investigations': _records(evidence, 'investigations', 'investigation_history'),
        'withdrawals': _records(evidence, 'withdrawals', 'decision_withdrawals'),
    }


def _event_packet(event, context):
    return event.get('packet') or _pick(event, 'source') or context['packet']


def _delegation_accounts(record):
    values = []
    for key in ('account', 'proxy_account'):
        value = record.get(key)
        if value is not None:
            values.append(value)
    for key in ('proxy_accounts', 'accounts', 'reviewers'):
        values.extend(_as_list(record.get(key)))
    return values


def _delegation_covers(record, account):
    values = _delegation_accounts(record)
    return not values or account in values


def _format(value):
    return None if value is None else str(value)


def _record_version(record):
    return _pick(record, 'version', 'release', 'source_version', 'label') if record else None


def _receipt_recipients(receipt):
    if not receipt:
        return []
    values = []
    for key in ('received_by', 'recipients', 'delivered_to', 'destination'):
        values.extend(_as_list(receipt.get(key)))
    return sorted({_format(value) for value in values if value not in (None, '')})


def _find_receipt(event, context):
    event_id = event.get('id')
    for receipt in context['receipts']:
        if receipt.get('event') == event_id:
            return receipt
        if event_id in _as_list(receipt.get('events')):
            return receipt
        if receipt.get('access') == event_id:
            return receipt
    return None


def _find_token(event, receipt, context):
    tokens = _index(context['tokens'])
    reference = _pick(receipt, 'token', 'token_id')
    if reference is None:
        reference = _pick(event, 'token', 'token_id')
    if isinstance(reference, dict):
        reference = reference.get('id')
    if reference is not None:
        return tokens.get(reference), reference
    account = event.get('account')
    candidates = [token for token in context['tokens'] if token.get('account') == account]
    active = [token for token in candidates if _token_active(token, event.get('at'))]
    if len(active) == 1:
        return active[0], active[0].get('id')
    if len(candidates) == 1:
        return candidates[0], candidates[0].get('id')
    return None, None


def _token_active(token, moment):
    issued = token.get('issued_at')
    expires = _pick(token, 'expires_at', 'expiry')
    revoked = token.get('revoked_at')
    if issued is None or not _at_or_before(issued, moment):
        return False
    if expires is not None and _at_or_before(expires, moment):
        return False
    if revoked is not None and _at_or_before(revoked, moment):
        return False
    return True


def _delegation_effective(record, moment):
    """A delegation counts at the access only if already issued and in window."""
    if _before(moment, record.get('issued_at')):
        return False
    return _within(moment, _pick(record, 'from', 'not_before', 'effective_from'),
                   _pick(record, 'to', 'not_after', 'effective_to'))


def _find_delegation(event, token, context):
    index = _index(context['delegations'])
    reference = _pick(token, 'delegation', 'delegation_id') if token else None
    if isinstance(reference, dict):
        reference = reference.get('id')
    if reference is not None:
        return index.get(reference), reference
    account = event.get('account')
    packet = _event_packet(event, context)
    moment = event.get('at')
    candidates = [
        delegation for delegation in context['delegations']
        if _delegation_covers(delegation, account)
        and delegation.get('packet') in (None, packet)
    ]
    # Prefer the delegation that was actually effective at the access; a
    # delegation issued later never ratifies an earlier access.
    effective = [item for item in candidates if _delegation_effective(item, moment)]
    if len(effective) == 1:
        return effective[0], effective[0].get('id')
    if effective:
        latest = max(effective, key=lambda item: str(item.get('issued_at') or ''))
        return latest, latest.get('id')
    if len(candidates) == 1:
        return candidates[0], candidates[0].get('id')
    if candidates:
        latest = max(candidates, key=lambda item: str(item.get('issued_at') or ''))
        return latest, latest.get('id')
    return None, None


def _find_device(event, receipt, session, context):
    devices = _index(context['devices'])
    reference = _pick(receipt, 'device', 'device_id')
    if reference is None:
        reference = _pick(session, 'device', 'device_id')
    if reference is None:
        reference = _pick(event, 'device', 'device_id')
    if isinstance(reference, dict):
        reference = reference.get('id')
    if reference is not None:
        return devices.get(reference), reference
    account = event.get('account')
    candidates = [device for device in context['devices'] if device.get('account') == account]
    if len(candidates) == 1:
        return candidates[0], candidates[0].get('id')
    return None, None


def _find_session(event, receipt, token, device, context):
    sessions = _index(context['sessions'])
    reference = _pick(receipt, 'session', 'session_id')
    if reference is None:
        reference = _pick(event, 'session', 'session_id')
    if isinstance(reference, dict):
        reference = reference.get('id')
    if reference is not None:
        return sessions.get(reference), reference
    token_id = token.get('id') if token else None
    device_id = device.get('id') if device else None
    account = event.get('account')
    matches = [
        session for session in context['sessions']
        if (token_id is None or session.get('token') == token_id)
        and (device_id is None or session.get('device') == device_id)
        and (session.get('account') is None or session.get('account') == account)
        and _session_covers(session, event.get('at'))
    ]
    if len(matches) == 1:
        return matches[0], matches[0].get('id')
    return None, None


def _session_covers(session, moment):
    started = _pick(session, 'started_at', 'from', 'handshake_at')
    ended = _pick(session, 'ended_at', 'to')
    if started is None:
        return False
    if _before(moment, started):
        return False
    if ended is not None and _before(ended, moment):
        return False
    return True


def _find_handshakes(session, token, device, context):
    session_id = session.get('id') if session else None
    token_id = token.get('id') if token else None
    device_id = device.get('id') if device else None
    matches = []
    for handshake in context['handshakes']:
        if session_id is not None and handshake.get('session') == session_id:
            matches.append(handshake)
        elif token_id is not None and handshake.get('token') == token_id:
            matches.append(handshake)
        elif device_id is not None and handshake.get('device') == device_id:
            matches.append(handshake)
    return matches


def _find_version(event, receipt, context):
    version = _record_version(receipt)
    if version is None:
        version = _record_version(event)
    if version is None:
        return None, None
    for record in context['versions']:
        keys = (
            record.get('id'), record.get('version'), record.get('release'),
            record.get('label'), record.get('source_version'),
        )
        if any(key is not None and str(key) == str(version) for key in keys):
            return version, record
    return version, None


def _interval(record):
    started = _pick(record, 'started_at', 'from')
    ended = _pick(record, 'ended_at', 'to')
    if started is None:
        return None
    return _time(started), _time(ended) if ended is not None else None


def _overlaps(left, right):
    one, two = _interval(left), _interval(right)
    if one is None or two is None:
        return False
    left_start, left_end = one
    right_start, right_end = two
    if left_end is not None and right_start > left_end:
        return False
    if right_end is not None and left_start > right_end:
        return False
    return True


def _overlapping_token_devices(token_id, sessions):
    relevant = [session for session in sessions if session.get('token') == token_id]
    for index, left in enumerate(relevant):
        for right in relevant[index + 1:]:
            if left.get('device') == right.get('device'):
                continue
            if _overlaps(left, right):
                return True
    return False


def _is_download(event):
    return str(event.get('action') or '').lower() in DOWNLOAD_ACTIONS


def _classify(event, context):
    account = event.get('account')
    moment = event.get('at')
    packet = _event_packet(event, context)
    known = account in context['known']
    conflicts, pending, notes = [], [], []

    def missing(reason):
        # A known account is a trusted baseline; an absent link only becomes a
        # finding for candidates whose identity still has to be established.
        if not known:
            pending.append(reason)

    receipt = _find_receipt(event, context)
    token, token_ref = _find_token(event, receipt, context)
    delegation, delegation_ref = _find_delegation(event, token, context)
    device, device_ref = _find_device(event, receipt, None, context)
    session, session_ref = _find_session(event, receipt, token, device, context)
    if device is None:
        device, device_ref = _find_device(event, receipt, session, context)
    handshakes = _find_handshakes(session, token, device, context)
    version, version_record = _find_version(event, receipt, context)
    action_download = _is_download(event)

    # --- delegation: client approval, issue time and inclusive window -----
    if delegation is None:
        if delegation_ref is not None:
            pending.append('delegation_record_missing')
        else:
            missing('delegation_missing')
    else:
        delegation_packet = delegation.get('packet')
        if delegation_packet is not None and packet is not None and delegation_packet != packet:
            conflicts.append('delegation_packet_mismatch')
        if not _delegation_covers(delegation, account):
            conflicts.append('delegation_account_mismatch')
        approved_by = delegation.get('approved_by')
        if delegation.get('approved') is False:
            conflicts.append('delegation_not_approved')
        elif approved_by in (None, ''):
            missing('delegation_approval_unverified')
        issued = delegation.get('issued_at')
        if issued is None:
            missing('delegation_issue_time_missing')
        elif _before(moment, issued):
            # A delegation signed later never ratifies an earlier access.
            conflicts.append('delegation_issued_after_access')
        start = _pick(delegation, 'from', 'not_before', 'effective_from')
        end = _pick(delegation, 'to', 'not_after', 'effective_to')
        if start is None and end is None:
            missing('delegation_window_missing')
        elif not _within(moment, start, end):
            conflicts.append('outside_authorization_window')
        revoked = delegation.get('revoked_at')
        if revoked is not None:
            if _at_or_before(revoked, moment):
                conflicts.append('delegation_revoked_before_access')
            else:
                notes.append('delegation_revoked_after_access')

    # --- token issuance, expiry and revocation ----------------------------
    if token is not None:
        token_account = token.get('account')
        if token_account is not None and token_account != account:
            conflicts.append('token_account_mismatch')
        token_delegation = _pick(token, 'delegation', 'delegation_id')
        if isinstance(token_delegation, dict):
            token_delegation = token_delegation.get('id')
        if token_delegation is not None:
            if delegation is None:
                pending.append('delegation_record_missing')
            elif delegation.get('id') is not None and token_delegation != delegation.get('id'):
                conflicts.append('token_delegation_mismatch')
        issued = token.get('issued_at')
        if issued is None:
            missing('token_issue_time_missing')
        elif _before(moment, issued):
            conflicts.append('token_issued_after_access')
        expires = _pick(token, 'expires_at', 'expiry')
        if expires is None:
            missing('token_expiry_missing')
        elif _at_or_before(expires, moment):
            conflicts.append('token_expired')
        revoked = token.get('revoked_at')
        if revoked is not None:
            if _at_or_before(revoked, moment):
                conflicts.append('token_revoked_before_access')
            else:
                notes.append('token_revoked_after_access')
    elif token_ref is not None:
        pending.append('token_record_missing')
    elif action_download:
        missing('token_missing')

    # --- registered device -------------------------------------------------
    if device is not None:
        device_account = device.get('account')
        if device_account is not None and device_account != account:
            conflicts.append('device_account_mismatch')
        registered = _pick(device, 'registered_at', 'enrolled_at')
        if registered is None:
            missing('device_registration_missing')
        elif _before(moment, registered):
            conflicts.append('device_registered_after_access')
        deregistered = _pick(device, 'deregistered_at', 'revoked_at')
        if deregistered is not None and _at_or_before(deregistered, moment):
            conflicts.append('device_deregistered_before_access')
        if str(device.get('status') or '').lower() in ('revoked', 'deregistered', 'unregistered'):
            conflicts.append('device_deregistered_before_access')
    elif device_ref is not None:
        if context['devices']:
            conflicts.append('device_unregistered')
        else:
            pending.append('device_record_missing')
    elif action_download:
        missing('device_missing')

    # --- active device session --------------------------------------------
    if session is not None:
        session_account = session.get('account')
        if session_account is not None and session_account != account:
            conflicts.append('session_account_mismatch')
        if token is not None and session.get('token') not in (None, token.get('id')):
            conflicts.append('session_token_mismatch')
        if device is not None and session.get('device') not in (None, device.get('id')):
            conflicts.append('session_device_mismatch')
        started = _pick(session, 'started_at', 'from')
        if started is None:
            missing('session_time_missing')
        elif _before(moment, started):
            conflicts.append('session_not_active')
        ended = _pick(session, 'ended_at', 'to')
        if ended is not None and _before(ended, moment):
            conflicts.append('session_not_active')
    elif session_ref is not None:
        if context['sessions']:
            conflicts.append('session_not_found')
        else:
            pending.append('session_record_missing')
    elif action_download:
        missing('session_missing')

    # --- same token on overlapping devices / off-site handshake -----------
    if token is not None and token.get('id') is not None:
        if _overlapping_token_devices(token.get('id'), context['sessions']):
            conflicts.append('token_used_on_overlapping_devices')
    for handshake in handshakes:
        site = _pick(handshake, 'site', 'location')
        device_site = _pick(device, 'site', 'location') if device else None
        offsite = handshake.get('offsite') is True or (
            site is not None and device_site is not None and site != device_site
        )
        if not offsite:
            continue
        handshake_at = _pick(handshake, 'at', 'handshake_at')
        distance = _gap(handshake_at, moment)
        if distance is not None and distance > HANDSHAKE_TOLERANCE:
            continue
        conflicts.append('offsite_device_handshake')

    # --- actual download receipt ------------------------------------------
    if receipt is not None:
        receipt_account = receipt.get('account')
        if receipt_account is not None and receipt_account != account:
            conflicts.append('receipt_account_mismatch')
        if receipt.get('denied_by') is not None or receipt.get('denied') is True:
            conflicts.append('receipt_denied_by_reviewer')
        receipt_version = _record_version(receipt)
        event_version = _record_version(event)
        if receipt_version is not None and event_version is not None:
            if str(receipt_version) != str(event_version):
                conflicts.append('receipt_version_mismatch')
        receipt_at = _pick(receipt, 'at', 'downloaded_at')
        if receipt_at is not None and _before(receipt_at, moment):
            conflicts.append('receipt_before_access')
    elif action_download:
        missing('receipt_missing')

    # --- released source version ------------------------------------------
    if version is None:
        if action_download or not known:
            missing('version_missing')
    elif version_record is not None:
        released = _pick(version_record, 'released_at', 'at', 'published_at')
        if released is None:
            missing('version_release_missing')
        elif _before(moment, released):
            conflicts.append('version_not_released')
        version_packet = version_record.get('packet')
        if version_packet is not None and packet is not None and version_packet != packet:
            conflicts.append('version_packet_mismatch')
    elif context['versions']:
        conflicts.append('version_not_released')
    else:
        missing('version_record_missing')

    if delegation is not None and version is not None:
        allowed = _pick(delegation, 'versions', 'releases', 'authorized_versions')
        if allowed is not None:
            allowed_values = {str(item) for item in _as_list(allowed)}
            if str(version) not in allowed_values:
                conflicts.append('version_not_delegated')

    # --- retained history: revocation after the fact is not erasure -------
    if receipt is not None:
        if 'token_revoked_after_access' in notes or 'delegation_revoked_after_access' in notes:
            notes.append('receipt_retained_after_revocation')
        for withdrawal in context['withdrawals']:
            if withdrawal.get('event') == event.get('id') or (
                token is not None and withdrawal.get('token') == token.get('id')
            ):
                notes.append('prior_decision_withdrawn')
                break

    if known:
        notes.append('known_account_baseline')

    conflicts = _ordered(conflicts, CONFLICT_PRIORITY)
    pending = _ordered(pending, PENDING_PRIORITY)
    notes = _ordered(notes, NOTE_PRIORITY)
    if conflicts:
        verdict = CONFLICT
    elif pending:
        verdict = PENDING
    elif known:
        verdict = KNOWN
    else:
        verdict = COMPLIANT

    return {
        'id': event.get('id'),
        'account': account,
        'packet': packet,
        'action': event.get('action'),
        'at': moment,
        'verdict': verdict,
        'reasons': conflicts + pending,
        'conflicts': conflicts,
        'pending': pending,
        'notes': notes,
        'version': version,
        'receipt': receipt,
        'delegation': delegation,
        'token': token,
        'device': device,
        'session': session,
        'handshakes': handshakes,
    }


def _normalise_evidence(events, known_accounts, evidence):
    """Accept explicit arguments, a full packet mapping, or a nested evidence dict."""
    if isinstance(events, dict) and evidence is None:
        packet = events
        events = packet.get('events') or []
        known_accounts = packet.get('known_accounts') or known_accounts
        evidence = packet.get('evidence') or {}
    elif isinstance(evidence, dict) and isinstance(evidence.get('evidence'), dict):
        evidence = evidence['evidence']
    return list(events or []), list(known_accounts or []), evidence or {}


def capture_evidence(events, known_accounts=None, evidence=None):
    """Classify every event against the access issuance chain.

    ``evidence`` may be the full packet (with ``events`` and
    ``known_accounts``) or a separate evidence mapping.  Each event is judged
    independently, so a single proxy account can hold both a legitimate and a
    stolen session.  Verdicts are ``compliant``, ``pending`` or ``conflict``;
    known accounts remain a trusted baseline unless explicit conflict evidence
    appears.
    """
    events, known_accounts, evidence = _normalise_evidence(events, known_accounts, evidence)
    context = _context(events, known_accounts, evidence)
    records = [_classify(event, context) for event in context['events']]
    return _summarize(records, context)


def access_scope(record):
    """Build one notification-scope item from a conflicting access record.

    The version is the one actually on the download receipt; recipients come
    from the receipt, so the scope is determined item by item rather than from
    the account label.
    """
    receipt = record.get('receipt') or {}
    version = _record_version(receipt)
    if version is None:
        version = record.get('version')
    return {
        'event': record.get('id'),
        'account': record.get('account'),
        'version': version,
        'recipients': _receipt_recipients(receipt),
        'receipt': receipt.get('id'),
        'reasons': list(record.get('conflicts') or []),
    }


def _summarize(records, context):
    conflicts = [record for record in records if record['verdict'] == CONFLICT]
    pending = [record for record in records if record['verdict'] == PENDING]
    compliant = [record for record in records if record['verdict'] == COMPLIANT]
    reportable = conflicts + pending

    scopes = [access_scope(record) for record in conflicts]
    affected_versions = sorted({
        str(item['version']) for item in scopes if item['version'] is not None
    })
    frozen = {
        (record.get('token') or {}).get('id')
        for record in conflicts
        if (record.get('token') or {}).get('id') is not None
    }
    receipts = {}
    for record in records:
        receipt = record.get('receipt')
        if receipt is not None:
            receipts[receipt.get('id') or record.get('id')] = receipt
    chain = []
    for record in records:
        chain.append({
            'event': record.get('id'),
            'verdict': record['verdict'],
            'delegation': (record.get('delegation') or {}).get('id'),
            'token': (record.get('token') or {}).get('id'),
            'device': (record.get('device') or {}).get('id'),
            'session': (record.get('session') or {}).get('id'),
            'version': record.get('version'),
            'receipt': (record.get('receipt') or {}).get('id'),
        })
    audit = {
        'packet': context['packet'],
        'proxy_accounts': sorted({
            record.get('account') for record in records
            if record.get('account') not in context['known']
        }),
        'known_accounts': sorted(context['known']),
        'receipts': [receipts[key] for key in sorted(receipts, key=str)],
        'issuance_chain': chain,
        'prior_investigations': deepcopy(context['investigations']),
        'withdrawals': deepcopy(context['withdrawals']),
    }
    return {
        'notify': bool(conflicts),
        'escalate': bool(reportable),
        'affected': len(affected_versions),
        'affected_versions': affected_versions,
        'scope': scopes,
        'frozen_tokens': sorted(frozen),
        'statuses': {record.get('id'): record['verdict'] for record in records},
        'conflicts': [record.get('id') for record in conflicts],
        'pending': [record.get('id') for record in pending],
        'compliant': [record.get('id') for record in compliant],
        'known': [record.get('id') for record in records if record['verdict'] == KNOWN],
        'events': records,
        'audit': audit,
    }
