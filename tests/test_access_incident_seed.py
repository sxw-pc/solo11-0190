import copy
import json
import unittest
from pathlib import Path

from termsettle.access_capture import capture_evidence, unknown_accesses
from termsettle.incident_decision import decide

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'source-access.json'


def load():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


def evidence_for(data, scenario=None):
    """Return shared evidence, optionally overlaying a named scenario."""
    evidence = copy.deepcopy(data['evidence'])
    if scenario:
        evidence.update(copy.deepcopy(data['scenarios'][scenario]['evidence']))
    return evidence


def add_second_access(data, evidence, *, token, device, version='v7', recipients=('external-drop-7',),
                      at='2026-04-01T10:15:00Z', denied_by=None, handshake=None):
    """Add a second download event and its full issuance chain on the same account."""
    event = {'id': 'e3', 'account': 'review-proxy', 'packet': 'SRC-47', 'action': 'download', 'at': at}
    data['events'].append(event)
    evidence['tokens'].append({
        'id': token, 'account': 'review-proxy', 'delegation': 'del-47',
        'issued_at': '2026-04-01T08:06:00Z', 'expires_at': '2026-04-03T18:00:00Z',
    })
    evidence['devices'].append({
        'id': device, 'account': 'review-proxy', 'site': 'office-hk',
        'registered_at': '2026-03-01T00:00:00Z', 'status': 'active',
    })
    evidence['sessions'].append({
        'id': f'ses-{token}', 'account': 'review-proxy', 'token': token, 'device': device,
        'started_at': '2026-04-01T10:10:00Z', 'ended_at': '2026-04-01T11:00:00Z',
    })
    receipt = {
        'id': f'rcpt-{event["id"]}-{token}', 'event': event['id'], 'account': 'review-proxy',
        'token': token, 'device': device, 'session': f'ses-{token}', 'version': version,
        'received_by': list(recipients), 'at': at,
    }
    if denied_by:
        receipt['denied_by'] = denied_by
    evidence['receipts'].append(receipt)
    if handshake:
        evidence['handshakes'].append({
            'id': f'hs-{token}', 'token': token, 'device': device, 'session': f'ses-{token}',
            'site': handshake, 'at': at,
        })
    return event


class IncidentSeedTest(unittest.TestCase):
    """Original seed behaviour: unknown account is a candidate, not a finding."""

    def test_unknown_proxy_is_candidate_but_not_a_finding(self):
        data = load()
        self.assertEqual([e['id'] for e in unknown_accesses(data['events'], data['known_accounts'])], ['e2'])

    def test_delegated_access_is_contractual_without_client_notice(self):
        data = load()
        result = decide(data['events'], data['known_accounts'], evidence_for(data, 'delegated'))
        self.assertFalse(result['notify'])
        self.assertFalse(result['escalate'])
        self.assertEqual(result['affected'], 0)
        self.assertEqual(result['statuses']['e2'], 'compliant')
        # The proxy account and the original receipt still appear in the audit.
        self.assertEqual(result['audit']['proxy_accounts'], ['review-proxy'])
        self.assertEqual([r['id'] for r in result['audit']['receipts']], ['rcpt-e2'])

    def test_identity_alone_never_decides(self):
        data = load()
        no_evidence = {key: [] for key in ('delegations', 'tokens', 'devices', 'sessions',
                                           'versions', 'receipts', 'handshakes')}
        result = decide(data['events'], data['known_accounts'], no_evidence)
        self.assertEqual(result['statuses']['e2'], 'pending')
        self.assertTrue(result['escalate'])
        self.assertFalse(result['notify'])
        self.assertEqual(result['affected'], 0)


