from __future__ import annotations

from pathlib import Path

from student_agent.llm_validator import _structured_output_schema
from student_agent.workflow import _looks_not_found, _select_order_id


def _order_evidence(order_id: str, data: object) -> dict[str, object]:
    return {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": f"ev_{order_id:0<20}",
        "result_hash": "sha256:" + "0" * 64,
        "domain": "order",
        "data": data,
    }


def test_select_order_rejects_explicit_not_found_candidate() -> None:
    candidates = ["wrong-order", "right-order"]
    evidence = {
        "wrong-order": _order_evidence("wrong-order", {"found": False}),
        "right-order": _order_evidence("right-order", {"order_id": "right-order"}),
    }
    assert _select_order_id(candidates, evidence) == "right-order"


def test_not_found_detection_is_conservative() -> None:
    assert _looks_not_found({"status": "not_found"})
    assert not _looks_not_found({"status": "canceled", "order_id": "order-1"})


def test_structured_schema_has_local_references_only() -> None:
    root = Path(__file__).resolve().parents[1]
    schema = _structured_output_schema(root / "contracts" / "schemas")
    assert "claim_assessments" in schema["required"]
    assert "$defs" in schema
    assert "l3a-output-v2.schema.json" not in str(schema)
