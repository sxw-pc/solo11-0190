"""Glossary snapshots that only publish a proven, closed directory revision.

The module turns a :func:`termsettle.portal_capture.capture_portal` receipt
into an immutable SQLite snapshot. Only a receipt whose entire page chain
and customer-issued directory digest close at one frozen revision can be
published. Mixed-revision attempts are still retained verbatim (raw pages
plus failure status) so the supplementary capture can be audited later.

A snapshot is keyed by ``(customer, directory, revision)``. Publishing a
newer revision never rewrites an older one: older snapshots remain readable,
and a deletion tombstone from ``v11`` is never pushed back into ``v10``.

Public entry points
-------------------

``build(start=None, pages=None, expected=None, ...)``
    Capture (and, with ``store=``, retain) one attempt. Returns the receipt.

``SnapshotStore``
    Immutable snapshot history, raw attempt pages and failure states.

``proven_terms(store, customer, directory, revision=None)``
    The only supported way for downstream policy code to consume glossary
    terms: it refuses snapshots that were never proven complete.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from .portal_capture import (
    COMPLETE_LABEL,
    DirectoryDigest,
    capture_portal,
    directory_digest,
)

__all__ = [
    "SnapshotError",
    "SnapshotStore",
    "build",
    "proven_terms",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS glossary_snapshots (
    customer TEXT NOT NULL,
    directory TEXT NOT NULL,
    revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    terms_digest TEXT NOT NULL,
    directory_digest TEXT NOT NULL,
    receipt_sha TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    PRIMARY KEY (customer, directory, revision)
);
CREATE TABLE IF NOT EXISTS glossary_snapshot_terms (
    customer TEXT NOT NULL,
    directory TEXT NOT NULL,
    revision INTEGER NOT NULL,
    position INTEGER NOT NULL,
    term TEXT NOT NULL,
    effective_revision INTEGER,
    PRIMARY KEY (customer, directory, revision, term)
);
CREATE TABLE IF NOT EXISTS glossary_snapshot_tombstones (
    customer TEXT NOT NULL,
    directory TEXT NOT NULL,
    revision INTEGER NOT NULL,
    term TEXT NOT NULL,
    effective_revision INTEGER,
    PRIMARY KEY (customer, directory, revision, term)
);
CREATE TABLE IF NOT EXISTS glossary_snapshot_pages (
    customer TEXT NOT NULL,
    directory TEXT NOT NULL,
    revision INTEGER NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    body BLOB NOT NULL,
    PRIMARY KEY (customer, directory, revision, sha256)
);
CREATE TABLE IF NOT EXISTS glossary_capture_attempts (
    attempt_sha TEXT PRIMARY KEY,
    customer TEXT,
    directory TEXT,
    revisions TEXT NOT NULL,
    status TEXT NOT NULL,
    complete INTEGER NOT NULL,
    receipt_json TEXT NOT NULL,
    problems_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS glossary_attempt_pages (
    attempt_sha TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    body BLOB NOT NULL,
    PRIMARY KEY (attempt_sha, sha256)
);
"""


class SnapshotError(ValueError):
    """Raised when an attempt cannot be published as a proven snapshot."""


def _sha256_bytes(body):
    return hashlib.sha256(body).hexdigest()


def _terms_digest(customer, directory, revision, terms):
    return directory_digest(customer, directory, revision, terms)


