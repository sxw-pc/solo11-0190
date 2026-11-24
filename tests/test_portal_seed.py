import tempfile
import unittest
from pathlib import Path

from termsettle.glossary_snapshot import (
    SnapshotError,
    SnapshotStore,
    build,
    proven_terms,
)
from termsettle.portal_capture import (
    COMPLETE_LABEL,
    PENDING_LABEL,
    DirectoryDigest,
    capture_portal,
    directory_digest,
    parse_page,
    read_page,
)

ROOT = Path(__file__).resolve().parents[1] / "fixtures"
CUSTOMER = "acme"
DIRECTORY = "api-glossary"
V10_DIGEST = directory_digest(CUSTOMER, DIRECTORY, 10, ["A", "X"])
V11_DIGEST = directory_digest(CUSTOMER, DIRECTORY, 11, ["A", "B"])


def v10():
    return DirectoryDigest(CUSTOMER, DIRECTORY, 10, V10_DIGEST)


def v11():
    return DirectoryDigest(CUSTOMER, DIRECTORY, 11, V11_DIGEST)


class PageWriter:
    """Build local portal pages in a temporary directory."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def close(self):
        self.tmp.cleanup()

    def page(
        self,
        name,
        revision=10,
        page=1,
        cursor=None,
        next_cursor=None,
        next_link=None,
        terms=(),
        tombstones=(),
        customer=CUSTOMER,
        directory=DIRECTORY,
        barrier=False,
        extra="",
    ):
        cursor = cursor if cursor is not None else f"{customer}/{directory}/v{revision}:p{page}"
        attrs = [
            f'data-customer="{customer}"',
            f'data-directory="{directory}"',
            f'data-revision="{revision}"',
            f'data-page="{page}"',
            f'data-cursor="{cursor}"',
        ]
        if next_cursor:
            attrs.append(f'data-next-cursor="{next_cursor}"')
        if barrier:
            attrs.append('data-barrier="true"')
        if extra:
            attrs.append(extra)
        body = [f"<html><body {' '.join(attrs)}>"]
        for term in terms:
            if isinstance(term, tuple):
                name_, effective = term
                body.append(
                    f'<div data-term="{name_}" '
                    f'data-effective-revision="{effective}">{name_}</div>'
                )
            else:
                body.append(f'<div data-term="{term}">{term}</div>')
        for tomb in tombstones:
            name_, effective = tomb
            body.append(
                f'<div data-deleted-term="{name_}" '
                f'data-effective-revision="{effective}">{name_}</div>'
            )
        if next_link:
            body.append(f'<a data-next="{next_link}">Next</a>')
        body.append("</body></html>")
        path = self.root / name
        path.write_text("".join(body), encoding="utf-8")
        return path


class PortalSeedTest(unittest.TestCase):
    """The two original fixtures must still parse, but no longer lie."""

    def test_old_fixtures_still_parse(self):
        self.assertEqual(read_page(ROOT / "portal-page-1.html"), ["A"])
        self.assertEqual(read_page(ROOT / "portal-page-2.html"), ["B"])

    def test_old_first_page_metadata_is_explicit(self):
        first = parse_page(ROOT / "portal-page-1.html")
        self.assertEqual((first.customer, first.directory), (CUSTOMER, DIRECTORY))
        self.assertEqual(first.revision, 10)
        self.assertEqual(first.cursor, "acme/api-glossary/v10:p1")
        self.assertEqual(first.next_cursor, "acme/api-glossary/v10:p2")
        self.assertEqual(first.next, "portal-page-2.html")

    def test_first_page_alone_is_not_complete(self):
        # The old build() returned {'complete': True, 'terms': ['A']}.
        from termsettle.glossary_snapshot import build as build_receipt

        # An isolated copy of page 1, without following its next link.
        receipt = build_receipt(pages=[ROOT / "portal-page-1.html"], expected=v10())
        self.assertFalse(receipt["complete"])
        self.assertEqual(receipt["status"], PENDING_LABEL)
        self.assertEqual(receipt["terms"], [])
        self.assertTrue(
            any("页链未闭合" in problem for problem in receipt["problems"])
        )
        self.assertTrue(
            any("缺少页" in problem for problem in receipt["problems"])
        )

    def test_following_page_one_reaches_mixed_page_two(self):
        from termsettle.glossary_snapshot import build as build_receipt

        receipt = build_receipt(ROOT / "portal-page-1.html", expected=v10())
        self.assertFalse(receipt["complete"])
        self.assertTrue(receipt["mixed"])
        self.assertEqual(receipt["terms"], [])

    def test_no_digest_means_never_complete(self):
        from termsettle.glossary_snapshot import build as build_receipt

        receipt = build_receipt(pages=[ROOT / "portal-page-1.html", ROOT / "portal-v10-page-2.html"])
        self.assertFalse(receipt["complete"])
        self.assertTrue(any("目录摘要" in problem for problem in receipt["problems"]))


class MixedRevisionTest(unittest.TestCase):
    def setUp(self):
        self.writer = PageWriter()

    def tearDown(self):
        self.writer.close()

    def test_mixed_pages_are_pending_not_a_glossary(self):
        receipt = capture_portal(ROOT / "portal-page-1.html", expected=v10())
        self.assertFalse(receipt["complete"])
        self.assertEqual(receipt["status"], PENDING_LABEL)
        self.assertTrue(receipt["mixed"])
        self.assertEqual(receipt["revisions"], [10, 11])
        # Neither A nor A+B may be presented as a same-revision directory.
        self.assertEqual(receipt["terms"], [])
        self.assertEqual(
            receipt["observed_by_revision"]["10"]["terms"], ["A"]
        )
        self.assertEqual(
            receipt["observed_by_revision"]["11"]["terms"], ["B"]
        )

    def test_mixed_pages_match_no_single_digest(self):
        for expected in (v10(), v11()):
            receipt = capture_portal(
                ROOT / "portal-page-1.html", expected=expected
            )
            self.assertFalse(receipt["complete"])
            self.assertFalse(receipt["digest"]["matches"])
            self.assertEqual(receipt["terms"], [])

    def test_missing_pages_are_reported_per_revision(self):
        receipt = capture_portal(ROOT / "portal-page-1.html", expected=v10())
        self.assertEqual(
            receipt["missing_pages"],
            [
                {"revision": 10, "pages": [2]},
                {"revision": 11, "pages": [1]},
            ],
        )

    def test_recovery_routes_differ_in_terms_and_pages(self):
        receipt = capture_portal(ROOT / "portal-page-1.html", expected=v10())
        routes = {route["route"]: route for route in receipt["recovery_routes"]}
        self.assertEqual(routes["v10"]["pages_needed"], [2])
        self.assertEqual(routes["v10"]["deferred_terms"], ["B"])
        self.assertEqual(routes["v11"]["pages_needed"], [1])
        # v11 must add B while deleting X; v10 must not receive B at all.
        self.assertEqual(routes["v11"]["resulting_terms"], ["A", "B"])
        self.assertEqual(routes["v10"]["known_terms"], ["A"])


class CursorTest(unittest.TestCase):
    def setUp(self):
        self.writer = PageWriter()

    def tearDown(self):
        self.writer.close()

    def test_cursor_must_belong_to_expected_customer(self):
        first = self.writer.page("p1.html", terms=["A"], next_cursor="other/api-glossary/v10:p2")
        receipt = capture_portal(first, expected=v10())
        self.assertFalse(receipt["complete"])
        self.assertTrue(
            any("客户 other" in problem for problem in receipt["problems"])
        )

    def test_cursor_must_belong_to_expected_directory(self):
        first = self.writer.page(
            "p1.html",
            terms=["A"],
            cursor="acme/other-glossary/v10:p1",
            next_cursor="acme/other-glossary/v10:p2",
        )
        receipt = capture_portal(first, expected=v10())
        self.assertFalse(receipt["complete"])
        self.assertTrue(
            any("目录 other-glossary" in problem for problem in receipt["problems"])
        )

    def test_cursor_revision_must_match_page_revision(self):
        first = self.writer.page(
            "p1.html",
            revision=10,
            cursor="acme/api-glossary/v9:p1",
            terms=["A"],
        )
        receipt = capture_portal(first, expected=v10())
        self.assertFalse(receipt["complete"])
        self.assertTrue(
            any("属于版次 9" in problem for problem in receipt["problems"])
        )

    def test_reordered_pages_are_read_once_each(self):
        first = self.writer.page(
            "p1.html", page=1, terms=["A"], next_cursor="acme/api-glossary/v10:p2"
        )
        second = self.writer.page("p2.html", page=2, terms=["X"])
        # Physical submission order is reversed; cursors re-establish order.
        receipt = capture_portal(pages=[second, first], expected=v10())
        self.assertTrue(receipt["complete"], receipt["problems"])
        self.assertEqual(receipt["terms"], ["A", "X"])
        self.assertTrue(any("重排" in hazard for hazard in receipt["hazards"]))

    def test_renumbered_pages_do_not_drop_terms(self):
        first = self.writer.page(
            "p1.html",
            page=2,
            cursor="acme/api-glossary/v10:p1",
            next_cursor="acme/api-glossary/v10:p2",
            terms=["A"],
        )
        second = self.writer.page(
            "p2.html", page=1, cursor="acme/api-glossary/v10:p2", terms=["X"]
        )
        receipt = capture_portal(pages=[first, second], expected=v10())
        self.assertTrue(receipt["complete"], receipt["problems"])
        self.assertEqual(receipt["terms"], ["A", "X"])
        self.assertTrue(any("页码重排" in hazard for hazard in receipt["hazards"]))

    def test_duplicate_cursor_is_rejected(self):
        first = self.writer.page(
            "p1.html",
            page=1,
            cursor="acme/api-glossary/v10:p1",
            terms=["A"],
            next_cursor="acme/api-glossary/v10:p1",
        )
        second = self.writer.page(
            "p2.html",
            page=2,
            cursor="acme/api-glossary/v10:p1",
            terms=["X"],
        )
        receipt = capture_portal(pages=[first, second], expected=v10())
        self.assertFalse(receipt["complete"])
        self.assertTrue(
            any("重复出现" in problem for problem in receipt["problems"])
        )

    def test_same_page_submitted_twice_is_not_double_counted(self):
        first = self.writer.page(
            "p1.html",
            page=1,
            terms=["A"],
            next_cursor="acme/api-glossary/v10:p2",
        )
        second = self.writer.page("p2.html", page=2, terms=["X"])
        receipt = capture_portal(
            pages=[first, second, first], expected=v10()
        )
        self.assertTrue(receipt["complete"], receipt["problems"])
        self.assertEqual(receipt["terms"], ["A", "X"])
        self.assertTrue(any("去重" in hazard for hazard in receipt["hazards"]))


class TombstoneTest(unittest.TestCase):
    def setUp(self):
        self.writer = PageWriter()

    def tearDown(self):
        self.writer.close()

    def test_tombstone_not_backdated_into_older_revision(self):
        # A v10 page exported behind a v11 barrier carries the deletion, but
        # the tombstone is effective at v11 and must not touch v10.
        writer = PageWriter()
        try:
            page = writer.page(
                "v10.html",
                revision=10,
                terms=[("A", 10), ("X", 10)],
                tombstones=[("X", 11)],
                barrier=True,
            )
            expected = DirectoryDigest(
                CUSTOMER, DIRECTORY, 10,
                directory_digest(CUSTOMER, DIRECTORY, 10, ["A", "X"]),
            )
            receipt = capture_portal(page, expected=expected)
            self.assertTrue(receipt["complete"], receipt["problems"])
            self.assertEqual(receipt["terms"], ["A", "X"])
        finally:
            writer.close()

    def test_tombstone_applies_at_its_own_revision(self):
        first = self.writer.page(
            "v11p1.html",
            revision=11,
            page=1,
            terms=[("A", 10)],
            tombstones=[("X", 11)],
            next_cursor="acme/api-glossary/v11:p2",
            barrier=True,
        )
        second = self.writer.page(
            "v11p2.html", revision=11, page=2, terms=[("B", 11)], barrier=True
        )
        receipt = capture_portal(pages=[first, second], expected=v11())
        self.assertTrue(receipt["complete"], receipt["problems"])
        self.assertEqual(receipt["terms"], ["A", "B"])

    def test_replay_uses_business_revision_not_arrival_order(self):
        first = self.writer.page(
            "v11p1.html",
            revision=11,
            page=1,
            terms=[("A", 10)],
            tombstones=[("X", 11)],
            next_cursor="acme/api-glossary/v11:p2",
        )
        second = self.writer.page(
            "v11p2.html", revision=11, page=2, terms=[("B", 11)]
        )
        # Same evidence, opposite arrival order; result must not change.
        for order in ([first, second], [second, first]):
            receipt = capture_portal(pages=order, expected=v11())
            self.assertTrue(receipt["complete"])
            self.assertEqual(receipt["terms"], ["A", "B"])

    def test_unclosed_chain_with_declared_next_is_pending(self):
        first = self.writer.page(
            "p1.html", terms=["A"], next_cursor="acme/api-glossary/v10:p2"
        )
        receipt = capture_portal(pages=[first], expected=v10())
        self.assertFalse(receipt["complete"])
        self.assertTrue(
            any("页链未闭合" in problem for problem in receipt["problems"])
        )


class RecoveryRouteTest(unittest.TestCase):
    def setUp(self):
        self.store = SnapshotStore()

    def tearDown(self):
        self.store.close()

    def test_route_v10_publishes_old_snapshot_only(self):
        receipt = build(
            pages=[ROOT / "portal-page-1.html", ROOT / "portal-v10-page-2.html"],
            expected=v10(),
            store=self.store,
        )
        self.assertTrue(receipt["complete"], receipt["problems"])
        self.assertEqual(receipt["terms"], ["A", "X"])
        self.assertEqual(proven_terms(self.store, CUSTOMER, DIRECTORY), ["A", "X"])
        self.assertIsNone(self.store.snapshot(CUSTOMER, DIRECTORY, 11))

    def test_route_v11_requires_barrier_and_applies_tombstone(self):
        receipt = build(
            pages=[ROOT / "portal-v11-page-1.html", ROOT / "portal-v11-page-2.html"],
            expected=v11(),
            require_barrier=True,
            store=self.store,
        )
        self.assertTrue(receipt["complete"], receipt["problems"])
        self.assertEqual(receipt["terms"], ["A", "B"])
        snapshot = self.store.snapshot(CUSTOMER, DIRECTORY, 11)
        self.assertEqual(snapshot["tombstones"], [{"term": "X", "effective_revision": 11}])
        self.assertEqual(proven_terms(self.store, CUSTOMER, DIRECTORY), ["A", "B"])

    def test_route_v11_without_barrier_is_refused_when_required(self):
        writer = PageWriter()
        try:
            first = writer.page(
                "v11p1.html",
                revision=11,
                page=1,
                terms=[("A", 10)],
                tombstones=[("X", 11)],
                next_cursor="acme/api-glossary/v11:p2",
            )
            second = writer.page(
                "v11p2.html", revision=11, page=2, terms=[("B", 11)]
            )
            receipt = capture_portal(
                pages=[first, second], expected=v11(), require_barrier=True
            )
            self.assertFalse(receipt["complete"])
            self.assertTrue(
                any("版本屏障" in problem for problem in receipt["problems"])
            )
        finally:
            writer.close()

    def test_two_routes_keep_distinct_history(self):
        build(
            pages=[ROOT / "portal-page-1.html", ROOT / "portal-v10-page-2.html"],
            expected=v10(),
            store=self.store,
        )
        build(
            pages=[ROOT / "portal-v11-page-1.html", ROOT / "portal-v11-page-2.html"],
            expected=v11(),
            require_barrier=True,
            store=self.store,
        )
        self.assertEqual(self.store.revisions(CUSTOMER, DIRECTORY), [10, 11])
        # v11's X tombstone must never flow back into the v10 snapshot.
        self.assertEqual(proven_terms(self.store, CUSTOMER, DIRECTORY, 10), ["A", "X"])
        self.assertEqual(proven_terms(self.store, CUSTOMER, DIRECTORY, 11), ["A", "B"])
        self.assertEqual(self.store.snapshot(CUSTOMER, DIRECTORY, 10)["tombstones"], [])

    def test_old_snapshot_pages_remain_readable(self):
        build(
            pages=[ROOT / "portal-page-1.html", ROOT / "portal-v10-page-2.html"],
            expected=v10(),
            store=self.store,
        )
        build(
            pages=[ROOT / "portal-v11-page-1.html", ROOT / "portal-v11-page-2.html"],
            expected=v11(),
            require_barrier=True,
            store=self.store,
        )
        old = self.store.snapshot(CUSTOMER, DIRECTORY, 10)
        bodies = {page["body"] for page in old["pages"]}
        self.assertIn((ROOT / "portal-page-1.html").read_bytes(), bodies)
        self.assertIn((ROOT / "portal-v10-page-2.html").read_bytes(), bodies)


class SnapshotStoreTest(unittest.TestCase):
    def setUp(self):
        self.store = SnapshotStore()

    def tearDown(self):
        self.store.close()

    def test_mixed_attempt_is_retained_but_never_published(self):
        receipt = build(
            ROOT / "portal-page-1.html", expected=v10(), store=self.store
        )
        self.assertFalse(receipt["complete"])
        self.assertEqual(receipt["store"]["attempt"]["status"], PENDING_LABEL)
        self.assertIsNone(receipt["store"]["published"])
        attempts = self.store.attempts()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["complete"], 0)
        stored = self.store.db.execute(
            "SELECT COUNT(*) FROM glossary_attempt_pages"
        ).fetchone()[0]
        self.assertGreaterEqual(stored, 2)
        self.assertEqual(self.store.revisions(CUSTOMER, DIRECTORY), [])

    def test_publish_incomplete_receipt_raises(self):
        receipt = capture_portal(ROOT / "portal-page-1.html", expected=v10())
        with self.assertRaises(SnapshotError):
            self.store.publish(receipt)

    def test_proven_terms_refuses_unknown_revision(self):
        with self.assertRaises(SnapshotError):
            proven_terms(self.store, CUSTOMER, DIRECTORY, 10)

    def test_replay_same_snapshot_is_idempotent(self):
        first = build(
            pages=[ROOT / "portal-page-1.html", ROOT / "portal-v10-page-2.html"],
            expected=v10(),
            store=self.store,
        )
        second = build(
            pages=[ROOT / "portal-page-1.html", ROOT / "portal-v10-page-2.html"],
            expected=v10(),
            store=self.store,
        )
        self.assertTrue(first["store"]["published"]["published"])
        self.assertTrue(second["store"]["published"]["replayed"])
        self.assertEqual(len(self.store.attempts()), 1)

    def test_conflicting_terms_for_same_revision_are_rejected(self):
        build(
            pages=[ROOT / "portal-page-1.html", ROOT / "portal-v10-page-2.html"],
            expected=v10(),
            store=self.store,
        )
        writer = PageWriter()
        try:
            single = writer.page(
                "only-a.html",
                revision=10,
                terms=[("A", 10)],
            )
            digest = directory_digest(CUSTOMER, DIRECTORY, 10, ["A"])
            with self.assertRaises(SnapshotError):
                build(
                    pages=[single],
                    expected=DirectoryDigest(CUSTOMER, DIRECTORY, 10, digest),
                    store=self.store,
                )
            self.assertEqual(proven_terms(self.store, CUSTOMER, DIRECTORY, 10), ["A", "X"])
        finally:
            writer.close()

    def test_digest_binds_customer_directory_and_revision(self):
        base = directory_digest(CUSTOMER, DIRECTORY, 10, ["A", "X"])
        self.assertNotEqual(base, directory_digest("other", DIRECTORY, 10, ["A", "X"]))
        self.assertNotEqual(base, directory_digest(CUSTOMER, "other", 10, ["A", "X"]))
        self.assertNotEqual(base, directory_digest(CUSTOMER, DIRECTORY, 11, ["A", "X"]))
        self.assertEqual(base, directory_digest(CUSTOMER, DIRECTORY, 10, ["X", "A", "A"]))

    def test_digest_mismatch_blocks_completion(self):
        receipt = capture_portal(
            pages=[ROOT / "portal-page-1.html", ROOT / "portal-v10-page-2.html"],
            expected=DirectoryDigest(CUSTOMER, DIRECTORY, 10, V11_DIGEST),
        )
        self.assertFalse(receipt["complete"])
        self.assertEqual(receipt["status"], PENDING_LABEL)
        self.assertTrue(
            any("目录摘要不闭合" in problem for problem in receipt["problems"])
        )

    def test_complete_receipt_fingerprint_is_stable(self):
        first = capture_portal(
            pages=[ROOT / "portal-page-1.html", ROOT / "portal-v10-page-2.html"],
            expected=v10(),
        )
        second = capture_portal(
            pages=[ROOT / "portal-page-1.html", ROOT / "portal-v10-page-2.html"],
            expected=v10(),
        )
        self.assertEqual(first["receipt"], second["receipt"])
        self.assertEqual(first["status"], COMPLETE_LABEL)
        self.assertTrue(first["complete"])


if __name__ == "__main__":
    unittest.main()
