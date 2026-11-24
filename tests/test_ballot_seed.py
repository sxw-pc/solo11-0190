"""injunction→禁令 签署表决的身份/任命/回避驱动与决议版本追溯测试。

覆盖题目要求的六类情形，并完整演练从“同一初始三票 + 错误定版”出发的
两条获授权补证路径：

1. 双帐号同人：人事名册 + 双机构帐号绑定 + 译者委派 => A-East/B-West
   是同一名合格自然人，最多一票，退回 待议；
2. 双人：两机构任命原件 + 本人签署 + 独立身份核验 => 两个不同且未参与
   该译文的自然人，两票有效，保留 定版；
3. 任命失效：投票时点任命未生效/已届满 => 该票剔除；
4. 译者回避：该译文译者本人或已申报回避者 => 该票剔除；
5. 反对票：C 的反对票既不抵充赞成名额，也不否决两票合格赞成；
6. 旧版追溯：补证、撤票只产生新决议版本，旧版签名与证据快照、摘要
   哈希原样保留，旧的错误“帐号数定版”v1 亦可追溯。
"""
import copy
import json
import unittest
from pathlib import Path

from termsettle.reviewer_registry import (
    ReviewerRegistry,
    ballot_digest,
    canonical,
    positive_reviewers,
)
from termsettle.term_ballot import (
    BallotCase,
    decide,
    legacy_decide,
    verify_resolution,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "term-ballot.json"
VOTE_DAY = "2026-11-18"


def seed_votes():
    return copy.deepcopy(json.loads(FIXTURE.read_text(encoding="utf-8")))


def review_of(result, ballot_id):
    return next(r for r in result["ballot_reviews"] if r["ballot_id"] == ballot_id)


def build_path_a_same_person(registry=None):
    """补证路径 A：三证合一，证明 A-East 与 B-West 是 P-Zhang 同一人。"""
    reg = registry or ReviewerRegistry()
    reg.add_evidence(
        "ev-east-roster", kind="institution_personnel_roster",
        institution="东屿机构人事处", document_sha256="sha256:east-roster",
    )
    reg.add_evidence(
        "ev-west-roster", kind="institution_personnel_roster",
        institution="西岭机构人事处", document_sha256="sha256:west-roster",
    )
    reg.add_evidence(
        "ev-dual-binding", kind="dual_institution_account_binding",
        detail="双机构帐号绑定原件，载明 A-East 与 B-West 为同一自然人",
        document_sha256="sha256:dual-binding",
    )
    reg.add_evidence(
        "ev-delegation", kind="translator_delegation",
        detail="injunction→禁令 译文委派 P-Qian 翻译，P-Zhang 非本版译者",
        document_sha256="sha256:delegation-qian",
    )
    reg.bind_account("A-East", "P-Zhang", evidence_id="ev-east-roster")
    reg.bind_account("B-West", "P-Zhang", evidence_id="ev-west-roster")
    reg.add_account_alias(
        "A-East", "B-West", person_id="P-Zhang", evidence_id="ev-dual-binding"
    )
    reg.add_appointment(
        "ap-zhang-east", person_id="P-Zhang", institution="东屿机构",
        title="在册审校员", effective_from="2025-01-01",
        evidence_id="ev-east-roster",
    )
    reg.add_appointment(
        "ap-zhang-west", person_id="P-Zhang", institution="西岭机构",
        title="跨机构兼任审校员", effective_from="2025-06-01",
        evidence_id="ev-west-roster",
    )
    reg.add_translator_assignment(
        term="injunction", translation="禁令",
        person_id="P-Qian", translator_name="钱译员",
        delegated_by="P-Zhang", evidence_id="ev-delegation",
    )
    return reg


def build_path_b_two_persons(registry=None):
    """补证路径 B：任命原件 + 本人签署 + 独立身份核验，证实是两个人。"""
    reg = registry or ReviewerRegistry()
    reg.add_evidence(
        "ev-appt-a", kind="appointment_original", institution="东屿机构",
        document_sha256="sha256:appointment-a-original",
    )
    reg.add_evidence(
        "ev-appt-b", kind="appointment_original", institution="西岭机构",
        document_sha256="sha256:appointment-b-original",
    )
    reg.add_evidence(
        "ev-id-a", kind="independent_identity_verification",
        provider="中立身份核验方·甲", document_sha256="sha256:id-check-a",
    )
    reg.add_evidence(
        "ev-id-b", kind="independent_identity_verification",
        provider="中立身份核验方·乙", document_sha256="sha256:id-check-b",
    )
    reg.add_evidence("ev-sig-a", kind="ballot_signature_verification",
                     detail="vote-a 为 P-Li 本人签署")
    reg.add_evidence("ev-sig-b", kind="ballot_signature_verification",
                     detail="vote-b 为 P-Wang 本人签署")
    reg.add_evidence(
        "ev-delegation", kind="translator_delegation",
        detail="injunction→禁令 译文由 P-Qian 翻译，P-Li/P-Wang 均未参与翻译",
    )
    reg.add_person("P-Li", display_name="李审校")
    reg.add_person("P-Wang", display_name="王审校")
    reg.add_appointment(
        "ap-li", person_id="P-Li", account_id="A-East", institution="东屿机构",
        effective_from="2025-01-01", evidence_id="ev-appt-a",
    )
    reg.add_appointment(
        "ap-wang", person_id="P-Wang", account_id="B-West", institution="西岭机构",
        effective_from="2025-01-01", evidence_id="ev-appt-b",
    )
    reg.add_identity_verification(
        evidence_id="ev-id-a", person_id="P-Li", account_id="A-East",
        provider="中立身份核验方·甲",
        identity_token_sha256="sha256:identity-token-li",
    )
    reg.add_identity_verification(
        evidence_id="ev-id-b", person_id="P-Wang", account_id="B-West",
        provider="中立身份核验方·乙",
        identity_token_sha256="sha256:identity-token-wang",
    )
    reg.add_signature_check(
        vote_id="vote-a", person_id="P-Li", matched=True,
        verifier="线下笔迹+证件比对", evidence_id="ev-sig-a",
    )
    reg.add_signature_check(
        vote_id="vote-b", person_id="P-Wang", matched=True,
        verifier="线下笔迹+证件比对", evidence_id="ev-sig-b",
    )
    reg.add_translator_assignment(
        term="injunction", translation="禁令",
        person_id="P-Qian", evidence_id="ev-delegation",
    )
    return reg


class BallotSeedTest(unittest.TestCase):
    """保留种子测试类名；旧断言（2 帐号即定版）正是被纠正的错误结果。"""

    def setUp(self):
        self.votes = seed_votes()

    def test_fixture_keeps_three_seed_ballots_and_signatures(self):
        self.assertEqual([v["id"] for v in self.votes],
                         ["vote-a", "vote-b", "vote-c"])
        self.assertEqual([v["reviewer_id"] for v in self.votes],
                         ["A-East", "B-West", "C"])
        self.assertEqual([v["approve"] for v in self.votes],
                         [True, True, False])
        for vote in self.votes:
            self.assertTrue(vote["signature"])
            # fixture 中嵌入的正文摘要必须与规范化正文一致（签名/摘要字段除外）
            self.assertEqual(vote["payload_sha256"], ballot_digest(vote))

    def test_two_accounts_no_evidence_are_pending_not_final(self):
        # 帐号表象仍是两个赞成帐号——这正是旧实现错误数到 2 的来源。
        result = decide(self.votes)
        self.assertEqual(result["account_positive_count"], 2)

        # 但没有任何身份/任命证据：不能确认两个独立合格自然人。
        self.assertEqual(positive_reviewers(self.votes), 0)
        self.assertEqual(result["positive_reviewers"], 0)
        self.assertEqual(result["status"], "待议")

        pending = {p["account_id"]: p for p in result["pending_accounts"]}
        self.assertEqual(set(pending), {"A-East", "B-West"})
        for item in pending.values():
            self.assertEqual(item["identity_status"], "待核")
            self.assertIn("identity_pending", item["reasons"])

        # C 的反对票原样留存在反对名册中。
        self.assertEqual([o["id"] for o in result["opposition"]], ["vote-c"])

    def test_two_accounts_approve_term_legacy_shape_is_superseded(self):
        # 旧口径的输出被显式保留为 legacy_decide，用于封存错误 v1。
        self.assertEqual(
            legacy_decide(self.votes),
            {"positive_reviewers": 2, "status": "定版"},
        )
        # 新 decide 保留 positive_reviewers/status 键，但不再仅凭帐号定版。
        current = decide(self.votes)
        self.assertIn("positive_reviewers", current)
        self.assertIn("status", current)
        self.assertEqual(current["status"], "待议")


class DualAccountSamePersonTest(unittest.TestCase):
    """情形一：双帐号同人，最多一票。"""

    def setUp(self):
        self.votes = seed_votes()

    def test_path_a_counts_one_natural_person_and_returns_to_pending(self):
        registry = build_path_a_same_person()
        result = decide(self.votes, registry)

        # 帐号数仍是 2，但独立自然人只有 1。
        self.assertEqual(result["account_positive_count"], 2)
        self.assertEqual(result["positive_reviewers"], 1)
        self.assertEqual(result["status"], "待议")
        self.assertEqual(len(result["roster"]), 1)

        roster = result["roster"][0]
        self.assertEqual(roster["person_id"], "P-Zhang")
        self.assertEqual(roster["seat_count"], 1)
        self.assertEqual(set(roster["accounts"]), {"A-East", "B-West"})
        self.assertEqual(set(roster["counted_ballot_ids"]),
                         {"vote-a", "vote-b"})
        # 两票均为同一自然人的有效签署票，但只有一个席位，第二帐号票显式标注。
        self.assertEqual(roster["representative_ballot_id"], "vote-a")
        self.assertEqual(roster["same_person_extra_ballot_ids"], ["vote-b"])
        # 两票的签署件都保留在同一自然人名册条目下，可复核。
        self.assertEqual(
            {s["ballot_id"] for s in roster["signatures"]},
            {"vote-a", "vote-b"},
        )

        # 身份归并确实由显式绑定/别名证据驱动，而非姓名或帐号猜测。
        identities = {
            account: comp
            for comp in registry.resolve_identities()
            for account in comp["accounts"]
        }
        self.assertEqual(identities["A-East"]["component_key"],
                         identities["B-West"]["component_key"])
        self.assertEqual(identities["A-East"]["person_id"], "P-Zhang")

        # 三类授权补证证据都必须出现在决议依据中。
        kinds = {e["kind"] for e in result["evidence_snapshot"]}
        self.assertIn("institution_personnel_roster", kinds)
        self.assertIn("dual_institution_account_binding", kinds)
        self.assertIn("translator_delegation", kinds)
        self.assertIn("ev-dual-binding", result["evidence_refs"])
        # 译者委派记录由 P-Zhang 委派、P-Qian 实际翻译，进一步排除其译者身份。
        delegation = next(
            t for t in registry.translator_assignments
            if t["translation"] == "禁令"
        )
        self.assertEqual(delegation["person_id"], "P-Qian")
        self.assertEqual(delegation["delegated_by"], "P-Zhang")
        self.assertTrue(verify_resolution(result))


class TwoDistinctReviewersTest(unittest.TestCase):
    """情形二：两个不同且合格的自然人，两票有效，保留定版。"""

    def setUp(self):
        self.votes = seed_votes()

    def test_path_b_finalizes_with_two_natural_persons(self):
        registry = build_path_b_two_persons()
        result = decide(self.votes, registry)

        self.assertEqual(result["positive_reviewers"], 2)
        self.assertEqual(result["status"], "定版")
        self.assertEqual(len(result["roster"]), 2)

        account_to_person = {
            account: entry["person_id"]
            for entry in result["roster"]
            for account in entry["accounts"]
        }
        self.assertEqual(account_to_person,
                         {"A-East": "P-Li", "B-West": "P-Wang"})
        self.assertEqual(
            {entry["person_id"] for entry in result["roster"]},
            {"P-Li", "P-Wang"},
        )
        self.assertEqual([e["seat_count"] for e in result["roster"]], [1, 1])
        self.assertTrue(all(not e["same_person_extra_ballot_ids"]
                            for e in result["roster"]))
        # 路径 B 要求的独立身份核验与本人签署均在名册中体现。
        self.assertTrue(all(e["identity_verified"] for e in result["roster"]))
        self.assertTrue(all(e["signature_verified"] is True
                            for e in result["roster"]))
        self.assertEqual(
            {s["ballot_id"] for e in result["roster"] for s in e["signatures"]},
            {"vote-a", "vote-b"},
        )

        kinds = {e["kind"] for e in result["evidence_snapshot"]}
        self.assertIn("appointment_original", kinds)
        self.assertIn("independent_identity_verification", kinds)
        self.assertIn("ballot_signature_verification", kinds)

        # C 的反对票记录在案，但不阻止两票合格赞成带来的定版。
        self.assertEqual(len(result["opposition"]), 1)
        self.assertEqual(result["opposition"][0]["id"], "vote-c")
        self.assertTrue(verify_resolution(result))


class IdentityAmbiguityTest(unittest.TestCase):
    """关联不明显式待核；姓名相同不得自动合并。"""

    def test_same_display_name_without_binding_is_pending_not_two(self):
        registry = ReviewerRegistry()
        registry.add_person("P-One", display_name="张伟")
        registry.add_person("P-Two", display_name="张伟")  # 同名不同人
        votes = [
            {"id": "v1", "reviewer_id": "X-East", "approve": True,
             "term": "injunction", "translation": "禁令", "at": VOTE_DAY},
            {"id": "v2", "reviewer_id": "Y-West", "approve": True,
             "term": "injunction", "translation": "禁令", "at": VOTE_DAY},
        ]
        result = decide(votes, registry)
        self.assertEqual(result["positive_reviewers"], 0)
        pending = {p["account_id"] for p in result["pending_accounts"]}
        self.assertEqual(pending, {"X-East", "Y-West"})

        # 即便有同名 person 记录，没有显式绑定也不能把帐号挂上去。
        identities = registry.resolve_identities()
        self.assertTrue(all(c["identity_status"] == "待核" for c in identities))

    def test_same_name_with_distinct_evidence_backed_persons_counts_two(self):
        registry = ReviewerRegistry()
        registry.add_person("P-One", display_name="张伟")
        registry.add_person("P-Two", display_name="张伟")
        registry.bind_account("X-East", "P-One", evidence_id="ev-bind-x")
        registry.bind_account("Y-West", "P-Two", evidence_id="ev-bind-y")
        registry.add_appointment(
            "ap1", person_id="P-One", effective_from="2025-01-01",
            evidence_id="ev-ap1",
        )
        registry.add_appointment(
            "ap2", person_id="P-Two", effective_from="2025-01-01",
            evidence_id="ev-ap2",
        )
        votes = [
            {"id": "v1", "reviewer_id": "X-East", "approve": True,
             "term": "injunction", "translation": "禁令", "at": VOTE_DAY},
            {"id": "v2", "reviewer_id": "Y-West", "approve": True,
             "term": "injunction", "translation": "禁令", "at": VOTE_DAY},
        ]
        result = decide(votes, registry)
        self.assertEqual(result["positive_reviewers"], 2)
        self.assertEqual(result["status"], "定版")


class AppointmentValidityTest(unittest.TestCase):
    """情形三：任命在投票时点不生效，票不得计入。"""

    def setUp(self):
        self.votes = seed_votes()

    def test_future_appointment_excludes_ballot(self):
        registry = build_path_b_two_persons()
        appointment = next(a for a in registry.appointments
                           if a["appointment_id"] == "ap-li")
        appointment["effective_from"] = "2026-12-01"  # 投票 2026-11-18 之后才生效

        result = decide(self.votes, registry)
        review_a = review_of(result, "vote-a")
        self.assertIn("appointment_inactive", review_a["reasons"])
        self.assertFalse(review_a["countable"])
        # 只剩 P-Wang 一票，不能定版。
        self.assertEqual(result["positive_reviewers"], 1)
        self.assertEqual(result["status"], "待议")

    def test_expired_appointment_excludes_ballot(self):
        registry = build_path_b_two_persons()
        appointment = next(a for a in registry.appointments
                           if a["appointment_id"] == "ap-li")
        appointment["effective_to"] = "2026-10-31"  # 投票前已届满

        result = decide(self.votes, registry)
        self.assertIn("appointment_inactive",
                      review_of(result, "vote-a")["reasons"])
        self.assertEqual(result["positive_reviewers"], 1)
        self.assertEqual(result["status"], "待议")

    def test_missing_appointment_excludes_ballot(self):
        registry = ReviewerRegistry()
        registry.bind_account("A-East", "P-Li", evidence_id="ev-bind-a")
        registry.bind_account("B-West", "P-Wang", evidence_id="ev-bind-b")
        result = decide(self.votes, registry)
        for bid in ("vote-a", "vote-b"):
            self.assertIn("appointment_missing",
                          review_of(result, bid)["reasons"])
        self.assertEqual(result["positive_reviewers"], 0)
        self.assertEqual(result["status"], "待议")


class TranslatorRecusalTest(unittest.TestCase):
    """情形四：译者本人或回避申报，票不得计入。"""

    def setUp(self):
        self.votes = seed_votes()

    def test_translator_of_this_translation_excluded(self):
        registry = build_path_b_two_persons()
        registry.add_translator_assignment(
            term="injunction", translation="禁令",
            person_id="P-Li", evidence_id="ev-li-is-translator",
        )
        result = decide(self.votes, registry)
        review_a = review_of(result, "vote-a")
        self.assertIn("translator_conflict", review_a["reasons"])
        self.assertEqual(result["positive_reviewers"], 1)  # 只剩 P-Wang
        self.assertEqual(result["status"], "待议")

    def test_filed_recusal_excludes_ballot(self):
        registry = build_path_b_two_persons()
        registry.add_recusal(
            "rec-wang", person_id="P-Wang",
            term="injunction", translation="禁令",
            reason="近亲属在本案代理机构任职", at="2026-11-18",
            evidence_id="ev-rec-wang",
        )
        result = decide(self.votes, registry)
        review_b = review_of(result, "vote-b")
        self.assertIn("recused", review_b["reasons"])
        self.assertEqual(result["positive_reviewers"], 1)

        # 两名赞成人均被回避/译者规则命中时，赞成席位为 0。
        registry.add_translator_assignment(
            term="injunction", translation="禁令",
            person_id="P-Li", evidence_id="ev-li-translator-too",
        )
        second = decide(self.votes, registry)
        self.assertEqual(second["positive_reviewers"], 0)
        self.assertEqual(second["status"], "待议")

    def test_translator_of_other_translation_not_excluded(self):
        registry = build_path_b_two_persons()
        # P-Li 只是另一译文（命令）的译者，不是本版 禁令 的译者。
        registry.add_translator_assignment(
            term="injunction", translation="命令",
            person_id="P-Li", evidence_id="ev-li-translates-mingling",
        )
        result = decide(self.votes, registry)
        self.assertNotIn("translator_conflict",
                         review_of(result, "vote-a")["reasons"])
        self.assertEqual(result["positive_reviewers"], 2)
        self.assertEqual(result["status"], "定版")


class OppositionQuotaTest(unittest.TestCase):
    """情形五：反对票不能抵充赞成名额，也不否决合格两票赞成。"""

    def setUp(self):
        self.votes = seed_votes()

    def test_opposition_does_not_veto_two_qualified_approvals(self):
        result = decide(self.votes, build_path_b_two_persons())
        self.assertEqual(result["status"], "定版")
        self.assertEqual(result["positive_reviewers"], 2)
        self.assertEqual(len(result["opposition"]), 1)
        roster_ballot_ids = {
            bid for entry in result["roster"]
            for bid in entry["counted_ballot_ids"]
        }
        self.assertNotIn("vote-c", roster_ballot_ids)

    def test_opposition_cannot_fill_second_approval_seat(self):
        votes = [v for v in self.votes if v["id"] != "vote-b"]
        votes.append({
            "id": "vote-d", "reviewer_id": "D", "approve": False,
            "term": "injunction", "translation": "禁令", "at": VOTE_DAY,
            "signature": "offline-signature:D:2026-11-20:aa11bb",
        })
        result = decide(votes, build_path_b_two_persons())
        self.assertEqual(result["positive_reviewers"], 1)
        self.assertEqual(result["status"], "待议")
        self.assertEqual(len(result["opposition"]), 2)
        # 反对者帐号绝不进入赞成名册。
        roster_accounts = {a for e in result["roster"] for a in e["accounts"]}
        self.assertNotIn("C", roster_accounts)
        self.assertNotIn("D", roster_accounts)


class VersionTraceabilityTest(unittest.TestCase):
    """情形六：补证与撤票只生成新版本，旧版签名与证据完整可追溯。"""

    def setUp(self):
        self.votes = seed_votes()

    def test_legacy_final_then_supplement_and_withdraw_versions(self):
        case = BallotCase(term="injunction", translation="禁令")
        case.add_ballots(self.votes)

        # v1：封存当前错误的“帐号数=2 -> 定版”决议。
        wrong = legacy_decide(self.votes)
        self.assertEqual(wrong, {"positive_reviewers": 2, "status": "定版"})
        v1 = case.import_legacy_resolution(
            wrong, at="2026-11-19",
            source="fixtures/term-ballot.json + 旧 positive_reviewers/decide",
            note="旧口径错误定版，原样保留待追溯",
        )
        self.assertEqual(v1["version"], 1)
        self.assertTrue(v1["legacy"])
        self.assertEqual(v1["status"], "定版")
        self.assertEqual(len(v1["ballot_snapshot"]), 3)
        self.assertTrue(all(b["signature"] for b in v1["ballot_snapshot"]))
        self.assertTrue(verify_resolution(v1))

        # v2：不补任何身份证据，按新口径重算 -> 待议、帐号待核。
        v2 = case.decide(at="2026-11-20", note="身份/任命证据缺位，先行待议")
        self.assertEqual(v2["version"], 2)
        self.assertFalse(v2["legacy"])
        self.assertEqual(v2["status"], "待议")
        self.assertEqual(v2["positive_reviewers"], 0)
        self.assertEqual(
            {p["account_id"] for p in v2["pending_accounts"]},
            {"A-East", "B-West"},
        )

        # 状态未变时重复 decide 幂等重放，不制造新版本。
        replayed = case.decide(at="2026-11-20")
        self.assertEqual(replayed["version"], 2)
        self.assertEqual(case.versions(), [1, 2])
        v2_frozen = canonical(case.get_version(2))

        # 补证路径 A -> v3：同一自然人，1 票，仍 待议。
        build_path_a_same_person(case.registry)
        v3 = case.decide(at="2026-11-21",
                         note="补入人事名册、双机构绑定与译者委派记录")
        self.assertEqual(v3["version"], 3)
        self.assertEqual(v3["supersedes"], 2)
        self.assertEqual(v3["positive_reviewers"], 1)
        self.assertEqual(v3["status"], "待议")
        self.assertEqual(v3["roster"][0]["person_id"], "P-Zhang")

        # 旧 v2 内容与摘要哈希一字未改。
        self.assertEqual(canonical(case.get_version(2)), v2_frozen)
        self.assertTrue(verify_resolution(case.get_version(2)))

        # B-West 撤票：只追加撤票记录，不删原票/原签名。
        case.registry.add_evidence("ev-withdraw-b", kind="withdrawal_request",
                                   detail="B-West 本人书面请求撤票")
        case.withdraw_ballot(
            "vote-b", at="2026-11-22", evidence_id="ev-withdraw-b",
            reason="对译文措辞仍有疑虑，撤回赞成待再议",
            signature="offline-signature:B-West:withdraw-2026-11-22",
        )
        v4 = case.decide(at="2026-11-22", note="B-West 撤票后重算")
        self.assertEqual(v4["version"], 4)
        self.assertIn("withdrawn", review_of(v4, "vote-b")["reasons"])
        # A-East 仍属于 P-Zhang 同一人，故独立赞成仍为 1（不是 0 也不是 2）。
        self.assertEqual(v4["positive_reviewers"], 1)
        self.assertEqual(v4["status"], "待议")

        # v3 快照中的 vote-b 停留在撤票前：无撤票记录、原始签名还在。
        v3_ballot_b = next(
            b for b in case.get_version(3)["ballot_snapshot"]
            if b["id"] == "vote-b"
        )
        self.assertEqual(v3_ballot_b["withdrawals"], [])
        self.assertIn("offline-signature:B-West:2026-11-18",
                      v3_ballot_b["signature"])

        # v4 快照保留原始签名，同时带上撤票记录与撤票签名。
        v4_ballot_b = next(
            b for b in case.get_version(4)["ballot_snapshot"]
            if b["id"] == "vote-b"
        )
        self.assertEqual(len(v4_ballot_b["withdrawals"]), 1)
        self.assertEqual(v4_ballot_b["withdrawals"][0]["evidence_id"],
                         "ev-withdraw-b")
        self.assertIn("offline-signature:B-West:2026-11-18",
                      v4_ballot_b["signature"])

        # v1 错误定版同样原样保留，所有历史版本哈希均可复核。
        v1_after = case.get_version(1)
        self.assertEqual(v1_after["status"], "定版")
        self.assertTrue(v1_after["legacy"])
        self.assertEqual(case.versions(), [1, 2, 3, 4])
        for version in case.history():
            self.assertTrue(verify_resolution(version),
                            f"resolution v{version['version']} digest mismatch")

    def test_two_authorized_paths_branch_from_identical_seed(self):
        # 同一份初始三票、同一个被封存的错误定版。
        case_a = BallotCase()
        case_a.add_ballots(seed_votes())
        case_a.import_legacy_resolution(legacy_decide(seed_votes()),
                                        at="2026-11-19")
        build_path_a_same_person(case_a.registry)
        resolution_a = case_a.decide(at="2026-11-21")

        case_b = BallotCase()
        case_b.add_ballots(seed_votes())
        case_b.import_legacy_resolution(legacy_decide(seed_votes()),
                                        at="2026-11-19")
        build_path_b_two_persons(case_b.registry)
        resolution_b = case_b.decide(at="2026-11-21")

        # 两路都排除“单纯帐号数推断”：帐号数相同，结论取决于身份证据。
        self.assertEqual(resolution_a["account_positive_count"], 2)
        self.assertEqual(resolution_b["account_positive_count"], 2)
        self.assertEqual(resolution_a["positive_reviewers"], 1)
        self.assertEqual(resolution_a["status"], "待议")
        self.assertEqual(resolution_b["positive_reviewers"], 2)
        self.assertEqual(resolution_b["status"], "定版")

        # 两条分支的 v1 完全一致（错误定版被共同追溯），v2 起按证据分叉。
        self.assertEqual(canonical(case_a.get_version(1)),
                         canonical(case_b.get_version(1)))
        self.assertNotEqual(canonical(case_a.get_version(2)),
                            canonical(case_b.get_version(2)))

        # 路径 B 明确列出两个有效自然人。
        self.assertEqual(
            sorted(e["person_id"] for e in resolution_b["roster"]),
            ["P-Li", "P-Wang"],
        )


class ScopeIsolationTest(unittest.TestCase):
    """只重算 injunction→禁令 这一译文版本，不波及其他术语/译文。"""

    def test_other_terms_and_translations_do_not_leak(self):
        votes = seed_votes()
        votes.append({
            "id": "vote-z", "reviewer_id": "Z", "approve": True,
            "term": "contract", "translation": "保密", "at": VOTE_DAY,
        })
        votes.append({
            "id": "vote-d2", "reviewer_id": "D2", "approve": True,
            "term": "injunction", "translation": "救济", "at": VOTE_DAY,
        })
        registry = build_path_b_two_persons()
        registry.bind_account("Z", "P-Z", evidence_id="ev-bind-z")
        registry.add_appointment("ap-z", person_id="P-Z",
                                 effective_from="2025-01-01",
                                 evidence_id="ev-ap-z")
        registry.bind_account("D2", "P-D2", evidence_id="ev-bind-d2")
        registry.add_appointment("ap-d2", person_id="P-D2",
                                 effective_from="2025-01-01",
                                 evidence_id="ev-ap-d2")

        result = decide(votes, registry)
        reviews = {r["ballot_id"]: r for r in result["ballot_reviews"]}
        self.assertIn("out_of_scope", reviews["vote-z"]["reasons"])
        self.assertIn("other_version", reviews["vote-d2"]["reasons"])
        self.assertEqual(result["positive_reviewers"], 2)
        self.assertEqual(result["status"], "定版")

        # 其他术语版本可以另行立案重算，结论独立。
        other = decide(votes, registry, term="contract", translation="保密")
        self.assertEqual(other["positive_reviewers"], 1)
        self.assertEqual(other["status"], "待议")


if __name__ == "__main__":
    unittest.main()