class SnapshotStore:
    """Immutable glossary snapshot history backed by SQLite."""

    def __init__(self, path=":memory:"):
        path = str(path)
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    # -- attempts -----------------------------------------------------

    def record_attempt(self, receipt):
        """Retain a capture attempt, complete or not, with its raw pages.

        Attempts are keyed by the receipt fingerprint, so replaying the same
        evidence is idempotent. Raw page bytes are copied into the database
        so a later cleanup of the local export cannot erase the evidence.
        """

        attempt_sha = receipt["receipt"]
        revisions = json.dumps(receipt.get("revisions") or [], ensure_ascii=False)
        problems = json.dumps(
            receipt.get("problems") or [], ensure_ascii=False, sort_keys=True
        )
        pages = []
        for item in receipt.get("raw_html") or []:
            path = Path(item["path"])
            body = path.read_bytes()
            digest = _sha256_bytes(body)
            if item.get("sha256") and item["sha256"] != digest:
                raise SnapshotError(f"原始页摘要不符: {path}")
            pages.append((str(path), digest, body))

        self.db.execute("BEGIN IMMEDIATE")
        try:
            existing = self.db.execute(
                "SELECT 1 FROM glossary_capture_attempts WHERE attempt_sha=?",
                (attempt_sha,),
            ).fetchone()
            if existing:
                self.db.execute("ROLLBACK")
                return {
                    "attempt": attempt_sha,
                    "status": receipt.get("status"),
                    "replayed": True,
                    "stored_pages": len(pages),
                }
            self.db.execute(
                "INSERT INTO glossary_capture_attempts "
                "(attempt_sha, customer, directory, revisions, status, complete, "
                " receipt_json, problems_json) VALUES (?,?,?,?,?,?,?,?)",
                (
                    attempt_sha,
                    receipt.get("customer"),
                    receipt.get("directory"),
                    revisions,
                    receipt.get("status"),
                    1 if receipt.get("complete") else 0,
                    json.dumps(receipt, ensure_ascii=False, sort_keys=True),
                    problems,
                ),
            )
            for path, digest, body in pages:
                self.db.execute(
                    "INSERT OR IGNORE INTO glossary_attempt_pages "
                    "(attempt_sha, path, sha256, body) VALUES (?,?,?,?)",
                    (attempt_sha, path, digest, body),
                )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return {
            "attempt": attempt_sha,
            "status": receipt.get("status"),
            "replayed": False,
            "stored_pages": len(pages),
        }

    def attempts(self):
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM glossary_capture_attempts ORDER BY attempt_sha"
        )]

    # -- publication --------------------------------------------------

    def publish(self, receipt):
        """Publish a proven snapshot. Incomplete receipts are rejected.

        The same revision may be replayed with identical terms, but a
        different term set for the same customer/directory/revision is a
        contradiction and is refused rather than silently overwritten.
        """

        if not receipt.get("complete"):
            raise SnapshotError(
                "采录未闭合，状态为待补，不能发布为完整词表: "
                + "; ".join(receipt.get("problems") or ["未证明完整"])
            )
        revision = receipt.get("revision")
        customer = receipt.get("customer")
        directory = receipt.get("directory")
        terms = list(receipt.get("terms") or [])
        if revision is None or not customer or not directory:
            raise SnapshotError("完整采录缺少客户、目录或版次")

        digest = receipt.get("digest") or {}
        if not digest.get("matches"):
            raise SnapshotError("目录摘要未闭合，不能发布")
        actual = _terms_digest(customer, directory, revision, terms)
        if actual != digest.get("actual"):
            raise SnapshotError("回执中的目录摘要与实际词表不一致")

        terms_digest = _terms_digest(customer, directory, revision, terms)
        raw_pages = []
        for item in receipt.get("raw_html") or []:
            path = Path(item["path"])
            body = path.read_bytes()
            body_sha = _sha256_bytes(body)
            if item.get("sha256") and item["sha256"] != body_sha:
                raise SnapshotError(f"原始页摘要不符: {path}")
            raw_pages.append((str(path), body_sha, body))

        tombstones = []
        for observation in receipt.get("raw_pages") or []:
            if observation.get("revision") != revision:
                continue
            for tomb in observation.get("tombstones") or []:
                effective = tomb.get("effective_revision")
                if effective is None or effective <= revision:
                    tombstones.append((tomb["term"], effective))

        self.db.execute("BEGIN IMMEDIATE")
        try:
            existing = self.db.execute(
                "SELECT * FROM glossary_snapshots "
                "WHERE customer=? AND directory=? AND revision=?",
                (customer, directory, revision),
            ).fetchone()
            if existing:
                if existing["terms_digest"] == terms_digest:
                    self.db.execute("ROLLBACK")
                    return {
                        "published": False,
                        "replayed": True,
                        "customer": customer,
                        "directory": directory,
                        "revision": revision,
                        "terms": terms,
                        "terms_digest": terms_digest,
                    }
                raise SnapshotError(
                    f"同客户同目录同版次出现不同词表，拒绝覆盖: "
                    f"{customer}/{directory}/v{revision}"
                )

            self.db.execute(
                "INSERT INTO glossary_snapshots "
                "(customer, directory, revision, status, terms_digest, "
                " directory_digest, receipt_sha, receipt_json) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    customer,
                    directory,
                    revision,
                    COMPLETE_LABEL,
                    terms_digest,
                    digest.get("expected") or digest.get("actual"),
                    receipt["receipt"],
                    json.dumps(receipt, ensure_ascii=False, sort_keys=True),
                ),
            )
            for position, term in enumerate(terms):
                effective = None
                for observation in receipt.get("raw_pages") or []:
                    if observation.get("revision") != revision:
                        continue
                    for entry in observation.get("terms") or []:
                        if entry["term"] == term:
                            effective = entry.get("effective_revision")
                            break
                    if effective is not None:
                        break
                self.db.execute(
                    "INSERT INTO glossary_snapshot_terms "
                    "(customer, directory, revision, position, term, "
                    " effective_revision) VALUES (?,?,?,?,?,?)",
                    (customer, directory, revision, position, term, effective),
                )
            for term, effective in tombstones:
                self.db.execute(
                    "INSERT OR IGNORE INTO glossary_snapshot_tombstones "
                    "(customer, directory, revision, term, effective_revision) "
                    "VALUES (?,?,?,?,?)",
                    (customer, directory, revision, term, effective),
                )
            for path, body_sha, body in raw_pages:
                self.db.execute(
                    "INSERT OR IGNORE INTO glossary_snapshot_pages "
                    "(customer, directory, revision, path, sha256, body) "
                    "VALUES (?,?,?,?,?,?)",
                    (customer, directory, revision, path, body_sha, body),
                )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return {
            "published": True,
            "replayed": False,
            "customer": customer,
            "directory": directory,
            "revision": revision,
            "terms": terms,
            "terms_digest": terms_digest,
        }

    # -- reads --------------------------------------------------------

    def snapshot(self, customer, directory, revision):
        row = self.db.execute(
            "SELECT * FROM glossary_snapshots "
            "WHERE customer=? AND directory=? AND revision=?",
            (customer, directory, int(revision)),
        ).fetchone()
        if row is None:
            return None
        return self._expand(row)

    def latest(self, customer, directory):
        row = self.db.execute(
            "SELECT * FROM glossary_snapshots "
            "WHERE customer=? AND directory=? ORDER BY revision DESC LIMIT 1",
            (customer, directory),
        ).fetchone()
        return self._expand(row) if row is not None else None

    def history(self, customer, directory):
        rows = self.db.execute(
            "SELECT * FROM glossary_snapshots "
            "WHERE customer=? AND directory=? ORDER BY revision",
            (customer, directory),
        ).fetchall()
        return [self._expand(row) for row in rows]

    def revisions(self, customer, directory):
        return [
            row["revision"]
            for row in self.db.execute(
                "SELECT revision FROM glossary_snapshots "
                "WHERE customer=? AND directory=? ORDER BY revision",
                (customer, directory),
            )
        ]

    def _expand(self, row):
        key = (row["customer"], row["directory"], row["revision"])
        terms = [
            dict(term)
            for term in self.db.execute(
                "SELECT term, position, effective_revision "
                "FROM glossary_snapshot_terms WHERE customer=? AND directory=? "
                "AND revision=? ORDER BY position",
                key,
            )
        ]
        tombstones = [
            dict(tomb)
            for tomb in self.db.execute(
                "SELECT term, effective_revision FROM glossary_snapshot_tombstones "
                "WHERE customer=? AND directory=? AND revision=? ORDER BY term",
                key,
            )
        ]
        pages = [
            {
                "path": page["path"],
                "sha256": page["sha256"],
                "body": bytes(page["body"]),
            }
            for page in self.db.execute(
                "SELECT path, sha256, body FROM glossary_snapshot_pages "
                "WHERE customer=? AND directory=? AND revision=? "
                "ORDER BY path, sha256",
                key,
            )
        ]
        return {
            "customer": row["customer"],
            "directory": row["directory"],
            "revision": row["revision"],
            "status": row["status"],
            "complete": row["status"] == COMPLETE_LABEL,
            "terms": [term["term"] for term in terms],
            "term_rows": terms,
            "tombstones": tombstones,
            "terms_digest": row["terms_digest"],
            "directory_digest": row["directory_digest"],
            "receipt_sha": row["receipt_sha"],
            "receipt": json.loads(row["receipt_json"]),
            "pages": pages,
        }


