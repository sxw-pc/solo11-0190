"""审校会谈录音的物理采样映射与采样覆盖账。

覆盖账的唯一坐标是同一录音设备的**原始采样时轴**（设备自身时钟下连续
编号的原始样本号）。以下材料都不能证明声波覆盖：

* 上传文件数 / upload_id 数——同一物理片可被转码后重新上报；
* 压缩字节哈希、文件头、解码后哈希——有损转码（PCM->AAC、重采样）会改写
  全部字节与采样值，哈希必然不同，却仍是同一物理声波；
* 音频时长——采样率变化时时长与样本数不再成固定比例。

因此转码副本只能凭转码链回溯到源片，映射回源片所在的原始区间，永远不能
填补源片之外的缺口；独立录音设备的可信原件只能作为辅助音轨，不得标注为
主麦原始采样；静音是真实采到的有效原始样本，不计缺口。
"""
import hashlib
import json
from collections import OrderedDict

# 来源等级：数值越小，越接近主麦设备原始采样
MAIN_GRADE = '主麦原始'        # 主麦设备自身封存的原始样本
RECON_GRADE = '冗余重构'       # 主麦奇偶冗余块按签名采样索引重构并验证
AUX_GRADE = '备录原件'         # 独立录音设备封存的原件，只能作辅助音轨
TRANSCODE_GRADE = '转码副本'   # 自主麦片转码/重采样得到的副本

GRADES = (MAIN_GRADE, RECON_GRADE, AUX_GRADE, TRANSCODE_GRADE)

# 重采样取整允许的采样点偏差
MAPPING_TOLERANCE = 2


def canonical_digest(value):
    """确定性 JSON 摘要，供冗余块载荷与签名采样索引对账。"""
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True,
                   separators=(',', ':')).encode('utf-8')).hexdigest()


def parse_span(text):
    """解析闭区间采样号 'start-end'，返回 (start, end)。"""
    start, end = str(text).split('-')
    start, end = int(start), int(end)
    if end < start:
        raise ValueError(f'illegal sample span: {text}')
    return start, end


def span_dict(span):
    return {'start': span[0], 'end': span[1], 'count': span[1] - span[0] + 1}


def merge_spans(spans):
    """合并相交或相邻的闭区间。"""
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [tuple(item) for item in merged]


def subtract_spans(expected, covered):
    """从期望区间 expected 中扣除 covered，返回精确缺口闭区间列表。"""
    exp_start, exp_end = expected
    holes = []
    cursor = exp_start
    for start, end in merge_spans(covered):
        if start > cursor:
            holes.append((cursor, min(start - 1, exp_end)))
        cursor = max(cursor, end + 1)
        if cursor > exp_end:
            break
    if cursor <= exp_end:
        holes.append((cursor, exp_end))
    return holes


def _kind(row):
    return row.get('kind', 'upload')


def _select_meeting(rows, meeting_id):
    labeled = [row for row in rows if row.get('meeting_id') is not None]
    if meeting_id is None:
        ids = {row['meeting_id'] for row in labeled}
        if len(ids) > 1:
            raise ValueError('rows span multiple meetings; meeting_id is required')
        meeting_id = next(iter(ids), None)
    if labeled:
        # 有会议标签时，无标签行不得并入，迟到块只修订对应会议
        scoped = [row for row in labeled if row['meeting_id'] == meeting_id]
    else:
        # 兼容无标签的最小夹具：全部视为同一会议
        scoped = list(rows)
    return meeting_id, scoped


def _seal_of(row, late_seals):
    """封存回执：随件封存或迟到的封存回执行均可。"""
    if isinstance(row.get('seal'), dict) and row['seal'].get('receipt') and row['seal'].get('sha256'):
        return dict(row['seal'])
    late = late_seals.get(row.get('upload_id'))
    if late:
        return {key: late[key] for key in ('receipt', 'sealer', 'sha256', 'sealed_at') if key in late}
    return None


def is_trusted(row):
    """可信原件须有封存回执（冗余块/签名索引同理）。"""
    seal = row.get('seal')
    return isinstance(seal, dict) and bool(seal.get('receipt') and seal.get('sha256'))


def map_transcode(row):
    """按转码映射把副本本地采样号映射回主麦原始采样轴。

    返回 (声明源区间, 实算区间, mapping_ok)。重采样比例由两侧采样率推出，
    实算区间与转码声明不一致即 provenance_mismatch，不得采信其覆盖主张。
    """
    tc = row['transcode']
    declared = parse_span(tc.get('source_samples') or row['samples'])
    local = parse_span(row['local_samples'])
    scale = tc['source_rate'] / tc['target_rate']
    anchor_local = tc.get('anchor_local_sample', local[0])
    anchor_meeting = tc.get('anchor_meeting_sample', declared[0])
    computed = (
        anchor_meeting + round((local[0] - anchor_local) * scale),
        anchor_meeting + round((local[1] - anchor_local) * scale),
    )
    ok = (
        abs(computed[0] - declared[0]) <= MAPPING_TOLERANCE
        and abs(computed[1] - declared[1]) <= MAPPING_TOLERANCE
    )
    return declared, computed, ok


