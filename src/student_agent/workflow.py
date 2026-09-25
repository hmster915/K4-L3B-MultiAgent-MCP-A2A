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


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Implement the L3B coordinator and specialist-agent workflow here.

    Include entity resolution, conflict handling and evidence-efficient investigation.
    The starter kit intentionally does not generate invented fallback answers.
    """
    del case, gateway, trace
    raise NotImplementedError("Implement the L3B multi-agent workflow in solve_case()")
