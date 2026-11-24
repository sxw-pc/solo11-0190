"""Notification decision driven by captured access evidence.

The decision is never derived from account membership alone.  It consumes the
issuance chain captured by :mod:`termsettle.access_capture` and applies the
contract rules:

* an unknown account is a review candidate, not an external person;
* a notification is raised only when the effective delegation, token, device or
  session evidence contradicts the access;
* events that still need verification stay ``pending`` and are escalated, never
  silently folded into ``compliant``;
* notification scope is built item by item from the source version actually
  downloaded and the recipients on the download receipt;
* a receipt is retained in the audit even when the delegation or token is
  revoked after the access, and late revocation never erases history.
"""
from __future__ import annotations

from .access_capture import (
    COMPLIANT,
    CONFLICT,
    KNOWN,
    PENDING,
    _normalise_evidence,
    access_scope,
    capture_evidence,
)


def _decision_item(record):
    if record['verdict'] == CONFLICT:
        scope = access_scope(record)
        return {
            'event': record.get('id'),
            'account': record.get('account'),
            'decision': 'notify',
            'reasons': list(record.get('conflicts') or []),
            'version': scope['version'],
            'recipients': scope['recipients'],
        }
    if record['verdict'] == PENDING:
        return {
            'event': record.get('id'),
            'account': record.get('account'),
            'decision': 'escalate_pending',
            'reasons': list(record.get('pending') or []),
        }
    if record['verdict'] == COMPLIANT:
        return {
            'event': record.get('id'),
            'account': record.get('account'),
            'decision': 'compliant_no_notice',
            'reasons': list(record.get('notes') or []),
            'version': access_scope(record)['version'],
        }
    return {'event': record.get('id'), 'account': record.get('account'), 'decision': 'known_baseline'}


def decide(events, known_accounts=None, evidence=None):
    """Return the notification decision for a source-access packet.

    ``affected`` counts the distinct source versions actually readable in a
    conflicting access, and falls back to one per conflict event when the
    version is still unknown.  ``notify`` is true only for contract conflicts.
    ``escalate`` is true for conflicts and pending verification, so a pending
    event is never silently treated as compliant.  ``frozen_tokens`` lists the
    tokens behind conflicting accesses.
    """
    events, known_accounts, evidence = _normalise_evidence(events, known_accounts, evidence)
    captured = capture_evidence(events, known_accounts, evidence)

    conflicts = [record for record in captured['events'] if record['verdict'] == CONFLICT]
    pending = [record for record in captured['events'] if record['verdict'] == PENDING]
    compliant = [record for record in captured['events'] if record['verdict'] == COMPLIANT]
    known = [record for record in captured['events'] if record['verdict'] == KNOWN]

    scope = [access_scope(record) for record in conflicts]
    affected_versions = sorted({
        str(item['version']) for item in scope if item['version'] is not None
    })
    affected = len(affected_versions) if affected_versions else len(conflicts)

    frozen = sorted({
        (record.get('token') or {}).get('id')
        for record in conflicts
        if (record.get('token') or {}).get('id') is not None
    })

    audit = dict(captured['audit'])
    audit['decisions'] = [_decision_item(record) for record in captured['events']]
    audit['compliant_accesses'] = [record.get('id') for record in compliant]
    audit['pending_accesses'] = [record.get('id') for record in pending]
    audit['known_accesses'] = [record.get('id') for record in known]
    audit['conflict_accesses'] = [record.get('id') for record in conflicts]

    return {
        'notify': bool(conflicts),
        'escalate': bool(conflicts or pending),
        'affected': affected,
        'affected_versions': affected_versions,
        'scope': scope,
        'frozen_tokens': frozen,
        'statuses': captured['statuses'],
        'conflicts': captured['conflicts'],
        'pending': captured['pending'],
        'compliant': captured['compliant'],
        'known': captured['known'],
        'events': captured['events'],
        'audit': audit,
    }
