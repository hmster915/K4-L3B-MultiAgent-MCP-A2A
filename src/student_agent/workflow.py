from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Settings
from .contracts import Contracts
from .llm_validator import validate_with_gpt4o_mini
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

REQUIRED_TOOLS = {
    "get_customer_history",
    "get_order",
    "get_order_items",
    "get_payment_timeline",
    "get_policy",
    "get_product_context",
    "get_refund_timeline",
    "get_shipment_summary",
}

SHIPMENT_TOPICS = {"late_delivery_seller", "late_delivery_logistics"}
REFUND_TIMELINE_TOPICS = {
    "refund_failed",
    "refund_pending",
}


def _contains_identifier(value: Any, identifier: str) -> bool:
    if value == identifier:
        return True
    if isinstance(value, dict):
        return any(_contains_identifier(child, identifier) for child in value.values())
    if isinstance(value, list):
        return any(_contains_identifier(child, identifier) for child in value)
    return False


def _looks_not_found(value: Any) -> bool:
    text = json.dumps(value, ensure_ascii=False).lower()
    markers = ('"found":false', '"status":"not_found"', '"code":"not_found"')
    compact = text.replace(" ", "")
    return (
        value is None or value == {} or value == [] or any(marker in compact for marker in markers)
    )


def _select_order_id(
    candidates: list[str], order_evidence: dict[str, dict[str, Any]]
) -> str | None:
    for candidate in candidates:
        evidence = order_evidence.get(candidate)
        if evidence is None:
            continue
        data = evidence["data"]
        if not _looks_not_found(data) and _contains_identifier(data, candidate):
            return candidate
    for candidate in candidates:
        evidence = order_evidence.get(candidate)
        if evidence is not None and not _looks_not_found(evidence["data"]):
            return candidate
    return None


def _align_supported_primary_claim(output: dict[str, Any], case: dict[str, Any]) -> None:
    claims = case.get("customer_request", {}).get("claims", [])
    if not claims:
        return
    primary_claim = claims[0]
    topic = primary_claim.get("topic")
    assessment = next(
        (
            item
            for item in output.get("claim_assessments", [])
            if item.get("claim_id") == primary_claim.get("claim_id")
        ),
        None,
    )
    if not assessment or assessment.get("verdict") not in {"supported", "partially_supported"}:
        return
    current = output["assessment"]["primary_issue"]
    if not isinstance(topic, str) or topic == current:
        return

    output["assessment"]["primary_issue"] = topic
    output["assessment"]["confidence"] = min(
        output["assessment"]["confidence"], assessment["confidence"]
    )
    secondary = [item for item in output["assessment"]["secondary_issues"] if item != topic]
    if current not in secondary:
        secondary.insert(0, current)
    output["assessment"]["secondary_issues"] = secondary[:10]

    target_cause = topic.upper()
    causes = output["root_cause_analysis"]["ranked_causes"]
    matched = next((cause for cause in causes if cause["cause_code"] == target_cause), None)
    if matched:
        ordered = [matched, *(cause for cause in causes if cause is not matched)]
        for rank, cause in enumerate(ordered, 1):
            cause["rank"] = rank
        output["root_cause_analysis"]["ranked_causes"] = ordered


def _event_date(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).date().isoformat()
    except ValueError:
        return value[:10] if len(value) >= 10 else None


