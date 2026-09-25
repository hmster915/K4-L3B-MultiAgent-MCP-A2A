from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import Settings
from .contracts import Contracts
from .llm_validator import validate_with_gpt4o_mini
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter
from .verifier import verify_and_repair

REQUIRED_TOOLS = {
    "get_customer_history",
    "get_order",
    "get_order_items",
    "get_order_payments",
    "get_payment_timeline",
    "get_policy",
    "get_product_context",
    "get_refund_timeline",
    "get_shipment_summary",
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
        data = order_evidence[candidate]["data"]
        if not _looks_not_found(data) and _contains_identifier(data, candidate):
            return candidate
    for candidate in candidates:
        if not _looks_not_found(order_evidence[candidate]["data"]):
            return candidate
    return None


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
    for order_id in candidates:
        try:
            order_evidence[order_id] = await _consume(
                gateway,
                trace,
                evidence,
                case_id=case_id,
                actor="entity-agent",
                tool_name="get_order",
                arguments={"order_id": order_id},
            )
        except RuntimeError:
            # The competition gateway reports an unknown candidate as a tool error
            # instead of returning a synthetic evidence object. Preserve that observed
            # lookup outcome for entity resolution without inventing an evidence_ref.
            order_evidence[order_id] = {"data": None}
            evidence.append(
                {
                    "tool_name": "get_order",
                    "arguments": {"order_id": order_id},
                    "observed_result": "tool_error_no_evidence",
                }
            )
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
        await _consume(
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
        _assign(trace, case_id, "fulfillment-agent", "ANALYZE_ORDER_AND_SHIPMENT")
        for tool_name in ("get_order_items", "get_product_context", "get_shipment_summary"):
            await _consume(
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
        for tool_name in (
            "get_order_payments",
            "get_payment_timeline",
            "get_refund_timeline",
        ):
            await _consume(
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
    await _consume(
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
    output, local_repairs_applied = verify_and_repair(output, evidence, case)
    contracts.validate_output(output, f"local-verifier/{case_id}")
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="gpt-4o-mini-verifier",
        target="coordinator",
        decision_code="SCHEMA_AND_EVIDENCE_VALIDATED",
        evidence_refs=output["evidence_refs"],
        attributes={
            "model": settings.openai_model,
            "mcp_calls": len(evidence),
            "local_repairs_applied": local_repairs_applied,
        },
    )
    return output
