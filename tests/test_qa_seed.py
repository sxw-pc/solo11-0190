import copy
import json
import unittest
from pathlib import Path

from termsettle.qa_capture import (
    SamplingPlanError,
    confirmed_flagged,
    flagged,
    label_evidence,
    plan_seal,
)
from termsettle.quality_report import CONTINUE, EXPAND, HOLD_EVIDENCE, HOLD_REVIEW, decisions, defect_rate, estimate

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'qa-sample.json'
ROOT = Path(__file__).resolve().parents[1] / 'fixtures'


def load():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


def reseal(data, index=0):
    plan = data['plans'][index]
    plan['issued_digest'] = plan_seal(plan, data['frame'])
    return data


def by_design(data):
    return {result['plan']['design']: result for result in decisions(data)['results']}


class DesignChoiceTest(unittest.TestCase):
    """The two pre-signed designs legitimately reach different decisions."""

    def setUp(self):
        self.data = load()

    def test_same_sample_under_srs_expands(self):
        result = by_design(self.data)['simple_random']
        self.assertAlmostEqual(result['estimate'], 0.5)
        self.assertEqual(result['decision'], EXPAND)
        self.assertGreater(result['estimate'], result['limit'])

    def test_same_sample_under_stratified_continues(self):
        result = by_design(self.data)['stratified']
        self.assertAlmostEqual(result['estimate'], 0.2)
        self.assertEqual(result['decision'], CONTINUE)
        self.assertLessEqual(result['estimate'], result['limit'])

    def test_each_decision_keeps_its_own_signed_plan(self):
        results = decisions(self.data)['results']
        self.assertEqual([r['plan']['plan_id'] for r in results],
                         ['PLAN-SRS-2026-11-02', 'PLAN-STRAT-2026-11-02'])
        for result in results:
            plan = result['plan']
            self.assertTrue(plan['issued_at'])
            self.assertTrue(plan['client_signed_at'])
            self.assertTrue(plan['client_signature'])
            self.assertTrue(plan['issued_digest'])
            self.assertEqual(sorted(result['sample']['active']), ['seg-H1', 'seg-L1'])

    def test_stratified_weights_follow_the_2_8_population(self):
        result = by_design(self.data)['stratified']
        probabilities = result['plan']['inclusion_probabilities']
        self.assertEqual(probabilities['seg-H1'], 0.5)   # 1 of 2 high-risk
        self.assertEqual(probabilities['seg-H2'], 0.5)
        self.assertEqual(probabilities['seg-L1'], 0.125)  # 1 of 8 low-risk
        self.assertEqual(result['plan']['strata']['high']['population'], 2)
        self.assertEqual(result['plan']['strata']['low']['population'], 8)
        # (1/0.5 + 0/0.125) / (1/0.5 + 1/0.125) = 2 / 10
        self.assertAlmostEqual(result['estimate'], 2 / 10)

    def test_srs_weights_are_uniform_and_not_stratified(self):
        result = by_design(self.data)['simple_random']
        probabilities = result['plan']['inclusion_probabilities']
        self.assertEqual(set(probabilities.values()), {0.2})
        # 1/0.2 over 2/0.2 -> 1/2, not the 0.2 stratified answer
        self.assertAlmostEqual(result['estimate'], 0.5)

    def test_pasting_stratified_weights_onto_the_srs_sample_is_rejected(self):
        data = load()
        srs = data['plans'][0]
        srs['inclusion_probabilities'] = dict(data['plans'][1]['inclusion_probabilities'])
        reseal(data, 0)
        with self.assertRaises(SamplingPlanError):
            estimate(data, 'PLAN-SRS-2026-11-02')

    def test_annotating_the_srs_sample_as_stratified_is_rejected(self):
        data = load()
        srs = data['plans'][0]
        srs['strata'] = {'high': {'population': 2, 'sample_size': 1},
                         'low': {'population': 8, 'sample_size': 1}}
        reseal(data, 0)
        with self.assertRaises(SamplingPlanError):
            estimate(data, 'PLAN-SRS-2026-11-02')

    def test_stratified_plan_requires_every_frame_stratum(self):
        data = load()
        del data['plans'][1]['strata']['low']
        reseal(data, 1)
        with self.assertRaises(SamplingPlanError):
            estimate(data, 'PLAN-STRAT-2026-11-02')

    def test_stratified_estimator_reproduces_the_hand_computation(self):
        result = by_design(self.data)['stratified']
        numerator = result['versions'][-1]['horvitz_thompson_numerator']
        denominator = result['versions'][-1]['horvitz_thompson_denominator']
        self.assertAlmostEqual(numerator, 1 / 0.5)
        self.assertAlmostEqual(denominator, 1 / 0.5 + 1 / 0.125)
        self.assertAlmostEqual(numerator / denominator, 0.2)

    def test_switching_design_after_the_result_breaks_the_seal(self):
        data = load()
        data['plans'][0]['design'] = 'stratified'  # switched to get the 0.2 answer
        with self.assertRaises(SamplingPlanError):
            estimate(data, 'PLAN-SRS-2026-11-02')

    def test_changing_the_denominator_after_the_result_is_rejected(self):
        data = load()
        data['delivered_count'] = 5  # flagged / 5 would be 0.2 and "pass"
        with self.assertRaises(SamplingPlanError):
            estimate(data, 'PLAN-SRS-2026-11-02')

    def test_changing_the_trigger_after_the_result_is_rejected(self):
        data = load()
        data['plans'][0]['limit'] = 0.6  # would let the 0.5 SRS estimate "pass"
        with self.assertRaises(SamplingPlanError):
            estimate(data, 'PLAN-SRS-2026-11-02')


