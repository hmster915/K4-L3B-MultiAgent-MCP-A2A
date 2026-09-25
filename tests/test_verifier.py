from __future__ import annotations

import copy

from student_agent.verifier import verify_and_repair

REAL_REF = "ev_" + "a" * 24
OTHER_REF = "ev_" + "b" * 24
FAKE_REF = "ev_" + "z" * 24


def _evidence() -> list[dict]:
    return [
        {
            "tool_name": "get_order",
            "response": {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": REAL_REF,
                "result_hash": "sha256:" + "0" * 64,
                "domain": "order",
                "data": {"order_id": "order-1"},
            },
        },
    ]


def _base_output() -> dict:
    return {
        "schema_version": "day09-l3b-output-v2",
        "case_id": "L3B_CASE_001",
        "assessment": {
            "primary_issue": "late_delivery_logistics",
            "secondary_issues": [],
            "case_status": "action_required",
            "confidence": 0.8,
        },
        "affected_entities": {
            "order_ids": ["order-1"], "item_ids": [], "seller_ids": [],
            "payment_references": [], "shipment_ids": [],
        },
        "entity_resolution": {
            "status": "resolved", "resolved_order_ids": ["order-1"],
            "rejected_candidates": [], "confidence": 0.9,
        },
        "customer_context": {"customer_unique_id": "c-1", "related_order_ids": []},
        "shipment_analysis": {
            "verdict": "logistics_delay", "late_seller_ids": [], "timeline_complete": True,
        },
        "payment_analysis": {
            "verdict": "reconciled", "captured_total_brl": 100.0,
            "refunded_total_brl": 0.0, "refundable_total_brl": 100.0,
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": "CARRIER_TRANSIT_DELAY", "rank": 1}],
            "responsible_parties": [{"party_type": "logistics_provider", "party_id": None}],
        },
        "evidence_refs": [REAL_REF],
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL", "recommended_refund_brl": 0.0, "refund_lines": [],
        },
        "resolution_actions": ["ESCALATE_TO_CARRIER"],
    }


def _case() -> dict:
    return {"case_id": "L3B_CASE_001", "candidate_order_ids": ["order-1"]}


def test_valid_evidence_ref_is_kept_and_no_repair_counted() -> None:
    output = _base_output()
    fixed, repairs = verify_and_repair(copy.deepcopy(output), _evidence(), _case())
    assert fixed["evidence_refs"] == [REAL_REF]
    assert repairs == 0


def test_unknown_top_level_evidence_ref_is_stripped() -> None:
    output = _base_output()
    output["evidence_refs"] = [REAL_REF, FAKE_REF]
    fixed, repairs = verify_and_repair(output, _evidence(), _case())
    assert fixed["evidence_refs"] == [REAL_REF]
    assert repairs >= 1


def test_claim_with_only_fake_refs_is_downgraded_to_insufficient_evidence() -> None:
    output = _base_output()
    output["claim_assessments"] = [
        {"claim_id": "c1", "verdict": "supported", "confidence": 0.9,
         "evidence_refs": [FAKE_REF]},
    ]
    fixed, repairs = verify_and_repair(output, _evidence(), _case())
    ca = fixed["claim_assessments"][0]
    assert ca["evidence_refs"] == []
    assert ca["verdict"] == "insufficient_evidence"
    assert repairs >= 1


def test_claim_keeps_verdict_when_a_real_ref_survives() -> None:
    output = _base_output()
    output["claim_assessments"] = [
        {"claim_id": "c1", "verdict": "supported", "confidence": 0.9,
         "evidence_refs": [REAL_REF, FAKE_REF]},
    ]
    fixed, repairs = verify_and_repair(output, _evidence(), _case())
    ca = fixed["claim_assessments"][0]
    assert ca["evidence_refs"] == [REAL_REF]
    assert ca["verdict"] == "supported"


