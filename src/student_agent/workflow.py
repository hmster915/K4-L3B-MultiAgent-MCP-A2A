from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


class EvidenceCollector:
    """Case-scoped MCP access, caching, and evidence trace recording."""

    def __init__(
        self,
        *,
        case_id: str,
        gateway: EvidenceGateway,
        trace: TraceWriter,
    ) -> None:
        self.case_id = case_id
        self.gateway = gateway
        self.trace = trace
        self._cache: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any]] = {}

    async def collect(
        self,
        *,
        actor: str,
        tool_name: str,
        **arguments: str,
    ) -> dict[str, Any]:
        cache_key = (tool_name, tuple(sorted(arguments.items())))
        if cache_key not in self._cache:
            self._cache[cache_key] = await self.gateway.call(
                tool_name,
                case_id=self.case_id,
                **arguments,
            )

        evidence = self._cache[cache_key]
        self.trace.emit(
            case_id=self.case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool_name,
            evidence_refs=[evidence["evidence_ref"]],
        )
        return evidence


@dataclass(frozen=True)
class CaseContext:
    case_id: str
    candidate_order_ids: tuple[str, ...]
    claims: tuple[dict[str, Any], ...]
    policy_version: str
    investigation_scope: dict[str, Any]
    customer_unique_id_hint: str | None
    collector: EvidenceCollector


def _contains_identifier(value: Any, key: str, expected: str) -> bool:
    if isinstance(value, dict):
        if value.get(key) == expected:
            return True
        return any(_contains_identifier(child, key, expected) for child in value.values())
    if isinstance(value, list):
        return any(_contains_identifier(child, key, expected) for child in value)
    return False


def _collect_identifiers(value: Any, keys: set[str]) -> list[str]:
    found: list[str] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                if key in keys:
                    if isinstance(child, str) and child not in found:
                        found.append(child)
                    elif isinstance(child, list):
                        for item in child:
                            if isinstance(item, str) and item not in found:
                                found.append(item)
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return found


def create_case_context(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> CaseContext:
    case_id = case["case_id"]
    customer_request = case.get("customer_request", {})
    collector = EvidenceCollector(case_id=case_id, gateway=gateway, trace=trace)
    context = CaseContext(
        case_id=case_id,
        candidate_order_ids=tuple(case.get("candidate_order_ids", [])),
        claims=tuple(customer_request.get("claims", [])),
        policy_version=case["policy_version"],
        investigation_scope=case.get("investigation_scope", {}),
        customer_unique_id_hint=case.get("customer_unique_id_hint"),
        collector=collector,
    )
    for target in ("order-item-agent", "payment-agent", "shipment-agent"):
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=target,
        )
    return context


async def run_order_item_agent(context: CaseContext) -> dict[str, Any]:
    evidence_refs: list[str] = []
    supported_order_ids: list[str] = []

    for order_id in context.candidate_order_ids:
        evidence = await context.collector.collect(
            actor="order-item-agent",
            tool_name="get_order",
            order_id=order_id,
        )
        evidence_refs.append(evidence["evidence_ref"])
        if _contains_identifier(evidence.get("data"), "order_id", order_id):
            supported_order_ids.append(order_id)

    rejected_candidates = [
        order_id
        for order_id in context.candidate_order_ids
        if order_id not in supported_order_ids
    ]

    item_ids: list[str] = []
    seller_ids: list[str] = []
    if len(supported_order_ids) == 1:
        order_id = supported_order_ids[0]
        for tool_name in ("get_order_items", "get_product_context", "get_sellers"):
            evidence = await context.collector.collect(
                actor="order-item-agent",
                tool_name=tool_name,
                order_id=order_id,
            )
            evidence_refs.append(evidence["evidence_ref"])
            if tool_name == "get_order_items":
                item_ids.extend(
                    _collect_identifiers(evidence.get("data"), {"order_item_id", "item_id"})
                )
                seller_ids.extend(
                    _collect_identifiers(evidence.get("data"), {"seller_id"})
                )
            elif tool_name == "get_sellers":
                seller_ids.extend(
                    _collect_identifiers(evidence.get("data"), {"seller_id"})
                )

    if len(supported_order_ids) == 1:
        status = "resolved"
        resolved_order_ids = supported_order_ids
        confidence = 1.0
    elif supported_order_ids:
        status = "ambiguous"
        resolved_order_ids = []
        confidence = 0.5
    else:
        status = "not_found"
        resolved_order_ids = []
        confidence = 0.0

    result = {
        "entity_resolution": {
            "status": status,
            "resolved_order_ids": resolved_order_ids,
            "rejected_candidates": rejected_candidates,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": resolved_order_ids,
            "item_ids": item_ids,
            "seller_ids": sorted(set(seller_ids)),
            "payment_references": [],
            "shipment_ids": [],
        },
        "evidence_refs": list(dict.fromkeys(evidence_refs)),
    }
    context.collector.trace.emit(
        case_id=context.case_id,
        event_type="handoff",
        actor="order-item-agent",
        target="policy-agent",
        decision_code="order_item_investigation_completed",
        evidence_refs=result["evidence_refs"],
    )
    return result


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Implement the L3B coordinator and specialist-agent workflow here.

    Include entity resolution, conflict handling and evidence-efficient investigation.
    The starter kit intentionally does not generate invented fallback answers.
    """
    del case, gateway, trace
    raise NotImplementedError("Implement the L3B multi-agent workflow in solve_case()")