class SigningOrderTest(unittest.TestCase):
    def setUp(self):
        self.data = load()

    def test_plan_must_exist_before_the_inspection(self):
        data = copy.deepcopy(self.data)
        data['plans'][0]['issued_at'] = '2026-11-03T10:30:00+08:00'
        reseal(data)
        with self.assertRaises(SamplingPlanError):
            estimate(data, 'PLAN-SRS-2026-11-02')

    def test_client_must_sign_before_the_plan_is_issued(self):
        data = copy.deepcopy(self.data)
        data['plans'][0]['client_signed_at'] = '2026-11-02T11:00:00+08:00'
        reseal(data)
        with self.assertRaises(SamplingPlanError):
            estimate(data, 'PLAN-SRS-2026-11-02')

    def test_missing_client_signature_is_rejected(self):
        data = copy.deepcopy(self.data)
        del data['plans'][1]['client_signature']
        reseal(data, 1)
        with self.assertRaises(SamplingPlanError):
            estimate(data, 'PLAN-STRAT-2026-11-02')

    def test_inspection_before_client_signature_is_rejected(self):
        data = copy.deepcopy(self.data)
        data['plans'][0]['client_signed_at'] = '2026-11-03T10:30:00+08:00'
        data['plans'][0]['issued_at'] = '2026-11-03T10:30:00+08:00'
        reseal(data)
        with self.assertRaises(SamplingPlanError):
            estimate(data, 'PLAN-SRS-2026-11-02')

    def test_seal_is_required(self):
        data = copy.deepcopy(self.data)
        del data['plans'][0]['issued_digest']
        with self.assertRaises(SamplingPlanError):
            estimate(data, 'PLAN-SRS-2026-11-02', require_seal=True)
        # Without the strict switch the unsealed plan still validates its dates.
        self.assertEqual(estimate(data, 'PLAN-SRS-2026-11-02', require_seal=False)['decision'], EXPAND)

    def test_missing_inclusion_probability_is_rejected(self):
        data = copy.deepcopy(self.data)
        del data['plans'][1]['inclusion_probabilities']['seg-L8']
        reseal(data, 1)
        with self.assertRaises(SamplingPlanError):
            estimate(data, 'PLAN-STRAT-2026-11-02')


