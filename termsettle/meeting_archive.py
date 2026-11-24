"""审校会谈录音归档：完整性裁定与可复听证书。

完整性由 :mod:`termsettle.audio_capture` 的物理采样覆盖账驱动，文件数、
压缩字节哈希、解码哈希、时间排序与 SQLite 事务都不参与覆盖判断。

两条经授权的恢复路径（双方对相同三份录音和已发的错误“完整”状态认可）：

A. 取得主麦记录的奇偶冗余块与签名采样索引，且重构验证通过——签发
   主录音全段“完整”证书，缺口由 ``冗余重构`` 等级补齐；
B. 主麦冗余不存在，但取得房间备录设备覆盖到缺口内的可信原件——签发
   “组合”证书：主录音 0–191999 保持主麦等级，辅助 192000–239999 保持
   备录设备等级，240000–287999 精确保留缺段，备录永不转写为主麦原件。

旧的计数式错误报告和每次重裁都按修订留痕；迟到块只修订对应会议。
本模块不裁决术语译法或稿费。
"""
import hashlib
import json

from . import audio_capture as ac
from .audio_capture import (
    AUX_GRADE, MAIN_GRADE, RECON_GRADE, build_ledger,
    parse_span, span_dict, subtract_spans,
)

STATUS_COMPLETE = '完整'
STATUS_GAP = '缺段'
STATUS_COMBINED = '组合'


def verify_parity_recovery(rows, meeting_id, gaps):
    """恢复路径 A：签名采样索引 + 奇偶冗余块重构并逐块校验。"""
    rows = [row for row in rows
            if row.get('meeting_id', meeting_id) == meeting_id
            and row.get('kind') in ('parity_block', 'signed_index')]
    index = next((row for row in rows if row['kind'] == 'signed_index'), None)
    blocks = [row for row in rows if row['kind'] == 'parity_block']
    if index is None or not blocks or not ac.is_trusted(index):
        return {'ok': False, 'reason': '缺少可信的签名采样索引或奇偶冗余块'}
    expected_range = parse_span(index['signed_samples'])
    if not all(_gap_inside(gap, expected_range) for gap in gaps):
        return {'ok': False, 'reason': '签名采样索引未覆盖全部缺段'}
    parity = {'奇', '偶'}
    seen = {}
    for block in blocks:
        span = parse_span(block['samples'])
        if block.get('parity') not in parity:
            return {'ok': False, 'reason': f"冗余块 {block['id']} 奇偶标识缺失"}
        if not ac.is_trusted(block):
            return {'ok': False, 'reason': f"冗余块 {block['id']} 缺少封存回执"}
        digest = ac.canonical_digest(block['payload'])
        if digest != block['seal']['sha256'] or digest != index.get('sample_index', {}).get(block['id']):
            return {'ok': False, 'reason': f"冗余块 {block['id']} 与签名采样索引校验不符"}
        if block['id'] in seen and seen[block['id']] != span:
            return {'ok': False, 'reason': f"冗余块 {block['id']} 同号异区间"}
        seen[block['id']] = span
    uncovered = subtract_spans((gaps[0]['start'], gaps[-1]['end']),
                               [span for span in seen.values()]) if gaps else []
    uncovered = [hole for hole in uncovered
                 if any(_overlap(hole, (gap['start'], gap['end'])) for gap in gaps)]
    if uncovered:
        return {'ok': False, 'reason': '奇偶冗余块未覆盖全部缺段',
                'uncovered': [span_dict(hole) for hole in uncovered]}
    return {
        'ok': True, 'grade': RECON_GRADE, 'index_id': index['id'],
        'block_ids': sorted(seen), 'reconstructed': [span_dict((g['start'], g['end'])) for g in gaps],
    }


def _gap_inside(gap, outer):
    return outer[0] <= gap['start'] and gap['end'] <= outer[1]


def _overlap(a, b):
    return a[0] <= b[1] and b[0] <= a[1]


def verify_auxiliary_recovery(ledger):
    """恢复路径 B：备录设备可信原件覆盖缺口，但不冒充主麦。"""
    gap_span = (ledger['gaps'][0]['start'], ledger['gaps'][-1]['end']) if ledger['gaps'] else None
    candidates, usable = [], []
    for item in ledger['auxiliary']:
        if not item['sealed']:
            continue
        span = (item['span']['start'], item['span']['end'])
        if gap_span is None or not _overlap(span, gap_span):
            continue
        candidate = dict(item)
        candidate['usable'] = span[0] <= gap_span[0]
        candidates.append(candidate)
        if candidate['usable']:
            usable.append(candidate)
    if not usable:
        return {'ok': False, 'reason': '无可信备录原件覆盖缺口起点',
                'candidates': candidates}
    return {'ok': True, 'grade': AUX_GRADE, 'candidates': candidates,
            'uploads': sorted(item['upload_id'] for item in usable)}


def assess(rows, expected=3, meeting_id=None):
    """由物理采样覆盖账裁定完整性（expected 参数仅为兼容旧签名，不参与判断）。"""
    ledger = build_ledger(rows, meeting_id)
    cert = build_certificate(ledger, rows, revision=1)
    return {
        'uploads': ac.received(rows, meeting_id)['upload_ids'],
        'status': cert['status'],
        'meeting_id': cert['meeting_id'],
        'expected_samples': cert['expected']['count'],
        'gaps': cert['gaps'],
    }


def _relisten_from_ledger(ledger):
    tracks = []
    for item in ledger['covered']:
        tracks.append({'upload_id': item['upload_id'], 'recorder': item['recorder'],
                       'grade': item['grade'], 'span': item['span'],
                       'sealed': item['sealed']})
    return tracks


