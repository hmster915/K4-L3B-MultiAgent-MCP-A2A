from __future__ import annotations

import asyncio
from typing import Any

from student_agent.workflow import (
    EvidenceCollector,
    create_case_context,
    run_order_item_agent,
    run_payment_agent,
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