class UnsampledIsNotZeroTest(unittest.TestCase):
    def setUp(self):
        self.data = load()

    def test_unsampled_segments_carry_no_label(self):
        result = by_design(self.data)['stratified']
        self.assertEqual(len(result['sample']['unobserved']), 8)
        self.assertIn('未知', result['sample']['unobserved_note'])
        for observation in result['observations']:
            self.assertIn(observation['id'], {'seg-H1', 'seg-L1'})
        # The decision does not depend on pretending the other eight are clean.
        self.assertEqual([c['case_id'] for c in result['versions'][-1]['clusters']],
                         ['case-H1', 'case-L1'])

    def test_partially_observed_case_holds_instead_of_assuming_clean(self):
        data = copy.deepcopy(self.data)
        plan = data['plans'][0]
        plan['sample'] = ['seg-L3', 'seg-H1']  # seg-L3 shares case-L3L4 with seg-L4
        reseal(data)
        data['observations'] = [
            {'id': 'seg-L3', 'defect': False, 'inspected_at': '2026-11-03T10:00:00+08:00'},
            {'id': 'seg-H1', 'defect': True, 'inspected_at': '2026-11-03T10:00:00+08:00'},
        ]
        result = estimate(data, 'PLAN-SRS-2026-11-02')
        self.assertEqual(result['decision'], HOLD_EVIDENCE)
        self.assertIn('case-L3L4', result['versions'][-1]['unknown_clusters'])

    def test_raw_flag_count_never_erases_the_original_label(self):
        data = load()
        self.assertEqual(flagged(data['observations']), 1)
        observations = copy.deepcopy(data['observations'])
        observations[0]['review'] = {'reviewer': 'R', 'decision': 'overturn',
                                     'signed_at': '2026-11-04T10:00:00+08:00', 'signature': 'sig'}
        self.assertEqual(flagged(observations), 1)       # history preserved
        self.assertEqual(confirmed_flagged(observations), 0)  # effective label changed


class ClusterTest(unittest.TestCase):
    def setUp(self):
        self.data = load()

    def test_case_split_into_segments_counts_once(self):
        data = copy.deepcopy(self.data)
        plan = data['plans'][0]
        plan['sample'] = ['seg-L3', 'seg-L4']  # both segments of case-L3L4
        reseal(data)
        data['observations'] = [
            {'id': 'seg-L3', 'defect': True, 'inspected_at': '2026-11-03T10:00:00+08:00'},
            {'id': 'seg-L4', 'defect': False, 'inspected_at': '2026-11-03T10:00:00+08:00'},
        ]
        result = estimate(data, 'PLAN-SRS-2026-11-02')
        clusters = result['versions'][-1]['clusters']
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]['case_id'], 'case-L3L4')
        # P(at least one of 2 of 10) = 1 - C(8,2)/C(10,2) = 17/45
        self.assertAlmostEqual(clusters[0]['inclusion_probability'], 17 / 45)
        # One case with a defect, sampled with 17/45: the ratio is 1/1, not 1/2.
        self.assertAlmostEqual(result['estimate'], 1.0)
        self.assertEqual(result['decision'], EXPAND)

    def test_cluster_estimate_differs_from_naive_independent_segments(self):
        data = copy.deepcopy(self.data)
        plan = data['plans'][0]
        plan['sample'] = ['seg-H2', 'seg-L8']  # both belong to case-H2H8
        reseal(data)
        data['observations'] = [
            {'id': 'seg-H2', 'defect': False, 'inspected_at': '2026-11-03T10:00:00+08:00'},
            {'id': 'seg-L8', 'defect': True, 'inspected_at': '2026-11-03T10:00:00+08:00'},
        ]
        result = estimate(data, 'PLAN-SRS-2026-11-02')
        self.assertEqual(result['versions'][-1]['clusters'][0]['case_id'], 'case-H2H8')
        # Independent-segment diagnostic would wrongly report 1/2 = 0.5.
        self.assertAlmostEqual(result['segment_level_estimate'], 0.5)
        # Cluster ratio is 1/1 = 1.0 because the case is observed and defective.
        self.assertAlmostEqual(result['estimate'], 1.0)


