from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from student_agent.contracts import Contracts
from student_agent.mcp_gateway import EvidenceGateway


class ModernSession:
    async def call_tool(self, tool_name: str, *, arguments: dict[str, str]) -> Any:
        return SimpleNamespace(
            is_error=False,
            structured_content={
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_abcdefghijklmnopqrst",
                "result_hash": "sha256:" + "a" * 64,
                "domain": "order",
                "data": {"order_id": arguments["order_id"]},
            },
            content=[],
        )


def test_gateway_accepts_modern_mcp_is_error_property() -> None:
    root = Path(__file__).resolve().parents[1]
    gateway = EvidenceGateway(ModernSession(), Contracts(root / "contracts" / "schemas"))

    evidence = asyncio.run(
        gateway.call("get_order", case_id="CASE_001", order_id="order-123")
    )

    assert evidence["evidence_ref"] == "ev_abcdefghijklmnopqrst"