class VerificationRoutesTest(unittest.TestCase):
    """The two contract-authorized ways to verify the same e2 download."""

    def test_route_one_delegation_original_proves_contract_access(self):
        data = load()
        evidence = evidence_for(data, 'delegated')
        result = decide(data['events'], data['known_accounts'], evidence)
        self.assertEqual(result['statuses'], {'e1': 'known', 'e2': 'compliant'})
        self.assertFalse(result['notify'])
        self.assertFalse(result['escalate'])
        chain = {item['event']: item for item in result['audit']['issuance_chain']}
        self.assertEqual(chain['e2']['delegation'], 'del-47')
        self.assertEqual(chain['e2']['token'], 'tok-47')
        self.assertEqual(chain['e2']['device'], 'dev-reviewer-1')
        self.assertEqual(chain['e2']['session'], 'ses-47')
        self.assertEqual(chain['e2']['version'], 'v7')

    def test_route_two_stolen_token_notifies_by_version_and_recipients(self):
        data = load()
        evidence = evidence_for(data, 'stolen')
        result = decide(data['events'], data['known_accounts'], evidence)
        self.assertTrue(result['notify'])
        self.assertEqual(result['statuses']['e2'], 'conflict')
        self.assertEqual(result['affected_versions'], ['v7'])
        self.assertEqual(result['affected'], 1)
        self.assertEqual(result['frozen_tokens'], ['tok-47'])
        self.assertEqual(result['scope'], [{
            'event': 'e2',
            'account': 'review-proxy',
            'version': 'v7',
            'recipients': ['external-drop-7', 'reviewer-R2'],
            'receipt': 'rcpt-e2',
            'reasons': ['token_used_on_overlapping_devices',
                        'offsite_device_handshake',
                        'receipt_denied_by_reviewer'],
        }])
        # The stolen-token route still retains the original receipt in audit.
        self.assertEqual([r['id'] for r in result['audit']['receipts']], ['rcpt-e2'])
        self.assertEqual(result['audit']['proxy_accounts'], ['review-proxy'])

    def test_routes_diverge_on_identical_event(self):
        data = load()
        delegated = decide(data['events'], data['known_accounts'], evidence_for(data, 'delegated'))
        stolen = decide(data['events'], data['known_accounts'], evidence_for(data, 'stolen'))
        self.assertNotEqual(delegated['notify'], stolen['notify'])
        self.assertEqual(delegated['statuses']['e2'], 'compliant')
        self.assertEqual(stolen['statuses']['e2'], 'conflict')


class SameAccountSplitTest(unittest.TestCase):
    """One proxy account may carry a legitimate and a stolen session at once."""

    def test_sessions_split_by_issuance_chain(self):
        data = load()
        evidence = evidence_for(data, 'delegated')
        add_second_access(data, evidence, token='tok-47b', device='dev-remote-9',
                          denied_by='reviewer-R2', handshake='offsite-hotel')
        result = decide(data['events'], data['known_accounts'], evidence)
        self.assertEqual(result['statuses']['e2'], 'compliant')
        self.assertEqual(result['statuses']['e3'], 'conflict')
        self.assertEqual(result['conflicts'], ['e3'])
        self.assertEqual(result['compliant'], ['e2'])
        self.assertEqual(result['frozen_tokens'], ['tok-47b'])
        # The audit keeps both sessions for the same account under one chain.
        chain = {item['event']: item for item in result['audit']['issuance_chain']}
        self.assertEqual(chain['e2']['token'], 'tok-47')
        self.assertEqual(chain['e3']['token'], 'tok-47b')
        self.assertEqual(result['audit']['proxy_accounts'], ['review-proxy'])

    def test_overlapping_devices_on_one_token_is_conflict(self):
        data = load()
        evidence = evidence_for(data, 'delegated')
        evidence['devices'].append({
            'id': 'dev-remote-9', 'account': 'review-proxy', 'site': 'office-hk',
            'registered_at': '2026-03-01T00:00:00Z', 'status': 'active',
        })
        evidence['sessions'].append({
            'id': 'ses-overlap', 'account': 'review-proxy', 'token': 'tok-47',
            'device': 'dev-remote-9',
            'started_at': '2026-04-01T09:30:00Z', 'ended_at': '2026-04-01T10:30:00Z',
        })
        result = decide(data['events'], data['known_accounts'], evidence)
        self.assertEqual(result['statuses']['e2'], 'conflict')
        self.assertIn('token_used_on_overlapping_devices', result['events'][1]['conflicts'])
        self.assertEqual(result['frozen_tokens'], ['tok-47'])

    def test_sequential_devices_on_one_token_do_not_conflict(self):
        data = load()
        evidence = evidence_for(data, 'delegated')
        evidence['devices'].append({
            'id': 'dev-remote-9', 'account': 'review-proxy', 'site': 'office-hk',
            'registered_at': '2026-03-01T00:00:00Z', 'status': 'active',
        })
        evidence['sessions'].append({
            'id': 'ses-later', 'account': 'review-proxy', 'token': 'tok-47',
            'device': 'dev-remote-9',
            'started_at': '2026-04-01T12:30:00Z', 'ended_at': '2026-04-01T13:00:00Z',
        })
        result = decide(data['events'], data['known_accounts'], evidence)
        self.assertFalse(result['notify'])
        self.assertEqual(result['statuses']['e2'], 'compliant')


