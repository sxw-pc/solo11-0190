"""Design-weighted defect estimation and the QA re-check decision.

The estimate is computed from the sampling design that was signed *and sealed
before inspection*.  It uses the recorded inclusion probabilities, and the
eight unsampled segments receive no label: they contribute nothing to the
numerator, because unknown is not zero.

Two pre-signed designs can legitimately answer the same observed sample
differently.  For ten segments, a simple random sample of two with one defect
gives ``(1 / 0.2) / (1 / 0.2 + 1 / 0.2) = 0.5``; a stratified design drawing one
segment from each of the 2 high- and 8 low-risk strata gives
``(1 / 0.5) / (1 / 0.5 + 1 / 0.125) = 0.2``.  The design, drawn sample,
probabilities, and threshold all come from the selected signed plan, so the
denominator cannot be swapped after seeing the result.

Segments split from one case are correlated.  The case is therefore the
observation unit: a case sampled through several of its segments contributes
once, and its inclusion probability is the exact probability that the signed
design drew at least one of its segments.  When every case has one segment the
estimate reduces exactly to the segment-level Horvitz-Thompson ratio.

This ledger decides QA sampling scope only.  It never re-adjudicates
terminology, translation delivery, or billing.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from .qa_capture import (
    SamplingPlanError,
    case_clusters,
    label_evidence,
    normalise_observations,
    select_plan,
    validate_plan,
)

EXPAND = "expand_full_recheck"
CONTINUE = "continue_routine_sampling"
HOLD_REVIEW = "hold_for_adjudication"
HOLD_EVIDENCE = "hold_insufficient_evidence"

DECISION_TEXT = {
    EXPAND: "扩大全量复检",
    CONTINUE: "继续常规抽样",
    HOLD_REVIEW: "暂缓：复核意见未决",
    HOLD_EVIDENCE: "暂缓：样本证据不完整",
}

_OVERTURN = {"overturn", "overturned", "reject", "rejected", "revoke", "revoked", "推翻", "撤销"}
_UPHOLD = {"uphold", "upheld", "confirm", "confirmed", "维持", "确认"}


def _load(evidence: Any) -> Any:
    if isinstance(evidence, (str, bytes, Path)):
        return json.loads(Path(evidence).read_text(encoding="utf-8"))
    return evidence


def _comb(n: int, k: int) -> int:
    return math.comb(n, k) if 0 <= k <= n else 0


def _srs_miss(population: int, members: int, drawn: int) -> float:
    """Probability that none of ``members`` is in an SRS of ``drawn``/``population``."""
    total = _comb(population, drawn)
    if total == 0:
        raise SamplingPlanError("signed design sample size exceeds its population")
    return _comb(population - members, drawn) / total


def cluster_inclusion(
    kind: str,
    members: list[str],
    probabilities: dict[str, float],
    strata: dict[str, dict],
    frame_by_id: dict[str, dict],
) -> float:
    """Exact at-least-one inclusion probability for a case cluster.

    Under the signed simple-random design every segment has the same
    probability ``n/N``.  Under the signed stratified design each stratum is an
    independent simple random sample, so the cluster's miss probability is the
    product over strata of the hypergeometric misses.
    """
    if kind == "simple_random":
        population = len(probabilities)
        p = next(iter(probabilities.values()))
        drawn = round(p * population)
        if not math.isclose(p, drawn / population, rel_tol=0, abs_tol=1e-12):
            raise SamplingPlanError("simple-random inclusion probability must be n/N")
        miss = _srs_miss(population, len(members), drawn)
    else:
        miss = 1.0
        for stratum, config in strata.items():
            in_stratum = [segment_id for segment_id in members if frame_by_id[segment_id]["stratum"] == stratum]
            if not in_stratum:
                continue
            miss *= _srs_miss(int(config["population"]), len(in_stratum), int(config["sample_size"]))
    return 1.0 - miss


def _cluster_rows(validated: dict, active: list[str]) -> list[dict]:
    frame = validated["frame"]["segments"]
    probabilities = validated["inclusion_probabilities"]
    by_id = validated["by_id"]
    grouped: dict[str, list[str]] = {}
    for segment in frame:
        grouped.setdefault(segment["case_id"], []).append(segment["id"])
    sampled = set(active)
    rows = []
    for case_id, members in grouped.items():
        drawn = sorted(segment_id for segment_id in members if segment_id in sampled)
        if not drawn:
            continue
        inclusion = cluster_inclusion(
            validated["design"], members, probabilities, validated["strata"], by_id
        )
        if inclusion <= 0:
            raise SamplingPlanError(f"case cluster {case_id} has no positive inclusion probability")
        rows.append(
            {
                "case_id": case_id,
                "sampled": drawn,
                "members": sorted(members),
                "inclusion_probability": inclusion,
                "fully_sampled": set(members) <= sampled,
            }
        )
    return rows


def _point_estimate(validated: dict, active: list[str], labels: dict[str, bool]) -> dict:
    frame = validated["frame"]["segments"]
    probabilities = validated["inclusion_probabilities"]
    rows = _cluster_rows(validated, active)

    numerator = denominator = 0.0
    unknown: list[str] = []
    for row in rows:
        sampled_labels = [labels[segment_id] for segment_id in row["sampled"]]
        if row["fully_sampled"]:
            row["defect"] = any(sampled_labels)
            row["status"] = "observed"
        elif any(sampled_labels):
            # An observed defect settles the case even if siblings were not drawn.
            row["defect"] = True
            row["status"] = "defect_confirmed_from_partial"
        else:
            # A clean sampled segment does not prove un inspected siblings clean.
            row["defect"] = None
            row["status"] = "unknown_partial"
            unknown.append(row["case_id"])
        row["known"] = row["defect"] is not None
        row["weight"] = 1.0 / row["inclusion_probability"]
        if row["known"]:
            row["contribution"] = (1.0 if row["defect"] else 0.0) / row["inclusion_probability"]
            numerator += row["contribution"]
            denominator += row["weight"]
        else:
            row["contribution"] = None
    estimate = numerator / denominator if denominator else 0.0

    # The independent-segment ratio is retained only as a diagnostic.  It is
    # not used for the decision because it would count a multi-segment case
    # as several independent observations.
    segment_numerator = sum(
        (1.0 if labels[segment_id] else 0.0) / probabilities[segment_id] for segment_id in active
    )
    segment_denominator = sum(1.0 / probabilities[segment_id] for segment_id in active)
    segment_estimate = segment_numerator / segment_denominator if segment_denominator else 0.0

    # A design-based variance for the case-cluster ratio lets the report show
    # how uncertain the decision is.  The Taylor linearisation given in
    # Cochran (1977, ch. 6A) for a ratio of two Horvitz-Thompson totals is
    #     v(R) = (1 / D^2) * sum over sampled cases of
    #            (y_case - R * 1)^2 / pi_case^2
    # with the finite-population factor (1 - f) omitted because the sample is
    # small; it is labelled as an approximation and does not change the
    # point decision.  It is not used to widen or narrow the signed trigger.
    variance = None
    standard_error = None
    if denominator:
        variance = sum(
            ((1.0 if row["defect"] else 0.0) - estimate) ** 2 / row["inclusion_probability"] ** 2
            for row in rows
            if row["known"]
        ) / denominator ** 2
        standard_error = math.sqrt(variance)

    return {
        "variance_approximation": variance,
        "standard_error": standard_error,
        "estimate": estimate,
        "horvitz_thompson_numerator": numerator,
        "horvitz_thompson_denominator": denominator,
        "population_clusters": len({segment["case_id"] for segment in frame}),
        "sampled_cluster_count": len(rows),
        "clusters": rows,
        "unknown_clusters": sorted(unknown),
        "collapsed_clusters": sorted(
            row["case_id"] for row in rows if len(row["members"]) > 1
        ),
        "partially_sampled_clusters": [
            row["case_id"] for row in rows if not row["fully_sampled"]
        ],
        "segment_level_estimate": segment_estimate,
        "sampled_segments": sorted(active),
        "sample_unit": "case_cluster",
        "complete": not unknown,
    }


def _version(version: str, basis: str, validated: dict, active: list[str],
             labels: dict[str, bool], note: str) -> dict:
    point = _point_estimate(validated, active, labels)
    return {
        "version": version,
        "basis": basis,
        "estimate": point["estimate"],
        "segment_level_estimate": point["segment_level_estimate"],
        "horvitz_thompson_numerator": point["horvitz_thompson_numerator"],
        "horvitz_thompson_denominator": point["horvitz_thompson_denominator"],
        "population_clusters": point["population_clusters"],
        "clusters": point["clusters"],
        "unknown_clusters": point["unknown_clusters"],
        "variance_approximation": point["variance_approximation"],
        "standard_error": point["standard_error"],
        "complete": point["complete"],
        "note": note,
    }


def _review_chain(raw_record: dict) -> list[dict]:
    reviews = []
    if raw_record.get("review") is not None:
        reviews.append(raw_record["review"])
    if raw_record.get("reviews") is not None:
        reviews.extend(raw_record["reviews"])
    return reviews


def _apply_review(previous: bool, review: dict) -> bool:
    decision = str(review.get("decision", "")).strip().lower()
    if "corrected_defect" in review:
        return bool(review["corrected_defect"])
    if decision in _OVERTURN:
        return False
    if decision in _UPHOLD:
        return previous
    return bool(review.get("corrected_defect", not previous))


def estimate(evidence: Any, plan_id: str | None = None, *, require_seal: bool = True) -> dict:
    """Return a reproducible design-weighted estimate and QA decision.

    ``evidence`` is the fixture object or a path to it.  ``plan_id`` selects one
    pre-signed plan when several are recorded; it may also be a design name
    such as ``srs`` or ``stratified``.  A plan carrying ``issued_digest`` is
    always verified; ``require_seal=True`` additionally rejects plans without
    one.
    """
    data = _load(evidence)
    plan, frame, observations, meta = select_plan(data, plan_id)
    validated = validate_plan(plan, frame, observations, require_seal=require_seal)
    active = validated["active_sample"]
    frame_segments = validated["frame"]["segments"]

    if meta.get("delivered_count") is not None and int(meta["delivered_count"]) != len(frame_segments):
        raise SamplingPlanError(
            "delivered_count disagrees with the signed sampling frame; the population "
            "denominator cannot be chosen after the result is known"
        )
    if meta.get("population") is not None:
        supplied = meta["population"]
        actual: dict[str, int] = {}
        for segment in frame_segments:
            actual[segment["stratum"]] = actual.get(segment["stratum"], 0) + 1
        if not isinstance(supplied, dict) or {key: int(value) for key, value in supplied.items()} != actual:
            raise SamplingPlanError("population counts disagree with the signed sampling frame")

    raw = normalise_observations(observations)
    missing_observations = [segment_id for segment_id in active if segment_id not in raw]
    if missing_observations:
        raise SamplingPlanError(
            "no inspection record for sampled segment(s): " + ", ".join(missing_observations)
        )

    labels = {
        segment_id: label_evidence(raw[segment_id]) for segment_id in active
    }
    as_inspected = {segment_id: labels[segment_id]["as_inspected"] for segment_id in active}

    versions = [
        _version(
            "v1-as-inspected",
            "as_inspected",
            validated,
            active,
            as_inspected,
            "原始采录标签；复核与更正不覆盖此版本",
        )
    ]

    # Rebuild the signed review/correction chain stage by stage.  Each stage
    # creates a retained estimate version; earlier labels and versions survive.
    current = dict(as_inspected)
    stage = 1
    for segment_id in active:
        record = raw[segment_id]
        for review in _review_chain(record):
            stage += 1
            previous = current[segment_id]
            current[segment_id] = _apply_review(previous, review)
            versions.append(
                _version(
                    f"v{stage}-review",
                    "review",
                    validated,
                    active,
                    dict(current),
                    f"{segment_id} 复核 {review.get('decision')}，签名 {review.get('signed_at')}",
                )
            )
        correction = record.get("correction")
        if correction is not None:
            stage += 1
            current[segment_id] = bool(correction["defect"])
            versions.append(
                _version(
                    f"v{stage}-correction",
                    "correction",
                    validated,
                    active,
                    dict(current),
                    f"{segment_id} 更正为 {current[segment_id]}，签名 {correction.get('signed_at')}",
                )
            )

    final = versions[-1]
    disputed = sorted(
        segment_id
        for segment_id, row in labels.items()
        if row.get("disputed") and row.get("correction") is None
    )
    unresolved = list(validated["unresolved"])
    unobserved = [segment["id"] for segment in frame_segments if segment["id"] not in set(active)]

    if unresolved:
        decision = HOLD_EVIDENCE
        reason = (
            "被检段撤回后无同层签发替补或入样不完整；未观察单位没有标签，"
            "不得按零缺陷计入，需补齐样本后再定范围。"
        )
    elif not final["complete"]:
        decision = HOLD_EVIDENCE
        reason = (
            "同案其他未抽中段无法佐证该案为无缺陷，部分观察的案件缺陷状态仍未知；"
            "不得把未抽中段当作零缺陷补齐后强行出决定。"
        )
    elif disputed:
        decision = HOLD_REVIEW
        reason = "同一被检段存在相互冲突的已签名复核意见且无最终更正；需重新裁定后再定范围。"
    else:
        limit = float(meta["limit"])
        if not 0 < limit <= 1:
            raise SamplingPlanError("the signed re-check limit must be within (0, 1]")
        if final["estimate"] > limit + 1e-12:
            decision = EXPAND
            reason = (
                f"设计加权缺陷估计 {final['estimate']:.6f} 高于触发线 {limit:g}；"
                "按预先签发并封存的抽样计划启动扩大全量复检。"
            )
        else:
            decision = CONTINUE
            reason = (
                f"设计加权缺陷估计 {final['estimate']:.6f} 未超过触发线 {limit:g}；维持常规抽样。"
            )

    return {
        "plan": {
            "plan_id": validated["plan_id"],
            "design": validated["design"],
            "design_label": validated["design_label"],
            "issued_at": validated["issued_at"],
            "issued_by": validated["issued_by"],
            "client_signed_at": validated["client_signed_at"],
            "client_signature": validated["client_signature"],
            "issued_digest": validated["issued_digest"],
            "observations_digest": validated["observations_digest"],
            "issued_limit": float(meta["limit"]),
            "sample_size": validated["sample_size"],
            "population_size": validated["population_size"],
            "strata": validated["strata"],
            "inclusion_probabilities": validated["inclusion_probabilities"],
        },
        "frame": {
            "segment_count": len(frame_segments),
            "cluster_count": final["population_clusters"],
            "case_clusters": {
                case_id: sorted(ids)
                for case_id, ids in case_clusters(frame_segments).items()
            },
            "segments": [segment["id"] for segment in frame_segments],
            "bilingual": all(segment.get("bilingual", True) for segment in frame_segments),
        },
        "sample": {
            "drawn": list(validated["sample"]),
            "active": list(active),
            "withdrawn": list(validated["withdrawn"]),
            "replaced": dict(validated["replaced"]),
            "unresolved_withdrawals": unresolved,
            "unobserved": unobserved,
            "unobserved_note": "未抽中段没有观察标签；它们的贡献是未知，不是零缺陷。",
        },
        "observations": [labels[segment_id] for segment_id in active],
        "versions": versions,
        "estimate": final["estimate"],
        "defect_rate": final["estimate"],
        "design_estimate": final["estimate"],
        "segment_level_estimate": final["segment_level_estimate"],
        "standard_error": final["standard_error"],
        "limit": float(meta["limit"]),
        "decision": decision,
        "decision_text": DECISION_TEXT[decision],
        "reason": reason,
        "decision_basis": final["version"],
        "reproducible": {
            "issued_digest": validated["issued_digest"],
            "observations_digest": validated["observations_digest"],
            "estimate": final["estimate"],
            "decision": decision,
            "recompute": (
                "sum over sampled cases of (defective_case / pi_case) divided by "
                "sum over sampled cases of (1 / pi_case)"
            ),
        },
        "decides_scope_only": True,
        "scope_note": "本账只决定质检抽查范围，不重裁术语定版、译稿交付或计费。",
        "formula": (
            "case-cluster Horvitz-Thompson ratio: "
            "sum(y_case / pi_case) / sum(1 / pi_case); "
            "pi_case = P(design drew at least one segment of the case)"
        ),
    }


def defect_rate(evidence: Any, plan_id: str | None = None, *, require_seal: bool = True) -> float:
    """Design-weighted point estimate for the selected signed plan.

    This replaces the former ``flagged(inspections) / delivered_count``
    shortcut.  Evidence without a signed, pre-issued design raises
    ``SamplingPlanError`` instead of silently inventing a denominator.
    """
    return estimate(evidence, plan_id, require_seal=require_seal)["estimate"]


def quality_decision(evidence: Any, plan_id: str | None = None, *, require_seal: bool = True) -> dict:
    """Full reproducible decision record for one signed plan."""
    return estimate(evidence, plan_id, require_seal=require_seal)


def decisions(evidence: Any, *, require_seal: bool = True) -> dict:
    """Evaluate every signed plan recorded in the fixture independently.

    The same observed sample under two valid pre-signed designs can produce two
    different decisions.  Each result keeps its own plan id, issuance time,
    weights, and sealed digest; neither design's weights are pasted onto the
    other's sample.
    """
    data = _load(evidence)
    plans = data.get("plans") if isinstance(data, dict) else None
    if not plans:
        return {"results": [estimate(data, require_seal=require_seal)]}
    return {
        "results": [
            estimate(data, plan.get("plan_id"), require_seal=require_seal) for plan in plans
        ]
    }
