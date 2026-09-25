from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
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


def _record_tool_unavailable(context: CaseContext, actor: str, tool_name: str) -> None:
    context.collector.trace.emit(
        case_id=context.case_id,
        event_type="handoff",
        actor=actor,
        target="policy-agent",
        decision_code=f"{tool_name}_unavailable",
    )


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


def _find_numbers(value: Any, keys: set[str]) -> list[float]:
    found: list[float] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                if key in keys and isinstance(child, (int, float)) and not isinstance(child, bool):
                    found.append(float(child))
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return found


def _sum_numbers(value: Any, keys: set[str]) -> float | None:
    numbers = _find_numbers(value, keys)
    return sum(numbers) if numbers else None


def _find_strings(value: Any, keys: set[str]) -> list[str]:
    found: list[str] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                if key in keys and isinstance(child, str):
                    found.append(child)
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return found


def _first_datetime(value: Any, keys: set[str]) -> datetime | None:
    for text in _find_strings(value, keys):
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            continue
    return None


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
        try:
            evidence = await context.collector.collect(
                actor="order-item-agent",
                tool_name="get_order",
                order_id=order_id,
            )
        except RuntimeError:
            _record_tool_unavailable(context, "order-item-agent", "get_order")
            continue
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
            try:
                evidence = await context.collector.collect(
                    actor="order-item-agent",
                    tool_name=tool_name,
                    order_id=order_id,
                )
            except RuntimeError:
                _record_tool_unavailable(context, "order-item-agent", tool_name)
                continue
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


async def run_payment_agent(
    context: CaseContext, order_ids: list[str] | tuple[str, ...]
) -> dict[str, Any]:
    evidence_refs: list[str] = []
    payment_references: list[str] = []
    captured_total = 0.0
    refunded_total = 0.0
    refundable_total = 0.0
    captured_found = False
    refunded_found = False
    refundable_found = False

    for order_id in order_ids:
        try:
            payment_evidence = await context.collector.collect(
                actor="payment-agent",
                tool_name="get_order_payments",
                order_id=order_id,
            )
            timeline_evidence = await context.collector.collect(
                actor="payment-agent",
                tool_name="get_payment_timeline",
                order_id=order_id,
            )
            refund_evidence = await context.collector.collect(
                actor="payment-agent",
                tool_name="get_refund_timeline",
                order_id=order_id,
            )
        except RuntimeError:
            _record_tool_unavailable(context, "payment-agent", "payment_evidence")
            continue
        evidence_refs.extend(
            [
                payment_evidence["evidence_ref"],
                timeline_evidence["evidence_ref"],
                refund_evidence["evidence_ref"],
            ]
        )

        captured = _sum_numbers(
            payment_evidence.get("data"),
            {"payment_value", "captured_total_brl", "captured_amount_brl"},
        )
        if captured is None:
            captured = _sum_numbers(
                timeline_evidence.get("data"), {"captured_total_brl", "captured_amount_brl"}
            )
        if captured is not None:
            captured_total += captured
            captured_found = True
        payment_references.extend(
            _collect_identifiers(
                payment_evidence.get("data"),
                {"payment_id", "payment_reference", "payment_reference_id"},
            )
        )

        refunded = _sum_numbers(
            refund_evidence.get("data"),
            {"refunded_total_brl", "refunded_amount_brl", "refund_amount_brl"},
        )
        if refunded is not None:
            refunded_total += refunded
            refunded_found = True

        refundable = _sum_numbers(
            refund_evidence.get("data"), {"refundable_total_brl", "refundable_amount_brl"}
        )
        if refundable is not None:
            refundable_total += refundable
            refundable_found = True

    captured_value = captured_total if captured_found else None
    refunded_value = refunded_total if refunded_found else None
    refundable_value = refundable_total if refundable_found else None

    if not any((captured_found, refunded_found, refundable_found)):
        verdict = "insufficient_evidence"
    elif (
        captured_value is not None
        and refunded_value is not None
        and refunded_value > captured_value
    ):
        verdict = "capture_mismatch"
    elif (
        refundable_value is not None
        and refunded_value is not None
        and refundable_value > refunded_value
    ):
        verdict = "refund_pending"
    elif (
        refundable_value is not None
        and refunded_value is not None
        and refundable_value > 0
        and refunded_value >= refundable_value
    ):
        verdict = "refunded"
    else:
        verdict = "reconciled"

    result = {
        "payment_analysis": {
            "verdict": verdict,
            "captured_total_brl": captured_value,
            "refunded_total_brl": refunded_value,
            "refundable_total_brl": refundable_value,
        },
        "payment_references": list(dict.fromkeys(payment_references)),
        "evidence_refs": list(dict.fromkeys(evidence_refs)),
    }
    context.collector.trace.emit(
        case_id=context.case_id,
        event_type="handoff",
        actor="payment-agent",
        target="policy-agent",
        decision_code="payment_investigation_completed",
        evidence_refs=result["evidence_refs"][:20],
    )
    return result