class ReviewTest(unittest.TestCase):
    def setUp(self):
        self.data = load()

    def test_overturned_defect_keeps_both_versions(self):
        data = copy.deepcopy(self.data)
        for observation in data['observations']:
            if observation['id'] == 'seg-H1':
                observation['review'] = {'reviewer': 'Reviewer C', 'decision': 'overturn',
                                         'signed_at': '2026-11-04T10:00:00+08:00',
                                         'signature': 'review-sig-1'}
        result = estimate(data, 'PLAN-SRS-2026-11-02')
        self.assertEqual([v['version'] for v in result['versions']],
                         ['v1-as-inspected', 'v2-review'])
        self.assertAlmostEqual(result['versions'][0]['estimate'], 0.5)
        self.assertAlmostEqual(result['versions'][1]['estimate'], 0.0)
        self.assertEqual(result['decision'], CONTINUE)
        evidence = label_evidence(data['observations'][0])
        self.assertTrue(evidence['as_inspected'])  # original label retained
        self.assertFalse(evidence['effective'])
        self.assertEqual(evidence['reviews'][0]['signature'], 'review-sig-1')

    def test_overturn_then_correction_retains_three_versions(self):
        data = copy.deepcopy(self.data)
        for observation in data['observations']:
            if observation['id'] == 'seg-H1':
                observation['review'] = {'reviewer': 'R1', 'decision': 'overturn',
                                         'signed_at': '2026-11-04T10:00:00+08:00', 'signature': 's1'}
                observation['correction'] = {'reviewer': 'R1', 'defect': True,
                                             'signed_at': '2026-11-05T10:00:00+08:00', 'signature': 's2'}
        result = estimate(data, 'PLAN-SRS-2026-11-02')
        self.assertEqual([v['version'] for v in result['versions']],
                         ['v1-as-inspected', 'v2-review', 'v3-correction'])
        self.assertAlmostEqual(result['estimate'], 0.5)
        self.assertEqual(result['decision'], EXPAND)
        self.assertEqual(result['decision_basis'], 'v3-correction')

    def test_conflicting_signed_reviews_hold_for_adjudication(self):
        data = copy.deepcopy(self.data)
        for observation in data['observations']:
            if observation['id'] == 'seg-L1':
                observation['reviews'] = [
                    {'reviewer': 'R1', 'decision': 'uphold', 'corrected_defect': True,
                     'signed_at': '2026-11-04T10:00:00+08:00', 'signature': 's1'},
                    {'reviewer': 'R2', 'decision': 'uphold', 'corrected_defect': False,
                     'signed_at': '2026-11-04T11:00:00+08:00', 'signature': 's2'},
                ]
        result = estimate(data, 'PLAN-SRS-2026-11-02')
        self.assertEqual(result['decision'], HOLD_REVIEW)

    def test_unsigned_review_is_rejected(self):
        data = copy.deepcopy(self.data)
        data['observations'][0]['review'] = {'reviewer': 'R1', 'decision': 'overturn',
                                             'signed_at': '2026-11-04T10:00:00+08:00'}
        with self.assertRaises(SamplingPlanError):
            estimate(data, 'PLAN-SRS-2026-11-02')

    def test_correction_cannot_predate_the_review_it_resolves(self):
        data = copy.deepcopy(self.data)
        data['observations'][0]['review'] = {'reviewer': 'R1', 'decision': 'overturn',
                                             'signed_at': '2026-11-05T10:00:00+08:00', 'signature': 's1'}
        data['observations'][0]['correction'] = {'reviewer': 'R1', 'defect': True,
                                                 'signed_at': '2026-11-04T10:00:00+08:00', 'signature': 's2'}
        with self.assertRaises(SamplingPlanError):
            estimate(data, 'PLAN-SRS-2026-11-02')


