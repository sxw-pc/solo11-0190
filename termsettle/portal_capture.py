"""Version-aware capture of an offline customer glossary portal.

A page is not merely a bag of terms. Every observation is tied to the
customer, directory, revision, page number and cursor under which it was
seen. The portal may be rebuilt while a capture is in flight, so a chain
that crosses revisions is never a snapshot: it is an incomplete attempt
that has to be supplemented.

Public entry points
-------------------

``parse_page(path)``
    Parse one local HTML page into a :class:`PageObservation`.

``read_page(path)``
    Backwards-compatible list of active terms on one page.

``capture_portal(start=None, pages=None, expected=None, ...)``
    Walk ``data-next`` links (or accept an explicit, already exported page
    chain) and return a deterministic capture receipt. The receipt is
    ``complete`` only when the entire page chain and the customer-issued
    directory digest close at one frozen revision.

HTML contract
-------------

The fixtures use the following attributes; all of them are optional except
``data-revision`` for pages that are meant to prove a snapshot::

    <body data-customer="acme"
          data-directory="api-glossary"
          data-revision="10"
          data-page="1"
          data-cursor="v10:p1"
          data-next-cursor="v10:p2"
          data-directory-digest="sha256:...">
      <div data-term="A">A term</div>
      <div data-term="X" data-effective-revision="10">old term</div>
      <div data-deleted-term="X" data-effective-revision="11">removed</div>
      <a data-next="portal-page-2.html">Next</a>
    </body>

A term can also be marked as a tombstone with ``data-deleted="true"`` (or
``data-tombstone="true"``) next to ``data-term``. ``data-effective-revision``
is the business revision in which an addition or deletion takes effect. It
is deliberately independent of any file timestamp or capture arrival time.
``data-barrier`` (``data-version-barrier`` is accepted too) declares that
the page export is fenced by a version barrier.

Nothing here performs network I/O: every link is resolved to a local file.
"""

from __future__ import annotations

import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urldefrag

__all__ = [
    "PENDING_LABEL",
    "COMPLETE_LABEL",
    "DirectoryDigest",
    "PageObservation",
    "capture_portal",
    "directory_digest",
    "parse_page",
    "read_page",
]

PENDING_LABEL = "待补"
COMPLETE_LABEL = "完整"

_FALSE_VALUES = {"", "0", "false", "no", "off", "n", "f", "否"}
# A cursor may be scoped as ``customer/directory/v10:p2`` or left as the
# plain ``v10:p2``. Scope is optional so older exports keep parsing, but when
# present it is authoritative and must agree with the page and the digest.
_CURSOR_RE = re.compile(
    r"^\s*(?:(?P<customer>[^/:\s]+)/(?P<directory>[^/:\s]+)/)?"
    r"v?(?P<revision>\d+)\s*[:._-]\s*p(?P<page>\d+)\s*$",
    re.IGNORECASE,
)


def _is_true(value):
    return value is not None and value.strip().lower() not in _FALSE_VALUES


def _int_or_none(value):
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _cursor_match(cursor):
    if not cursor:
        return None
    return _CURSOR_RE.match(cursor)


def _cursor_parts(cursor):
    """Return ``(revision, page)`` encoded in a cursor such as ``v10:p2``."""

    match = _cursor_match(cursor)
    if not match:
        return None, None
    return int(match.group("revision")), int(match.group("page"))


def _cursor_scope(cursor):
    """Return ``(customer, directory)`` carried by a scoped cursor."""

    match = _cursor_match(cursor)
    if not match:
        return None, None
    return match.group("customer"), match.group("directory")


