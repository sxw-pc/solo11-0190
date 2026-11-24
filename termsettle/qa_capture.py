"""Capture the pre-issued QA sample design, case clusters, and review labels.

This module records evidence only.  It intentionally does not turn a sample
count into a population defect rate: the denominator and inclusion weights are
fixed by the signed sampling plan before any inspection result is visible, and
``quality_report`` applies those design weights.  A segment that was not drawn
has no observed label and therefore is never silently treated as defect-free.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

DESIGNS = {
    "srs": "simple_random",
    "simple_random": "simple_random",
    "equal_probability": "simple_random",
    "stratified": "stratified",
    "stratified_srs": "stratified",
    "stratified_random": "stratified",
}
_UPHOLD = {"uphold", "upheld", "confirm", "confirmed", "维持", "确认"}
_OVERTURN = {"overturn", "overturned", "reject", "rejected", "revoke", "revoked", "推翻", "撤销"}
_CORRECT = {"correct", "corrected", "更正", "修正"}


class SamplingPlanError(ValueError):
    """The signed sampling design cannot support a design-based estimate."""


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _time(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise SamplingPlanError(f"{field} is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SamplingPlanError(f"{field} is not an ISO-8601 timestamp: {value!r}") from exc
    return parsed


def _before(left: datetime, right: datetime, left_name: str, right_name: str) -> None:
    if (left.tzinfo is None) != (right.tzinfo is None):
        raise SamplingPlanError(f"{left_name} and {right_name} must use compatible time zones")
    if left > right:
        raise SamplingPlanError(f"{left_name} must not be later than {right_name}")


def normalise_frame(frame: Any) -> dict:
    """Return ``{'issued_at': ..., 'segments': [...]}`` for a sampling frame."""
    issued_at = None
    if isinstance(frame, dict):
        issued_at = frame.get("issued_at")
        segments = frame.get("segments")
    else:
        segments = frame
    if not isinstance(segments, list) or not segments:
        raise SamplingPlanError("a non-empty sampling frame is required")
    result: list[dict] = []
    seen: set[str] = set()
    for raw in segments:
        if not isinstance(raw, dict):
            raise SamplingPlanError("every frame segment must be an object")
        segment_id = raw.get("id")
        stratum = raw.get("stratum")
        case_id = raw.get("case_id") or raw.get("case") or raw.get("cluster_id")
        bilingual = raw.get("bilingual", True)
        if not segment_id or not isinstance(segment_id, str):
            raise SamplingPlanError("every frame segment requires a string id")
        if segment_id in seen:
            raise SamplingPlanError(f"duplicate frame segment: {segment_id}")
        if not stratum or not isinstance(stratum, str):
            raise SamplingPlanError(f"frame segment {segment_id} requires a stratum")
        if not case_id or not isinstance(case_id, str):
            raise SamplingPlanError(f"frame segment {segment_id} requires a case cluster id")
        if not isinstance(bilingual, bool):
            raise SamplingPlanError(f"frame segment {segment_id} bilingual must be boolean")
        seen.add(segment_id)
        result.append(
            {
                "id": segment_id,
                "stratum": stratum,
                "case_id": case_id,
                "bilingual": bilingual,
            }
        )
    return {"issued_at": issued_at, "segments": result}


def bilingual_frame(frame: Any) -> dict:
    """Reject frames with non-bilingual segments: this QA frame is bilingual only."""
    normalised = normalise_frame(frame)
    not_bilingual = [segment["id"] for segment in normalised["segments"] if not segment["bilingual"]]
    if not_bilingual:
        raise SamplingPlanError(
            "QA frame must contain only bilingual delivered segments; not bilingual: "
            + ", ".join(not_bilingual)
        )
    return normalised


def case_clusters(frame: Any) -> dict[str, list[str]]:
    """Group bilingual segments into their case cluster.

    Segments split from one case are correlated and must be treated as one
    observation unit, never as several independent ones.
    """
    normalised = bilingual_frame(frame)
    grouped: dict[str, list[str]] = {}
    for segment in normalised["segments"]:
        grouped.setdefault(segment["case_id"], []).append(segment["id"])
    return grouped


def normalise_observations(raw: Any) -> dict[str, dict]:
    """Return inspection records keyed by segment id."""
    if raw is None:
        return {}
    if isinstance(raw, dict) and ("observations" in raw or "inspections" in raw):
        raw = raw.get("observations", raw.get("inspections"))
    if isinstance(raw, dict):
        records = []
        for segment_id, item in raw.items():
            if not isinstance(item, dict):
                raise SamplingPlanError(f"inspection {segment_id} must be an object")
            records.append(dict(item, id=item.get("id", segment_id)))
    elif isinstance(raw, list):
        records = raw
    else:
        raise SamplingPlanError("observations must be a list or an id-keyed object")
    result: dict[str, dict] = {}
    for item in records:
        if not isinstance(item, dict) or not item.get("id"):
            raise SamplingPlanError("every inspection record requires an id")
        if item["id"] in result:
            raise SamplingPlanError(f"duplicate inspection record: {item['id']}")
        result[item["id"]] = item
    return result


def select_plan(data: Any, plan_id: str | None = None) -> tuple[dict, dict, dict, dict]:
    """Select one signed plan and its frame/observations from a fixture or plan."""
    if not isinstance(data, dict):
        raise SamplingPlanError("sampling evidence must be an object")
    if "plans" in data:
        plans = data["plans"]
        if not isinstance(plans, list) or not plans:
            raise SamplingPlanError("plans must be a non-empty list")
        if plan_id is None:
            if len(plans) != 1:
                raise SamplingPlanError("multiple signed plans exist; specify plan_id")
            plan = plans[0]
        else:
            matches = [item for item in plans if _plan_matches(item, plan_id)]
            if len(matches) != 1:
                raise SamplingPlanError(f"signed plan not found exactly once: {plan_id}")
            plan = matches[0]
        frame = normalise_frame(data.get("frame", plan.get("frame")))
        observations = normalise_observations(data.get("observations", data.get("inspections")))
        meta = {
            "limit": plan.get("limit", data.get("limit", 0.3)),
            "delivered_count": data.get("delivered_count", len(frame["segments"])),
            "population": data.get("population"),
        }
        return plan, frame, observations, meta
    plan = data.get("plan", data)
    if not isinstance(plan, dict):
        raise SamplingPlanError("plan must be an object")
    if plan_id is not None and not _plan_matches(plan, plan_id):
        raise SamplingPlanError(f"signed plan not found: {plan_id}")
    frame = normalise_frame(plan.get("frame", data.get("frame")))
    observations = normalise_observations(
        plan.get("observations", plan.get("inspections", data.get("observations", data.get("inspections"))))
    )
    meta = {
        "limit": plan.get("limit", data.get("limit", 0.3)),
        "delivered_count": data.get("delivered_count", len(frame["segments"])),
        "population": data.get("population"),
    }
    return plan, frame, observations, meta


def plan_seal(plan: dict, frame: Any | None = None) -> str:
    """Hash the method-relevant fields so post-result editing is detectable.

    The seal covers what was fixed before inspection: design, frame, draw,
    probabilities, allocation, and issuance/signing.  Later signed events such
    as a same-stratum replacement are recorded in ``replacements`` and are
    each validated on their own; they must not rewrite the issued plan.
    """
    if frame is None:
        frame = plan.get("frame")
    frame = normalise_frame(frame)
    payload = {
        "plan_id": plan.get("plan_id"),
        "design": DESIGNS.get(plan.get("design"), plan.get("design")),
        "issued_at": plan.get("issued_at"),
        "issued_by": plan.get("issued_by"),
        "client_signed_at": plan.get("client_signed_at"),
        "client_signature": plan.get("client_signature"),
        "frame_issued_at": plan.get("frame_issued_at"),
        "limit": plan.get("limit"),
        "sample_size": plan.get("sample_size"),
        "sample": sorted(plan.get("sample", [])),
        "strata": plan.get("strata"),
        "inclusion_probabilities": plan.get("inclusion_probabilities"),
        "frame": frame,
    }
    return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()


def _plan_matches(plan: dict, selector: str) -> bool:
    return selector in {
        plan.get("plan_id"),
        plan.get("design"),
        DESIGNS.get(plan.get("design")),
    }


def _design_kind(value: Any) -> str:
    if not isinstance(value, str) or value not in DESIGNS:
        raise SamplingPlanError(f"unknown sampling design: {value!r}")
    return DESIGNS[value]


def _close(left: float, right: float) -> bool:
    return abs(left - right) <= 1e-12


def _replacement_records(plan: dict) -> list[dict]:
    records = plan.get("replacements") or []
    if not isinstance(records, list):
        raise SamplingPlanError("replacements must be a list")
    return records


def resolve_sample(plan: dict, frame: Any | None = None) -> dict:
    """Rebuild the active sample after signed withdrawals/replacements.

    A withdrawn segment is never treated as a zero-defect observation.  If the
    signed plan supplied a same-stratum replacement, that replacement carries
    the design weight.  If it supplied none, the withdrawal is returned as
    unresolved so the caller must not manufacture an estimate by assumption.
    """
    frame = bilingual_frame(frame if frame is not None else plan.get("frame"))
    by_id = {segment["id"]: segment for segment in frame["segments"]}
    drawn = list(plan.get("sample") or [])
    drawn_set = set(drawn)
    replaced: dict[str, str] = {}
    unresolved: list[dict] = []
    trail: list[dict] = []
    for record in _replacement_records(plan):
        if not isinstance(record, dict):
            raise SamplingPlanError("each replacement record must be an object")
        withdrawn = record.get("withdrawn")
        if not withdrawn or withdrawn not in drawn_set:
            raise SamplingPlanError("a withdrawal must name a segment in the signed sample")
        if withdrawn in replaced or any(item["withdrawn"] == withdrawn for item in unresolved):
            raise SamplingPlanError(f"withdrawal repeated for {withdrawn}")
        for key in ("signed_at", "signature"):
            if not record.get(key):
                raise SamplingPlanError(f"replacement for {withdrawn} requires {key}")
        signed_at = _time(record["signed_at"], f"replacement signed_at for {withdrawn}")
        issued_at = _time(plan["issued_at"], "issued_at") if plan.get("issued_at") else None
        if issued_at is not None and signed_at < issued_at:
            raise SamplingPlanError(f"replacement for {withdrawn} was signed before the sampling plan was issued")
        replacement = record.get("replacement")
        if replacement is not None:
            if replacement not in by_id:
                raise SamplingPlanError(f"replacement {replacement} is not in the signed frame")
            if replacement in drawn_set:
                raise SamplingPlanError(f"replacement {replacement} was already part of the signed sample")
            if any(replacement == item for item in replaced.values()):
                raise SamplingPlanError(f"replacement {replacement} is used more than once")
            if by_id[replacement]["stratum"] != by_id[withdrawn]["stratum"]:
                raise SamplingPlanError("a replacement must stay in the withdrawn segment's stratum")
            replaced[withdrawn] = replacement
        else:
            unresolved.append({"withdrawn": withdrawn, "signed_at": record["signed_at"],
                               "signature": record["signature"], "reason": record.get("reason", "")})
        trail.append(dict(record))
    withdrawn_ids = set(replaced) | {item["withdrawn"] for item in unresolved}
    active = [segment_id for segment_id in drawn if segment_id not in withdrawn_ids]
    active.extend(replaced.values())
    return {
        "drawn_sample": drawn,
        "active_sample": active,
        "withdrawn": sorted(withdrawn_ids),
        "replaced": replaced,
        "unresolved": unresolved,
        "trail": trail,
    }


def validate_plan(
    plan: dict,
    frame: Any | None = None,
    observations: Any | None = None,
    *,
    require_seal: bool = False,
) -> dict:
    """Validate that a plan was signed before results and is internally coherent.

    The checks deliberately reject a sample whose recorded inclusion
    probabilities do not follow the design named in that same signed plan.  It
    is therefore impossible to paste a second design's weights onto a sample
    that was drawn under another design.
    """
    if not isinstance(plan, dict):
        raise SamplingPlanError("plan must be an object")
    required = (
        "plan_id",
        "design",
        "issued_at",
        "issued_by",
        "client_signed_at",
        "client_signature",
        "sample",
        "inclusion_probabilities",
    )
    missing = [key for key in required if not plan.get(key)]
    if missing:
        raise SamplingPlanError(f"sampling plan missing required signed fields: {', '.join(missing)}")

    kind = _design_kind(plan["design"])
    frame = bilingual_frame(frame if frame is not None else plan.get("frame"))
    segments = frame["segments"]
    by_id = {segment["id"]: segment for segment in segments}
    population_size = len(segments)

    sample = plan["sample"]
    if not isinstance(sample, list) or not sample:
        raise SamplingPlanError("sample must be a non-empty list of segment ids")
    if any(not isinstance(item, str) or item not in by_id for item in sample):
        raise SamplingPlanError("every sampled segment must exist in the signed frame")
    if len(set(sample)) != len(sample):
        raise SamplingPlanError("sample contains duplicate segment ids")
    if plan.get("sample_size") is not None and int(plan["sample_size"]) != len(sample):
        raise SamplingPlanError("sample_size does not match the sampled id list")

    probabilities = plan["inclusion_probabilities"]
    if not isinstance(probabilities, dict):
        raise SamplingPlanError("inclusion_probabilities must be an id-keyed object")
    if set(probabilities) != set(by_id):
        raise SamplingPlanError("inclusion probabilities must be recorded for every frame segment")
    for segment_id, probability in probabilities.items():
        if isinstance(probability, bool) or not isinstance(probability, (int, float)):
            raise SamplingPlanError(f"inclusion probability for {segment_id} must be numeric")
        if not 0 < float(probability) <= 1:
            raise SamplingPlanError(f"inclusion probability for {segment_id} must be in (0, 1]")

    sample_by_stratum: dict[str, list[str]] = {}
    for segment_id in sample:
        sample_by_stratum.setdefault(by_id[segment_id]["stratum"], []).append(segment_id)
    strata: dict[str, dict] = {}
    for stratum in sorted({segment["stratum"] for segment in segments}):
        members = [segment["id"] for segment in segments if segment["stratum"] == stratum]
        strata[stratum] = {
            "population": len(members),
            "sample_size": len(sample_by_stratum.get(stratum, [])),
            "sample": sorted(sample_by_stratum.get(stratum, [])),
        }

    if kind == "simple_random":
        if plan.get("strata"):
            raise SamplingPlanError(
                "simple-random plan must not declare strata; a stratified draw needs the "
                "stratified design, not SRS weights applied after the sample is known"
            )
        expected = len(sample) / population_size
        for segment_id, probability in probabilities.items():
            if not _close(float(probability), expected):
                raise SamplingPlanError(
                    "simple-random design requires the same inclusion probability "
                    f"{expected!r} for every frame segment; {segment_id} has {probability!r}"
                )
    else:
        configured = plan.get("strata")
        if not isinstance(configured, dict) or set(configured) != set(strata):
            raise SamplingPlanError("stratified design requires one pre-signed entry per frame stratum")
        expected_total = sum(
            int(config["sample_size"])
            for config in configured.values()
            if isinstance(config, dict) and "sample_size" in config
        )
        if expected_total != len(sample):
            raise SamplingPlanError("the drawn sample size disagrees with the pre-signed stratum allocation")
        for stratum, observed in strata.items():
            config = configured[stratum]
            if not isinstance(config, dict):
                raise SamplingPlanError(f"stratum {stratum} configuration must be an object")
            population = config.get("population")
            sample_size = config.get("sample_size")
            if population != observed["population"]:
                raise SamplingPlanError(f"stratum {stratum} population disagrees with the signed frame")
            if not isinstance(sample_size, int) or sample_size <= 0 or sample_size > population:
                raise SamplingPlanError(f"stratum {stratum} requires a valid pre-signed sample_size")
            if sample_size != observed["sample_size"]:
                raise SamplingPlanError(f"stratum {stratum} sample_size disagrees with the drawn sample")
            expected = sample_size / population
            for segment in segments:
                if segment["stratum"] == stratum and not _close(
                    float(probabilities[segment["id"]]), expected
                ):
                    raise SamplingPlanError(
                        f"stratum {stratum} inclusion probability must be {expected!r}; "
                        f"{segment['id']} has {probabilities[segment['id']]!r}"
                    )

    if plan.get("population") is not None:
        supplied = plan["population"]
        if not isinstance(supplied, dict):
            raise SamplingPlanError("population must be an id-keyed stratum count")
        for stratum, observed in strata.items():
            if supplied.get(stratum) != observed["population"]:
                raise SamplingPlanError(f"population count for stratum {stratum} disagrees with the frame")
        if set(supplied) != set(strata):
            raise SamplingPlanError("population strata disagree with the signed frame")

    issued_at = _time(plan["issued_at"], "issued_at")
    client_signed_at = _time(plan["client_signed_at"], "client_signed_at")
    # The client signs the design before it is issued, and the design is issued
    # before any sampled segment is inspected.
    _before(client_signed_at, issued_at, "client_signed_at", "issued_at")

    resolution = resolve_sample(plan, frame)

    records = normalise_observations(observations)
    observations_digest = hashlib.sha256(
        canonical(
            [
                {
                    "id": segment_id,
                    "defect": record.get("defect"),
                    "inspected_at": record.get("inspected_at"),
                    "inspection_signature": record.get("inspection_signature"),
                    "review": record.get("review"),
                    "reviews": record.get("reviews"),
                    "correction": record.get("correction"),
                }
                for segment_id, record in sorted(records.items())
            ]
        ).encode("utf-8")
    ).hexdigest()
    unknown = set(records) - set(by_id)
    if unknown:
        raise SamplingPlanError(f"inspection records outside the signed frame: {', '.join(sorted(unknown))}")
    if records:
        for segment_id in resolution["active_sample"]:
            record = records.get(segment_id)
            if record is None:
                raise SamplingPlanError(f"no inspection record for sampled segment {segment_id}")
            inspected_at = _time(record.get("inspected_at"), f"inspected_at for {segment_id}")
            if inspected_at < issued_at:
                raise SamplingPlanError(
                    f"sampling plan {plan['plan_id']} was issued after {segment_id} was inspected"
                )
            if inspected_at < client_signed_at:
                raise SamplingPlanError(
                    f"sampling plan {plan['plan_id']} was not client-signed before {segment_id} was inspected"
                )
    elif not resolution["unresolved"]:
        raise SamplingPlanError("inspection records are required for the signed sample")

    if plan.get("issued_digest") is not None:
        expected_digest = plan_seal(plan, frame)
        if plan["issued_digest"] != expected_digest:
            raise SamplingPlanError(
                "signed plan digest mismatch: design, sample, weights, strata, or issuance fields changed"
            )
    elif require_seal:
        raise SamplingPlanError("requirement: a sealed issued_digest is required")

    return {
        "plan_id": plan["plan_id"],
        "design": kind,
        "design_label": plan["design"],
        "issued_at": plan["issued_at"],
        "issued_by": plan["issued_by"],
        "client_signed_at": plan["client_signed_at"],
        "client_signature": plan["client_signature"],
        "issued_digest": plan.get("issued_digest"),
        "observations_digest": observations_digest,
        "population_size": population_size,
        "sample_size": len(sample),
        "sample": list(sample),
        "active_sample": list(resolution["active_sample"]),
        "withdrawn": list(resolution["withdrawn"]),
        "replaced": dict(resolution["replaced"]),
        "unresolved": list(resolution["unresolved"]),
        "replacement_trail": list(resolution["trail"]),
        "inclusion_probabilities": {key: float(value) for key, value in probabilities.items()},
        "strata": strata,
        "frame": frame,
        "by_id": by_id,
    }


def _signed_review(review: Any, segment_id: str, field: str) -> str:
    if not isinstance(review, dict):
        raise SamplingPlanError(f"{field} for {segment_id} must be an object")
    for key in ("reviewer", "signed_at", "signature"):
        if not review.get(key):
            raise SamplingPlanError(f"{field} for {segment_id} requires {key}")
    _time(review["signed_at"], f"{field}.signed_at for {segment_id}")
    decision = str(review.get("decision", "")).strip().lower()
    if field == "correction":
        if "defect" not in review:
            raise SamplingPlanError(f"correction for {segment_id} requires a defect label")
        return decision or "correct"
    if decision not in _UPHOLD | _OVERTURN | _CORRECT and "corrected_defect" not in review:
        raise SamplingPlanError(f"{field} for {segment_id} has an unknown decision: {review.get('decision')!r}")
    return decision


def label_evidence(item: dict) -> dict:
    """Return the original label, signed review trail, and final effective label."""
    if not isinstance(item, dict) or "defect" not in item:
        raise SamplingPlanError("an inspection record requires the original defect label")
    segment_id = item.get("id", "<unknown>")
    original = bool(item["defect"])
    effective = original
    evidence: dict[str, Any] = {
        "id": segment_id,
        "as_inspected": original,
        "inspection_signature": item.get("inspection_signature"),
        "reviews": [],
        "correction": None,
        "disputed": False,
    }

    reviews = []
    if item.get("review") is not None:
        reviews.append(item["review"])
    extra = item.get("reviews")
    if extra is not None:
        if not isinstance(extra, list):
            raise SamplingPlanError(f"reviews for {segment_id} must be a list")
        reviews.extend(extra)
    if len(reviews) > 1:
        timestamps = [_time(review.get("signed_at") if isinstance(review, dict) else None,
                            f"review.signed_at for {segment_id}") for review in reviews]
        if sorted(timestamps) != timestamps:
            raise SamplingPlanError(f"signed reviews for {segment_id} are not in signed order")

    previous = original
    for review in reviews:
        decision = _signed_review(review, segment_id, "review")
        if "corrected_defect" in review:
            current = bool(review["corrected_defect"])
        elif decision in _OVERTURN:
            current = False
        elif decision in _UPHOLD:
            current = previous
        else:
            current = bool(review.get("corrected_defect", not previous))
        if evidence["reviews"] and current != effective:
            evidence["disputed"] = True
        effective = current
        evidence["reviews"].append(dict(review))

    differing = sorted(
        {bool(review["corrected_defect"]) for review in reviews if "corrected_defect" in review}
    )
    if len(differing) > 1:
        evidence["disputed"] = True

    correction = item.get("correction")
    if correction is not None:
        _signed_review(correction, segment_id, "correction")
        if "defect" not in correction:
            raise SamplingPlanError(f"correction for {segment_id} requires a defect label")
        effective = bool(correction["defect"])
        evidence["correction"] = dict(correction)
        latest_review = max(
            (_time(review["signed_at"], "review.signed_at") for review in evidence["reviews"]),
            default=None,
        )
        if latest_review is not None and _time(
            correction["signed_at"], "correction.signed_at"
        ) < latest_review:
            raise SamplingPlanError(f"correction for {segment_id} predates the last signed review")
        evidence["disputed"] = False

    evidence["effective"] = effective
    return evidence


def effective_defect(item: dict) -> bool:
    """Final confirmed label after signed review/correction, keeping history intact."""
    return label_evidence(item)["effective"]


def flagged(inspections: Any) -> int:
    """Count raw as-inspected flags; reviews never erase this original label."""
    records = normalise_observations(inspections)
    return sum(bool(item["defect"]) for item in records.values())


def confirmed_flagged(inspections: Any) -> int:
    """Count defects that survive signed review/correction."""
    records = normalise_observations(inspections)
    return sum(1 for item in records.values() if effective_defect(item))