class WithdrawalTest(unittest.TestCase):
    def setUp(self):
        self.data = load()

    def test_signed_same_stratum_replacement_reweights_the_plan(self):
        data = copy.deepcopy(self.data)
        plan = data['plans'][1]
        plan['replacements'] = [{'withdrawn': 'seg-L1', 'replacement': 'seg-L2',
                                 'signed_at': '2026-11-02T12:00:00+08:00', 'signature': 'wg-1'}]
        reseal(data, 1)
        data['observations'].append({'id': 'seg-L2', 'defect': False,
                                     'inspected_at': '2026-11-03T11:00:00+08:00'})
        result = estimate(data, 'PLAN-STRAT-2026-11-02')
        self.assertEqual(result['sample']['active'], ['seg-H1', 'seg-L2'])
        self.assertEqual(result['sample']['replaced'], {'seg-L1': 'seg-L2'})
        self.assertAlmostEqual(result['estimate'], 0.2)
        self.assertEqual(result['decision'], CONTINUE)

    def test_withdrawal_without_replacement_holds_and_never_counts_zero(self):
        data = copy.deepcopy(self.data)
        plan = data['plans'][1]
        plan['replacements'] = [{'withdrawn': 'seg-L1', 'signed_at': '2026-11-02T12:00:00+08:00',
                                 'signature': 'wg-2', 'reason': '撤回'}]
        reseal(data, 1)
        result = estimate(data, 'PLAN-STRAT-2026-11-02')
        self.assertEqual(result['decision'], HOLD_EVIDENCE)
        self.assertEqual(len(result['sample']['unresolved_withdrawals']), 1)

    def test_replacement_must_stay_in_the_withdrawn_stratum(self):
        data = copy.deepcopy(self.data)
        plan = data['plans'][1]
        plan['replacements'] = [{'withdrawn': 'seg-L1', 'replacement': 'seg-H2',
                                 'signed_at': '2026-11-02T12:00:00+08:00', 'signature': 'wg-3'}]
        reseal(data, 1)
        with self.assertRaises(SamplingPlanError):
            estimate(data, 'PLAN-STRAT-2026-11-02')


class ScopeTest(unittest.TestCase):
    def test_estimate_reports_scope_only(self):
        result = estimate(load(), 'PLAN-SRS-2026-11-02')
        self.assertTrue(result['decides_scope_only'])
        self.assertIn('不重裁术语定版', result['scope_note'])

    def test_defect_rate_is_the_design_estimate_not_a_delivery_fraction(self):
        data = load()
        self.assertAlmostEqual(defect_rate(data, 'PLAN-SRS-2026-11-02'), 0.5)
        self.assertAlmostEqual(defect_rate(data, 'PLAN-STRAT-2026-11-02'), 0.2)

    def test_legacy_shape_without_a_plan_is_rejected(self):
        with self.assertRaises(SamplingPlanError):
            defect_rate({'delivered_count': 10,
                         'inspections': [{'id': 'seg-H1', 'defect': True}]})

    def test_full_record_is_reproducible_from_its_digests(self):
        result = by_design(load())['stratified']
        self.assertEqual(result['reproducible']['issued_digest'], result['plan']['issued_digest'])
        self.assertTrue(result['reproducible']['observations_digest'])
        self.assertEqual(result['reproducible']['estimate'], result['estimate'])
        self.assertEqual(result['reproducible']['decision'], result['decision'])
        # Two runs with unchanged evidence must agree exactly.
        self.assertEqual(result, by_design(load())['stratified'])

    def test_observations_digest_changes_with_a_signed_review(self):
        before = estimate(load(), 'PLAN-SRS-2026-11-02')['plan']['observations_digest']
        data = load()
        data['observations'][0]['review'] = {'reviewer': 'R', 'decision': 'overturn',
                                             'signed_at': '2026-11-04T10:00:00+08:00',
                                             'signature': 'sig'}
        after = estimate(data, 'PLAN-SRS-2026-11-02')['plan']['observations_digest']
        self.assertNotEqual(before, after)
        # The issued plan seal itself is untouched by later review evidence.
        self.assertEqual(
            estimate(data, 'PLAN-SRS-2026-11-02')['plan']['issued_digest'],
            estimate(load(), 'PLAN-SRS-2026-11-02')['plan']['issued_digest'],
        )

    def test_existing_term_fixtures_still_drive_the_workflow(self):
        from termsettle.core import connect, import_file
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / 'state.sqlite')
            for subsystem in ('source', 'term', 'delivery'):
                import_file(db, subsystem, ROOT / f'{subsystem}-ok.json')
            self.assertEqual(10, db.execute('SELECT COUNT(*) FROM current_decisions').fetchone()[0])
            db.close()


if __name__ == '__main__':
    unittest.main()
