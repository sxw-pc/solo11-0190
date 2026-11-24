"""injunction→禁令 等术语版本的签署表决、名册与版本化定版决议。

真实入口从旧的“按 reviewer_id 数帐号”升级为：

    选票（含本人签署） + ReviewerRegistry（身份/任命/回避证据）
        -> ReviewerRegistry.evaluate 给出按自然人归并的合格赞成名册
        -> BallotCase.decide 生成不可变、可复核、带版本号与摘要的决议

补证或撤票后再次 ``decide`` 只会追加新的决议版本；旧版本内嵌当时的
选票快照（含签名）与证据快照，永不被改写。旧的帐号数口径封存在
``legacy_decide`` / ``import_legacy_resolution`` 中，用于追溯那个曾经
错误作出的 v1“定版”。
"""
from __future__ import annotations

import copy

from .reviewer_registry import (
    ReviewerRegistry,
    sha256_obj,
)

DEFAULT_TERM = "injunction"
DEFAULT_TRANSLATION = "禁令"
REQUIRED_APPROVALS = 2
FINAL = "定版"
PENDING = "待议"


def legacy_decide(votes):
    """旧实现：只按 reviewer_id 去重数帐号。

    保留它是为了如实封存/追溯当时错误的“两票定版”，任何新决议都不
    再使用这一口径。
    """
    count = len({v["reviewer_id"] for v in votes if v.get("approve")})
    return {"positive_reviewers": count, "status": FINAL if count >= 2 else PENDING}


def resolution_digest(resolution):
    """决议内容摘要（不含 digest 字段自身），可离线复核版本未被篡改。"""
    body = {k: v for k, v in resolution.items() if k != "resolution_sha256"}
    return sha256_obj(body)


def verify_resolution(resolution):
    stored = resolution.get("resolution_sha256")
    return bool(stored) and stored == resolution_digest(resolution)