def build(
    first_page=None,
    *,
    pages=None,
    expected=None,
    customer=None,
    directory=None,
    require_barrier=False,
    store=None,
):
    """Capture one portal attempt and optionally retain it in ``store``.

    Returns the capture receipt. When a store is supplied the attempt is
    always recorded (including failed, mixed-revision attempts); complete
    receipts are additionally published as immutable snapshots.
    """

    receipt = capture_portal(
        start=first_page,
        pages=pages,
        expected=DirectoryDigest.from_dict(expected),
        customer=customer,
        directory=directory,
        require_barrier=require_barrier,
    )
    if store is not None:
        attempt = store.record_attempt(receipt)
        published = None
        if receipt.get("complete"):
            # A contradictory revision raises here; the attempt remains
            # retained and the immutable existing snapshot is never altered.
            published = store.publish(receipt)
        receipt["store"] = {"attempt": attempt, "published": published}
    return receipt


def proven_terms(store, customer, directory, revision=None):
    """Return terms only from a proven, complete customer revision.

    ``revision=None`` selects the newest published revision. There is no
    fallback to an incomplete capture: callers that need unproven data must
    read the retained attempt explicitly.
    """

    snapshot = (
        store.latest(customer, directory)
        if revision is None
        else store.snapshot(customer, directory, revision)
    )
    if snapshot is None:
        raise SnapshotError(f"没有该客户版次的快照: {customer}/{directory}")
    if not snapshot["complete"]:
        raise SnapshotError(
            f"快照未经证明完整: {customer}/{directory}/v{snapshot['revision']}"
        )
    return list(snapshot["terms"])