async def run_shipment_agent(
    context: CaseContext, order_ids: list[str] | tuple[str, ...]
) -> dict[str, Any]:
    evidence_refs: list[str] = []
    verdicts: list[str] = []
    late_seller_ids: list[str] = []
    timeline_complete = bool(order_ids)

    for order_id in order_ids:
        try:
            evidence = await context.collector.collect(
                actor="shipment-agent",
                tool_name="get_shipment_summary",
                order_id=order_id,
            )
        except RuntimeError:
            timeline_complete = False
            _record_tool_unavailable(context, "shipment-agent", "get_shipment_summary")
            continue
        evidence_refs.append(evidence["evidence_ref"])
        data = evidence.get("data")
        statuses = {
            text.lower()
            for text in _find_strings(data, {"shipment_status", "delivery_status", "order_status"})
        }
        if "lost" in statuses:
            verdicts.append("lost")
            continue
        if "returned" in statuses:
            verdicts.append("returned")
            continue

        estimated = _first_datetime(
            data,
            {"estimated_delivery_at", "estimated_delivery_date", "order_estimated_delivery_date"},
        )
        delivered = _first_datetime(
            data,
            {"delivered_at", "delivered_date", "order_delivered_customer_date"},
        )
        seller_deadline = _first_datetime(
            data,
            {"seller_handoff_deadline", "shipping_limit_date", "seller_shipping_deadline"},
        )
        seller_handoff = _first_datetime(
            data,
            {"seller_handoff_at", "shipped_at", "order_delivered_carrier_date"},
        )

        if estimated is None or delivered is None:
            timeline_complete = False
            verdicts.append("insufficient_evidence")
            continue

        if delivered <= estimated:
            verdicts.append("on_time")
            continue

        if seller_deadline is not None and seller_handoff is not None:
            if seller_handoff > seller_deadline:
                verdicts.append("seller_delay")
                late_seller_ids.extend(_collect_identifiers(data, {"seller_id"}))
            else:
                verdicts.append("logistics_delay")
        else:
            timeline_complete = False
            verdicts.append("conflicting")

    unique_verdicts = set(verdicts)
    if not verdicts or unique_verdicts == {"insufficient_evidence"}:
        verdict = "insufficient_evidence"
    elif len(unique_verdicts) > 1:
        verdict = "conflicting"
    else:
        verdict = verdicts[0]

    result = {
        "shipment_analysis": {
            "verdict": verdict,
            "late_seller_ids": sorted(set(late_seller_ids)),
            "timeline_complete": timeline_complete,
        },
        "evidence_refs": list(dict.fromkeys(evidence_refs)),
    }
    context.collector.trace.emit(
        case_id=context.case_id,
        event_type="handoff",
        actor="shipment-agent",
        target="policy-agent",
        decision_code="shipment_investigation_completed",
        evidence_refs=result["evidence_refs"][:20],
    )
    return result