class _PageParser(HTMLParser):
    """Collect body-level metadata and per-term attributes."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.body_attrs = {}
        self.terms = []
        self.tombstones = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs = {
            name.lower(): (value if value is not None else "")
            for name, value in attrs
        }
        if tag.lower() == "body":
            self.body_attrs.update(attrs)
        if "data-term" in attrs:
            name = attrs.get("data-term", "").strip()
            if name:
                deleted = _is_true(attrs.get("data-deleted")) or _is_true(
                    attrs.get("data-tombstone")
                )
                entry = {
                    "term": name,
                    "effective_revision": _int_or_none(
                        attrs.get("data-effective-revision")
                    ),
                    "deleted": deleted,
                }
                if deleted:
                    self.tombstones.append(entry)
                else:
                    self.terms.append(entry)
        if "data-deleted-term" in attrs:
            name = attrs.get("data-deleted-term", "").strip()
            if name:
                self.tombstones.append(
                    {
                        "term": name,
                        "effective_revision": _int_or_none(
                            attrs.get("data-effective-revision")
                        ),
                        "deleted": True,
                    }
                )
        if tag.lower() == "a" and "data-next" in attrs:
            self.links.append(attrs.get("data-next", "").strip())


class PageObservation:
    """One parsed page, frozen so later mutation cannot rewrite evidence."""

    __slots__ = (
        "path",
        "customer",
        "directory",
        "revision",
        "page",
        "cursor",
        "next_cursor",
        "directory_digest",
        "barrier",
        "terms",
        "tombstones",
        "next",
    )

    def __init__(
        self,
        path,
        customer,
        directory,
        revision,
        page,
        cursor,
        next_cursor,
        directory_digest,
        barrier,
        terms,
        tombstones,
        next_link,
    ):
        self.path = path
        self.customer = customer
        self.directory = directory
        self.revision = revision
        self.page = page
        self.cursor = cursor
        self.next_cursor = next_cursor
        self.directory_digest = directory_digest
        self.barrier = barrier
        self.terms = tuple(dict(term) for term in terms)
        self.tombstones = tuple(dict(tomb) for tomb in tombstones)
        self.next = next_link

    @property
    def active_terms(self):
        return [term["term"] for term in self.terms]

    def to_dict(self):
        return {
            "path": str(self.path),
            "customer": self.customer,
            "directory": self.directory,
            "revision": self.revision,
            "page": self.page,
            "cursor": self.cursor,
            "next_cursor": self.next_cursor,
            "directory_digest": self.directory_digest,
            "barrier": self.barrier,
            "terms": [dict(term) for term in self.terms],
            "tombstones": [dict(tomb) for tomb in self.tombstones],
            "next": self.next,
        }


def parse_page(file):
    """Parse a local HTML page into a :class:`PageObservation`."""

    path = Path(file)
    parser = _PageParser()
    parser.feed(path.read_text(encoding="utf-8"))
    parser.close()
    attrs = parser.body_attrs
    link = parser.links[0] if parser.links else None
    return PageObservation(
        path=path,
        customer=(attrs.get("data-customer") or "").strip() or None,
        directory=(attrs.get("data-directory") or "").strip() or None,
        revision=_int_or_none(attrs.get("data-revision")),
        page=_int_or_none(attrs.get("data-page")),
        cursor=(attrs.get("data-cursor") or "").strip() or None,
        next_cursor=(attrs.get("data-next-cursor") or "").strip() or None,
        directory_digest=(attrs.get("data-directory-digest") or "").strip() or None,
        barrier=_is_true(attrs.get("data-barrier"))
        or _is_true(attrs.get("data-version-barrier")),
        terms=parser.terms,
        tombstones=parser.tombstones,
        next_link=link or None,
    )


def read_page(file):
    """Return the active (non-tombstone) terms of one page in document order."""

    return parse_page(file).active_terms


def directory_digest(customer, directory, revision, terms):
    """Canonical customer-issued digest over a directory revision.

    The digest binds the customer, the directory name, the business revision
    and the exported term set. It cannot be reused across customers or
    revisions, and a capture cannot mix pages from two revisions and still
    match one digest.
    """

    payload = json.dumps(
        {
            "customer": customer,
            "directory": directory,
            "revision": int(revision),
            "terms": sorted(set(terms)),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


class DirectoryDigest:
    """A customer-issued expected digest for one frozen directory revision."""

    __slots__ = ("customer", "directory", "revision", "digest", "terms")

    def __init__(self, customer, directory, revision, digest, terms=None):
        self.customer = customer
        self.directory = directory
        self.revision = int(revision)
        self.digest = digest
        self.terms = tuple(sorted(set(terms))) if terms is not None else None

    @classmethod
    def from_dict(cls, value):
        if value is None or isinstance(value, DirectoryDigest):
            return value
        if isinstance(value, (tuple, list)):
            return cls(*value)
        return cls(
            customer=value["customer"],
            directory=value["directory"],
            revision=value["revision"],
            digest=value["digest"],
            terms=value.get("terms"),
        )

    def matches(self, customer, directory, revision, terms):
        if (self.customer, self.directory, int(revision)) != (
            customer,
            directory,
            self.revision,
        ):
            return False
        normalised = tuple(sorted(set(terms)))
        if self.terms is not None and normalised != self.terms:
            return False
        return self.digest == directory_digest(
            customer, directory, revision, normalised
        )

    def to_dict(self):
        return {
            "customer": self.customer,
            "directory": self.directory,
            "revision": self.revision,
            "digest": self.digest,
            "terms": list(self.terms) if self.terms is not None else None,
        }


def _resolve_link(current, link):
    """Resolve a page link to a local path without ever touching the network."""

    if not link:
        return None
    link = unquote(link.strip())
    link, _fragment = urldefrag(link)
    if "://" in link or link.startswith("//"):
        raise ValueError(f"remote links are not permitted in offline capture: {link!r}")
    candidate = Path(link)
    if not candidate.is_absolute():
        candidate = current.parent / candidate
    return candidate


def _walk_linked(start):
    """Follow local ``data-next`` links; return observations and dead links."""

    observations = []
    dangling = []
    seen = set()
    current = Path(start)
    while current is not None:
        key = current.resolve()
        if key in seen:
            raise ValueError(f"page chain loops back to {current}")
        if not current.is_file():
            dangling.append(str(current))
            break
        seen.add(key)
        observation = parse_page(current)
        observations.append(observation)
        current = _resolve_link(current, observation.next)
    return observations, dangling


def _order_by_cursor(observations):
    """Order an explicit page list by its cursor chain, when one exists.

    Page numbers may be reassigned by the portal while a capture is in
    flight. Cursors, not page numbers, are the stable anchors, so an
    explicit export that arrives in a different physical order is put back
    into cursor order before any term is replayed.
    """

    if not observations or any(not item.cursor for item in observations):
        return observations, None
    by_cursor = {item.cursor: item for item in observations}
    if len(by_cursor) != len(observations):
        return observations, None
    targets = {
        item.next_cursor for item in observations if item.next_cursor
    }
    roots = [item.cursor for item in observations if item.cursor not in targets]
    if len(roots) != 1:
        return observations, None
    ordered = []
    seen = set()
    current = roots[0]
    while current is not None:
        if current in seen:
            return observations, None
        seen.add(current)
        item = by_cursor.get(current)
        if item is None:
            return observations, None
        ordered.append(item)
        current = item.next_cursor
    if len(ordered) != len(observations):
        return observations, None
    if [item.path for item in ordered] != [item.path for item in observations]:
        return ordered, "按游标重排显式页链"
    return observations, None


def _read_explicit_pages(pages):
    observations = []
    hazards = []
    seen = set()
    for item in pages:
        path = Path(item)
        key = path.resolve()
        if key in seen:
            hazards.append(f"重复提交同一页，已去重: {path}")
            continue
        seen.add(key)
        observations.append(parse_page(path))
    observations, reorder_note = _order_by_cursor(observations)
    if reorder_note:
        hazards.append(reorder_note)
    return observations, hazards


def _page_number(observation):
    _revision, page = _cursor_parts(observation.cursor)
    return page if page is not None else observation.page


def _replay_revision(observations, revision):
    """Replay additions and tombstones at one business revision.

    Events are ordered by their business effective revision; a tombstone is
    applied after an addition at the same revision. Capture arrival order
    and page numbering have no influence on the resulting set. A page that
    purges history (for example a full export without legacy tombstones)
    is still replayed by its business effective revision, never by arrival.
    """

    events = []
    for observation in observations:
        if observation.revision != revision:
            continue
        for term in observation.terms:
            effective = term["effective_revision"]
            if effective is None:
                effective = revision
            if effective <= revision:
                events.append((effective, 0, term["term"]))
        for tomb in observation.tombstones:
            effective = tomb["effective_revision"]
            if effective is None:
                effective = revision
            if effective <= revision:
                events.append((effective, 1, tomb["term"]))
    # At a given effective revision apply additions first, then deletions.
    # Document order inside an observation is preserved by the term name
    # tiebreak only for determinism; it does not change set membership.
    events.sort(key=lambda item: (item[0], item[1], item[2]))
    active = set()
    for _effective, kind, name in events:
        if kind == 0:
            active.add(name)
        else:
            active.discard(name)
    return sorted(active)


def _observed_by_revision(observations):
    result = {}
    for observation in observations:
        if observation.revision is None:
            continue
        bucket = result.setdefault(
            observation.revision, {"pages": [], "terms": [], "tombstones": []}
        )
        page = _page_number(observation)
        if page is not None:
            bucket["pages"].append(page)
        bucket["terms"].extend(term["term"] for term in observation.terms)
        bucket["tombstones"].extend(tomb["term"] for tomb in observation.tombstones)
    for bucket in result.values():
        bucket["pages"] = sorted(set(bucket["pages"]))
        bucket["terms"] = sorted(set(bucket["terms"]))
        bucket["tombstones"] = sorted(set(bucket["tombstones"]))
    return {str(revision): bucket for revision, bucket in sorted(result.items())}


def _known_page_positions(observations):
    """Every page position the captured evidence names, cursor first."""

    by_cursor = any(observation.cursor for observation in observations)
    known = set()
    for observation in observations:
        if by_cursor:
            for cursor in (observation.cursor, observation.next_cursor):
                _cursor_revision, cursor_page = _cursor_parts(cursor)
                if cursor_page is not None:
                    known.add(cursor_page)
        elif observation.page is not None:
            known.add(observation.page)
    return known


def _missing_pages(observations):
    """Pages still absent for every revision the capture touched.

    The cursor is the stable anchor: page numbers can be reassigned by the
    portal mid-capture, so when cursors are available only cursor positions
    define which pages must exist. ``data-page`` is used solely as a legacy
    fallback when no cursor exists. The expected page positions are the
    union across the whole batch, because every revision of one directory
    has the same page span; a revision that lacks one of those positions is
    exactly what has to be re-captured.
    """

    observed = {}
    for observation in observations:
        revision = observation.revision
        page = _page_number(observation)
        if revision is not None and page is not None:
            observed.setdefault(revision, set()).add(page)
    known = _known_page_positions(observations)
    # Pagination starts at 0 only when the portal itself numbers a page 0.
    # Every position up to the highest known page must be observed for each
    # revision; a revision renumbered mid-capture still has to supply every
    # cursor position, so renumbering cannot hide an uncaptured page.
    low = 0 if known and min(known) == 0 else 1
    span = set(range(low, max(known) + 1)) if known else set()
    missing = []
    for revision in sorted(observed):
        absent = sorted(span - observed[revision])
        if absent:
            missing.append({"revision": revision, "pages": absent})
    return missing


def _analyze_chain(observations, expected, require_barrier, dangling=None):
    problems = []
    hazards = []
    dangling = list(dangling or [])
    if not observations:
        if dangling:
            problems.append(
                "页链指向未采录的本地页面: " + ", ".join(dangling)
            )
        return problems, hazards, [], False

    revisions = sorted(
        {
            observation.revision
            for observation in observations
            if observation.revision is not None
        }
    )
    missing_revision = any(
        observation.revision is None for observation in observations
    )
    if missing_revision:
        problems.append("页面缺少目录版次")
    mixed = len(revisions) > 1
    if mixed:
        problems.append(f"页面跨版次混用: {revisions}")

    customers = sorted(
        {observation.customer for observation in observations if observation.customer}
    )
    directories = sorted(
        {
            observation.directory
            for observation in observations
            if observation.directory
        }
    )
    if len(customers) > 1:
        problems.append(f"客户不一致: {customers}")
    if len(directories) > 1:
        problems.append(f"目录不一致: {directories}")
    if expected is not None:
        for index, observation in enumerate(observations, start=1):
            if observation.customer != expected.customer:
                problems.append(
                    f"第 {index} 页客户 {observation.customer!r} "
                    f"不等于目录摘要客户 {expected.customer!r}"
                )
            if observation.directory != expected.directory:
                problems.append(
                    f"第 {index} 页目录 {observation.directory!r} "
                    f"不等于目录摘要目录 {expected.directory!r}"
                )
            if observation.revision != expected.revision:
                problems.append(
                    f"第 {index} 页版次 {observation.revision!r} "
                    f"不等于目录摘要版次 {expected.revision}"
                )

    declared_pages = [
        observation.page if observation.page is not None else index
        for index, observation in enumerate(observations, start=1)
    ]
    if any(observation.page is None for observation in observations):
        hazards.append("部分页面缺少显式页码，按采录顺序解释")
    if declared_pages != sorted(declared_pages):
        hazards.append(f"页码重排: {declared_pages}；以游标为准")
    if len(set(declared_pages)) != len(declared_pages):
        hazards.append(f"页码重复: {declared_pages}；以游标为准")

    seen_cursors = set()
    for index, observation in enumerate(observations, start=1):
        if not observation.cursor:
            problems.append(f"第 {index} 页缺少游标")
        else:
            if _cursor_match(observation.cursor) is None:
                problems.append(
                    f"第 {index} 页游标 {observation.cursor!r} 无法解析版次/页码"
                )
            if observation.cursor in seen_cursors:
                problems.append(
                    f"游标 {observation.cursor} 重复出现，可能重复计入"
                )
            seen_cursors.add(observation.cursor)
            cursor_revision, _page = _cursor_parts(observation.cursor)
            if (
                cursor_revision is not None
                and observation.revision is not None
                and cursor_revision != observation.revision
            ):
                problems.append(
                    f"游标 {observation.cursor} 属于版次 {cursor_revision}，"
                    f"页面声明版次 {observation.revision}"
                )
            cursor_customer, cursor_directory = _cursor_scope(observation.cursor)
            if cursor_customer and observation.customer and (
                cursor_customer != observation.customer
            ):
                problems.append(
                    f"游标 {observation.cursor} 属于客户 {cursor_customer}，"
                    f"页面声明客户 {observation.customer}"
                )
            if cursor_directory and observation.directory and (
                cursor_directory != observation.directory
            ):
                problems.append(
                    f"游标 {observation.cursor} 属于目录 {cursor_directory}，"
                    f"页面声明目录 {observation.directory}"
                )
            if expected is not None:
                if cursor_customer and cursor_customer != expected.customer:
                    problems.append(
                        f"游标 {observation.cursor} 属于客户 {cursor_customer}，"
                        f"不是目录摘要客户 {expected.customer}"
                    )
                if cursor_directory and cursor_directory != expected.directory:
                    problems.append(
                        f"游标 {observation.cursor} 属于目录 {cursor_directory}，"
                        f"不是目录摘要目录 {expected.directory}"
                    )
        if observation.next_cursor:
            next_revision, _page = _cursor_parts(observation.next_cursor)
            if (
                next_revision is not None
                and observation.revision is not None
                and next_revision != observation.revision
            ):
                problems.append(
                    f"第 {index} 页的 next 游标 {observation.next_cursor} "
                    f"属于版次 {next_revision}，页面声明版次 {observation.revision}"
                )
            if observation.next_cursor == observation.cursor:
                problems.append(f"第 {index} 页的游标指向自身")
            next_customer, next_directory = _cursor_scope(observation.next_cursor)
            if next_customer and observation.customer and (
                next_customer != observation.customer
            ):
                problems.append(
                    f"第 {index} 页的 next 游标 {observation.next_cursor} "
                    f"属于客户 {next_customer}，页面声明客户 {observation.customer}"
                )
            if next_directory and observation.directory and (
                next_directory != observation.directory
            ):
                problems.append(
                    f"第 {index} 页的 next 游标 {observation.next_cursor} "
                    f"属于目录 {next_directory}，页面声明目录 {observation.directory}"
                )
            if expected is not None:
                if next_customer and next_customer != expected.customer:
                    problems.append(
                        f"第 {index} 页的 next 游标 {observation.next_cursor} "
                        f"属于客户 {next_customer}，不是目录摘要客户 {expected.customer}"
                    )
                if next_directory and next_directory != expected.directory:
                    problems.append(
                        f"第 {index} 页的 next 游标 {observation.next_cursor} "
                        f"属于目录 {next_directory}，不是目录摘要目录 {expected.directory}"
                    )

    for index in range(len(observations) - 1):
        previous = observations[index]
        current = observations[index + 1]
        if not previous.next_cursor:
            problems.append(
                f"第 {index + 1} 页缺少 next 游标，无法证明连续翻页"
            )
        elif current.cursor and previous.next_cursor != current.cursor:
            problems.append(
                f"第 {index + 1} 页的 next 游标 {previous.next_cursor} "
                f"与第 {index + 2} 页游标 {current.cursor} 不连续"
            )
        next_customer, next_directory = _cursor_scope(previous.next_cursor)
        if next_customer and previous.customer and next_customer != previous.customer:
            problems.append(
                f"第 {index + 1} 页的 next 游标属于客户 {next_customer}，"
                f"页面属于 {previous.customer}"
            )
        if next_directory and previous.directory and next_directory != previous.directory:
            problems.append(
                f"第 {index + 1} 页的 next 游标属于目录 {next_directory}，"
                f"页面属于 {previous.directory}"
            )

    last = observations[-1]
    if last.next_cursor:
        if last.next_cursor not in seen_cursors:
            # A captured page chain ends only at a page that declares no
            # (or an already-satisfied) next cursor. Anything else is an
            # incomplete chain, even when the last page was captured.
            problems.append(
                f"最后一页仍声明后续游标 {last.next_cursor}，页链未闭合"
            )
    if dangling:
        problems.append(
            "页链指向未采录的本地页面: " + ", ".join(dangling)
        )

    for item in _missing_pages(observations):
        problems.append(f"版次 {item['revision']} 缺少页 {item['pages']}")
    if require_barrier and not all(
        observation.barrier for observation in observations
    ):
        problems.append("缺少版本屏障，不能发布该版次快照")

    return problems, hazards, revisions, mixed


def _recovery_routes(receipt):
    """Describe both customer-authorised recovery routes for a mixed capture.

    A mixed batch cannot prove any snapshot by itself. Route 1 completes the
    older revision with its issued digest, missing original pages and cursor
    proof; route 2 publishes the newer revision from a barrier-fenced full
    export carrying the deletion tombstones. They need different pages and
    yield different term sets.
    """

    revisions = receipt.get("revisions") or []
    missing = receipt.get("missing_pages") or []
    if len(revisions) < 2:
        return []
    older, newer = revisions[0], revisions[-1]
    missing_by_revision = {
        item["revision"]: item["pages"] for item in missing
    }
    observed = receipt.get("observed_by_revision") or {}
    routes = []
    older_observed = observed.get(str(older), {})
    newer_observed = observed.get(str(newer), {})
    newer_tombstones = newer_observed.get("tombstones", [])
    # v11 with a barrier-fenced full export and the deletion tombstones:
    # legacy terms still active, minus deleted ones, plus terms added at v11.
    resulting_newer = sorted(
        (set(older_observed.get("terms", [])) | set(newer_observed.get("terms", [])))
        - set(newer_tombstones)
    )
    routes.append(
        {
            "route": f"v{older}",
            "revision": older,
            "pages_needed": missing_by_revision.get(older, []),
            "requirements": [
                f"取得 v{older} 客户签发目录摘要",
                f"取得 v{older} 缺失页原件",
                "提供连续游标证明",
            ],
            "known_terms": older_observed.get("terms", []),
            "resulting_terms": None,
            "resulting_terms_note": (
                f"须由 v{older} 缺失页原件补足，且不得包含 v{newer} 新增词条"
            ),
            "deferred_terms": sorted(
                set(newer_observed.get("terms", []))
                - set(older_observed.get("terms", []))
            ),
            "note": "只能发布旧版快照；较新版观察值留待后续版次，不得混入。",
        }
    )
    routes.append(
        {
            "route": f"v{newer}",
            "revision": newer,
            "pages_needed": missing_by_revision.get(newer, []),
            "requirements": [
                f"取得带版本屏障的 v{newer} 全页导出",
                "取得删除墓碑",
                f"取得 v{newer} 客户签发目录摘要",
            ],
            "known_terms": newer_observed.get("terms", []),
            "resulting_terms": resulting_newer,
            "resulting_terms_note": (
                f"旧版保留词条 + v{newer} 新增词条，记为删除的 "
                f"{sorted(newer_tombstones)} 不计入"
            ),
            "deferred_terms": [],
            "note": "发布较新版快照；旧版历史快照仍可回读，墓碑不得回灌旧版。",
        }
    )
    return routes


def capture_portal(
    start=None,
    pages=None,
    expected=None,
    customer=None,
    directory=None,
    require_barrier=False,
):
    """Capture a local portal page chain and return a deterministic receipt.

    ``start`` follows ``data-next`` links. ``pages`` supplies an explicit
    page chain (for customer exports). ``expected`` is the customer-issued
    :class:`DirectoryDigest`; without it a capture can never be complete.
    """

    if pages is None and start is None:
        raise ValueError("start or pages is required")
    if pages is not None:
        observations, hazards = _read_explicit_pages(pages)
        dangling = []
    else:
        observations, dangling = _walk_linked(start)
        hazards = []
    expected = DirectoryDigest.from_dict(expected)

    problems, chain_hazards, revisions, mixed = _analyze_chain(
        observations, expected, require_barrier, dangling=dangling
    )
    hazards.extend(chain_hazards)

    # A page that carries its own issued digest must agree with the caller's
    # expected digest; it is never allowed to override it.
    for index, observation in enumerate(observations, start=1):
        if observation.directory_digest is None:
            continue
        if expected is not None and observation.directory_digest != expected.digest:
            problems.append(
                f"第 {index} 页目录摘要 {observation.directory_digest} "
                f"与客户签发摘要 {expected.digest} 不一致"
            )

    if customer is not None:
        for index, observation in enumerate(observations, start=1):
            if observation.customer != customer:
                problems.append(
                    f"第 {index} 页客户 {observation.customer!r} "
                    f"与断言 {customer!r} 不一致"
                )
    if directory is not None:
        for index, observation in enumerate(observations, start=1):
            if observation.directory != directory:
                problems.append(
                    f"第 {index} 页目录 {observation.directory!r} "
                    f"与断言 {directory!r} 不一致"
                )

    frozen_revision = revisions[0] if len(revisions) == 1 else None
    if expected is None:
        problems.append("缺少客户签发目录摘要，不能闭合目录")
    digest_actual = None
    digest_matches = False
    terms = []
    if expected is not None and frozen_revision is not None and not mixed:
        terms = _replay_revision(observations, frozen_revision)
        digest_actual = directory_digest(
            expected.customer, expected.directory, frozen_revision, terms
        )
        digest_matches = expected.matches(
            expected.customer, expected.directory, frozen_revision, terms
        )
        if not digest_matches:
            problems.append(
                f"目录摘要不闭合: 期望 {expected.digest}，实际 {digest_actual}"
            )
    elif expected is not None:
        problems.append("未形成单一冻结版次，无法闭合目录摘要")

    complete = bool(observations) and not problems and digest_matches
    if not complete:
        # An incomplete attempt is never patched into a term list. Only the
        # per-revision observations are reported, so no A+B pseudo-snapshot
        # can be mistaken for a same-revision directory.
        terms = []

    page_customer = (
        expected.customer
        if expected is not None
        else customer
        if customer is not None
        else next(
            (
                observation.customer
                for observation in observations
                if observation.customer
            ),
            None,
        )
    )
    page_directory = (
        expected.directory
        if expected is not None
        else directory
        if directory is not None
        else next(
            (
                observation.directory
                for observation in observations
                if observation.directory
            ),
            None,
        )
    )

    raw_html = []
    for observation in observations:
        raw_html.append(
            {
                "path": str(observation.path),
                "sha256": hashlib.sha256(
                    Path(observation.path).read_bytes()
                ).hexdigest(),
            }
        )

    receipt = {
        "complete": complete,
        "status": COMPLETE_LABEL if complete else PENDING_LABEL,
        "customer": page_customer,
        "directory": page_directory,
        "revision": frozen_revision if complete else None,
        "revisions": revisions,
        "mixed": mixed,
        "terms": terms,
        "observed_by_revision": _observed_by_revision(observations),
        "missing_pages": _missing_pages(observations),
        "pages_requested": {
            "observed": sorted(
                {
                    _page_number(observation)
                    for observation in observations
                    if _page_number(observation) is not None
                }
            ),
            "known": sorted(_known_page_positions(observations)),
        },
        "problems": problems,
        "hazards": hazards,
        "digest": {
            "expected": expected.digest if expected is not None else None,
            "actual": digest_actual,
            "matches": digest_matches,
        },
        "pages": [
            {
                "path": str(observation.path),
                "customer": observation.customer,
                "directory": observation.directory,
                "revision": observation.revision,
                "page": observation.page,
                "cursor": observation.cursor,
                "next_cursor": observation.next_cursor,
                "barrier": observation.barrier,
                "terms": [term["term"] for term in observation.terms],
                "tombstones": [
                    {
                        "term": tomb["term"],
                        "effective_revision": tomb["effective_revision"],
                    }
                    for tomb in observation.tombstones
                ],
            }
            for observation in observations
        ],
        "raw_pages": [observation.to_dict() for observation in observations],
        "raw_html": raw_html,
    }
    receipt["recovery_routes"] = _recovery_routes(receipt)
    material = {key: value for key, value in receipt.items() if key != "receipt"}
    receipt["receipt"] = "sha256:" + hashlib.sha256(
        json.dumps(
            material, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    return receipt
