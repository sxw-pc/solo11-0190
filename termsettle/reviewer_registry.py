"""审校员身份、任命、回避与签署证据的采录和资格计算。

本模块是 injunction→禁令 定版入口的“人”的账本。核心约束：

* 帐号只是帐号。只有采录在案的显式证据（人事名册、帐号绑定、跨机构
  别名、任命原件、独立身份核验等）才能把帐号归到某个自然人；
* 绝不因为“姓名相同”而自动合并，姓名字段仅作展示；
* 关联不明或证据冲突的帐号一律标记为 ``待核``，不得默认成两个独立人；
* 同一自然人控制的多个帐号最多产生一票（按自然人去重，而不是按
  ``reviewer_id`` 去重）；
* 任命必须覆盖该票的投票时点，否则 ``appointment_inactive``；
* 该译文的译者本人、或已就该术语版本申报回避的人，其票不得计入；
* 反对票不进入赞成名册，也不能抵充两票赞成名额。

``evaluate`` 只依据采录在 ``ReviewerRegistry`` 中的证据计算，纯函数式
读取，便于 ``term_ballot`` 生成可复核名册与版本化决议。
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections import defaultdict

# 身份归并状态（中文标签直接出现在名册/决议中）
STATUS_VERIFIED = "已核验"
STATUS_LINKED = "已关联"
STATUS_PENDING = "待核"
STATUS_CONFLICT = "证据冲突"

REASON_TEXT = {
    "identity_pending": "帐号与自然人关联不明，显式待核，不能按独立人计票",
    "identity_conflict": "帐号指向多个自然人，身份证据冲突，待核",
    "appointment_missing": "查无覆盖该帐号/自然人的任命记录",
    "appointment_inactive": "任命在该次投票时点尚未生效或已经失效",
    "translator_conflict": "本人是此译文的译者，应当回避，该票不得计入",
    "recused": "本人已就该术语版本申报回避，该票不得计入",
    "withdrawn": "该签署选票已经撤回，只能在新决议版本中剔除",
    "other_version": "该票赞成的是其他译文版本，不计入本版本",
    "out_of_scope": "该票属于其他术语，不在本次重算范围",
}

# 选票中属于“签署/摘要”元数据的字段，计算选票内容摘要时剔除
_BALLOT_META_KEYS = {
    "signature",
    "signature_ref",
    "signature_evidence",
    "payload_sha256",
    "withdrawals",
}


def canonical(value):
    """与 termsettle.core 一致的规范化 JSON。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_obj(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def ballot_digest(ballot):
    """选票正文（不含签名件与撤票轨迹）的 SHA-256，供复核签名/内容一致性。"""
    payload = {k: v for k, v in ballot.items() if k not in _BALLOT_META_KEYS}
    return sha256_obj(payload)


def _date(value):
    """把 ISO 日期/时间归一为 YYYY-MM-DD；无法解析时返回 None。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:10]


class ReviewerRegistry:
    """人员、帐号别名、任命、回避与身份证据的 append-only 采录簿。

    所有 ``add_*`` / ``bind_*`` 方法都只追加证据；同 ID 同内容幂等，
    同 ID 不同内容抛 ``ValueError``（与主账本“异内容事件 ID 冲突回滚”
    的口径一致）。调用方传入的 dict 会做深拷贝，事后修改不会渗入账本。
    """

    def __init__(self):
        self.evidence_records = {}
        self.evidence_order = []
        self.persons = {}
        self.bindings = []          # {account_id, person_id, evidence_id}
        self.aliases = []           # {account_a, account_b, person_id, evidence_id}
        self.account_meta = {}      # account_id -> {institution, ...}
        self.appointments = []
        self.recusals = []
        self.translator_assignments = []
        self.identity_checks = []
        self.signature_checks = []

    # ------------------------------------------------------------------
    # 证据簿
    # ------------------------------------------------------------------
    def add_evidence(self, evidence_id, kind="document", **fields):
        if evidence_id in self.evidence_records:
            existing = self.evidence_records[evidence_id]
            candidate = {"id": evidence_id, "kind": kind, **fields}
            if existing != candidate:
                raise ValueError(f"evidence id {evidence_id!r} reused with different content")
            return copy.deepcopy(existing)
        record = {"id": evidence_id, "kind": kind}
        record.update(copy.deepcopy(fields))
        self.evidence_records[evidence_id] = record
        self.evidence_order.append(evidence_id)
        return copy.deepcopy(record)

    def _remember(self, evidence_id, kind, evidence):
        """登记随采录记录一并给出的证据摘要，返回 evidence_id。"""
        if evidence_id is None and isinstance(evidence, dict):
            evidence_id = evidence.get("id")
        if evidence_id is None:
            raise ValueError("evidence_id is required for evidence-backed intake")
        if evidence_id not in self.evidence_records:
            fields = dict(evidence) if isinstance(evidence, dict) else {}
            fields.setdefault("kind", kind)
            self.add_evidence(evidence_id, **fields)
        return evidence_id

    def snapshot_evidence(self, evidence_ids=None):
        """深拷贝证据簿；默认全量，指定 ids 时按给定顺序截取。"""
        if evidence_ids is None:
            ids = list(self.evidence_order)
        else:
            ids = list(dict.fromkeys(evidence_ids))
        return [copy.deepcopy(self.evidence_records[i]) for i in ids if i in self.evidence_records]

    def state_fingerprint_data(self):
        """供决议版本判断“证据状态是否变化”的可序列化状态。"""
        return {
            "evidence": [self.evidence_records[i] for i in self.evidence_order],
            "persons": self.persons,
            "bindings": self.bindings,
            "aliases": self.aliases,
            "account_meta": self.account_meta,
            "appointments": self.appointments,
            "recusals": self.recusals,
            "translator_assignments": self.translator_assignments,
            "identity_checks": self.identity_checks,
            "signature_checks": self.signature_checks,
        }

    # ------------------------------------------------------------------
    # 人员与帐号身份采录
    # ------------------------------------------------------------------
    def add_person(self, person_id, display_name=None, **profile):
        if person_id in self.persons:
            raise ValueError(f"person {person_id!r} already recorded")
        person = {"person_id": person_id, "display_name": display_name}
        person.update(copy.deepcopy(profile))
        self.persons[person_id] = person
        return copy.deepcopy(person)

    def bind_account(self, account_id, person_id, *, evidence_id=None, evidence=None,
                     institution=None):
        """凭显式证据把机构帐号绑定到自然人。这是归并身份的唯一入口。"""
        evidence_id = self._remember(evidence_id, "account_person_binding", evidence)
        if institution:
            self.account_meta.setdefault(account_id, {})["institution"] = institution
        row = {"account_id": account_id, "person_id": person_id, "evidence_id": evidence_id}
        if row not in self.bindings:
            self.bindings.append(row)
        return copy.deepcopy(row)

    def add_account_alias(self, account_a, account_b, *, evidence_id=None, evidence=None,
                          person_id=None):
        """采录跨机构帐号别名/双机构帐号绑定。

        即使暂未给出 ``person_id``，显式别名边也足以把两个帐号认定为
        同一自然人；没有任何显式证据时，系统永不自行连边。
        """
        evidence_id = self._remember(evidence_id, "cross_institution_account_alias", evidence)
        row = {
            "account_a": account_a,
            "account_b": account_b,
            "person_id": person_id,
            "evidence_id": evidence_id,
        }
        if row not in self.aliases:
            self.aliases.append(row)
        if person_id is not None:
            self.bind_account(account_a, person_id, evidence_id=evidence_id)
            self.bind_account(account_b, person_id, evidence_id=evidence_id)
        return copy.deepcopy(row)

    # ------------------------------------------------------------------
    # 任命、回避、译者委派、身份与签名核验采录
    # ------------------------------------------------------------------
    def add_appointment(self, appointment_id, *, person_id=None, account_id=None,
                        institution=None, effective_from=None, effective_to=None,
                        title="审校员", evidence_id=None, evidence=None):
        if person_id is None and account_id is None:
            raise ValueError("appointment requires person_id or account_id")
        evidence_id = self._remember(evidence_id, "appointment_original", evidence)
        row = {
            "appointment_id": appointment_id,
            "person_id": person_id,
            "account_id": account_id,
            "institution": institution,
            "title": title,
            "effective_from": _date(effective_from),
            "effective_to": _date(effective_to),
            "evidence_id": evidence_id,
        }
        self.appointments.append(row)
        # 任命原件同时具名帐号与自然人时，它本身就是一条绑定证据。
        if person_id is not None and account_id is not None:
            self.bind_account(account_id, person_id, evidence_id=evidence_id,
                              institution=institution)
        elif account_id is not None and institution:
            self.account_meta.setdefault(account_id, {})["institution"] = institution
        return copy.deepcopy(row)

    def add_recusal(self, recusal_id, *, person_id=None, account_id=None,
                    term=None, translation=None, reason="利益冲突/回避申报",
                    at=None, evidence_id=None, evidence=None):
        if person_id is None and account_id is None:
            raise ValueError("recusal requires person_id or account_id")
        evidence_id = self._remember(evidence_id, "recusal_declaration", evidence)
        row = {
            "recusal_id": recusal_id,
            "person_id": person_id,
            "account_id": account_id,
            "term": term,
            "translation": translation,
            "reason": reason,
            "at": _date(at),
            "evidence_id": evidence_id,
        }
        self.recusals.append(row)
        return copy.deepcopy(row)

    def add_translator_assignment(self, *, term, translation, person_id=None,
                                 account_id=None, translator_name=None,
                                 delegated_by=None, evidence_id=None, evidence=None):
        """采录译者委派记录：某术语版本的译文实际由谁翻译。

        ``person_id``/``account_id`` 指向实际译者（触发译者回避）；
        ``delegated_by`` 可记录委派/具名审校责任人，作为身份汇聚与
        “本人并非该版译者”的旁证，但不会被当成译者本人。
        """
        if person_id is None and account_id is None:
            raise ValueError("translator assignment requires person_id or account_id")
        evidence_id = self._remember(evidence_id, "translator_delegation", evidence)
        row = {
            "term": term,
            "translation": translation,
            "person_id": person_id,
            "account_id": account_id,
            "translator_name": translator_name,
            "delegated_by": delegated_by,
            "evidence_id": evidence_id,
        }
        self.translator_assignments.append(row)
        return copy.deepcopy(row)

    def add_identity_verification(self, *, evidence_id=None, evidence=None,
                                  person_id=None, account_id=None, provider=None,
                                  identity_token_sha256=None, independent=True, verified=True):
        """采录独立身份核验（路径 B 用于证实两帐号分属不同自然人）。"""
        evidence_id = self._remember(evidence_id, "independent_identity_verification", evidence)
        row = {
            "person_id": person_id,
            "account_id": account_id,
            "provider": provider,
            "identity_token_sha256": identity_token_sha256,
            "independent": bool(independent),
            "verified": bool(verified),
            "evidence_id": evidence_id,
        }
        self.identity_checks.append(row)
        if verified and person_id is not None and account_id is not None:
            self.bind_account(account_id, person_id, evidence_id=evidence_id)
        return copy.deepcopy(row)

    def add_signature_check(self, *, vote_id, evidence_id=None, evidence=None,
                            person_id=None, account_id=None, matched=True, verifier=None):
        """采录“选票本人签署”与身份/签名样本的比对结论。"""
        evidence_id = self._remember(evidence_id, "ballot_signature_verification", evidence)
        row = {
            "vote_id": vote_id,
            "person_id": person_id,
            "account_id": account_id,
            "matched": bool(matched),
            "verifier": verifier,
            "evidence_id": evidence_id,
        }
        self.signature_checks.append(row)
        return copy.deepcopy(row)

    # ------------------------------------------------------------------
    # 身份归并：只沿显式绑定/别名边做并查集，姓名完全不参与
    # ------------------------------------------------------------------
    def resolve_identities(self):
        parent = {}

        def add_node(x):
            if x not in parent:
                parent[x] = x

        def find(x):
            add_node(x)
            root = x
            while parent[root] != root:
                root = parent[root]
            while parent[x] != root:
                parent[x], x = root, parent[x]
            return root

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        claims = defaultdict(set)
        binding_evidence = defaultdict(list)

        for row in self.bindings:
            acid = row["account_id"]
            add_node(acid)
            claims[acid].add(row["person_id"])
            binding_evidence[acid].append(row["evidence_id"])

        for row in self.aliases:
            a, b = row["account_a"], row["account_b"]
            add_node(a)
            add_node(b)
            union(a, b)
            if row.get("person_id"):
                claims[a].add(row["person_id"])
                claims[b].add(row["person_id"])
            binding_evidence[a].append(row["evidence_id"])
            binding_evidence[b].append(row["evidence_id"])

        for acid in self.account_meta:
            add_node(acid)
        for ap in self.appointments:
            if ap.get("account_id"):
                add_node(ap["account_id"])
        for chk in self.identity_checks:
            if chk.get("account_id"):
                add_node(chk["account_id"])

        groups = defaultdict(set)
        for node in list(parent):
            groups[find(node)].add(node)

        components = []
        for accts in groups.values():
            accounts = sorted(accts)
            pids = set()
            refs = []
            for acid in accounts:
                pids |= claims.get(acid, set())
                refs += binding_evidence.get(acid, [])

            id_checks = [
                c for c in self.identity_checks
                if (c.get("account_id") in accts)
                or (c.get("person_id") and c["person_id"] in pids)
            ]
            identity_verified = any(c["verified"] and c["independent"] for c in id_checks)

            if len(pids) > 1:
                status = STATUS_CONFLICT
                person_id = None
                key = "identity-conflict:" + "|".join(accounts)
            elif len(pids) == 1:
                person_id = next(iter(pids))
                status = STATUS_VERIFIED if identity_verified else STATUS_LINKED
                key = person_id
            else:
                person_id = None
                if len(accounts) > 1:
                    # 显式别名边把多个帐号焊成同一自然人，但没有具名 person。
                    status = STATUS_LINKED
                    key = "linked-accounts:" + "|".join(accounts)
                else:
                    status = STATUS_PENDING
                    key = "pending-account:" + accounts[0]

            refs += [c["evidence_id"] for c in id_checks]
            components.append({
                "component_key": key,
                "person_id": person_id,
                "person_ids": sorted(pids),
                "accounts": accounts,
                "identity_status": status,
                "identity_verified": bool(identity_verified and status == STATUS_VERIFIED),
                "identity_checks": copy.deepcopy(id_checks),
                "evidence_refs": list(dict.fromkeys(r for r in refs if r)),
            })
        return components

    def _index_components(self, components):
        by_account = {}
        for comp in components:
            for acid in comp["accounts"]:
                by_account[acid] = comp
        return by_account

    def _matches_component(self, target, comp, account_id):
        if target.get("account_id") and target["account_id"] in comp["accounts"]:
            return True
        if target.get("person_id"):
            pid = target["person_id"]
            if pid == comp.get("person_id") or pid in comp.get("person_ids", []):
                return True
        return False

    def _appointment_covers(self, appt, on_date):
        day = _date(on_date)
        if day is None:
            return False  # 无法证明投票时点在任期内
        start = appt.get("effective_from")
        end = appt.get("effective_to")
        if start and day < start:
            return False
        if end and day > end:
            return False
        return True

    def _appointments_for(self, comp, account_id, vote_at):
        related, active = [], []
        for appt in self.appointments:
            if self._matches_component(appt, comp, account_id):
                related.append(appt)
                if self._appointment_covers(appt, vote_at):
                    active.append(appt)
        return related, active

    def _translator_hit(self, comp, account_id, term, translation):
        for row in self.translator_assignments:
            if term is not None and row.get("term") != term:
                continue
            if translation is not None and row.get("translation") != translation:
                continue
            if self._matches_component(row, comp, account_id):
                return row
        return None

    def _recusal_hit(self, comp, account_id, term, translation):
        for row in self.recusals:
            if term is not None and row.get("term") and row["term"] != term:
                continue
            if translation is not None and row.get("translation") not in (None, "*") \
                    and row["translation"] != translation:
                continue
            if self._matches_component(row, comp, account_id):
                return row
        return None

    def _signature_verdict(self, ballot, comp):
        bid = ballot.get("id")
        checks = [c for c in self.signature_checks if c.get("vote_id") == bid]
        if not checks and comp is not None:
            checks = [
                c for c in self.signature_checks
                if c.get("person_id") == comp.get("person_id")
                or c.get("account_id") in comp.get("accounts", [])
            ]
        if not checks:
            return None, []
        return all(c["matched"] for c in checks), checks

    # ------------------------------------------------------------------
    # 核心计算：选票 -> 每张票的资格 -> 按自然人归并的赞成名册
    # ------------------------------------------------------------------
    def evaluate(self, votes, *, term=None, translation=None, required=2, at=None):
        components = self.resolve_identities()
        by_account = self._index_components(components)

        positive = [v for v in votes if v.get("approve") is True]
        opposition = [v for v in votes if v.get("approve") is False]

        ballot_reviews = []
        applied_refs = []

        for vote in positive:
            acid = vote.get("reviewer_id")
            comp = by_account.get(acid)
            if comp is None:
                comp = {
                    "component_key": f"pending-account:{acid}",
                    "person_id": None,
                    "person_ids": [],
                    "accounts": [acid],
                    "identity_status": STATUS_PENDING,
                    "identity_verified": False,
                    "identity_checks": [],
                    "evidence_refs": [],
                }

            reasons = []
            vote_at = vote.get("at") or at

            if vote.get("withdrawals") or vote.get("withdrawn"):
                reasons.append("withdrawn")

            if term and vote.get("term") and vote["term"] != term:
                reasons.append("out_of_scope")
            elif translation and vote.get("translation") and vote["translation"] != translation:
                reasons.append("other_version")

            if comp["identity_status"] == STATUS_PENDING:
                reasons.append("identity_pending")
            elif comp["identity_status"] == STATUS_CONFLICT:
                reasons.append("identity_conflict")

            related_appts, active_appts = self._appointments_for(comp, acid, vote_at)
            if not related_appts:
                reasons.append("appointment_missing")
            elif not active_appts:
                reasons.append("appointment_inactive")

            translator_row = self._translator_hit(comp, acid, term, translation)
            if translator_row is not None:
                reasons.append("translator_conflict")

            recusal_row = self._recusal_hit(comp, acid, term, translation)
            if recusal_row is not None:
                reasons.append("recused")

            sig_verified, sig_checks = self._signature_verdict(vote, comp)
            refs = list(comp.get("evidence_refs", []))
            refs += [a["evidence_id"] for a in active_appts or related_appts]
            if translator_row:
                refs.append(translator_row["evidence_id"])
            if recusal_row:
                refs.append(recusal_row["evidence_id"])
            refs += [c["evidence_id"] for c in sig_checks]
            refs = [r for r in dict.fromkeys(refs) if r]
            applied_refs += refs

            signature = vote.get("signature")
            if isinstance(vote.get("signed"), dict) and not signature:
                signature = vote["signed"].get("signature")

            ballot_reviews.append({
                "ballot_id": vote.get("id"),
                "account_id": acid,
                "at": vote_at,
                "ballot_term": vote.get("term"),
                "ballot_translation": vote.get("translation"),
                "component_key": comp["component_key"],
                "person_id": comp.get("person_id"),
                "identity_status": comp["identity_status"],
                "signature_present": bool(signature),
                "signature": signature,
                "signed_by": vote.get("signed_by"),
                "signature_verified": sig_verified,
                "active_appointment_ids": [a["appointment_id"] for a in active_appts],
                "reasons": reasons,
                "reason_labels": [REASON_TEXT[r] for r in reasons if r in REASON_TEXT],
                "countable": not reasons,
                "withdrawal": (vote.get("withdrawals") or [None])[-1]
                if vote.get("withdrawals") else (vote.get("withdrawal") or None),
                "evidence_refs": refs,
            })

        # 按自然人组件归并“可计票”的赞成票：同一组件至多一个名册席位。
        roster_by_key = {}
        for row in ballot_reviews:
            if not row["countable"]:
                continue
            key = row["component_key"]
            entry = roster_by_key.setdefault(key, {
                "reviewer_key": key,
                "person_id": row["person_id"],
                "identity_status": row["identity_status"],
                "identity_verified": False,
                "accounts": set(),
                "counted_ballot_ids": [],
                "merged_ballot_ids": [],
                "appointment_ids": [],
                "evidence_refs": [],
                "signatures": [],
                "signature_verified": True,
            })
            entry["accounts"].add(row["account_id"])
            entry["counted_ballot_ids"].append(row["ballot_id"])
            entry["appointment_ids"] += row["active_appointment_ids"]
            entry["evidence_refs"] += row["evidence_refs"]
            if row["signature"]:
                entry["signatures"].append({
                    "ballot_id": row["ballot_id"],
                    "signed_by": row["signed_by"],
                    "signature": row["signature"],
                })
            if row["signature_verified"] is not True:
                entry["signature_verified"] = False

        comp_by_key = {c["component_key"]: c for c in components}
        roster = []
        for key, entry in roster_by_key.items():
            comp = comp_by_key.get(key, {})
            person = self.persons.get(entry["person_id"]) if entry["person_id"] else None
            all_positive_ids = [
                r["ballot_id"] for r in ballot_reviews if r["component_key"] == key
            ]
            valid_ids = sorted(dict.fromkeys(entry["counted_ballot_ids"]))
            entry["accounts"] = sorted(a for a in entry["accounts"] if a is not None)
            # 一个自然人只占一个席位：保留全部有效签署票以备复核，
            # 但除代表票外的帐号票明确标为“同一自然人的额外帐号票”。
            entry["seat_count"] = 1
            entry["counted_ballot_ids"] = valid_ids
            entry["representative_ballot_id"] = valid_ids[0] if valid_ids else None
            entry["same_person_extra_ballot_ids"] = valid_ids[1:]
            entry["merged_ballot_ids"] = [
                bid for bid in dict.fromkeys(all_positive_ids)
                if bid not in valid_ids
            ]
            entry["appointment_ids"] = list(dict.fromkeys(entry["appointment_ids"]))
            entry["evidence_refs"] = list(dict.fromkeys(entry["evidence_refs"]))
            entry["display_name"] = (person or {}).get("display_name")
            entry["identity_verified"] = comp.get("identity_verified", False)
            # 没有任何阳性签名核验记录时，不能宣称“本人签署已核验”。
            if not any(r["signature_verified"] is True
                        for r in ballot_reviews if r["component_key"] == key):
                entry["signature_verified"] = None
            roster.append(entry)

        roster.sort(key=lambda e: e["reviewer_key"])

        pending_accounts = [
            {
                "account_id": r["account_id"],
                "ballot_id": r["ballot_id"],
                "identity_status": r["identity_status"],
                "status_label": STATUS_PENDING,
                "reasons": r["reasons"],
                "reason_labels": r["reason_labels"],
            }
            for r in ballot_reviews
            if "identity_pending" in r["reasons"] or "identity_conflict" in r["reasons"]
        ]
        excluded_ballots = [r for r in ballot_reviews if not r["countable"]]

        # 译者委派证据即使未造成回避，也是“该人不是本版译者”的复核依据。
        for row in self.translator_assignments:
            if term is not None and row.get("term") != term:
                continue
            if translation is not None and row.get("translation") != translation:
                continue
            if row.get("evidence_id"):
                applied_refs.append(row["evidence_id"])

        evidence_refs = list(dict.fromkeys(applied_refs))
        return {
            "term": term,
            "translation": translation,
            "required_independent_approvals": required,
            "account_positive_count": len(positive),
            "positive_reviewers": len(roster),
            "status": "定版" if len(roster) >= required else "待议",
            "roster": roster,
            "ballot_reviews": ballot_reviews,
            "pending_accounts": pending_accounts,
            "excluded_ballots": excluded_ballots,
            "opposition": copy.deepcopy(opposition),
            "evidence_refs": evidence_refs,
            "evidence_snapshot": self.snapshot_evidence(),
        }


# 向后兼容的模块级函数：无 registry 时没有任何身份/任命证据，
# 因而不能确认任何“独立合格自然人”，返回 0 而不是帐号去重数 2。
def positive_reviewers(votes, registry=None, **evaluate_options):
    reg = registry if registry is not None else ReviewerRegistry()
    options = {"term": "injunction", "translation": "禁令"}
    options.update(evaluate_options)
    return reg.evaluate(votes, **options)["positive_reviewers"]