class WindowBoundaryTest(unittest.TestCase):
    """Delegation window is inclusive, and one second outside is a conflict."""

    def _at(self, moment):
        data = load()
        evidence = evidence_for(data, 'delegated')
        data['events'][1]['at'] = moment
        evidence['receipts'][0]['at'] = moment
        evidence['tokens'][0]['issued_at'] = '2026-04-01T00:00:00Z'
        evidence['tokens'][0]['expires_at'] = '2026-04-04T00:00:00Z'
        evidence['sessions'][0]['started_at'] = '2026-04-01T00:00:00Z'
        evidence['sessions'][0]['ended_at'] = '2026-04-04T00:00:00Z'
        return decide(data['events'], data['known_accounts'], evidence)

    def test_window_start_is_inclusive(self):
        result = self._at('2026-04-01T08:00:00Z')
        self.assertEqual(result['statuses']['e2'], 'compliant')
        self.assertFalse(result['notify'])

    def test_window_end_is_inclusive(self):
        result = self._at('2026-04-03T18:00:00Z')
        self.assertEqual(result['statuses']['e2'], 'compliant')
        self.assertFalse(result['notify'])

    def test_one_second_before_window_is_outside(self):
        result = self._at('2026-04-01T07:59:59Z')
        self.assertEqual(result['statuses']['e2'], 'conflict')
        self.assertIn('outside_authorization_window', result['events'][1]['conflicts'])

    def test_one_second_after_window_is_outside(self):
        result = self._at('2026-04-03T18:00:01Z')
        self.assertEqual(result['statuses']['e2'], 'conflict')
        self.assertIn('outside_authorization_window', result['events'][1]['conflicts'])


class LateEvidenceTest(unittest.TestCase):
    """Late revocation and late delegation must not rewrite the past."""

    def test_revocation_after_access_keeps_receipt_and_is_not_conflict(self):
        data = load()
        evidence = evidence_for(data, 'delegated')
        evidence['tokens'][0]['revoked_at'] = '2026-04-05T09:00:00Z'
        evidence['delegations'][0]['revoked_at'] = '2026-04-05T09:00:00Z'
        result = decide(data['events'], data['known_accounts'], evidence)
        self.assertFalse(result['notify'])
        self.assertEqual(result['statuses']['e2'], 'compliant')
        record = result['events'][1]
        self.assertIn('token_revoked_after_access', record['notes'])
        self.assertIn('receipt_retained_after_revocation', record['notes'])
        self.assertEqual([r['id'] for r in result['audit']['receipts']], ['rcpt-e2'])

    def test_revocation_before_access_is_conflict_and_receipt_survives(self):
        data = load()
        evidence = evidence_for(data, 'delegated')
        evidence['tokens'][0]['revoked_at'] = '2026-04-01T09:00:00Z'
        result = decide(data['events'], data['known_accounts'], evidence)
        self.assertTrue(result['notify'])
        self.assertIn('token_revoked_before_access', result['events'][1]['conflicts'])
        self.assertEqual([r['id'] for r in result['audit']['receipts']], ['rcpt-e2'])

    def test_delegation_issued_after_access_does_not_ratify(self):
        data = load()
        evidence = evidence_for(data, 'delegated')
        evidence['delegations'][0]['issued_at'] = '2026-04-01T10:05:00Z'
        result = decide(data['events'], data['known_accounts'], evidence)
        self.assertTrue(result['notify'])
        self.assertIn('delegation_issued_after_access', result['events'][1]['conflicts'])

    def test_prior_investigation_and_withdrawal_are_retained(self):
        data = load()
        evidence = evidence_for(data, 'delegated')
        evidence['investigations'] = [{
            'id': 'inv-1', 'status': 'closed', 'conclusion': 'compliant',
            'issued_at': '2026-03-31T00:00:00Z',
        }]
        evidence['withdrawals'] = [{
            'id': 'wd-1', 'event': 'e2', 'token': 'tok-47', 'at': '2026-04-02T00:00:00Z',
            'reason': 'new-evidence',
        }]
        result = decide(data['events'], data['known_accounts'], evidence)
        self.assertEqual(result['audit']['prior_investigations'], evidence['investigations'])
        self.assertEqual(result['audit']['withdrawals'], evidence['withdrawals'])
        self.assertIn('prior_decision_withdrawn', result['events'][1]['notes'])