async def run_specialists(
    context: CaseContext,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    return await asyncio.gather(
        run_order_item_agent(context),
        run_payment_agent(context, context.candidate_order_ids),
        run_shipment_agent(context, context.candidate_order_ids),
    )


def _unique_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _primary_issue(
    entity_resolution: dict[str, Any],
    payment_analysis: dict[str, Any],
    shipment_analysis: dict[str, Any],
) -> str:
    if entity_resolution.get("status") != "resolved":
        return "insufficient_evidence"
    payment_issue = {
        "capture_mismatch": "payment_mismatch",
        "duplicate_capture": "duplicate_charge",
        "refund_pending": "refund_pending",
        "refund_failed": "refund_failed",
    }.get(payment_analysis.get("verdict"))
    if payment_issue:
        return payment_issue
    shipment_issue = {
        "seller_delay": "late_delivery_seller",
        "logistics_delay": "late_delivery_logistics",
    }.get(shipment_analysis.get("verdict"))
    if shipment_issue:
        return shipment_issue
    if shipment_analysis.get("verdict") in {"lost", "returned", "conflicting"}:
        return "insufficient_evidence"
    if payment_analysis.get("verdict") == "insufficient_evidence":
        return "insufficient_evidence"
    return "unsupported_claim"


def _case_status(primary_issue: str) -> str:
    if primary_issue == "insufficient_evidence":
        return "needs_investigation"
    if primary_issue == "unsupported_claim":
        return "no_action"
    return "action_required"


def _root_cause(primary_issue: str, seller_ids: list[str]) -> dict[str, Any]:
    cause_codes = {
        "late_delivery_seller": "SELLER_DELAY",
        "late_delivery_logistics": "LOGISTICS_DELAY",
        "payment_mismatch": "PAYMENT_MISMATCH",
        "duplicate_charge": "DUPLICATE_CAPTURE",
        "refund_pending": "REFUND_PENDING",
        "refund_failed": "REFUND_FAILED",
        "unsupported_claim": "CLAIM_NOT_SUPPORTED",
        "insufficient_evidence": "INSUFFICIENT_EVIDENCE",
    }
    if primary_issue == "late_delivery_seller":
        responsible = [
            {"party_type": "seller", "party_id": seller_ids[0] if seller_ids else None}
        ]
    elif primary_issue == "late_delivery_logistics":
        responsible = [{"party_type": "logistics_provider", "party_id": None}]
    elif primary_issue in {
        "payment_mismatch",
        "duplicate_charge",
        "refund_pending",
        "refund_failed",
    }:
        responsible = [{"party_type": "payment_provider", "party_id": None}]
    else:
        responsible = [{"party_type": "unknown", "party_id": None}]
    return {
        "ranked_causes": [{"cause_code": cause_codes[primary_issue], "rank": 1}],
        "responsible_parties": responsible,
    }


async def run_policy_agent(
    context: CaseContext,
    order_result: dict[str, Any],
    payment_result: dict[str, Any],
    shipment_result: dict[str, Any],
) -> dict[str, Any]:
    policy_evidence_refs: list[str] = []
    try:
        policy_evidence = await context.collector.collect(
            actor="policy-agent",
            tool_name="get_policy",
            policy_version=context.policy_version,
        )
        policy_evidence_refs.append(policy_evidence["evidence_ref"])
    except RuntimeError:
        _record_tool_unavailable(context, "policy-agent", "get_policy")
    entity_resolution = order_result["entity_resolution"]
    payment_analysis = payment_result["payment_analysis"]
    shipment_analysis = shipment_result["shipment_analysis"]
    primary_issue = _primary_issue(entity_resolution, payment_analysis, shipment_analysis)
    case_status = _case_status(primary_issue)
    refund_amount = 0.0
    if payment_analysis.get("verdict") == "refund_pending":
        refund_amount = payment_analysis.get("refundable_total_brl") or 0.0
    order_id = (entity_resolution.get("resolved_order_ids") or [None])[0]
    return {
        "assessment": {
            "primary_issue": primary_issue,
            "secondary_issues": [],
            "case_status": case_status,
            "confidence": min(
                entity_resolution.get("confidence", 0.0),
                0.9 if primary_issue != "insufficient_evidence" else 0.4,
            ),
        },
        "root_cause_analysis": _root_cause(
            primary_issue, shipment_analysis.get("late_seller_ids", [])
        ),
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund_amount,
            "refund_lines": (
                [
                    {
                        "reason_code": "REFUND_PENDING",
                        "amount_brl": refund_amount,
                        "entity_id": order_id,
                    }
                ]
                if refund_amount > 0
                else []
            ),
        },
        "resolution_actions": (
            ["investigate_insufficient_evidence"]
            if primary_issue == "insufficient_evidence"
            else ["no_action"]
            if primary_issue == "unsupported_claim"
            else ["resolve_customer_complaint"]
        ),
        "policy_evidence_refs": policy_evidence_refs,
    }


