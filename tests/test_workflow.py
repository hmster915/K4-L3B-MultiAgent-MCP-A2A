from __future__ import annotations

from pathlib import Path

from student_agent.llm_validator import _structured_output_schema
from student_agent.workflow import (
    _align_supported_primary_claim,
    _enforce_output_invariants,
    _looks_not_found,
    _select_order_id,
    _target_financials,
)


def _order_evidence(order_id: str, data: object) -> dict[str, object]:
    return {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": f"ev_{order_id:0<20}",
        "result_hash": "sha256:" + "0" * 64,
        "domain": "order",
        "data": data,
    }


def test_select_order_rejects_explicit_not_found_candidate() -> None:
    candidates = ["wrong-order", "right-order"]
    evidence = {
        "wrong-order": _order_evidence("wrong-order", {"found": False}),
        "right-order": _order_evidence("right-order", {"order_id": "right-order"}),
    }
    assert _select_order_id(candidates, evidence) == "right-order"


def test_not_found_detection_is_conservative() -> None:
    assert _looks_not_found({"status": "not_found"})
    assert not _looks_not_found({"status": "canceled", "order_id": "order-1"})


def test_structured_schema_has_local_references_only() -> None:
    root = Path(__file__).resolve().parents[1]
    schema = _structured_output_schema(root / "contracts" / "schemas")
    assert "claim_assessments" in schema["required"]
    assert "$defs" in schema
    assert "l3a-output-v2.schema.json" not in str(schema)


def test_supported_first_claim_remains_primary() -> None:
    case = {
        "customer_request": {
            "claims": [{"claim_id": "claim-a", "topic": "late_delivery_seller"}]
        }
    }
    output = {
        "assessment": {
            "primary_issue": "canceled_order_paid",
            "secondary_issues": ["late_delivery_seller"],
            "confidence": 0.95,
        },
        "claim_assessments": [
            {"claim_id": "claim-a", "verdict": "supported", "confidence": 0.9}
        ],
        "root_cause_analysis": {
            "ranked_causes": [
                {"cause_code": "CANCELED_ORDER_PAID", "rank": 1},
                {"cause_code": "LATE_DELIVERY_SELLER", "rank": 2},
            ]
        },
    }
    _align_supported_primary_claim(output, case)
    assert output["assessment"]["primary_issue"] == "late_delivery_seller"
    assert output["assessment"]["secondary_issues"] == ["canceled_order_paid"]
    assert output["root_cause_analysis"]["ranked_causes"][0] == {
        "cause_code": "LATE_DELIVERY_SELLER",
        "rank": 1,
    }


def test_refund_pending_does_not_recommend_duplicate_refund() -> None:
    case = {
        "customer_request": {
            "claims": [
                {"claim_id": "claim-a", "topic": "refund_pending"},
                {"claim_id": "claim-b", "topic": "requested_full_refund"},
            ]
        }
    }
    output = {
        "assessment": {"primary_issue": "refund_pending", "case_status": "action_required"},
        "affected_entities": {"seller_ids": []},
        "claim_assessments": [
            {"claim_id": "claim-a", "evidence_refs": []},
            {"claim_id": "claim-b", "evidence_refs": []},
        ],
        "shipment_analysis": {
            "verdict": "on_time",
            "late_seller_ids": [],
            "timeline_complete": True,
        },
        "payment_analysis": {"verdict": "reconciled", "refundable_total_brl": 89},
        "root_cause_analysis": {
            "responsible_parties": [{"party_type": "payment_provider", "party_id": None}]
        },
        "financial_resolution": {
            "recommended_refund_brl": 89,
            "refund_lines": [{"reason_code": "refund_pending", "amount_brl": 89}],
        },
        "resolution_actions": ["issue_refund"],
    }
    refs = {
        "get_payment_timeline": ["ev_payment"],
        "get_refund_timeline": ["ev_refund"],
        "get_policy": ["ev_policy"],
    }
    _enforce_output_invariants(output, case, refs)
    assert output["payment_analysis"]["verdict"] == "refund_pending"
    assert output["financial_resolution"]["recommended_refund_brl"] == 0
    assert output["financial_resolution"]["refund_lines"] == []
    assert output["assessment"]["case_status"] == "needs_investigation"
    assert output["resolution_actions"] == ["monitor_refund"]
    assert output["claim_assessments"][1]["evidence_refs"] == [
        "ev_payment",
        "ev_refund",
        "ev_policy",
    ]


def test_target_financials_selects_split_payment_cluster() -> None:
    evidence = [
        {
            "tool_name": "get_order",
            "response": {"data": {"order_approved_at": "2018-04-10T10:00:00-03:00"}},
        },
        {
            "tool_name": "get_payment_timeline",
            "response": {
                "data": {
                    "events": [
                        {
                            "event_at": "2018-04-10T10:00:00-03:00",
                            "event_type": "captured",
                            "amount_brl": "52.00",
                            "status": "confirmed",
                        },
                        {
                            "event_at": "2018-01-21T10:00:00-03:00",
                            "event_type": "captured",
                            "amount_brl": "44.50",
                            "status": "confirmed",
                        },
                        {
                            "event_at": "2018-01-21T11:00:00-03:00",
                            "event_type": "captured",
                            "amount_brl": "44.50",
                            "status": "confirmed",
                        },
                    ]
                }
            },
        },
    ]
    assert _target_financials("valid_split_payment", evidence) == (89.0, 0.0, 0.0)


def test_target_financials_selects_canceled_order_cluster() -> None:
    evidence = [
        {
            "tool_name": "get_customer_history",
            "response": {
                "data": {
                    "orders": [
                        {
                            "order_status": "delivered",
                            "order_approved_at": "2018-07-04T10:00:00-03:00",
                        },
                        {
                            "order_status": "canceled",
                            "order_approved_at": "2018-07-27T10:00:00-03:00",
                        },
                    ]
                }
            },
        },
        {
            "tool_name": "get_payment_timeline",
            "response": {
                "data": {
                    "events": [
                        {
                            "event_at": "2018-07-04T10:00:00-03:00",
                            "event_type": "captured",
                            "amount_brl": "18.00",
                            "status": "confirmed",
                        },
                        {
                            "event_at": "2018-07-27T10:00:00-03:00",
                            "event_type": "captured",
                            "amount_brl": "79.00",
                            "status": "confirmed",
                        },
                    ]
                }
            },
        },
    ]
    assert _target_financials("canceled_order_paid", evidence) == (79.0, 0.0, 79.0)


def test_target_financials_deduplicates_identical_capture_events() -> None:
    event = {
        "event_at": "2018-02-28T10:00:00-03:00",
        "event_type": "captured",
        "amount_brl": "89.00",
        "status": "confirmed",
    }
    evidence = [
        {
            "tool_name": "get_order",
            "response": {"data": {"order_approved_at": "2018-02-28T10:00:00-03:00"}},
        },
        {
            "tool_name": "get_payment_timeline",
            "response": {"data": {"events": [event, dict(event)]}},
        },
    ]
    assert _target_financials("unavailable_order_paid", evidence) == (89.0, 0.0, 89.0)
