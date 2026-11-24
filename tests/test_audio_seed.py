import copy
import json
import unittest
from pathlib import Path

from termsettle.audio_capture import (
    AUX_GRADE, MAIN_GRADE, RECON_GRADE, TRANSCODE_GRADE,
    build_ledger, canonical_digest, map_transcode, received,
)
from termsettle.meeting_archive import (
    STATUS_COMBINED, STATUS_COMPLETE, STATUS_GAP,
    MeetingArchive, assess, build_certificate,
)

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'review-audio.json'


def seal(sha, receipt, sealer='主麦封存'):
    return {'receipt': receipt, 'sealer': sealer, 'sha256': sha,
            'sealed_at': '2026-11-20T10:30:00Z'}


def make_block(block_id, samples, parity, meeting='M1'):
    payload = {'block': block_id, 'samples': samples, 'parity': parity,
               'device': 'main', 'waveform_seed': block_id}
    return {
        'kind': 'parity_block', 'meeting_id': meeting, 'id': block_id,
        'samples': samples, 'parity': parity, 'payload': payload,
        'seal': seal(canonical_digest(payload), 'SEAL-' + block_id),
    }


def make_index(block_rows, meeting='M1', signed='0-287999'):
    index = {row['id']: row['seal']['sha256'] for row in block_rows}
    return {
        'kind': 'signed_index', 'meeting_id': meeting, 'id': 'IDX-MAIN',
        'signed_samples': signed, 'sample_index': index,
        'seal': seal(canonical_digest(index), 'SEAL-IDX', '主麦签名采样索引'),
    }


def make_backup(meeting='M1', samples='192000-239999', sealed=True,
                upload_id='b1', recorder='room-backup', rate=48000,
                fingerprint='FP-ROOM-003'):
    row = {
        'upload_id': upload_id, 'meeting_id': meeting, 'kind': 'upload',
        'recorder': recorder, 'samples': samples, 'codec': 'pcm',
        'header': 'RIFF/WAVE-PCM', 'sample_rate': rate,
        'channels': {'left': samples, 'right': samples, 'sync': 'locked'},
        'rate_changes': [], 'silence': [],
        'sync': {'local_rate': rate, 'meeting_rate': 48000,
                 'anchor_local_sample': int(samples.split('-')[0]),
                 'anchor_meeting_sample': int(samples.split('-')[0])},
        'byte_hash': 'hash-%s-bytes' % upload_id,
        'decoded_hash': 'hash-%s-decoded' % upload_id,
        'device_fingerprint': fingerprint,
    }
    if sealed:
        row['seal'] = {**seal('seal-%s-sha256' % upload_id, 'SEAL-' + upload_id, '房间备录封存'),
                       'device_fingerprint': fingerprint}
    return row


