from __future__ import annotations

import copy
from typing import Any


def _real_evidence_refs(evidence: list[dict[str, Any]]) -> set[str]:
    refs: set[str] = set()
    for item in evidence:
        response = item.get("response")
        if response and "evidence_ref" in response:
            refs.add(response["evidence_ref"])
    return refs


def verify_and_repair(
    output: dict[str, Any],
    evidence: list[dict[str, Any]],
    case: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    """Repair an LLM-produced L3B output against locally observed evidence.

    Never calls MCP or an LLM. Strips evidence_refs the LLM invented, downgrades
    verdicts/case_status/confidence that no longer have real support, and
    re-derives a few deterministic fields (refundable total, late_seller_ids,
    resolution_actions). Returns a new dict plus the number of fields repaired.
    """
    out = copy.deepcopy(output)
    real_refs = _real_evidence_refs(evidence)
    repairs = 0

    before = list(out["evidence_refs"])
    out["evidence_refs"] = [r for r in before if r in real_refs]
    if out["evidence_refs"] != before:
        repairs += 1

    for ca in out.get("claim_assessments") or []:
        before_refs = list(ca["evidence_refs"])
        ca["evidence_refs"] = [r for r in before_refs if r in real_refs]
        if ca["evidence_refs"] != before_refs:
            repairs += 1
        if not ca["evidence_refs"] and ca["verdict"] != "insufficient_evidence":
            ca["verdict"] = "insufficient_evidence"
            repairs += 1
            if ca["confidence"] > 0.3:
                ca["confidence"] = 0.3
                repairs += 1

    if not out["evidence_refs"] and out["assessment"]["case_status"] == "action_required":
        out["assessment"]["case_status"] = "needs_investigation"
        repairs += 1

    pa = out["payment_analysis"]
    captured, refunded = pa["captured_total_brl"], pa["refunded_total_brl"]
    if captured is not None and refunded is not None:
        expected = max(0.0, round(captured - refunded, 2))
        if pa["refundable_total_brl"] != expected:
            pa["refundable_total_brl"] = expected
            repairs += 1

    sa = out["shipment_analysis"]
    if sa["verdict"] == "on_time" and sa["late_seller_ids"]:
        sa["late_seller_ids"] = []
        repairs += 1

    if out["assessment"]["case_status"] == "no_action":
        if out["resolution_actions"]:
            out["resolution_actions"] = []
            repairs += 1
        if out["financial_resolution"]["recommended_refund_brl"] != 0.0:
            out["financial_resolution"]["recommended_refund_brl"] = 0.0
            repairs += 1
        if out["financial_resolution"]["refund_lines"]:
            out["financial_resolution"]["refund_lines"] = []
            repairs += 1

    er = out["entity_resolution"]
    resolved = set(er["resolved_order_ids"])
    rejected = set(er["rejected_candidates"])
    for candidate in case.get("candidate_order_ids", []):
        if candidate in resolved or candidate in rejected:
            continue
        if len(er["rejected_candidates"]) >= 20:  # idSet schema cap
            break
        er["rejected_candidates"].append(candidate)
        rejected.add(candidate)
        repairs += 1

    assessment = out["assessment"]
    cap: float | None = None
    if er["status"] == "not_found":
        cap = 0.2
    elif assessment["case_status"] == "needs_investigation":
        cap = 0.6
    if not out["evidence_refs"] and (cap is None or cap > 0.3):
        cap = 0.3
    if cap is not None and assessment["confidence"] > cap:
        assessment["confidence"] = cap
        repairs += 1

    return out, repairs
