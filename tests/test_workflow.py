from __future__ import annotations

import asyncio
from typing import Any

from student_agent.workflow import EvidenceCollector, create_case_context


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