def test_claim_confidence_clamped_when_downgraded_to_insufficient_evidence() -> None:
    output = _base_output()
    # Top-level evidence_refs keeps a real ref, so the top-level empty-refs
    # clamp does NOT fire — this claim's own confidence must be clamped
    # independently.
    output["claim_assessments"] = [
        {"claim_id": "c1", "verdict": "supported", "confidence": 0.9,
         "evidence_refs": [FAKE_REF]},
    ]
    fixed, repairs = verify_and_repair(output, _evidence(), _case())
    ca = fixed["claim_assessments"][0]
    assert ca["verdict"] == "insufficient_evidence"
    assert ca["confidence"] <= 0.3
    assert repairs >= 1


def test_claim_confidence_left_alone_when_verdict_not_downgraded() -> None:
    output = _base_output()
    output["claim_assessments"] = [
        {"claim_id": "c1", "verdict": "supported", "confidence": 0.9,
         "evidence_refs": [REAL_REF]},
    ]
    fixed, _ = verify_and_repair(output, _evidence(), _case())
    assert fixed["claim_assessments"][0]["confidence"] == 0.9


def test_action_required_downgraded_when_all_evidence_refs_are_fake() -> None:
    output = _base_output()
    output["evidence_refs"] = [FAKE_REF]
    output["assessment"]["case_status"] = "action_required"
    fixed, repairs = verify_and_repair(output, _evidence(), _case())
    assert fixed["evidence_refs"] == []
    assert fixed["assessment"]["case_status"] == "needs_investigation"
    assert repairs >= 1


def test_action_required_unaffected_when_a_real_ref_remains() -> None:
    output = _base_output()
    output["evidence_refs"] = [REAL_REF]
    output["assessment"]["case_status"] = "action_required"
    fixed, _ = verify_and_repair(output, _evidence(), _case())
    assert fixed["assessment"]["case_status"] == "action_required"


def test_refundable_total_recomputed_when_wrong() -> None:
    output = _base_output()
    output["payment_analysis"]["captured_total_brl"] = 100.0
    output["payment_analysis"]["refunded_total_brl"] = 40.0
    output["payment_analysis"]["refundable_total_brl"] = 999.0
    fixed, repairs = verify_and_repair(output, _evidence(), _case())
    assert fixed["payment_analysis"]["refundable_total_brl"] == 60.0
    assert repairs >= 1


def test_refundable_total_clamped_to_zero_when_refunded_exceeds_captured() -> None:
    output = _base_output()
    output["payment_analysis"]["captured_total_brl"] = 100.0
    output["payment_analysis"]["refunded_total_brl"] = 150.0
    output["payment_analysis"]["refundable_total_brl"] = -50.0
    fixed, _ = verify_and_repair(output, _evidence(), _case())
    assert fixed["payment_analysis"]["refundable_total_brl"] == 0.0


def test_refundable_total_left_null_when_totals_are_null() -> None:
    output = _base_output()
    output["payment_analysis"]["verdict"] = "insufficient_evidence"
    output["payment_analysis"]["captured_total_brl"] = None
    output["payment_analysis"]["refunded_total_brl"] = None
    output["payment_analysis"]["refundable_total_brl"] = None
    fixed, repairs = verify_and_repair(output, _evidence(), _case())
    assert fixed["payment_analysis"]["refundable_total_brl"] is None
    assert repairs == 0


def test_on_time_shipment_clears_late_seller_ids() -> None:
    output = _base_output()
    output["shipment_analysis"]["verdict"] = "on_time"
    output["shipment_analysis"]["late_seller_ids"] = ["seller-1"]
    fixed, repairs = verify_and_repair(output, _evidence(), _case())
    assert fixed["shipment_analysis"]["late_seller_ids"] == []
    assert repairs >= 1


def test_seller_delay_keeps_late_seller_ids() -> None:
    output = _base_output()
    output["shipment_analysis"]["verdict"] = "seller_delay"
    output["shipment_analysis"]["late_seller_ids"] = ["seller-1"]
    fixed, _ = verify_and_repair(output, _evidence(), _case())
    assert fixed["shipment_analysis"]["late_seller_ids"] == ["seller-1"]