class AudioSeedTest(unittest.TestCase):
    def setUp(self):
        self.rows = json.loads(FIXTURE.read_text(encoding='utf-8'))

    # ---- 原始夹具：计数法失效，物理采样账暴露缺段 ----
    def test_transcoded_repeat_masquerades_as_final_chunk(self):
        # 旧事实保留：仍有三个上传号、两个声明采样区间
        uploads = [r for r in self.rows if r.get('kind', 'upload') == 'upload']
        self.assertEqual(len({r.get('upload_id') for r in uploads}), 3)
        self.assertEqual(len({r['samples'] for r in uploads}), 2)

        intake = received(self.rows)
        self.assertEqual(intake['upload_ids'], 3)
        self.assertEqual(intake['physical_intervals'], 2)
        self.assertEqual(intake['transcoded_duplicates'], 1)
        self.assertEqual(intake['missing_samples'], 96000)
        self.assertEqual(intake['covered_samples'], 192000)

        result = assess(self.rows)
        self.assertEqual(result['uploads'], 3)
        self.assertEqual(result['status'], STATUS_GAP)
        self.assertEqual(result['gaps'],
                         [{'start': 192000, 'end': 287999, 'count': 96000,
                           'required_grade': MAIN_GRADE,
                           'reason': result['gaps'][0]['reason']}])

    def test_three_distinct_hashes_still_same_waveform(self):
        a2, a3 = (r for r in self.rows if r.get('upload_id') in ('a2', 'a3'))
        # 字节哈希、解码哈希、文件头、采样率全部不同
        self.assertNotEqual(a2['byte_hash'], a3['byte_hash'])
        self.assertNotEqual(a2['decoded_hash'], a3['decoded_hash'])
        self.assertNotEqual(a2['header'], a3['header'])
        self.assertNotEqual(a2['sample_rate'], a3['sample_rate'])
        ledger = build_ledger(self.rows)
        dup = next(d for d in ledger['duplicates'] if d['upload_id'] == 'a3')
        self.assertTrue(dup['mapping_ok'])
        self.assertTrue(dup['provenance_ok'])
        self.assertEqual(dup['root'], 'a2')
        self.assertEqual(dup['grade'], TRANSCODE_GRADE)
        self.assertEqual(dup['mapped'], {'start': 96000, 'end': 191999, 'count': 96000})
        # 转码重复片没有填补 192000-287999
        self.assertEqual([(g['start'], g['end']) for g in ledger['gaps']],
                         [(192000, 287999)])

    # ---- 重采样映射 ----
    def test_resample_map_44100_back_to_48000_axis(self):
        a3 = next(r for r in self.rows if r.get('upload_id') == 'a3')
        declared, computed, ok = map_transcode(a3)
        self.assertEqual(declared, (96000, 191999))
        self.assertEqual(computed, (96000, 191999))
        self.assertTrue(ok)

    def test_tampered_resample_mapping_is_flagged(self):
        rows = copy.deepcopy(self.rows)
        a3 = next(r for r in rows if r.get('upload_id') == 'a3')
        a3['transcode']['target_rate'] = 32000
        ledger = build_ledger(rows)
        dup = next(d for d in ledger['duplicates'] if d['upload_id'] == 'a3')
        self.assertFalse(dup['mapping_ok'])
        self.assertFalse(dup['provenance_ok'])
        # 来源声明被否后，缺段判定保持不变，绝不采信其覆盖主张
        self.assertEqual([(g['start'], g['end']) for g in ledger['gaps']],
                         [(192000, 287999)])

    # ---- 静音是有效样本；真正丢采给精确缺段 ----
    def test_silence_counts_as_original_samples(self):
        ledger = build_ledger(self.rows)
        self.assertEqual(len(ledger['silence']), 2)
        quiet_starts = sorted(s['span']['start'] for s in ledger['silence'])
        self.assertEqual(quiet_starts, [2000, 120000])
        # 静音区间在覆盖内，缺口仍是 192000 起，而非静音处
        self.assertEqual(ledger['gaps'][0]['start'], 192000)

    def test_silence_outside_span_is_defect_not_coverage(self):
        rows = copy.deepcopy(self.rows)
        a1 = next(r for r in rows if r.get('upload_id') == 'a1')
        a1['silence'].append('95000-96500')
        ledger = build_ledger(rows)
        self.assertTrue(any(d['upload_id'] == 'a1' for d in ledger['channel_defects']))
        self.assertEqual([(g['start'], g['end']) for g in ledger['gaps']],
                         [(192000, 287999)])

    # ---- 双声道同步、采样率变化、封存回执采录 ----
    def test_rate_change_and_locked_channels_are_recorded(self):
        ledger = build_ledger(self.rows)
        self.assertEqual(len(ledger['rate_changes']), 1)
        change = ledger['rate_changes'][0]
        self.assertEqual(change['from_rate'], 44100)
        self.assertEqual(change['to_rate'], 48000)
        self.assertEqual(change['at_sample'], 48000)
        self.assertEqual(ledger['channel_defects'], [])
        sealed = {item['upload_id'] for item in ledger['seals']}
        self.assertEqual(sealed, {'a1', 'a2', 'a3'})

    def test_unlocked_channels_become_defect(self):
        rows = copy.deepcopy(self.rows)
        a1 = next(r for r in rows if r.get('upload_id') == 'a1')
        a1['channels'] = {'left': '0-95999', 'right': '100-96099', 'sync': 'drift'}
        ledger = build_ledger(rows)
        self.assertEqual(len(ledger['channel_defects']), 1)
        self.assertEqual(ledger['channel_defects'][0]['upload_id'], 'a1')
        # 声道缺陷不伪造缺口，也不抹掉真实缺口
        self.assertEqual([(g['start'], g['end']) for g in ledger['gaps']],
                         [(192000, 287999)])

    def test_late_seal_receipt_only_revises_its_meeting(self):
        rows = copy.deepcopy(self.rows)
        unsealed = copy.deepcopy(self.rows)
        for row in unsealed:
            row.pop('seal', None)
        ledger_before = build_ledger(unsealed)
        self.assertIn('a1', ledger_before['unsealed_uploads'])
        late = {'kind': 'seal', 'meeting_id': 'M1', 'upload_id': 'a1',
                'receipt': 'SEAL-A1-LATE', 'sealer': '主麦封存',
                'sha256': 'seal-a1-sha256', 'sealed_at': '2026-11-21T09:00:00Z'}
        ledger_after = build_ledger(unsealed + [late])
        self.assertNotIn('a1', ledger_after['unsealed_uploads'])
        self.assertTrue(any(s['receipt'] == 'SEAL-A1-LATE' for s in ledger_after['seals']))

    # ---- 独立录音设备不得冒充主麦原始采样 ----
    def test_independent_device_stays_auxiliary_grade(self):
        backup = make_backup(samples='192000-239999')
        ledger = build_ledger(self.rows + [backup])
        aux = next(a for a in ledger['auxiliary'] if a['upload_id'] == 'b1')
        self.assertEqual(aux['grade'], AUX_GRADE)
        self.assertEqual(aux['recorder'], 'room-backup')
        covered_recorders = {c['recorder'] for c in ledger['covered']}
        self.assertEqual(covered_recorders, {'main'})
        # 即便备录覆盖了缺口起点，主麦缺口依然精确存在
        self.assertEqual([(g['start'], g['end']) for g in ledger['gaps']],
                         [(192000, 287999)])

    def test_transcoded_relay_copy_cannot_serve_as_auxiliary_original(self):
        ledger = build_ledger(self.rows)
        # a3 是转码副本，不能出现在备录原件候选里
        self.assertEqual({a['upload_id'] for a in ledger['auxiliary']}, set())
        self.assertEqual({d['upload_id'] for d in ledger['duplicates']}, {'a3'})

    def test_unsealed_backup_is_not_trusted(self):
        backup = make_backup(sealed=False)
        archive = MeetingArchive()
        cert = archive.receive(self.rows + [backup])
        self.assertEqual(cert['status'], STATUS_GAP)
        self.assertEqual(cert['recovery']['auxiliary']['ok'], False)
        self.assertEqual([(g['start'], g['end']) for g in cert['residual_gaps']],
                         [(192000, 287999)])

    # ---- 恢复路径 A：奇偶冗余 + 签名采样索引 ----
    def test_path_a_parity_reconstruction_issues_full_certificate(self):
        blocks = [make_block('pb-even', '192000-239999', '偶'),
                  make_block('pb-odd', '240000-287999', '奇')]
        evidence = self.rows + blocks + [make_index(blocks)]
        archive = MeetingArchive()
        cert = archive.receive(evidence)
        self.assertEqual(cert['status'], STATUS_COMPLETE)
        self.assertEqual(cert['recovery']['path'], 'A')
        self.assertEqual(cert['gaps'], [])
        self.assertEqual(cert['residual_gaps'], [])
        self.assertEqual(cert['required_capture'], [])
        recon = [t for t in cert['relistenable'] if t['grade'] == RECON_GRADE]
        self.assertEqual([(t['span']['start'], t['span']['end']) for t in recon],
                         [(192000, 287999)])
        self.assertTrue(all(t['recorder'] == 'main' for t in recon))
        self.assertEqual(sorted(cert['recovery']['block_ids']),
                         ['pb-even', 'pb-odd'])

    def test_path_a_fails_on_hash_mismatch(self):
        blocks = [make_block('pb-even', '192000-239999', '偶'),
                  make_block('pb-odd', '240000-287999', '奇')]
        blocks[1]['payload']['waveform_seed'] = 'tampered'
        evidence = self.rows + blocks + [make_index(blocks)]
        archive = MeetingArchive()
        cert = archive.receive(evidence)
        self.assertEqual(cert['status'], STATUS_GAP)
        self.assertFalse(cert['recovery']['parity']['ok'])
        self.assertIn('校验不符', cert['recovery']['parity']['reason'])

    def test_path_a_fails_when_blocks_miss_part_of_gap(self):
        blocks = [make_block('pb-even', '192000-239999', '偶')]
        evidence = self.rows + blocks + [make_index(blocks)]
        archive = MeetingArchive()
        cert = archive.receive(evidence)
        self.assertEqual(cert['status'], STATUS_GAP)
        self.assertFalse(cert['recovery']['parity']['ok'])
        self.assertEqual([(g['start'], g['end']) for g in cert['residual_gaps']],
                         [(192000, 287999)])

    def test_path_a_fails_without_sealed_index(self):
        blocks = [make_block('pb-even', '192000-239999', '偶'),
                  make_block('pb-odd', '240000-287999', '奇')]
        index = make_index(blocks)
        index.pop('seal')
        archive = MeetingArchive()
        cert = archive.receive(self.rows + blocks + [index])
        self.assertEqual(cert['status'], STATUS_GAP)
        self.assertIn('签名采样索引', cert['recovery']['parity']['reason'])

    # ---- 恢复路径 B：房间备录原件组合证书 ----
    def test_path_b_combined_certificate_keeps_residual_gap(self):
        backup = make_backup(samples='192000-239999')
        archive = MeetingArchive()
        cert = archive.receive(self.rows + [backup])
        self.assertEqual(cert['status'], STATUS_COMBINED)
        self.assertEqual(cert['recovery']['path'], 'B')

        by_grade = {}
        for track in cert['relistenable']:
            by_grade.setdefault(track['grade'], []).append(
                (track['span']['start'], track['span']['end']))
        # 主麦段保持主麦等级
        self.assertEqual(by_grade[MAIN_GRADE], [(0, 95999), (96000, 191999)])
        # 辅助段保持备录设备等级，绝不转写为主麦原件
        self.assertEqual(by_grade[AUX_GRADE], [(192000, 239999)])
        aux = next(t for t in cert['relistenable'] if t['grade'] == AUX_GRADE)
        self.assertTrue(aux['auxiliary'])
        self.assertEqual(aux['recorder'], 'room-backup')
        # 240000-287999 精确保留缺段并要求主麦补采
        self.assertEqual([(g['start'], g['end']) for g in cert['residual_gaps']],
                         [(240000, 287999)])
        self.assertEqual(cert['required_capture'],
                         [{'start': 240000, 'end': 287999, 'count': 48000,
                           'required_grade': MAIN_GRADE}])

    def test_two_recovery_paths_differ_in_range_grade_and_capture(self):
        blocks = [make_block('pb-even', '192000-239999', '偶'),
                  make_block('pb-odd', '240000-287999', '奇')]
        cert_a = MeetingArchive().receive(self.rows + blocks + [make_index(blocks)])
        cert_b = MeetingArchive().receive(self.rows + [make_backup()])

        self.assertEqual(cert_a['status'], STATUS_COMPLETE)
        self.assertEqual(cert_b['status'], STATUS_COMBINED)
        self.assertEqual(cert_a['required_capture'], [])
        self.assertEqual(sum(g['count'] for g in cert_b['required_capture']), 48000)
        grades_a = {t['grade'] for t in cert_a['relistenable']}
        grades_b = {t['grade'] for t in cert_b['relistenable']}
        self.assertIn(RECON_GRADE, grades_a)
        self.assertNotIn(AUX_GRADE, grades_a)
        self.assertIn(AUX_GRADE, grades_b)
        self.assertNotIn(RECON_GRADE, grades_b)

    # ---- 旧错误报告留痕与修订链 ----
    def test_legacy_complete_report_is_retained_and_superseded(self):
        archive = MeetingArchive()
        cert = archive.receive(self.rows)
        history = archive.history('M1')
        self.assertEqual(len(history), 2)
        legacy, current = history
        self.assertTrue(legacy['legacy'])
        self.assertEqual(legacy['status'], STATUS_COMPLETE)
        self.assertTrue(legacy['retained'])
        self.assertEqual(current['status'], STATUS_GAP)
        self.assertEqual(current['supersedes'], legacy['certificate_id'])
        self.assertEqual(archive.legacy_report('M1')['report_id'], 'LEGACY-M1-R1')
        self.assertEqual(cert['certificate_id'], current['certificate_id'])

    def test_late_block_revises_only_its_own_meeting(self):
        other = [
            {'kind': 'session', 'meeting_id': 'M2', 'main_recorder': 'main',
             'expected_samples': '0-47999', 'meeting_rate': 48000, 'channels': 2},
            {'upload_id': 'c1', 'meeting_id': 'M2', 'kind': 'upload',
             'recorder': 'main', 'samples': '0-47999', 'codec': 'pcm',
             'sample_rate': 48000,
             'channels': {'left': '0-47999', 'right': '0-47999', 'sync': 'locked'},
             'rate_changes': [], 'silence': [],
             'seal': seal('seal-c1', 'SEAL-C1')},
        ]
        archive = MeetingArchive()
        archive.receive(self.rows)
        m2_first = archive.receive(other)
        self.assertEqual(archive.certificate('M2')['status'], STATUS_COMPLETE)

        blocks = [make_block('pb-even', '192000-239999', '偶', meeting='M1'),
                  make_block('pb-odd', '240000-287999', '奇', meeting='M1')]
        m1_revised = archive.receive(self.rows + blocks + [make_index(blocks)])

        self.assertEqual(m1_revised['status'], STATUS_COMPLETE)
        self.assertEqual(archive.certificate('M2')['certificate_id'],
                         m2_first['certificate_id'])
        self.assertEqual(len(archive.history('M2')), 1)
        self.assertEqual([h['revision'] for h in archive.history('M1')], [0, 1, 2])
        self.assertEqual(sorted(archive.meetings()), ['M1', 'M2'])

    # ---- 覆盖账不接受文件数/哈希/时长替代 ----
    def test_duration_and_hashes_never_drive_coverage(self):
        ledger = build_ledger(self.rows)
        # 覆盖完全由原始样本号区间构成
        self.assertEqual(
            [(c['span']['start'], c['span']['end']) for c in ledger['covered']],
            [(0, 95999), (96000, 191999)])
        # a3 时长、哈希不同但映射后落在 a2 区间，只进 duplicates
        self.assertEqual(len(ledger['duplicates']), 1)
        self.assertEqual(ledger['covered_union'],
                         [{'start': 0, 'end': 191999, 'count': 192000}])

    def test_certificate_is_deterministic(self):
        c1 = build_certificate(build_ledger(self.rows), self.rows, revision=2)
        c2 = build_certificate(build_ledger(self.rows), self.rows, revision=2)
        self.assertEqual(c1['certificate_id'], c2['certificate_id'])

    # ---- 跨会议隔离与设备冒名 ----
    def test_late_block_for_other_meeting_never_enters_ledger(self):
        blocks = [make_block('pb-even', '192000-239999', '偶', meeting='M2'),
                  make_block('pb-odd', '240000-287999', '奇', meeting='M2')]
        index = make_index(blocks, meeting='M2')
        ledger = build_ledger(self.rows + blocks + [index], meeting_id='M1')
        # M1 的覆盖账里看不到任何 M2 的冗余证据，缺段原样保留
        self.assertEqual([(g['start'], g['end']) for g in ledger['gaps']],
                         [(192000, 287999)])
        self.assertEqual(ledger['meeting_id'], 'M1')

    def test_independent_device_cannot_claim_main_grade_even_if_named_main(self):
        # 备录设备把 recorder 改成主麦名，但封存指纹仍是备录设备的：
        # 设备身份核验失败，按冒名隔离，不能计入主麦覆盖，也不能走恢复路径 A。
        imposter = make_backup(recorder='main', upload_id='evil',
                               samples='192000-287999',
                               fingerprint='FP-ROOM-003')
        ledger = build_ledger(self.rows + [imposter])
        self.assertEqual([s['upload_id'] for s in ledger['spoofs']], ['evil'])
        self.assertTrue(all('冒作' in s['reason'] for s in ledger['spoofs']))
        self.assertEqual({c['upload_id'] for c in ledger['covered']},
                         {'a1', 'a2'})
        archive = MeetingArchive()
        cert = archive.receive(self.rows + [imposter])
        self.assertEqual(cert['status'], STATUS_GAP)
        self.assertEqual([(g['start'], g['end']) for g in cert['residual_gaps']],
                         [(192000, 287999)])
        # 真正的主麦奇偶冗余与签名索引到达后才能走 A，冒名件不参与重构
        blocks = [make_block('pb-even', '192000-239999', '偶'),
                  make_block('pb-odd', '240000-287999', '奇')]
        cert_ok = archive.receive(self.rows + [imposter] + blocks + [make_index(blocks)])
        self.assertEqual(cert_ok['status'], STATUS_COMPLETE)
        self.assertEqual(cert_ok['recovery']['path'], 'A')

    def test_spoofed_backup_with_relay_recorder_stays_auxiliary(self):
        backup = make_backup(recorder='room-backup', samples='192000-239999')
        ledger = build_ledger(self.rows + [backup])
        # 独立设备原件永远落在 auxiliary，主麦 covered 中只有 main
        self.assertEqual({c['recorder'] for c in ledger['covered']}, {'main'})
        self.assertEqual(
            [(a['upload_id'], a['grade']) for a in ledger['auxiliary']],
            [('b1', AUX_GRADE)])


if __name__ == '__main__':
    unittest.main()
