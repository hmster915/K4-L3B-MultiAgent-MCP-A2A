# L3B Multi-Agent Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement a schema-valid L3B case investigation workflow with a coordinator, three specialist agents, a shared MCP Evidence Collector, policy evaluation, and final verification.

**Architecture:** `solve_case()` is the coordinator. It creates a case-scoped collector, emits observable task handoffs, and runs the Order/Item, Payment, and Shipment specialists concurrently. Their evidence-backed internal findings flow to the Policy Agent, then the Verifier Agent builds and validates the exact L3B output.

**Tech Stack:** Python 3.11+, `asyncio`, `jsonschema`, MCP client gateway, pytest, ruff.

## Global Constraints

- Implement only variant `l3b`; do not alter public contracts or extend L3A.
- `contracts/schemas/l3b-output-v2.schema.json` is the highest-priority source of truth.
- Final outputs, trace events, MCP evidence envelopes, and manifests may contain no fields beyond their respective schemas.
- All MCP access goes through `EvidenceCollector`; specialist agents must not call `gateway.call()` directly.
- Every MCP request includes the current `case_id`; evidence refs remain unchanged and are never reused across cases.
- Use only discovered MCP tools: `get_order`, `get_order_items`, `get_product_context`, `get_sellers`, `get_order_payments`, `get_payment_timeline`, `get_refund_timeline`, `get_shipment_summary`, `get_policy`, and `get_customer_history`.
- Tool allocation: Order/Item uses the first four tools; Payment uses the next three; Shipment uses `get_shipment_summary`; Policy uses `get_policy`; Verifier uses `get_customer_history`.
- Do not add speculative facts when evidence is absent. Use schema-supported insufficient-evidence values instead.
- Run focused tests before each implementation change, then run `ruff check src tests` and the non-release-safety tests after each task.

---

### Task 1: Preserve and test the shared Evidence Collector

**Files:**

- Modify: `src/student_agent/workflow.py`
- Test: `tests/test_workflow.py`

**Consumes:** `EvidenceGateway.call(tool_name, *, case_id, **arguments)` and `TraceWriter.emit(...)`.

**Produces:** `EvidenceCollector.collect(actor, tool_name, **arguments) -> dict[str, Any]`.

- [x] **Step 1: Write the failing cache-and-trace test**

  Test two identical `get_order` requests in one case with different consuming actors. Assert exactly one gateway call, equal evidence returned twice, and two `tool_result_consumed` events.

- [x] **Step 2: Verify the test fails because `EvidenceCollector` is absent**

  Run:

  ```bash
  .venv/bin/pytest -q tests/test_workflow.py
  ```

  Expected: import failure for `EvidenceCollector`.

- [x] **Step 3: Implement the minimal collector**

  Add a case-scoped cache keyed by `(tool_name, sorted arguments)`. On a cache miss call:

  ```python
  await gateway.call(tool_name, case_id=self.case_id, **arguments)
  ```

  Then emit:

  ```python
  trace.emit(
      case_id=self.case_id,
      event_type="tool_result_consumed",
      actor=actor,
      tool_name=tool_name,
      evidence_refs=[evidence["evidence_ref"]],
  )
  ```

- [x] **Step 4: Verify the collector**

  Run:

  ```bash
  .venv/bin/pytest -q tests/test_workflow.py tests/test_starter.py
  .venv/bin/ruff check src tests
  ```

  Verified result: 4 tests pass and ruff reports no violations.

### Task 2: Add a coordinator that initializes one case safely

**Files:**

- Modify: `src/student_agent/workflow.py`
- Test: `tests/test_workflow.py`

**Consumes:** Case input, `EvidenceCollector`, `TraceWriter`.

**Produces:** An internal case context and three valid `task_assigned` events.

- [ ] **Step 1: Write the failing coordinator test**

  Add a fake trace that records events. Call the coordinator helper with a minimal case:

  ```python
  case = {
      "case_id": "CASE_001",
      "candidate_order_ids": ["order-123"],
      "customer_request": {"claims": []},
      "policy_version": "EC_POLICY_V2",
      "investigation_scope": {},
  }
  ```

  Assert exactly three `task_assigned` events with targets `order-item-agent`,
  `payment-agent`, and `shipment-agent`.

- [ ] **Step 2: Run the targeted test and verify RED**

  Run:

  ```bash
  .venv/bin/pytest -q tests/test_workflow.py::test_coordinator_assigns_three_specialists
  ```

  Expected: FAIL because the coordinator helper is absent.