def map_to_meeting(row):
    """把任意录音行的本地样本号映射到会议主采样轴。"""
    if 'transcode' in row:
        declared, computed, ok = map_transcode(row)
        return declared if ok else computed
    sync = row.get('sync')
    start, end = parse_span(row['samples'])
    if not sync:
        return start, end
    rate = sync.get('meeting_rate', row['sample_rate']) / sync['local_rate']
    return (
        sync['anchor_meeting_sample'] + round((start - sync['anchor_local_sample']) * rate),
        sync['anchor_meeting_sample'] + round((end - sync['anchor_local_sample']) * rate),
    )


def resolve_chain(row, by_id):
    """沿 derived_from 回溯转码链，返回 (链上 upload_id, 根行)。"""
    chain = []
    current = row
    seen = set()
    while current is not None and current.get('upload_id') not in seen:
        uid = current.get('upload_id')
        chain.append(uid)
        seen.add(uid)
        tc = current.get('transcode')
        if not tc:
            break
        current = by_id.get(tc.get('derived_from'))
    return chain, current


def grade_of(row, main_recorder):
    if 'transcode' in row:
        return TRANSCODE_GRADE
    if row.get('recorder') == main_recorder:
        return MAIN_GRADE
    return AUX_GRADE


def _default_expected(uploads, main_recorder):
    ends = [map_to_meeting(row)[1] for row in uploads
            if row.get('recorder') == main_recorder and 'transcode' not in row]
    return (0, max(ends)) if ends else (0, 0)