class BallotCase:
    """单个术语版本（term + translation + term_version）的表决案卷。

    案卷内：

    * ``ballots`` 是 append-only 的签署选票档案，撤票只追加撤票记录，
      绝不删除/覆盖原票与签名；
    * ``resolutions`` 是 append-only 的决议版本链；
    * registry 保存人员身份/任命/回避/核验证据。
    """

    def __init__(self, *, term=DEFAULT_TERM, translation=DEFAULT_TRANSLATION,
                 term_version=1, required=REQUIRED_APPROVALS, registry=None,
                 subject=None):
        self.term = term
        self.translation = translation
        self.term_version = term_version
        self.required = required
        self.registry = registry if registry is not None else ReviewerRegistry()
        self.subject = subject or f"{term}:{translation}#v{term_version}"
        self.ballots = []
        self._ballot_ids = set()
        self.resolutions = []

    # ------------------------------------------------------------------
    # 签署表决与撤票采录
    # ------------------------------------------------------------------
    def add_ballot(self, ballot):
        record = copy.deepcopy(ballot)
        bid = record.get("id") or f"vote-{len(self.ballots) + 1}"
        if bid in self._ballot_ids:
            raise ValueError(f"ballot id {bid!r} already captured")
        record["id"] = bid
        record.setdefault("term", self.term)
        record.setdefault("translation", self.translation)
        record.setdefault("withdrawals", [])
        self.ballots.append(record)
        self._ballot_ids.add(bid)
        return copy.deepcopy(record)

    # 语义别名
    add_vote = add_ballot
    capture_signed_ballot = add_ballot

    def add_ballots(self, ballots):
        return [self.add_ballot(b) for b in ballots]

    def withdraw_ballot(self, ballot_id, *, at, reason, evidence_id=None,
                        signature=None, by=None):
        """撤票：不删原票，只在选票档案上追加一条撤票记录。"""
        record = self._find_ballot(ballot_id)
        withdrawal = {
            "at": at,
            "reason": reason,
            "evidence_id": evidence_id,
            "signature": signature,
            "by": by or record.get("signed_by") or record.get("reviewer_id"),
        }
        record.setdefault("withdrawals", []).append(withdrawal)
        return copy.deepcopy(withdrawal)

    withdraw_vote = withdraw_ballot

    def _find_ballot(self, ballot_id):
        for record in self.ballots:
            if record["id"] == ballot_id:
                return record
        raise KeyError(f"unknown ballot id {ballot_id!r}")

    # ------------------------------------------------------------------
    # 旧错误决议的封存（旧版追溯的起点）
    # ------------------------------------------------------------------
    def import_legacy_resolution(self, legacy, *, at, source=None, note=None):
        """把旧的“按帐号数定版”结果封存为 v1（显式标记为旧口径）。"""
        if self.resolutions:
            raise RuntimeError("legacy resolution must be imported before any new resolution")
        account_count = legacy.get(
            "account_positive_count", legacy.get("positive_reviewers")
        )
        resolution = {
            "version": 1,
            "method": "legacy_account_count_only",
            "legacy": True,
            "known_defect": (
                "仅按 reviewer_id 去重数帐号，未核验自然人身份、任命时点与回避，"
                "同一自然人的两个机构帐号被错误计为两票"
            ),
            "subject": self.subject,
            "term": self.term,
            "translation": self.translation,
            "term_version": self.term_version,
            "decided_at": at,
            "status": legacy["status"],
            "positive_reviewers": legacy["positive_reviewers"],
            "account_positive_count": account_count,
            "required_independent_approvals": self.required,
            "roster": [],
            "evidence_refs": [],
            "evidence_snapshot": [],
            "ballot_snapshot": copy.deepcopy(self.ballots),
            "source": source,
            "note": note,
            "supersedes": None,
        }
        resolution["resolution_sha256"] = resolution_digest(resolution)
        self.resolutions.append(resolution)
        return copy.deepcopy(resolution)

    # ------------------------------------------------------------------
    # 新版本决议
    # ------------------------------------------------------------------
    def state_fingerprint(self):
        """选票档案 + 全部身份证据的状态指纹；状态不变时重放不新增版本。"""
        return sha256_obj({
            "ballots": self.ballots,
            "registry": self.registry.state_fingerprint_data(),
            "term": self.term,
            "translation": self.translation,
            "term_version": self.term_version,
            "required": self.required,
        })

    def decide(self, *, at=None, note=None, force_new_version=False):
        fingerprint = self.state_fingerprint()
        if (not force_new_version and self.resolutions
                and not self.resolutions[-1].get("legacy")
                and self.resolutions[-1].get("state_fingerprint") == fingerprint):
            # 证据/选票状态未变：幂等重放当前版本，不制造重复版本。
            return copy.deepcopy(self.resolutions[-1])

        evaluation = self.registry.evaluate(
            self.ballots,
            term=self.term,
            translation=self.translation,
            required=self.required,
            at=at,
        )

        if self.resolutions:
            version = self.resolutions[-1]["version"] + 1
            supersedes = self.resolutions[-1]["version"]
        else:
            version = 1
            supersedes = None

        resolution = {
            "version": version,
            "method": "natural_person_identity_appointment_recusal_evidence",
            "legacy": False,
            "subject": self.subject,
            "term": self.term,
            "translation": self.translation,
            "term_version": self.term_version,
            "decided_at": at,
            "status": evaluation["status"],
            "positive_reviewers": evaluation["positive_reviewers"],
            "account_positive_count": evaluation["account_positive_count"],
            "required_independent_approvals": evaluation["required_independent_approvals"],
            "roster": evaluation["roster"],
            "pending_accounts": evaluation["pending_accounts"],
            "ballot_reviews": evaluation["ballot_reviews"],
            "excluded_ballots": evaluation["excluded_ballots"],
            "opposition": evaluation["opposition"],
            "evidence_refs": evaluation["evidence_refs"],
            # 全量证据与当时选票（含签名、撤票轨迹）快照：旧版可独立复核。
            "evidence_snapshot": evaluation["evidence_snapshot"],
            "ballot_snapshot": copy.deepcopy(self.ballots),
            "state_fingerprint": fingerprint,
            "basis": (
                "reviewer_registry 按显式身份证据归并自然人，"
                "并以任命时点、译者委派/回避申报结果计算资格"
            ),
            "note": note,
            "supersedes": supersedes,
        }
        resolution["resolution_sha256"] = resolution_digest(resolution)
        self.resolutions.append(resolution)
        return copy.deepcopy(resolution)

    # ------------------------------------------------------------------
    # 只读访问（一律深拷贝，防止外部改写历史版本）
    # ------------------------------------------------------------------
    def history(self):
        return copy.deepcopy(self.resolutions)

    def current(self):
        return copy.deepcopy(self.resolutions[-1]) if self.resolutions else None

    def get_version(self, version):
        for res in self.resolutions:
            if res["version"] == version:
                return copy.deepcopy(res)
        raise KeyError(f"resolution version {version!r} not found")

    def versions(self):
        return [r["version"] for r in self.resolutions]


# 可能的调用方命名习惯，提供无语义差异的别名。
TermBallot = BallotCase
BallotLedger = BallotCase


def decide(votes, registry=None, *, term=DEFAULT_TERM, translation=DEFAULT_TRANSLATION,
           term_version=1, required=REQUIRED_APPROVALS, at=None, note=None,
           **case_options):
    """一次性入口：采录 votes 并立即给出当前版本决议 dict。

    与旧 ``decide(votes)`` 调用兼容，但结果由 registry 的身份/任命/
    回避证据驱动；未提供 registry 时没有任何合格自然人可确认，状态
    必为 ``待议``，帐号会显式列入 ``pending_accounts``。
    """
    case = BallotCase(
        term=term,
        translation=translation,
        term_version=term_version,
        required=required,
        registry=registry,
        **case_options,
    )
    case.add_ballots(votes)
    return case.decide(at=at, note=note)