- [ ] **Step 3: Implement the minimal coordinator helper**

  Add a helper that extracts `case_id`, `candidate_order_ids`, `customer_request.claims`,
  `policy_version`, `investigation_scope`, and `customer_unique_id_hint`. It must emit:

  ```python
  trace.emit(
      case_id=case_id,
      event_type="task_assigned",
      actor="coordinator",
      target="order-item-agent",
  )
  ```

  Repeat only for the two remaining specialist targets. Do not call MCP or return
  final output in this task.

- [ ] **Step 4: Verify GREEN**

  Run the targeted test, then:

  ```bash
  .venv/bin/pytest -q tests/test_workflow.py tests/test_starter.py
  .venv/bin/ruff check src tests
  ```

### Task 3: Implement the Order/Item specialist and entity resolution

**Files:**

- Modify: `src/student_agent/workflow.py`
- Test: `tests/test_workflow.py`

**Consumes:** Candidate order IDs and the collector.

**Produces:** An internal finding with resolved and rejected order IDs, item IDs,
seller IDs, evidence refs, and a schema-valid entity-resolution fragment.

- [ ] **Step 1: Write failing tests for resolved and ambiguous candidates**

  Fake `get_order` results where one candidate is supported, and another scenario
  where two candidates are equally supported. Assert respectively:

  ```python
  assert result["status"] == "resolved"
  assert result["resolved_order_ids"] == ["order-123"]
  ```

  and:

  ```python
  assert result["status"] == "ambiguous"
  assert result["resolved_order_ids"] == []
  ```

- [ ] **Step 2: Run the targeted tests and verify RED**

  Run:

  ```bash
  .venv/bin/pytest -q tests/test_workflow.py -k entity_resolution
  ```

  Expected: FAIL because the specialist is absent.

- [ ] **Step 3: Implement minimal evidence-backed resolution**

  For each candidate call `get_order` through the collector. For supported order
  IDs only, request `get_order_items`, `get_product_context`, and `get_sellers`.
  Return only schema-permitted entity-resolution values: `resolved`, `ambiguous`,
  or `not_found`. Emit one `handoff` to `policy-agent` after the specialist result.

- [ ] **Step 4: Verify GREEN**

  Run the entity tests, the starter tests, and ruff.

### Task 4: Implement the Payment specialist

**Files:**

- Modify: `src/student_agent/workflow.py`
- Test: `tests/test_workflow.py`

**Consumes:** Candidate or resolved order IDs and the collector.

**Produces:** An internal payment finding with a schema-valid payment-analysis fragment.

- [ ] **Step 1: Write failing tests for reconciled and refund-pending evidence**

  Stub `get_order_payments`, `get_payment_timeline`, and `get_refund_timeline`.
  Assert the payment verdict and the captured/refunded/refundable totals.

- [ ] **Step 2: Run the payment tests and verify RED**

  Run:

  ```bash
  .venv/bin/pytest -q tests/test_workflow.py -k payment
  ```

  Expected: FAIL because the payment specialist is absent.

- [ ] **Step 3: Implement minimal reconciliation**

  Use only the three payment tools through the collector. Return one of the
  schema enum values: `reconciled`, `capture_mismatch`, `duplicate_capture`,
  `refund_pending`, `refund_failed`, `refunded`, or `insufficient_evidence`.
  Use `null` for a total that evidence cannot establish; never invent a zero.

- [ ] **Step 4: Verify GREEN**

  Run the payment tests, starter tests, and ruff.

### Task 5: Implement the Shipment specialist

**Files:**

- Modify: `src/student_agent/workflow.py`
- Test: `tests/test_workflow.py`

**Consumes:** Candidate or resolved order IDs and the collector.

**Produces:** An internal shipment finding with a schema-valid shipment-analysis fragment.

- [ ] **Step 1: Write failing tests for on-time, seller delay, and logistics delay**

  Stub `get_shipment_summary` with the required timestamps and seller handoff
  limits. Assert the correct verdict, `late_seller_ids`, and `timeline_complete`.

- [ ] **Step 2: Run shipment tests and verify RED**

  Run:

  ```bash
  .venv/bin/pytest -q tests/test_workflow.py -k shipment
  ```

  Expected: FAIL because the shipment specialist is absent.