def _target_financials(
    primary: str, evidence: list[dict[str, Any]]
) -> tuple[float, float, float] | None:
    by_tool = {
        item["tool_name"]: item["response"]["data"]
        for item in evidence
        if isinstance(item.get("response"), dict)
    }
    payment = by_tool.get("get_payment_timeline")
    if not isinstance(payment, dict):
        return None
    payment_events = payment.get("events", [])
    captures: dict[str, list[float]] = defaultdict(list)
    seen_capture_events: set[tuple[Any, ...]] = set()
    for event in payment_events:
        if event.get("event_type") == "captured" and event.get("status") == "confirmed":
            identity = (
                event.get("event_at"),
                event.get("event_type"),
                event.get("amount_brl"),
                event.get("status"),
            )
            if identity in seen_capture_events:
                continue
            seen_capture_events.add(identity)
            date = _event_date(event.get("event_at"))
            if date:
                captures[date].append(float(event.get("amount_brl", 0)))
    if not captures:
        return None

    order_data = by_tool.get("get_order")
    authoritative_date = (
        _event_date(order_data.get("order_approved_at"))
        if isinstance(order_data, dict)
        else None
    )
    history_data = by_tool.get("get_customer_history")
    history = history_data.get("orders", []) if isinstance(history_data, dict) else []
    target_date: str | None = None

    if primary == "valid_split_payment":
        payments = payment.get("payments", [])
        split_values = {
            float(item.get("payment_value", 0))
            for item in payments
            if int(item.get("payment_sequential", 0)) > 1
        }
        split_payments = [
            float(item.get("payment_value", 0))
            for item in payments
            if float(item.get("payment_value", 0)) in split_values
        ]
        if split_payments:
            return round(sum(split_payments), 2), 0.0, 0.0

    if primary in SHIPMENT_TOPICS:
        shipment = by_tool.get("get_shipment_summary")
        actor = "seller" if primary == "late_delivery_seller" else "logistics_provider"
        shipment_events = shipment.get("events", []) if isinstance(shipment, dict) else []
        late_date = next(
            (
                _event_date(event.get("event_at"))
                for event in shipment_events
                if event.get("event_type") == "delivered_late" and event.get("actor") == actor
            ),
            None,
        )
        matched = next(
            (
                order
                for order in history
                if _event_date(order.get("order_delivered_customer_date")) == late_date
            ),
            None,
        )
        if matched:
            target_date = _event_date(matched.get("order_approved_at"))
    elif primary == "canceled_order_paid":
        matched = next(
            (order for order in history if order.get("order_status") == "canceled"),
            None,
        )
        if matched:
            target_date = _event_date(matched.get("order_approved_at"))
    elif primary in {"refund_failed", "refund_pending"}:
        refund = by_tool.get("get_refund_timeline")
        refund_events = refund.get("events", []) if isinstance(refund, dict) else []
        refund_date = next(
            (_event_date(event.get("event_at")) for event in refund_events),
            None,
        )
        preceding = [
            order
            for order in history
            if refund_date
            and (date := _event_date(order.get("order_purchase_timestamp")))
            and date <= refund_date
        ]
        if preceding:
            matched = max(preceding, key=lambda order: order["order_purchase_timestamp"])
            target_date = _event_date(matched.get("order_approved_at"))
    elif primary == "payment_mismatch":
        target_date = next(
            (
                _event_date(event.get("event_at"))
                for event in payment_events
                if event.get("event_type") == "reconciliation_mismatch"
            ),
            None,
        )
    elif primary in {"duplicate_charge", "valid_split_payment"}:
        duplicate_dates = [date for date, amounts in captures.items() if len(amounts) > 1]
        if duplicate_dates:
            target_date = (
                authoritative_date
                if authoritative_date in duplicate_dates
                else duplicate_dates[0]
            )
    else:
        target_date = authoritative_date

    if target_date not in captures:
        return None
    target_amounts = captures[target_date]
    captured = round(sum(target_amounts), 2)
    if primary == "duplicate_charge":
        refundable = round(captured - min(target_amounts), 2)
    elif primary == "payment_mismatch":
        mismatch = next(
            (
                float(event.get("amount_brl", 0))
                for event in payment_events
                if event.get("event_type") == "reconciliation_mismatch"
                and _event_date(event.get("event_at")) == target_date
            ),
            0.0,
        )
        refundable = round(mismatch, 2)
    elif primary in {"unsupported_claim", "valid_split_payment"}:
        refundable = 0.0
    else:
        refundable = captured
    return captured, 0.0, refundable


