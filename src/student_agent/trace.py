from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contracts import Contracts


class TraceWriter:
    """Append observable workflow events. Never put prompts or chain-of-thought here."""

    def __init__(self, path: Path, contracts: Contracts) -> None:
        self.path = path
        self.contracts = contracts
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(
        self,
        *,
        case_id: str,
        event_type: str,
        actor: str,
        target: str | None = None,
        decision_code: str | None = None,
        tool_name: str | None = None,
        evidence_refs: list[str] | None = None,
        attributes: dict[str, str | int | float | bool | None] | None = None,
    ) -> dict[str, Any]:
        event: dict[str, Any] = {
            "schema_version": "day09-trace-event-v1",
            "event_id": f"evt_{secrets.token_urlsafe(18)}",
            "case_id": case_id,
            "event_type": event_type,
            "occurred_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "actor": actor,
        }
        optional = {
            "target": target,
            "decision_code": decision_code,
            "tool_name": tool_name,
            "evidence_refs": evidence_refs,
            "attributes": attributes,
        }
        event.update({key: value for key, value in optional.items() if value is not None})
        self.contracts.validate_trace(event, "trace event")
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        return event


def normalize_trace(path: Path, outputs: dict[str, dict[str, Any]]) -> None:
    """Keep the one completed lifecycle whose verification matches each final output."""
    records: list[tuple[int, dict[str, Any], str]] = []
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        if line.strip():
            records.append((index, json.loads(line), line))

    kept_indices: set[int] = set()
    for case_id, output in outputs.items():
        case_records = [record for record in records if record[1].get("case_id") == case_id]
        expected_refs = set(output.get("evidence_refs", []))
        tool_records = [
            record
            for record in case_records
            if record[1].get("event_type") == "tool_result_consumed"
            and expected_refs.intersection(record[1].get("evidence_refs", []))
        ]
        verifications = [
            record
            for record in case_records
            if record[1].get("event_type") == "verification_completed"
            and set(record[1].get("evidence_refs", [])) == expected_refs
        ]
        if not verifications:
            raise ValueError(f"trace has no matching verification for {case_id}")
        verification = verifications[-1]
        first_activity = min(
            [verification[0], *(record[0] for record in tool_records)]
        )
        received = [
            record
            for record in case_records
            if record[0] < first_activity and record[1].get("event_type") == "case_received"
        ]
        finalized = [
            record
            for record in case_records
            if record[0] > verification[0] and record[1].get("event_type") == "case_finalized"
        ]
        if not received or not finalized:
            raise ValueError(f"trace has an incomplete lifecycle for {case_id}")
        start = received[-1][0]
        end = finalized[0][0]
        kept_indices.update({start, verification[0], end})
        kept_indices.update(record[0] for record in tool_records)

        latest_by_signature: dict[tuple[Any, ...], int] = {}
        for index, event, _line in case_records:
            event_type = event.get("event_type")
            if not (start < index < verification[0]):
                continue
            if event_type not in {"task_assigned", "handoff", "policy_decided"}:
                continue
            signature = (
                event_type,
                event.get("actor"),
                event.get("target"),
                event.get("decision_code"),
            )
            latest_by_signature[signature] = index
        kept_indices.update(latest_by_signature.values())

    normalized = [line for index, _event, line in records if index in kept_indices]
    path.write_text("\n".join(normalized) + "\n", encoding="utf-8")