async def run_verifier_agent(
    context: CaseContext,
    order_result: dict[str, Any],
    payment_result: dict[str, Any],
    shipment_result: dict[str, Any],
    policy_result: dict[str, Any],
) -> dict[str, Any]:
    evidence_refs = _unique_strings(
        order_result.get("evidence_refs", [])
        + payment_result.get("evidence_refs", [])
        + shipment_result.get("evidence_refs", [])
        + policy_result.get("policy_evidence_refs", [])
    )
    customer_unique_id = None
    related_order_ids: list[str] = []
    if context.customer_unique_id_hint:
        try:
            customer_evidence = await context.collector.collect(
                actor="verifier-agent",
                tool_name="get_customer_history",
                customer_unique_id=context.customer_unique_id_hint,
            )
            evidence_refs.append(customer_evidence["evidence_ref"])
            customer_data = customer_evidence.get("data")
            customer_ids = _collect_identifiers(customer_data, {"customer_unique_id"})
            customer_unique_id = customer_ids[0] if customer_ids else None
            related_order_ids = _collect_identifiers(customer_data, {"order_id", "order_ids"})
        except RuntimeError:
            _record_tool_unavailable(context, "verifier-agent", "get_customer_history")

    affected_entities = dict(order_result["affected_entities"])
    affected_entities["payment_references"] = _unique_strings(
        payment_result.get("payment_references", [])
    )
    output = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": context.case_id,
        "assessment": policy_result["assessment"],
        "affected_entities": affected_entities,
        "entity_resolution": order_result["entity_resolution"],
        "customer_context": {
            "customer_unique_id": customer_unique_id,
            "related_order_ids": _unique_strings(related_order_ids),
        },
        "shipment_analysis": shipment_result["shipment_analysis"],
        "payment_analysis": payment_result["payment_analysis"],
        "root_cause_analysis": policy_result["root_cause_analysis"],
        "evidence_refs": _unique_strings(evidence_refs)[:30],
        "data_conflicts": policy_result["data_conflicts"],
        "financial_resolution": policy_result["financial_resolution"],
        "resolution_actions": policy_result["resolution_actions"],
    }
    contracts = getattr(context.collector.trace, "contracts", None)
    if contracts is not None:
        contracts.validate_output(output, f"outputs/{context.case_id}.json")
    context.collector.trace.emit(
        case_id=context.case_id,
        event_type="verification_completed",
        actor="verifier-agent",
        decision_code="output_schema_and_consistency_validated",
        evidence_refs=output["evidence_refs"][:20],
    )
    return output


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Implement the L3B coordinator and specialist-agent workflow here.

    Include entity resolution, conflict handling and evidence-efficient investigation.
    The starter kit intentionally does not generate invented fallback answers.
    """
    context = create_case_context(case, gateway, trace)
    order_result, payment_result, shipment_result = await run_specialists(context)
    policy_result = await run_policy_agent(
        context,
        order_result,
        payment_result,
        shipment_result,
    )
    trace.emit(
        case_id=context.case_id,
        event_type="policy_decided",
        actor="policy-agent",
        decision_code=policy_result["assessment"]["primary_issue"],
        evidence_refs=policy_result["policy_evidence_refs"],
    )
    return await run_verifier_agent(
        context,
        order_result,
        payment_result,
        shipment_result,
        policy_result,
    )
