import json
import unittest
from pathlib import Path

from termsettle.availability import remaining
from termsettle.time_capture import booked_hours, resolve

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'worktime-overlap.json'


def rows():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


class WorktimeSeedTest(unittest.TestCase):
    def test_fixture_is_the_pinned_ambiguous_seed(self):
        seed = rows()
        self.assertEqual([(r['project'], r['device'], r['start'], r['end']) for r in seed],
                         [('J1', 'phone', 9, 13), ('J2', 'laptop', 9, 13)])
        self.assertNotIn('tz', seed[0])
        self.assertNotIn('date', seed[0])

    def test_overlapping_timers_are_summed(self):
        # 旧夹具与旧报告：朴素逐行相加的基线被保留
        self.assertEqual(booked_hours(rows()), 8)
        cap = remaining(rows())
        self.assertEqual(cap['legacy'], {'booked': 8, 'remaining': 0})

    def test_dual_worlds_stay_unresolved_without_anchor(self):
        # 双世界：同一 JSON 的两种物理世界都保留，不任取笔记本偏移
        cap = remaining(rows())
        self.assertFalse(cap['resolved'])
        self.assertEqual(cap['effective'], {'min': 4, 'max': 8})
        self.assertEqual(cap['remaining'], {'min': 0, 'max': 4})
        worlds = {(w['offsets'].get('phone'), w['offsets'].get('laptop')): w['effective']
                  for w in cap['worlds']}
        self.assertEqual(worlds[(0, 0)], 4)   # 两设备同报 9-13：同一物理四小时
        self.assertEqual(worlds[(0, 4)], 8)   # 笔记本整体慢四小时：J2 实际 13-17
        self.assertEqual(cap['projects'], {})  # 未锚定前不做跨设备项目归属

    def test_evidence_path_duplicate_sync_pending_split(self):
        # 补证路径一：9 点启动同步事件 + 项目操作日志 → 同一四小时重复计时
        evidence = [
            {'type': 'sync', 'at': 9, 'readings': {'phone': 9, 'laptop': 9},
             'note': '手机与笔记本共同拍到的 9 点启动同步事件'},
            {'type': 'operation-log', 'device': 'laptop',
             'entries': [{'at': 9, 'action': 'timer-start', 'session': 'timer-b'},
                         {'at': 13, 'action': 'timer-stop', 'session': 'timer-b'}],
             'note': '项目操作日志证明 J1/J2 是同一四小时的重复计时'},
        ]
        cap = remaining(rows(), evidence=evidence)
        self.assertTrue(cap['resolved'])
        self.assertEqual(cap['effective'], {'min': 4, 'max': 4})
        self.assertEqual(cap['remaining'], {'min': 4, 'max': 4})
        self.assertEqual(cap['projects']['J1'], {'intervals': [[9, 13]], 'hours': 4})
        self.assertEqual(cap['projects']['J2'], {'intervals': [], 'hours': 0})
        pend = [p for p in cap['pending'] if p['fragments'] == ['timer-b']]
        self.assertEqual(len(pend), 1)  # J2 记录待拆分
        self.assertEqual(pend[0]['range'], [9, 13])
        self.assertEqual(pend[0]['candidates'], ['J1', 'J2'])
        self.assertEqual(pend[0]['reason'], 'pending-split')
        # 原始计时、旧报告与未分配片段都保留
        self.assertEqual({f['session'] for f in cap['fragments']}, {'timer-a', 'timer-b'})
        self.assertEqual(cap['legacy'], {'booked': 8, 'remaining': 0})

    def test_evidence_path_calibration_and_signoff(self):
        # 补证路径二：笔记本可核对校时日志 + J1 交付后 J2 启动签认 → J2 实际 13-17
        evidence = [
            {'type': 'calibration', 'device': 'laptop', 'offset': 4,
             'source': '笔记本可核对的校时日志'},
            {'type': 'signoff', 'project': 'J2', 'kind': 'start', 'at': 13,
             'after': {'project': 'J1', 'kind': 'end'}, 'by': 'pm',
             'note': 'J1 交付后 J2 启动签认'},
        ]
        cap = remaining(rows(), evidence=evidence)
        self.assertTrue(cap['resolved'])
        self.assertEqual(cap['effective'], {'min': 8, 'max': 8})
        self.assertEqual(cap['remaining'], {'min': 0, 'max': 0})
        self.assertEqual(cap['projects']['J1'], {'intervals': [[9, 13]], 'hours': 4})
        self.assertEqual(cap['projects']['J2'], {'intervals': [[13, 17]], 'hours': 4})
        self.assertEqual(cap['pending'], [])
        self.assertEqual(cap['legacy'], {'booked': 8, 'remaining': 0})
        # 只有校时、没有签认时手机仍未锚定：双世界依旧，不能只凭单条证据定案
        partial = remaining(rows(), evidence=evidence[:1])
        self.assertFalse(partial['resolved'])
        self.assertEqual(partial['effective'], {'min': 4, 'max': 8})

    def test_device_offset_consistent_and_late_calibration_scoped(self):
        # 设备偏移在整个计时会话内一致，不能逐段挑选偏移来满足预算
        rows_ = [
            {'id': 's1', 'account': 't', 'project': 'J1', 'device': 'laptop',
             'start': 9, 'end': 11, 'date': '2026-11-24'},
            {'id': 's2', 'account': 't', 'project': 'J1', 'device': 'laptop',
             'start': 14, 'end': 16, 'date': '2026-11-24'},
        ]
        cal = [{'type': 'calibration', 'device': 'laptop', 'offset': 4, 'date': '2026-11-24'}]
        cap = remaining(rows_, evidence=cal)
        physical = {f['session']: f['physical'] for f in cap['fragments']}
        self.assertEqual(physical['s1'], [[13, 15]])
        self.assertEqual(physical['s2'], [[18, 20]])  # 同设备同日同一偏移
        conflict = cal + [{'type': 'calibration', 'device': 'laptop', 'offset': 2,
                           'date': '2026-11-24'}]
        with self.assertRaises(ValueError):
            remaining(rows_, evidence=conflict)
        # 迟到校时只修订同设备相关日期
        rows2 = rows_ + [{'id': 's3', 'account': 't', 'project': 'J1', 'device': 'laptop',
                          'start': 9, 'end': 11, 'date': '2026-11-25'}]
        reports = {r['date']: r for r in resolve(rows2, evidence=cal)}
        self.assertEqual(reports['2026-11-25']['fragments'][0]['physical'], [[9, 11]])
        self.assertEqual({f['session'] for f in reports['2026-11-24']['fragments']}, {'s1', 's2'})

    def test_retransmission_idempotent_and_conflict_rejected(self):
        # 同一设备重复上传的片段按原始会话 ID 幂等
        sync = [{'type': 'sync', 'at': 9, 'readings': {'phone': 9, 'laptop': 9}}]
        replay = rows() + [dict(rows()[1])]
        cap = remaining(replay, evidence=sync)
        self.assertEqual(cap['effective'], {'min': 4, 'max': 4})
        self.assertEqual(cap['remaining'], {'min': 4, 'max': 4})
        self.assertEqual(cap['dedup'], {'replayed': ['timer-b'], 'kept': 2})
        mutated = rows() + [dict(rows()[1], end=14)]
        with self.assertRaises(ValueError):
            remaining(mutated, evidence=sync)

    def test_project_switch_split_and_uncertain_range(self):
        # 项目切换：无可证明时间线时保留不确定范围，有切换证据时按点分割
        rows_ = [
            {'id': 's1', 'account': 't', 'project': 'J1', 'device': 'phone', 'start': 9, 'end': 13},
            {'id': 's2', 'account': 't', 'project': 'J2', 'device': 'phone', 'start': 11, 'end': 15},
        ]
        cap = remaining(rows_)
        self.assertTrue(cap['resolved'])  # 同一设备同一时钟
        self.assertEqual(cap['effective'], {'min': 6, 'max': 6})
        self.assertEqual(cap['projects']['J1']['hours'], 4)
        self.assertEqual(cap['projects']['J2']['hours'], 2)
        self.assertEqual(len(cap['pending']), 1)
        self.assertEqual(cap['pending'][0]['range'], [11, 13])  # 尚不确定的工时范围保留
        self.assertEqual(cap['pending'][0]['candidates'], ['J1', 'J2'])
        switch = [{'type': 'switch', 'device': 'phone', 'at': 12, 'from': 'J1', 'to': 'J2'}]
        cap2 = remaining(rows_, evidence=switch)
        self.assertEqual(cap2['projects']['J1'], {'intervals': [[9, 12]], 'hours': 3})
        self.assertEqual(cap2['projects']['J2'], {'intervals': [[12, 15]], 'hours': 3})
        self.assertEqual(cap2['pending'], [])

    def test_pauses_and_declared_timezone(self):
        # 活动暂停扣减有效区间；声明时区换算到共同时间线
        pause_rows = [{'id': 's1', 'account': 't', 'project': 'J1', 'device': 'phone',
                       'start': 9, 'end': 17, 'pauses': [[12, 13]]}]
        cap = remaining(pause_rows)
        self.assertEqual(cap['effective'], {'min': 7, 'max': 7})
        self.assertEqual(cap['legacy'], {'booked': 8, 'remaining': 0})
        tz_rows = [
            {'id': 's1', 'account': 't', 'project': 'J1', 'device': 'phone',
             'start': 9, 'end': 13, 'tz': 0},
            {'id': 's2', 'account': 't', 'project': 'J2', 'device': 'laptop',
             'start': 9, 'end': 13, 'tz': '-04:00'},
        ]
        cap = remaining(tz_rows)
        self.assertTrue(cap['resolved'])
        self.assertEqual(cap['effective'], {'min': 8, 'max': 8})
        self.assertEqual(cap['projects']['J2']['intervals'], [[13, 17]])

    def test_contradictory_clock_evidence_rejected(self):
        # 校时日志与签认矛盾时拒绝采录，不能只修改单条 start 造成设备内矛盾
        evidence = [
            {'type': 'calibration', 'device': 'laptop', 'offset': 4},
            {'type': 'signoff', 'project': 'J2', 'kind': 'start', 'at': 14,
             'after': {'project': 'J1', 'kind': 'end'}},
        ]
        with self.assertRaises(ValueError):
            remaining(rows(), evidence=evidence)


if __name__ == '__main__':
    unittest.main()