def test_no_action_clears_resolution_actions_and_recommended_refund() -> None:
    output = _base_output()
    output["assessment"]["case_status"] = "no_action"
    output["resolution_actions"] = ["ESCALATE_TO_CARRIER"]
    output["financial_resolution"]["recommended_refund_brl"] = 50.0
    fixed, repairs = verify_and_repair(output, _evidence(), _case())
    assert fixed["resolution_actions"] == []
    assert fixed["financial_resolution"]["recommended_refund_brl"] == 0.0
    assert repairs >= 1


def test_no_action_clears_populated_refund_lines() -> None:
    output = _base_output()
    output["assessment"]["case_status"] = "no_action"
    output["financial_resolution"]["recommended_refund_brl"] = 50.0
    output["financial_resolution"]["refund_lines"] = [
        {"reason_code": "DUPLICATE_CHARGE", "amount_brl": 50.0, "entity_id": "order-1"},
    ]
    fixed, repairs = verify_and_repair(output, _evidence(), _case())
    assert fixed["financial_resolution"]["refund_lines"] == []
    assert repairs >= 1


def test_action_required_keeps_resolution_actions() -> None:
    output = _base_output()
    output["assessment"]["case_status"] = "action_required"
    output["resolution_actions"] = ["ESCALATE_TO_CARRIER"]
    fixed, _ = verify_and_repair(output, _evidence(), _case())
    assert fixed["resolution_actions"] == ["ESCALATE_TO_CARRIER"]


def test_missing_candidate_is_added_to_rejected() -> None:
    output = _base_output()
    case = _case()
    case["candidate_order_ids"] = ["order-1", "candidate-fake"]
    fixed, repairs = verify_and_repair(output, _evidence(), case)
    assert "candidate-fake" in fixed["entity_resolution"]["rejected_candidates"]
    assert repairs >= 1


def test_candidate_already_covered_is_left_alone() -> None:
    output = _base_output()
    case = _case()  # candidate_order_ids == ["order-1"], already in resolved_order_ids
    fixed, repairs = verify_and_repair(output, _evidence(), case)
    assert fixed["entity_resolution"]["rejected_candidates"] == []


def test_rejected_candidates_never_exceeds_schema_max_items() -> None:
    # idSet (rejected_candidates) has maxItems: 20 in l3a-output-v2.schema.json.
    # 25 uncovered candidates must not push the repaired list past that cap,
    # or the follow-up contracts.validate_output() call would raise and crash
    # the whole batch run instead of just this case.
    output = _base_output()
    case = _case()
    case["candidate_order_ids"] = ["order-1", *[f"candidate-{i}" for i in range(25)]]
    fixed, repairs = verify_and_repair(output, _evidence(), case)
    assert len(fixed["entity_resolution"]["rejected_candidates"]) <= 20
    assert repairs >= 1


def test_confidence_clamped_when_entity_not_found() -> None:
    output = _base_output()
    output["entity_resolution"]["status"] = "not_found"
    output["assessment"]["confidence"] = 0.95
    fixed, repairs = verify_and_repair(output, _evidence(), _case())
    assert fixed["assessment"]["confidence"] <= 0.2
    assert repairs >= 1


def test_confidence_clamped_when_needs_investigation() -> None:
    output = _base_output()
    output["assessment"]["case_status"] = "needs_investigation"
    output["assessment"]["confidence"] = 0.9
    fixed, repairs = verify_and_repair(output, _evidence(), _case())
    assert fixed["assessment"]["confidence"] <= 0.6
    assert repairs >= 1


def test_confidence_clamped_when_evidence_refs_empty() -> None:
    output = _base_output()
    output["evidence_refs"] = []
    # needs_investigation avoids also tripping the unrelated action_required rule
    output["assessment"]["case_status"] = "needs_investigation"
    output["assessment"]["confidence"] = 0.5
    fixed, repairs = verify_and_repair(output, _evidence(), _case())
    assert fixed["assessment"]["confidence"] <= 0.3
    assert repairs >= 1


def test_confidence_left_alone_when_already_within_bounds() -> None:
    output = _base_output()
    output["assessment"]["confidence"] = 0.8
    fixed, repairs = verify_and_repair(output, _evidence(), _case())
    assert fixed["assessment"]["confidence"] == 0.8
    assert repairs == 0
