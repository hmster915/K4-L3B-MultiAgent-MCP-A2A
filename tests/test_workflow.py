from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import (
    EvidenceCollector,
    create_case_context,
    run_order_item_agent,
    run_payment_agent,
    run_shipment_agent,
    run_specialists,
    solve_case,
)


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, case_id, arguments))
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": "ev_abcdefghijklmnopqrst",
            "result_hash": "sha256:" + "a" * 64,
            "domain": "order",
            "data": {"order_id": arguments["order_id"]},
        }


class FakeTrace:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, **event: Any) -> dict[str, Any]:
        self.events.append(event)
        return event


class OrderGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, case_id, arguments))
        order_id = arguments["order_id"]
        if tool_name == "get_order":
            data = {"order_id": order_id} if order_id == "order-123" else {}
            domain = "order"
        elif tool_name == "get_order_items":
            data = {"items": [{"order_item_id": "item-1", "seller_id": "seller-1"}]}
            domain = "item"
        elif tool_name == "get_product_context":
            data = {"products": [{"product_id": "product-1"}]}
            domain = "product"
        else:
            data = {"sellers": [{"seller_id": "seller-1"}]}
            domain = "seller"
        suffix = len(self.calls)
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{suffix:020d}",
            "result_hash": "sha256:" + "a" * 64,
            "domain": domain,
            "data": data,
        }


class PaymentGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, case_id, arguments))
        data_by_tool = {
            "get_order_payments": {"payments": [{"payment_value": 100.0}]},
            "get_payment_timeline": {"events": []},
            "get_refund_timeline": {
                "refunded_total_brl": 20.0,
                "refundable_total_brl": 80.0,
            },
        }
        suffix = len(self.calls)
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{suffix:020d}",
            "result_hash": "sha256:" + "b" * 64,
            "domain": "payment" if tool_name != "get_refund_timeline" else "refund",
            "data": data_by_tool[tool_name],
        }


class ShipmentGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, case_id, arguments))
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": "ev_abcdefghijklmnopqrst",
            "result_hash": "sha256:" + "c" * 64,
            "domain": "shipment",
            "data": {
                "seller_id": "seller-1",
                "shipping_limit_date": "2018-01-02T09:00:00-03:00",
                "order_delivered_carrier_date": "2018-01-04T09:00:00-03:00",
                "order_estimated_delivery_date": "2018-01-05T09:00:00-03:00",
                "order_delivered_customer_date": "2018-01-10T09:00:00-03:00",
            },
        }


class FailingShipmentGateway(ShipmentGateway):
    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        if arguments["order_id"] == "candidate-001":
            raise RuntimeError("unknown candidate")
        return await super().call(tool_name, case_id=case_id, **arguments)


class ParallelGateway(FakeGateway):
    def __init__(self) -> None:
        super().__init__()
        self.active_calls = 0
        self.max_active_calls = 0

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.active_calls += 1
        self.max_active_calls = max(self.max_active_calls, self.active_calls)
        try:
            await asyncio.sleep(0)
            return await super().call(tool_name, case_id=case_id, **arguments)
        finally:
            self.active_calls -= 1


class FullGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, case_id, arguments))
        data_by_tool = {
            "get_order": {"order_id": "order-123"},
            "get_order_items": {
                "items": [{"order_item_id": "item-1", "seller_id": "seller-1"}]
            },
            "get_product_context": {"products": [{"product_id": "product-1"}]},
            "get_sellers": {"sellers": [{"seller_id": "seller-1"}]},
            "get_order_payments": {"payments": [{"payment_value": 100.0}]},
            "get_payment_timeline": {"events": []},
            "get_refund_timeline": {
                "refunded_total_brl": 0.0,
                "refundable_total_brl": 0.0,
            },
            "get_shipment_summary": {
                "shipping_limit_date": "2018-01-02T09:00:00-03:00",
                "order_delivered_carrier_date": "2018-01-02T08:00:00-03:00",
                "order_estimated_delivery_date": "2018-01-05T09:00:00-03:00",
                "order_delivered_customer_date": "2018-01-04T09:00:00-03:00",
            },
            "get_policy": {"policy_version": "EC_POLICY_V2"},
            "get_customer_history": {
                "customer_unique_id": "customer-123",
                "orders": [{"order_id": "order-123"}],
            },
        }
        domains = {
            "get_order": "order",
            "get_order_items": "item",
            "get_product_context": "product",
            "get_sellers": "seller",
            "get_order_payments": "payment",
            "get_payment_timeline": "payment",
            "get_refund_timeline": "refund",
            "get_shipment_summary": "shipment",
            "get_policy": "policy",
            "get_customer_history": "customer",
        }
        suffix = len(self.calls)
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{suffix:020d}",
            "result_hash": "sha256:" + "d" * 64,
            "domain": domains[tool_name],
            "data": data_by_tool[tool_name],
        }