class ScopeTest(unittest.TestCase):
    """Notification scope follows the versions actually read and their recipients."""

    def test_scope_is_per_version_and_per_recipient(self):
        data = load()
        evidence = evidence_for(data, 'delegated')
        evidence['versions'].append({'id': 'v8', 'packet': 'SRC-47', 'released_at': '2026-03-26T00:00:00Z'})
        add_second_access(data, evidence, token='tok-47b', device='dev-remote-9',
                          version='v8', recipients=['external-drop-7'],
                          at='2026-04-03T18:00:01Z', denied_by='reviewer-R2')
        result = decide(data['events'], data['known_accounts'], evidence)
        self.assertEqual(result['statuses']['e2'], 'compliant')
        self.assertEqual(result['statuses']['e3'], 'conflict')
        self.assertEqual(result['affected_versions'], ['v8'])
        self.assertEqual(result['affected'], 1)
        self.assertEqual(result['scope'][0]['version'], 'v8')
        self.assertEqual(result['scope'][0]['recipients'], ['external-drop-7'])

    def test_two_conflicting_versions_count_as_two(self):
        data = load()
        evidence = evidence_for(data, 'delegated')
        evidence['versions'].append({'id': 'v8', 'packet': 'SRC-47', 'released_at': '2026-03-26T00:00:00Z'})
        add_second_access(data, evidence, token='tok-47b', device='dev-remote-9',
                          version='v8', recipients=['external-drop-7'],
                          at='2026-04-03T18:00:01Z', denied_by='reviewer-R2')
        data['events'][1]['at'] = '2026-04-04T00:00:00Z'
        evidence['receipts'][0]['at'] = '2026-04-04T00:00:00Z'
        result = decide(data['events'], data['known_accounts'], evidence)
        self.assertEqual(result['statuses']['e2'], 'conflict')
        self.assertEqual(result['statuses']['e3'], 'conflict')
        self.assertEqual(result['affected_versions'], ['v7', 'v8'])
        self.assertEqual(result['affected'], 2)

    def test_receipt_version_mismatch_is_conflict(self):
        data = load()
        evidence = evidence_for(data, 'delegated')
        data['events'][1]['version'] = 'v8'
        result = decide(data['events'], data['known_accounts'], evidence)
        self.assertEqual(result['statuses']['e2'], 'conflict')
        self.assertIn('receipt_version_mismatch', result['events'][1]['conflicts'])

    def test_receipt_before_access_is_conflict(self):
        data = load()
        evidence = evidence_for(data, 'delegated')
        evidence['receipts'][0]['at'] = '2026-04-01T09:59:59Z'
        result = decide(data['events'], data['known_accounts'], evidence)
        self.assertEqual(result['statuses']['e2'], 'conflict')
        self.assertIn('receipt_before_access', result['events'][1]['conflicts'])

    def test_missing_receipt_stays_pending_not_compliant(self):
        data = load()
        evidence = evidence_for(data, 'delegated')
        evidence['receipts'] = []
        result = decide(data['events'], data['known_accounts'], evidence)
        self.assertEqual(result['statuses']['e2'], 'pending')
        self.assertTrue(result['escalate'])
        self.assertFalse(result['notify'])
        self.assertIn('receipt_missing', result['events'][1]['pending'])


class IntegrityTest(unittest.TestCase):
    """Evidence must not be mutated, and packet form must behave like explicit form."""

    def test_inputs_are_not_mutated(self):
        data = load()
        before = copy.deepcopy(data)
        decide(data['events'], data['known_accounts'], data['evidence'])
        capture_evidence(data['events'], data['known_accounts'], data['evidence'])
        self.assertEqual(data, before)

    def test_packet_form_matches_explicit_form(self):
        data = load()
        explicit = decide(data['events'], data['known_accounts'], data['evidence'])
        packet = decide(data)
        self.assertEqual(explicit['statuses'], packet['statuses'])
        self.assertEqual(explicit['affected'], packet['affected'])

    def test_original_fixtures_still_work(self):
        # The source/term/delivery workflow fixtures are untouched.
        from termsettle.policy import calculate
        root = FIXTURE.parent
        rows = []
        for subsystem in ('source', 'term', 'delivery'):
            rows.extend(json.loads((root / f'{subsystem}-ok.json').read_text(encoding='utf-8')))
        _, decisions = calculate(rows)
        self.assertEqual(len(decisions), 10)


if __name__ == '__main__':
    unittest.main()