def build_ledger(rows, meeting_id=None, main_recorder=None, expected=None):
    """生成单个会议的采样覆盖账（纯函数，不写库、不看时钟）。"""
    evidence_rows = [row for row in rows if _kind(row) != 'legacy_report']
    meeting_id, scoped = _select_meeting(evidence_rows, meeting_id)
    session = next((row for row in scoped if _kind(row) == 'session'), {})
    main_recorder = main_recorder or session.get('main_recorder', 'main')
    if expected is None:
        if session.get('expected_samples'):
            expected = parse_span(session['expected_samples'])
        else:
            expected = _default_expected(
                [row for row in scoped if _kind(row) == 'upload'], main_recorder)

    late_seals = {row['upload_id']: row for row in scoped if _kind(row) == 'seal'}
    uploads = [row for row in scoped if _kind(row) == 'upload']
    by_id = {row['upload_id']: row for row in uploads}

    # 设备身份：会话清单是主麦身份的权威登记；封存回执里的设备指纹须与之相符，
    # 否则即便是 recorder 字段写成主麦名，也按冒名隔离，不得计入主麦覆盖。
    registry = session.get('authorized_devices')
    if registry:
        main_fingerprint = next(
            (d.get('fingerprint') for d in registry
             if d.get('recorder') == main_recorder and d.get('role') == '主麦'), None)
        known_fingerprints = {d.get('fingerprint') for d in registry if d.get('fingerprint')}
    else:
        main_fingerprint, known_fingerprints = None, set()

    covered, duplicates, auxiliary, spoofs = [], [], [], []
    silence, rate_changes, channel_defects, seals = [], [], [], []
    unsealed_uploads, devices = [], OrderedDict()

    for row in uploads:
        uid = row['upload_id']
        recorder = row['recorder']
        device = devices.setdefault(recorder, {'recorder': recorder,
                                               'role': '主麦' if recorder == main_recorder else '独立设备',
                                               'uploads': [], 'transcoded': []})
        seal = _seal_of(row, late_seals)
        if seal:
            seals.append({'upload_id': uid, **seal})
        else:
            unsealed_uploads.append(uid)
        grade = grade_of(row, main_recorder)
        mapped = map_to_meeting(row)
        fingerprint = row.get('device_fingerprint') or (seal or {}).get('device_fingerprint')
        claims_main = recorder == main_recorder and 'transcode' not in row
        identity_ok = True
        if claims_main and main_fingerprint is not None:
            identity_ok = fingerprint == main_fingerprint
        elif claims_main and fingerprint and known_fingerprints and fingerprint not in known_fingerprints:
            identity_ok = False

        for change in row.get('rate_changes', []):
            rate_changes.append({'recorder': recorder, **change})

        channels = row.get('channels')
        if isinstance(channels, dict):
            left, right = parse_span(channels['left']), parse_span(channels['right'])
            if left != right or channels.get('sync') != 'locked':
                channel_defects.append({'upload_id': uid,
                                        'defect': '双声道不同步或声道区间不一致',
                                        'left': span_dict(left), 'right': span_dict(right),
                                        'sync': channels.get('sync')})

        if 'transcode' in row:
            # 转码副本：只做来源映射与留痕，绝不并入主麦覆盖
            declared, computed, mapping_ok = map_transcode(row)
            chain, root = resolve_chain(row, by_id)
            root_main = root is not None and root.get('recorder') == main_recorder and 'transcode' not in root
            within_root = root is not None and _within(mapped, map_to_meeting(root))
            provenance_ok = mapping_ok and root_main and within_root
            duplicates.append({
                'upload_id': uid, 'recorder': recorder, 'grade': grade,
                'mapped': span_dict(mapped), 'declared': span_dict(declared),
                'computed': span_dict(computed), 'mapping_ok': mapping_ok,
                'provenance_ok': provenance_ok,
                'root': root.get('upload_id') if root else None,
                'chain': chain,
                'codec': row['codec'], 'sample_rate': row['sample_rate'],
                'byte_hash': row.get('byte_hash'), 'decoded_hash': row.get('decoded_hash'),
                'header': row.get('header', row['codec']),
                'sealed': seal is not None,
            })
            device['transcoded'].append(uid)
            continue

        if claims_main and not identity_ok:
            # 冒名主麦：隔离留痕，不进主麦覆盖，也不进合法备录候选
            spoofs.append({
                'upload_id': uid, 'declared_recorder': recorder,
                'device_fingerprint': fingerprint,
                'expected_fingerprint': main_fingerprint,
                'span': span_dict(mapped), 'sealed': seal is not None,
                'reason': '封存设备指纹与主麦登记不符，独立设备不得冒作主麦原始采样',
            })
            device['transcoded'].append(uid)
            continue

        device['uploads'].append(uid)
        entry = {'upload_id': uid, 'recorder': recorder, 'grade': grade,
                 'span': span_dict(mapped), 'sealed': seal is not None,
                 'codec': row['codec'], 'sample_rate': row['sample_rate'],
                 'byte_hash': row.get('byte_hash')}

        for text in row.get('silence', []):
            quiet = parse_span(text)
            if not _within(quiet, mapped):
                channel_defects.append({'upload_id': uid,
                                        'defect': f'静音区间 {text} 超出本片原始采样区间',
                                        'sync': None})
                continue
            silence.append({'upload_id': uid, 'span': span_dict(quiet)})

        if recorder == main_recorder:
            covered.append(entry)
        else:
            # 独立设备原件：单列辅助音轨，任何情况下不冒充主麦原始采样
            auxiliary.append({**entry, 'auxiliary': True})

    covered_union = merge_spans(
        parse_span(f"{item['span']['start']}-{item['span']['end']}") for item in covered)
    gap_reason = ('主麦原始采样缺失：转码副本不得顶替缺口，独立设备原件不得标注为主麦采样')
    gaps = [
        {'start': start, 'end': end, 'count': end - start + 1,
         'required_grade': MAIN_GRADE, 'reason': gap_reason}
        for start, end in subtract_spans(expected, covered_union)
    ]

    return {
        'meeting_id': meeting_id,
        'main_recorder': main_recorder,
        'expected': span_dict(expected),
        'covered': covered,
        'covered_union': [span_dict(span) for span in covered_union],
        'gaps': gaps,
        'duplicates': duplicates,
        'auxiliary': auxiliary,
        'spoofs': spoofs,
        'silence': silence,
        'rate_changes': rate_changes,
        'channel_defects': channel_defects,
        'seals': seals,
        'unsealed_uploads': unsealed_uploads,
        'devices': list(devices.values()),
    }


def _within(inner, outer):
    return outer[0] <= inner[0] and inner[1] <= outer[1]


def received(rows, meeting_id=None):
    """采录 intake 统计：按物理采样区间计数，而非按 upload_id 计数。"""
    ledger = build_ledger(rows, meeting_id)
    covered_samples = sum(item['count'] for item in ledger['covered_union'])
    expected_samples = ledger['expected']['count']
    return {
        'meeting_id': ledger['meeting_id'],
        'upload_ids': len(ledger['covered']) + len(ledger['duplicates'])
        + len(ledger['auxiliary']) + len(ledger['spoofs']),
        'recorders': sorted({item['recorder'] for item in ledger['devices']}),
        'physical_intervals': len(ledger['covered']),
        'covered_union': len(ledger['covered_union']),
        'covered_samples': covered_samples,
        'expected_samples': expected_samples,
        'missing_samples': max(0, expected_samples - covered_samples),
        'transcoded_duplicates': len(ledger['duplicates']),
        'auxiliary_devices': sorted({item['recorder'] for item in ledger['auxiliary']}),
        'spoofed_uploads': [item['upload_id'] for item in ledger['spoofs']],
        'unsealed_uploads': list(ledger['unsealed_uploads']),
    }