- [ ] **Step 3: Implement timeline classification**

  Call only `get_shipment_summary` through the collector. Return only allowed
  verdicts: `on_time`, `seller_delay`, `logistics_delay`, `lost`, `returned`,
  `conflicting`, or `insufficient_evidence`. Set `timeline_complete` to `true`
  only when the needed evidence exists and is internally consistent.

- [ ] **Step 4: Verify GREEN**

  Run shipment tests, starter tests, and ruff.

### Task 6: Run specialists concurrently and apply policy

**Files:**

- Modify: `src/student_agent/workflow.py`
- Test: `tests/test_workflow.py`

**Consumes:** Coordinator context and three internal specialist findings.

**Produces:** A policy decision with assessment, conflicts, root cause, refund,
and action fields limited to L3B schema values.

- [ ] **Step 1: Write a failing concurrency and policy test**

  Use three async fake specialists that each set a completion marker. Assert all
  markers are present before policy is invoked. Stub `get_policy` and assert it
  receives:

  ```python
  policy_version=case["policy_version"]
  ```

- [ ] **Step 2: Run the test and verify RED**

  Run:

  ```bash
  .venv/bin/pytest -q tests/test_workflow.py -k policy
  ```

  Expected: FAIL because the coordinator does not join specialists or call policy.

- [ ] **Step 3: Implement the join and policy decision**

  Use `asyncio.gather(...)` for the three specialists. After all three complete,
  call `get_policy` through the collector and emit `policy_decided`. Build only
  allowed L3B values for `assessment`, `root_cause_analysis`, `data_conflicts`,
  `financial_resolution`, and `resolution_actions`.

- [ ] **Step 4: Verify GREEN**

  Run policy tests, starter tests, and ruff.

### Task 7: Implement the Verifier and exact output builder

**Files:**

- Modify: `src/student_agent/workflow.py`
- Test: `tests/test_workflow.py`

**Consumes:** Policy decision, specialist evidence refs, `customer_unique_id_hint`,
and the collector.

**Produces:** The final dictionary accepted by `Contracts.validate_output()`.

- [ ] **Step 1: Write a failing schema-validation test**

  Use a fake `get_customer_history` response and a complete internal package.
  Assert `solve_case()` returns a dictionary that validates with:

  ```python
  contracts.validate_output(output, "test output")
  ```

  Add a negative assertion that an injected `"debug"` field causes schema
  validation to fail.

- [ ] **Step 2: Run the verifier test and verify RED**

  Run:

  ```bash
  .venv/bin/pytest -q tests/test_workflow.py -k verifier
  ```

  Expected: FAIL because `solve_case()` still raises `NotImplementedError`.

- [ ] **Step 3: Implement verification and output construction**

  Call `get_customer_history` through the collector only when a non-empty
  customer ID exists. Build the final object explicitly with only L3B fields.
  Validate it using `trace.contracts.validate_output(output, "workflow output")`.
  Emit `verification_completed`. Do not emit `case_finalized`; `cli.py` owns it.

- [ ] **Step 4: Verify GREEN**

  Run verifier tests, starter tests, and ruff.

### Task 8: Validate the complete workflow without spending live-call budget unnecessarily

**Files:**

- Modify: `tests/test_workflow.py`
- Modify: `ARCHITECTURE.md` only if actual implementation decisions differ from it

**Consumes:** All completed workflow helpers and fake MCP evidence envelopes.

**Produces:** An end-to-end test proving required trace lifecycle events and a
schema-valid L3B result.

- [ ] **Step 1: Write a failing end-to-end fake-gateway test**

  Stub all required tools. Assert the trace includes:

  ```python
  {
      "task_assigned",
      "handoff",
      "tool_result_consumed",
      "policy_decided",
      "verification_completed",
  }
  ```

  Assert the output validates against the L3B schema and contains no unknown
  fields.

- [ ] **Step 2: Run the end-to-end test and verify RED**

  Run:

  ```bash
  .venv/bin/pytest -q tests/test_workflow.py -k end_to_end
  ```

  Expected: FAIL until all prior tasks are complete.

- [ ] **Step 3: Make only the minimal integration fixes required**

  Do not add tools, fields, or fallback facts. Keep all test evidence case-scoped
  and use the collector for every fake gateway call.

- [ ] **Step 4: Verify the full local suite and package readiness**

  Run:

  ```bash
  .venv/bin/pytest -q tests/test_workflow.py tests/test_starter.py
  .venv/bin/ruff check src tests
  day09 validate-inputs
  ```

  The release-safety test is expected to fail in this local workspace because
  downloaded competition inputs are intentionally present.
