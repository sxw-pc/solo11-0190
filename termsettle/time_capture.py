"""Offline work-time capture: sessions, device clocks, dedup and physical-overlap resolution.

Raw timer rows keep device-local readings.  A natural person's physically
overlapping time is allocated only once: fragments are placed on a common
physical timeline exclusively through evidenced clock anchors (sync events,
verifiable calibrations, manual sign-offs, declared timezones).  A device
clock offset is a property of the whole device-day, never of a single
fragment, and segments re-uploaded by the same device are idempotent by
original session id.  Without a cross-device anchor the ambiguity is
reported as candidate worlds; no offset is picked merely because it would
satisfy the budget.  Original rows, the legacy naive report and every
unallocatable fragment are preserved in each resolution report.
"""


def booked_hours(records):
    """Legacy naive total: sum of end-start per row (the preserved old report)."""
    return sum(row['end'] - row['start'] for row in records)


def _r(x):
    v = round(float(x), 9)
    return 0.0 if v == 0 else v


def _tz(value):
    """Timezone offset in hours; accepts numbers or '+HH:MM' / '-HH:MM' strings."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = value.strip()
    sign = 1
    if text.startswith(('+', '-')):
        sign = -1 if text[0] == '-' else 1
        text = text[1:]
    if ':' in text:
        hours, minutes = text.split(':', 1)
        return sign * (int(hours) + int(minutes) / 60)
    return sign * float(text)


def _fragments(records):
    frags = []
    for order, row in enumerate(records):
        session = row.get('session') or row.get('id')
        if session is None:
            raise ValueError('timer row requires id or session')
        start, end = float(row['start']), float(row['end'])
        if not start < end:
            raise ValueError('timer row with start >= end')
        pauses = []
        for p in row.get('pauses', []):
            s, e = float(p[0]), float(p[1])
            if not (start <= s < e <= end):
                raise ValueError('pause outside timer row')
            pauses.append((s, e))
        frags.append({'session': session, 'device': row['device'], 'account': row['account'],
                      'project': row['project'], 'date': row.get('date'), 'start': start, 'end': end,
                      'pauses': pauses, 'tz': _tz(row.get('tz')), 'order': order, 'raw': row})
    return frags


def _dedup(frags):
    """Retransmitted segments are idempotent by original session id per device."""
    seen, kept, replayed = {}, [], []
    for f in frags:
        key = (f['device'], f['session'])
        sig = (f['account'], f['project'], f['date'], f['start'], f['end'], tuple(f['pauses']), f['tz'])
        if key in seen:
            if seen[key] != sig:
                raise ValueError('conflicting retransmission of session %r' % (f['session'],))
            replayed.append(f['session'])
            continue
        seen[key] = sig
        kept.append(f)
    return kept, replayed


def _pick(frags, project, session=None):
    cands = [f for f in frags if f['project'] == project and (session is None or f['session'] == session)]
    if len(cands) != 1:
        raise ValueError('evidence does not identify a unique session for project %r' % (project,))
    return cands[0]


def _device_date(frags, device, ev):
    if 'date' in ev:
        return ev['date']
    dates = {f['date'] for f in frags if f['device'] == device}
    if len(dates) == 1:
        return dates.pop()
    raise ValueError('evidence for device %r requires an explicit date' % (device,))


def _device_tz(frags, device):
    for f in frags:
        if f['device'] == device and f['tz'] is not None:
            return f['tz']
    return 0.0


def _offsets(frags, evidence):
    """Clock offsets per (device, date) derived from authorized evidence.

    One offset covers the whole device-day, so a timing session keeps a
    single consistent offset; contradictory evidence is rejected instead of
    letting per-segment offsets be picked to satisfy the budget.
    """
    offsets, switches, applied = {}, [], []

    def set_offset(device, date, value, source):
        key = (device, date)
        if key in offsets and offsets[key][0] != value:
            raise ValueError('inconsistent clock evidence for device %r' % (device,))
        offsets[key] = (value, source)

    for ev in evidence or []:
        kind = ev.get('type')
        if kind == 'calibration':
            set_offset(ev['device'], ev.get('date'), float(ev['offset']), 'calibration')
        elif kind == 'sync':
            at = float(ev['at'])
            for device, reading in ev['readings'].items():
                tz = _device_tz(frags, device)
                set_offset(device, _device_date(frags, device, ev), at - (float(reading) - tz), 'sync')
        elif kind == 'signoff':
            at = float(ev['at'])
            f = _pick(frags, ev['project'], ev.get('session'))
            local = f['start'] if ev.get('kind', 'start') == 'start' else f['end']
            set_offset(f['device'], f['date'], at - (local - (f['tz'] or 0.0)), 'signoff')
            after = ev.get('after')
            if after:
                g = _pick(frags, after['project'], after.get('session'))
                local = g['end'] if after.get('kind', 'end') == 'end' else g['start']
                set_offset(g['device'], g['date'], at - (local - (g['tz'] or 0.0)), 'signoff-after')
        elif kind == 'switch':
            switches.append(ev)
        elif kind == 'operation-log':
            pass
        else:
            raise ValueError('unknown evidence type %r' % (kind,))
        applied.append(ev)
    return offsets, switches, applied


def _subtract_pauses(start, end, pauses):
    spans, cur = [], start
    for s, e in sorted(pauses):
        if cur < s:
            spans.append((cur, s))
        cur = max(cur, e)
    if cur < end:
        spans.append((cur, end))
    return spans


def _merge(intervals):
    out = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1] + 1e-9:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def _measure(intervals):
    return sum(e - s for s, e in _merge(intervals))


def _cut(intervals, span):
    s, e = span
    out = []
    for a, b in intervals:
        if b <= s + 1e-9 or a >= e - 1e-9:
            out.append((a, b))
        else:
            if a < s - 1e-9:
                out.append((a, s))
            if b > e + 1e-9:
                out.append((e, b))
    return out


def _attribute(frags, switches):
    """Allocate each physical sub-interval once inside one anchored group.

    The first uploaded claim stands; later overlapping claims are suspended
    as pending-split ranges.  A provable project switch splits a contested
    range at its evidenced point.
    """
    points = sorted({p for f in frags for iv in f['physical'] for p in iv})
    certain, parts = {}, []
    for a, b in zip(points, points[1:]):
        if b - a <= 1e-9:
            continue
        claimants = [f for f in frags if any(s <= a + 1e-9 and b <= e + 1e-9 for s, e in f['physical'])]
        if not claimants:
            continue
        first = min(claimants, key=lambda f: f['order'])
        certain.setdefault(first['project'], []).append((a, b))
        projects = sorted({f['project'] for f in claimants})
        for f in claimants:
            if f is not first:
                parts.append({'frag': f, 'range': (a, b), 'candidates': projects})
    pending = []
    ordered = []
    for p in parts:
        if not any(p['frag'] is q for q in ordered):
            ordered.append(p['frag'])
    for f in ordered:
        own = [p for p in parts if p['frag'] is f]
        cands = sorted({c for p in own for c in p['candidates']})
        for s, e in _merge([p['range'] for p in own]):
            pending.append({'fragments': [f['session']], 'device': f['device'], 'range': [s, e],
                            'candidates': cands, 'reason': 'pending-split'})
    for sw in switches:
        shift = next((f['shift'] for f in frags if f['device'] == sw['device']), None)
        if shift is None:
            continue
        point = float(sw['at']) + shift
        for pend in list(pending):
            lo, hi = pend['range']
            if (pend['device'] == sw['device'] and lo + 1e-9 < point < hi - 1e-9
                    and {sw['from'], sw['to']} <= set(pend['candidates'])):
                for proj in list(certain):
                    certain[proj] = _cut(certain[proj], (lo, hi))
                certain.setdefault(sw['from'], []).append((lo, point))
                certain.setdefault(sw['to'], []).append((point, hi))
                pending.remove(pend)
    return {p: _merge(ivs) for p, ivs in certain.items()}, pending


def _world_offsets(group, shift):
    return {f['device']: _r(f['shift'] + shift) for f in group['frags']}


def _resolve_day(account, date, frags, offsets, switches, day_hours, applied, replayed, records):
    groups = {}
    for f in frags:
        evidenced = offsets.get((f['device'], f['date']))
        tz = f['tz'] if f['tz'] is not None else 0.0
        if evidenced is not None:
            f['shift'], key = evidenced[0] - tz, 'common'
        elif f['tz'] is not None:
            f['shift'], key = -tz, 'common'
        else:
            f['shift'], key = 0.0, ('device', f['device'])
        f['physical'] = [(s + f['shift'], e + f['shift'])
                         for s, e in _subtract_pauses(f['start'], f['end'], f['pauses'])]
        groups.setdefault(key, []).append(f)
    keys = sorted(groups, key=lambda k: (0 if k == 'common' else 1, min(f['order'] for f in groups[k])))
    infos, pending = [], []
    for key in keys:
        fs = groups[key]
        certain, pend = _attribute(fs, switches)
        intervals = [iv for f in fs for iv in f['physical']]
        infos.append({'key': key, 'frags': fs, 'intervals': intervals,
                      'hours': _measure(intervals), 'certain': certain})
        pending.extend(pend)
    resolved = len(infos) == 1
    if resolved:
        g = infos[0]
        eff = (g['hours'], g['hours'])
        worlds = [{'offsets': _world_offsets(g, 0.0), 'effective': _r(g['hours'])}]
        projects = {f['project']: [] for f in frags}
        for proj, ivs in g['certain'].items():
            projects.setdefault(proj, []).extend(ivs)
        projects = {p: _merge(ivs) for p, ivs in projects.items()}
    elif len(infos) == 2:
        ref, other = infos
        shifts = {0.0}
        for a0, a1 in _merge(ref['intervals']):
            for b0, b1 in _merge(other['intervals']):
                shifts |= {a0 - b0, a0 - b1, a1 - b0, a1 - b1}
        worlds = []
        for s in sorted(shifts):
            moved = [(b0 + s, b1 + s) for b0, b1 in other['intervals']]
            worlds.append({'offsets': {**_world_offsets(ref, 0.0), **_world_offsets(other, s)},
                           'effective': _r(_measure(list(ref['intervals']) + moved))})
        vals = [w['effective'] for w in worlds]
        eff, projects = (min(vals), max(vals)), {}
    else:
        hours = [g['hours'] for g in infos]
        eff, worlds, projects = (max(hours), sum(hours)), [], {}
    booked = booked_hours([r for r in records if r['account'] == account and r.get('date') == date])
    return {
        'account': account, 'date': date, 'day_hours': day_hours, 'resolved': resolved,
        'effective': {'min': _r(eff[0]), 'max': _r(eff[1])},
        'remaining': {'min': _r(day_hours - eff[1]), 'max': _r(day_hours - eff[0])},
        'worlds': worlds,
        'projects': {p: {'intervals': [[_r(s), _r(e)] for s, e in ivs], 'hours': _r(_measure(ivs))}
                     for p, ivs in projects.items()},
        'pending': [{**p, 'range': [_r(p['range'][0]), _r(p['range'][1])]} for p in pending],
        'groups': [{'devices': sorted({f['device'] for f in g['frags']}),
                    'frame': 'common' if g['key'] == 'common' else 'device:%s' % (g['key'][1],),
                    'intervals': [[_r(s), _r(e)] for s, e in _merge(g['intervals'])],
                    'hours': _r(g['hours'])} for g in infos],
        'fragments': [{'session': f['session'], 'device': f['device'], 'project': f['project'],
                       'local': [_r(f['start']), _r(f['end'])],
                       'pauses': [[_r(s), _r(e)] for s, e in f['pauses']],
                       'physical': [[_r(s), _r(e)] for s, e in f['physical']],
                       'status': 'pending-split' if any(f['session'] in p['fragments'] for p in pending)
                       else 'effective'}
                      for f in sorted(frags, key=lambda f: f['order'])],
        'dedup': {'replayed': list(replayed), 'kept': len(frags)},
        'evidence': applied,
        'legacy': {'booked': _r(booked), 'remaining': _r(day_hours - booked)},
    }


def resolve(records, evidence=None, day_hours=8):
    """Resolve raw timer rows plus authorized evidence into per-day capacity reports.

    One report per (account, date).  Each report carries the per-project
    effective work intervals, the remaining-capacity range, candidate worlds
    while cross-device clocks stay unanchored, pending unallocatable
    fragments, the dedup outcome and the preserved legacy naive report.
    """
    kept, replayed = _dedup(_fragments(records))
    offsets, switches, applied = _offsets(kept, evidence)
    days = {}
    for f in kept:
        days.setdefault((f['account'], f['date']), []).append(f)
    return [_resolve_day(account, date, fs, offsets, switches, day_hours, applied, replayed, records)
            for (account, date), fs in sorted(days.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1])))]