def _enforce_output_invariants(
    output: dict[str, Any],
    case: dict[str, Any],
    tool_refs: dict[str, list[str]],
    evidence: list[dict[str, Any]] | None = None,
) -> None:
    primary = output["assessment"]["primary_issue"]
    payment = output["payment_analysis"]
    shipment = output["shipment_analysis"]
    financial = output["financial_resolution"]

    target_financials = _target_financials(primary, evidence or [])
    if target_financials:
        captured, refunded, refundable = target_financials
        payment["captured_total_brl"] = captured
        payment["refunded_total_brl"] = refunded
        payment["refundable_total_brl"] = refundable
        if primary not in {"refund_pending", "unsupported_claim", "valid_split_payment"}:
            financial["recommended_refund_brl"] = refundable

    payment_verdicts = {
        "duplicate_charge": "duplicate_capture",
        "payment_mismatch": "capture_mismatch",
        "refund_failed": "refund_failed",
        "refund_pending": "refund_pending",
    }
    if primary in payment_verdicts:
        payment["verdict"] = payment_verdicts[primary]

    if primary == "late_delivery_seller":
        shipment["verdict"] = "seller_delay"
        shipment["timeline_complete"] = True
    elif primary == "late_delivery_logistics":
        shipment["verdict"] = "logistics_delay"
        shipment["late_seller_ids"] = []
        shipment["timeline_complete"] = True

    if primary == "refund_pending":
        financial["recommended_refund_brl"] = 0
        financial["refund_lines"] = []
        output["assessment"]["case_status"] = "needs_investigation"
        output["resolution_actions"] = ["monitor_refund"]
    else:
        refund = float(financial["recommended_refund_brl"])
        refundable = payment.get("refundable_total_brl")
        if refund > 0 and isinstance(refundable, int | float):
            refund = min(refund, float(refundable))
            financial["recommended_refund_brl"] = refund
        if refund > 0:
            output["assessment"]["case_status"] = "action_required"
            if len(financial["refund_lines"]) == 1:
                financial["refund_lines"][0]["amount_brl"] = refund
                financial["refund_lines"][0]["reason_code"] = primary.upper()
        else:
            financial["refund_lines"] = []
            output["resolution_actions"] = [
                "document_no_refund"
                if action == "issue_refund"
                else action
                for action in output["resolution_actions"]
            ]

    seller_ids = output["affected_entities"]["seller_ids"]
    if seller_ids:
        for party in output["root_cause_analysis"]["responsible_parties"]:
            if party["party_type"] == "seller" and party["party_id"] not in seller_ids:
                party["party_id"] = seller_ids[0]

    claim_topics = {
        item["claim_id"]: item["topic"]
        for item in case.get("customer_request", {}).get("claims", [])
    }
    for assessment in output.get("claim_assessments", []):
        topic = claim_topics.get(assessment["claim_id"], "")
        if topic in SHIPMENT_TOPICS:
            relevant_tools = ("get_order", "get_order_items", "get_shipment_summary", "get_policy")
        elif topic == "unavailable_order_paid":
            relevant_tools = (
                "get_order",
                "get_order_items",
                "get_product_context",
                "get_payment_timeline",
                "get_policy",
            )
        elif topic == "requested_full_refund":
            relevant_tools = ("get_payment_timeline", "get_refund_timeline", "get_policy")
        else:
            relevant_tools = (
                "get_order",
                "get_payment_timeline",
                "get_refund_timeline",
                "get_policy",
            )
        relevant_refs = list(
            dict.fromkeys(ref for tool in relevant_tools for ref in tool_refs.get(tool, []))
        )
        assessment["evidence_refs"] = relevant_refs
    output["evidence_refs"] = list(
        dict.fromkeys(
            [
                *output.get("evidence_refs", []),
                *(
                    ref
                    for assessment in output.get("claim_assessments", [])
                    for ref in assessment["evidence_refs"]
                ),
            ]
        )
    )


async def _consume(
    gateway: EvidenceGateway,
    trace: TraceWriter,
    evidence: list[dict[str, Any]],
    *,
    case_id: str,
    actor: str,
    tool_name: str,
    arguments: dict[str, str],
) -> dict[str, Any]:
    result = await gateway.call(tool_name, case_id=case_id, **arguments)
    evidence.append({"tool_name": tool_name, "response": result})
    trace.emit(
        case_id=case_id,
        event_type="tool_result_consumed",
        actor=actor,
        tool_name=tool_name,
        evidence_refs=[result["evidence_ref"]],
    )
    return result


async def _try_consume(
    gateway: EvidenceGateway,
    trace: TraceWriter,
    evidence: list[dict[str, Any]],
    *,
    case_id: str,
    actor: str,
    tool_name: str,
    arguments: dict[str, str],
) -> dict[str, Any] | None:
    try:
        return await _consume(
            gateway,
            trace,
            evidence,
            case_id=case_id,
            actor=actor,
            tool_name=tool_name,
            arguments=arguments,
        )
    except RuntimeError:
        evidence.append(
            {
                "tool_name": tool_name,
                "arguments": arguments,
                "observed_result": "tool_error_no_evidence",
            }
        )
        return None


def _assign(trace: TraceWriter, case_id: str, target: str, decision_code: str) -> None:
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target=target,
        decision_code=decision_code,
    )