class CanceledPaidGateway(FullGateway):
    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        evidence = await super().call(tool_name, case_id=case_id, **arguments)
        if tool_name == "get_order":
            evidence["data"] = {"order_id": arguments["order_id"], "order_status": "canceled"}
        elif tool_name == "get_order_payments":
            evidence["data"] = {"payments": [{"payment_value": 45.0}]}
        elif tool_name == "get_refund_timeline":
            evidence["data"] = {"refunded_total_brl": 0.0, "refundable_total_brl": 45.0}
        return evidence


def test_evidence_collector_caches_case_scoped_request_and_traces_each_consumer() -> None:
    async def scenario() -> tuple[FakeGateway, FakeTrace, dict[str, Any], dict[str, Any]]:
        gateway = FakeGateway()
        trace = FakeTrace()
        collector = EvidenceCollector(case_id="CASE_001", gateway=gateway, trace=trace)

        first = await collector.collect(
            actor="order-item-agent",
            tool_name="get_order",
            order_id="order-123",
        )
        second = await collector.collect(
            actor="payment-agent",
            tool_name="get_order",
            order_id="order-123",
        )
        return gateway, trace, first, second

    gateway, trace, first, second = asyncio.run(scenario())

    assert len(gateway.calls) == 1
    assert gateway.calls == [("get_order", "CASE_001", {"order_id": "order-123"})]
    assert first == second
    assert [event["event_type"] for event in trace.events] == [
        "tool_result_consumed",
        "tool_result_consumed",
    ]
    assert [event["actor"] for event in trace.events] == ["order-item-agent", "payment-agent"]


def test_coordinator_creates_context_and_assigns_three_specialists() -> None:
    gateway = FakeGateway()
    trace = FakeTrace()
    case = {
        "case_id": "CASE_001",
        "candidate_order_ids": ["order-123"],
        "customer_request": {"claims": []},
        "policy_version": "EC_POLICY_V2",
        "investigation_scope": {},
        "customer_unique_id_hint": "customer-123",
    }

    context = create_case_context(case, gateway, trace)

    assert context.case_id == "CASE_001"
    assert context.candidate_order_ids == ("order-123",)
    assert context.policy_version == "EC_POLICY_V2"
    assert context.customer_unique_id_hint == "customer-123"
    assert [event["event_type"] for event in trace.events] == [
        "task_assigned",
        "task_assigned",
        "task_assigned",
    ]
    assert [event["target"] for event in trace.events] == [
        "order-item-agent",
        "payment-agent",
        "shipment-agent",
    ]


def test_order_item_agent_resolves_candidate_and_collects_related_entities() -> None:
    gateway = OrderGateway()
    trace = FakeTrace()
    case = {
        "case_id": "CASE_001",
        "candidate_order_ids": ["order-123", "candidate-001"],
        "customer_request": {"claims": []},
        "policy_version": "EC_POLICY_V2",
        "investigation_scope": {},
    }
    context = create_case_context(case, gateway, trace)

    result = asyncio.run(run_order_item_agent(context))

    assert result["entity_resolution"] == {
        "status": "resolved",
        "resolved_order_ids": ["order-123"],
        "rejected_candidates": ["candidate-001"],
        "confidence": 1.0,
    }
    assert result["affected_entities"] == {
        "order_ids": ["order-123"],
        "item_ids": ["item-1"],
        "seller_ids": ["seller-1"],
        "payment_references": [],
        "shipment_ids": [],
    }
    assert [call[0] for call in gateway.calls] == [
        "get_order",
        "get_order",
        "get_order_items",
        "get_product_context",
        "get_sellers",
    ]
    assert any(
        event["event_type"] == "handoff" and event["target"] == "policy-agent"
        for event in trace.events
    )


def test_payment_agent_reconciles_capture_and_pending_refund() -> None:
    gateway = PaymentGateway()
    trace = FakeTrace()
    case = {
        "case_id": "CASE_001",
        "candidate_order_ids": ["order-123"],
        "customer_request": {"claims": []},
        "policy_version": "EC_POLICY_V2",
        "investigation_scope": {},
    }
    context = create_case_context(case, gateway, trace)

    result = asyncio.run(run_payment_agent(context, ["order-123"]))

    assert result["payment_analysis"] == {
        "verdict": "refund_pending",
        "captured_total_brl": 100.0,
        "refunded_total_brl": 20.0,
        "refundable_total_brl": 80.0,
    }
    assert [call[0] for call in gateway.calls] == [
        "get_order_payments",
        "get_payment_timeline",
        "get_refund_timeline",
    ]


