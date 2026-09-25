from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from student_agent import workflow
from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter

REAL_REF = "ev_" + "a" * 24
FAKE_REF = "ev_" + "z" * 24


class _FakeGateway:
    available_tools = frozenset(workflow.REQUIRED_TOOLS)

    async def call(self, tool_name: str, *, case_id: str, **arguments: Any) -> dict[str, Any]:
        del case_id
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": REAL_REF,
            "result_hash": "sha256:" + "0" * 64,
            "domain": "order",
            "data": {"order_id": arguments.get("order_id", "order-1")},
        }


def _llm_output_with_fake_ref(case_id: str) -> dict[str, Any]:
    return {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
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
        "evidence_refs": [REAL_REF, FAKE_REF],
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL", "recommended_refund_brl": 0.0, "refund_lines": [],
        },
        "resolution_actions": ["ESCALATE_TO_CARRIER"],
    }


def test_solve_case_strips_evidence_ref_the_llm_invented(tmp_path, monkeypatch) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)

    monkeypatch.setattr(
        workflow, "Settings",
        SimpleNamespace(load=lambda root: SimpleNamespace(openai_model="gpt-4o-mini")),
    )

    async def _fake_validate(case, evidence, settings, contracts):
        del evidence, settings, contracts
        return _llm_output_with_fake_ref(case["case_id"])

    monkeypatch.setattr(workflow, "validate_with_gpt4o_mini", _fake_validate)

    case = {
        "case_id": "L3B_CASE_001",
        "candidate_order_ids": ["order-1"],
        "customer_request": {"claimed_order_id": "order-1"},
        "customer_unique_id_hint": "customer-1",
        "policy_version": "EC_POLICY_V2",
    }

    output = asyncio.run(workflow.solve_case(case, _FakeGateway(), trace))

    assert FAKE_REF not in output["evidence_refs"]
    assert output["evidence_refs"] == [REAL_REF]

    events = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    verification = [e for e in events if e["event_type"] == "verification_completed"][0]
    assert verification["attributes"]["local_repairs_applied"] >= 1