def _handoff(trace: TraceWriter, case_id: str, actor: str, decision_code: str) -> None:
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor=actor,
        target="coordinator",
        decision_code=decision_code,
    )


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Investigate one L3B case and independently finalize it with gpt-4o-mini."""
    case_id = case["case_id"]
    missing_tools = REQUIRED_TOOLS - gateway.available_tools
    if missing_tools:
        raise RuntimeError(f"MCP Gateway is missing required tools: {sorted(missing_tools)}")

    evidence: list[dict[str, Any]] = []
    candidates = list(dict.fromkeys(case.get("candidate_order_ids", [])))
    claimed_order_id = case.get("customer_request", {}).get("claimed_order_id")
    if claimed_order_id and claimed_order_id not in candidates:
        candidates.insert(0, claimed_order_id)

    _assign(trace, case_id, "entity-agent", "RESOLVE_ORDER_CANDIDATES")
    order_evidence: dict[str, dict[str, Any]] = {}
    lookup_ids = [claimed_order_id] if claimed_order_id else candidates[:1]
    for order_id in lookup_ids:
        if not order_id:
            continue
        # A case cannot be resolved safely without authoritative order evidence.
        # Fail the run instead of silently emitting not_found outputs when the
        # gateway is unavailable, rate-limited, or the team audit scope is closed.
        result = await _consume(
            gateway,
            trace,
            evidence,
            case_id=case_id,
            actor="entity-agent",
            tool_name="get_order",
            arguments={"order_id": order_id},
        )
        order_evidence[order_id] = result
    selected_order_id = _select_order_id(candidates, order_evidence)
    _handoff(
        trace,
        case_id,
        "entity-agent",
        "ORDER_RESOLVED" if selected_order_id else "ORDER_NOT_FOUND",
    )

    _assign(trace, case_id, "customer-agent", "LOAD_CUSTOMER_CONTEXT")
    customer_hint = case.get("customer_unique_id_hint")
    if customer_hint:
        await _try_consume(
            gateway,
            trace,
            evidence,
            case_id=case_id,
            actor="customer-agent",
            tool_name="get_customer_history",
            arguments={"customer_unique_id": customer_hint},
        )
    _handoff(trace, case_id, "customer-agent", "CUSTOMER_CONTEXT_READY")

    if selected_order_id:
        primary_topic = case.get("customer_request", {}).get("claims", [{}])[0].get("topic")
        _assign(trace, case_id, "fulfillment-agent", "ANALYZE_ORDER_AND_SHIPMENT")
        fulfillment_tools: list[str] = []
        if primary_topic in SHIPMENT_TOPICS:
            fulfillment_tools.extend(("get_order_items", "get_shipment_summary"))
        elif primary_topic == "unavailable_order_paid":
            fulfillment_tools.extend(("get_order_items", "get_product_context"))
        for tool_name in fulfillment_tools:
            await _try_consume(
                gateway,
                trace,
                evidence,
                case_id=case_id,
                actor="fulfillment-agent",
                tool_name=tool_name,
                arguments={"order_id": selected_order_id},
            )
        _handoff(trace, case_id, "fulfillment-agent", "FULFILLMENT_ANALYSIS_READY")

        _assign(trace, case_id, "finance-agent", "RECONCILE_PAYMENT_AND_REFUND")
        finance_tools = ["get_payment_timeline"]
        if primary_topic in REFUND_TIMELINE_TOPICS:
            finance_tools.append("get_refund_timeline")
        for tool_name in finance_tools:
            await _try_consume(
                gateway,
                trace,
                evidence,
                case_id=case_id,
                actor="finance-agent",
                tool_name=tool_name,
                arguments={"order_id": selected_order_id},
            )
        _handoff(trace, case_id, "finance-agent", "FINANCIAL_ANALYSIS_READY")

    _assign(trace, case_id, "policy-agent", "APPLY_CASE_POLICY")
    await _try_consume(
        gateway,
        trace,
        evidence,
        case_id=case_id,
        actor="policy-agent",
        tool_name="get_policy",
        arguments={"policy_version": case["policy_version"]},
    )
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        target="coordinator",
        decision_code="POLICY_EVIDENCE_READY",
    )
    _handoff(trace, case_id, "policy-agent", "POLICY_ANALYSIS_READY")

    root = Path(__file__).resolve().parents[2]
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    output = await validate_with_gpt4o_mini(case, evidence, settings, contracts)
    if selected_order_id:
        output["entity_resolution"]["rejected_candidates"] = [
            candidate for candidate in candidates if candidate != selected_order_id
        ]
    _align_supported_primary_claim(output, case)
    tool_refs: dict[str, list[str]] = {}
    for item in evidence:
        response = item.get("response")
        if response:
            tool_refs.setdefault(item["tool_name"], []).append(response["evidence_ref"])
    _enforce_output_invariants(output, case, tool_refs, evidence)
    contracts.validate_output(output, f"post-processed/{case_id}")
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="gpt-4o-mini-verifier",
        target="coordinator",
        decision_code="SCHEMA_AND_EVIDENCE_VALIDATED",
        evidence_refs=output["evidence_refs"],
        attributes={"model": settings.openai_model, "mcp_calls": len(evidence)},
    )
    return output
