from __future__ import annotations

from pathlib import Path
from typing import Any

from student_agent.llm_validator import _structured_output_schema


def _walk(node: Any) -> list[Any]:
    """Yield every dict node in the schema tree, depth-first."""
    nodes: list[Any] = []
    if isinstance(node, dict):
        nodes.append(node)
        for value in node.values():
            nodes.extend(_walk(value))
    elif isinstance(node, list):
        for item in node:
            nodes.extend(_walk(item))
    return nodes


def test_structured_schema_has_no_unique_items() -> None:
    # OpenAI Structured Outputs (response_format=json_schema, strict=True) rejects
    # any schema containing 'uniqueItems': "'uniqueItems' is not permitted." — this
    # was verified against the live API. l3a/l3b-output-v2.schema.json use
    # uniqueItems throughout (idSet, evidenceRefs, resolution_actions, etc.), so the
    # schema handed to OpenAI must have it stripped or every case's completion 400s.
    root = Path(__file__).resolve().parents[1]
    schema = _structured_output_schema(root / "contracts" / "schemas")
    for node in _walk(schema):
        assert "uniqueItems" not in node, f"uniqueItems leaked into schema node: {node}"


def test_structured_schema_every_enum_or_const_node_has_a_type() -> None:
    # OpenAI Structured Outputs requires every schema node to declare an explicit
    # 'type' — verified live: "schema must have a 'type' key" for
    # $defs.financialResolution.properties.currency ({"const": "BRL"}, no type).
    # The public contract relies on JSON Schema's implicit typing for enum/const
    # nodes (case_status, primary_issue, currency, party_type, schema_version,
    # ...), so the schema handed to OpenAI must fill in 'type' for those nodes.
    root = Path(__file__).resolve().parents[1]
    schema = _structured_output_schema(root / "contracts" / "schemas")
    for node in _walk(schema):
        if "enum" in node or "const" in node:
            assert "type" in node, f"enum/const node missing 'type': {node}"
