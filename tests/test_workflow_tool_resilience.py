from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import _consume


class _OkGateway:
    async def call(self, tool_name: str, *, case_id: str, **arguments: Any) -> dict[str, Any]:
        del case_id
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": "ev_" + "a" * 24,
            "result_hash": "sha256:" + "0" * 64,
            "domain": "refund",
            "data": {"order_id": arguments.get("order_id")},
        }


class _FailingGateway:
    async def call(self, tool_name: str, *, case_id: str, **arguments: Any) -> dict[str, Any]:
        del case_id, arguments
        raise RuntimeError(f"MCP tool {tool_name} failed: Error executing tool {tool_name}")


def _trace(tmp_path: Path) -> TraceWriter:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    return TraceWriter(tmp_path / "trace.jsonl", contracts)


def test_consume_returns_result_and_emits_event_on_success(tmp_path: Path) -> None:
    trace = _trace(tmp_path)
    evidence: list[dict[str, Any]] = []

    async def _run() -> dict[str, Any] | None:
        return await _consume(
            _OkGateway(), trace, evidence,
            case_id="L3B_CASE_001", actor="finance-agent",
            tool_name="get_refund_timeline", arguments={"order_id": "order-1"},
        )

    result = asyncio.run(_run())
    assert result is not None
    assert result["evidence_ref"] == "ev_" + "a" * 24
    assert evidence == [{"tool_name": "get_refund_timeline", "response": result}]
    events = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    assert len(events) == 1
    assert events[0]["event_type"] == "tool_result_consumed"


def test_consume_returns_none_and_records_observed_result_on_tool_error(
    tmp_path: Path,
) -> None:
    trace = _trace(tmp_path)
    evidence: list[dict[str, Any]] = []

    async def _run() -> dict[str, Any] | None:
        return await _consume(
            _FailingGateway(), trace, evidence,
            case_id="L3B_CASE_001", actor="finance-agent",
            tool_name="get_refund_timeline", arguments={"order_id": "order-1"},
        )

    result = asyncio.run(_run())
    assert result is None
    assert evidence == [{
        "tool_name": "get_refund_timeline",
        "arguments": {"order_id": "order-1"},
        "observed_result": "tool_error_no_evidence",
    }]
    # No evidence was consumed, so no tool_result_consumed event should exist.
    # TraceWriter only creates the file on the first emit(), so its absence
    # here already proves no event was written.
    trace_file = tmp_path / "trace.jsonl"
    assert not trace_file.exists() or not trace_file.read_text().strip()