def test_shipment_agent_identifies_seller_delay_from_timeline() -> None:
    gateway = ShipmentGateway()
    trace = FakeTrace()
    case = {
        "case_id": "CASE_001",
        "candidate_order_ids": ["order-123"],
        "customer_request": {"claims": []},
        "policy_version": "EC_POLICY_V2",
        "investigation_scope": {},
    }
    context = create_case_context(case, gateway, trace)

    result = asyncio.run(run_shipment_agent(context, ["order-123"]))

    assert result["shipment_analysis"] == {
        "verdict": "seller_delay",
        "late_seller_ids": ["seller-1"],
        "timeline_complete": True,
    }
    assert gateway.calls == [
        ("get_shipment_summary", "CASE_001", {"order_id": "order-123"})
    ]


def test_shipment_agent_preserves_valid_evidence_when_candidate_call_fails() -> None:
    gateway = FailingShipmentGateway()
    trace = FakeTrace()
    case = {
        "case_id": "CASE_001",
        "candidate_order_ids": ["candidate-001", "order-123"],
        "customer_request": {"claims": []},
        "policy_version": "EC_POLICY_V2",
        "investigation_scope": {},
    }
    context = create_case_context(case, gateway, trace)

    result = asyncio.run(run_shipment_agent(context, ["candidate-001", "order-123"]))

    assert result["shipment_analysis"]["verdict"] == "seller_delay"
    assert result["shipment_analysis"]["timeline_complete"] is False


def test_specialists_run_concurrently_and_return_three_results() -> None:
    gateway = ParallelGateway()
    trace = FakeTrace()
    case = {
        "case_id": "CASE_001",
        "candidate_order_ids": ["order-123"],
        "customer_request": {"claims": []},
        "policy_version": "EC_POLICY_V2",
        "investigation_scope": {},
    }
    context = create_case_context(case, gateway, trace)

    results = asyncio.run(run_specialists(context))

    assert len(results) == 3
    assert {"entity_resolution", "payment_analysis", "shipment_analysis"} == {
        next(iter(result)) for result in results
    }
    assert gateway.max_active_calls >= 2


def test_solve_case_returns_schema_valid_verified_output(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    gateway = FullGateway()
    case = {
        "case_id": "CASE_001",
        "candidate_order_ids": ["order-123"],
        "customer_request": {"claims": []},
        "policy_version": "EC_POLICY_V2",
        "investigation_scope": {"include_customer_history": True},
        "customer_unique_id_hint": "customer-123",
    }

    output = asyncio.run(solve_case(case, gateway, trace))

    contracts.validate_output(output, "test output")
    assert output["case_id"] == "CASE_001"
    assert set(output) <= {
        "schema_version",
        "case_id",
        "assessment",
        "affected_entities",
        "claim_assessments",
        "entity_resolution",
        "customer_context",
        "shipment_analysis",
        "payment_analysis",
        "root_cause_analysis",
        "evidence_refs",
        "data_conflicts",
        "financial_resolution",
        "resolution_actions",
    }
    assert any(
        json.loads(event)["event_type"] == "verification_completed"
        for event in trace.path.read_text(encoding="utf-8").splitlines()
    )


def test_policy_and_verifier_handle_canceled_paid_order_consistently(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    case = {
        "case_id": "CASE_002",
        "candidate_order_ids": ["order-123"],
        "customer_request": {"claims": []},
        "policy_version": "EC_POLICY_V2",
        "investigation_scope": {},
    }

    output = asyncio.run(solve_case(case, CanceledPaidGateway(), trace))

    assert output["assessment"]["primary_issue"] == "canceled_order_paid"
    assert output["root_cause_analysis"]["responsible_parties"] == [
        {"party_type": "platform", "party_id": None}
    ]
    assert output["financial_resolution"] == {
        "currency": "BRL",
        "recommended_refund_brl": 45.0,
        "refund_lines": [
            {"reason_code": "CANCELED_ORDER_PAID", "amount_brl": 45.0, "entity_id": "order-123"}
        ],
    }
    assert output["assessment"]["confidence"] < 1.0
    contracts.validate_output(output, "canceled paid output")