def build_certificate(ledger, rows, revision, supersedes=None):
    """按覆盖账与两条恢复路径生成可复听证书。"""
    base = {
        'certificate_id': None,
        'meeting_id': ledger['meeting_id'],
        'revision': revision,
        'status': STATUS_GAP,
        'expected': ledger['expected'],
        'relistenable': _relisten_from_ledger(ledger),
        'gaps': list(ledger['gaps']),
        'auxiliary': list(ledger['auxiliary']),
        'duplicates': list(ledger['duplicates']),
        'spoofs': list(ledger.get('spoofs', [])),
        'silence': list(ledger['silence']),
        'rate_changes': list(ledger['rate_changes']),
        'channel_defects': list(ledger['channel_defects']),
        'seals': list(ledger['seals']),
        'devices': list(ledger['devices']),
        'recovery': None,
        'residual_gaps': list(ledger['gaps']),
        'required_capture': [
            {'start': gap['start'], 'end': gap['end'], 'count': gap['count'],
             'required_grade': MAIN_GRADE} for gap in ledger['gaps']
        ],
        'supersedes': supersedes,
        'basis': '物理采样覆盖账：同一设备原始采样时轴；文件数、字节/解码哈希与时长均不作覆盖依据',
    }

    gaps = ledger['gaps']
    if not gaps:
        base['status'] = STATUS_COMPLETE
        base['required_capture'] = []
        base['residual_gaps'] = []
        base['recovery'] = {'path': None, 'note': '主麦原始采样已覆盖全段，无需恢复'}
    else:
        parity = verify_parity_recovery(rows, ledger['meeting_id'], gaps)
        if parity['ok']:
            base['status'] = STATUS_COMPLETE
            base['recovery'] = {'path': 'A', **parity}
            for span in parity['reconstructed']:
                base['relistenable'].append(
                    {'upload_id': None, 'recorder': ledger['main_recorder'],
                     'grade': RECON_GRADE, 'span': span, 'sealed': True,
                     'reconstructed_from': parity['block_ids'],
                     'index_id': parity['index_id']})
            base['gaps'] = []
            base['residual_gaps'] = []
            base['required_capture'] = []
        else:
            auxiliary = verify_auxiliary_recovery(ledger)
            if auxiliary['ok']:
                base['status'] = STATUS_COMBINED
                base['recovery'] = {'path': 'B', **auxiliary}
                gap_start = gaps[0]['start']
                for item in ledger['auxiliary']:
                    if not item['sealed']:
                        continue
                    span = (item['span']['start'], item['span']['end'])
                    if span[0] <= gap_start and _overlap(span, (gap_start, gaps[-1]['end'])):
                        base['relistenable'].append(
                            {'upload_id': item['upload_id'], 'recorder': item['recorder'],
                             'grade': AUX_GRADE, 'span': item['span'],
                             'sealed': True, 'auxiliary': True})
                covered_aux = merge_aux(
                    (item['span']['start'], item['span']['end'])
                    for item in base['relistenable'] if item['grade'] == AUX_GRADE)
                remaining = []
                for gap in gaps:
                    remaining.extend(subtract_spans((gap['start'], gap['end']), covered_aux))
                base['residual_gaps'] = [
                    {'start': start, 'end': end, 'count': end - start + 1,
                     'required_grade': MAIN_GRADE,
                     'reason': '备录原件未覆盖该段；仍须主麦补采，备录不得转写为主麦原件'}
                    for start, end in remaining]
                base['required_capture'] = [
                    {'start': gap['start'], 'end': gap['end'], 'count': gap['count'],
                     'required_grade': MAIN_GRADE} for gap in base['residual_gaps']]
            else:
                base['recovery'] = {'path': None, 'parity': parity, 'auxiliary': auxiliary}

    material = json.dumps(
        {'meeting_id': base['meeting_id'], 'revision': base['revision'],
         'status': base['status'], 'relistenable': base['relistenable'],
         'gaps': base['gaps'], 'residual_gaps': base['residual_gaps']},
        ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    base['certificate_id'] = 'CERT-' + hashlib.sha256(material).hexdigest()[:16]
    return base


def merge_aux(spans):
    return ac.merge_spans(list(spans))


class MeetingArchive:
    """会议归档库（纯内存、确定性；可由外层 SQLite 持久化，不依赖时钟）。"""

    def __init__(self):
        self._rows = {}
        self._history = {}
        self._legacy = {}

    def receive(self, rows, meeting_id=None):
        """摄入一会议的全部采录行并重裁；迟到块只修订该会议。"""
        ledger = build_ledger(rows, meeting_id)
        mid = ledger['meeting_id']
        self._rows[mid] = list(rows)
        history = self._history.setdefault(mid, [])

        legacy = next((row for row in rows
                       if row.get('kind') == 'legacy_report'
                       and row.get('meeting_id', mid) == mid), None)
        if mid not in self._legacy and legacy is not None:
            self._legacy[mid] = dict(legacy)
            history.append({
                'certificate_id': legacy.get('report_id', 'LEGACY-' + mid),
                'meeting_id': mid, 'revision': 0,
                'status': legacy.get('status', STATUS_COMPLETE),
                'legacy': True,
                'basis': legacy.get('basis', '计数式裁定：upload_id 达到 3 即标完整（错误，已停用）'),
                'retained': True,
            })

        supersedes = history[-1]['certificate_id'] if history else None
        revision = sum(1 for item in history if not item.get('legacy')) + 1
        cert = build_certificate(ledger, self._rows[mid], revision, supersedes)
        history.append(cert)
        return cert

    def certificate(self, meeting_id):
        return self._history[meeting_id][-1]

    def history(self, meeting_id):
        return list(self._history[meeting_id])

    def legacy_report(self, meeting_id):
        return self._legacy.get(meeting_id)

    def meetings(self):
        return sorted(self._history)
