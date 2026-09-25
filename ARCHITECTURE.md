# L3B Architecture Record

Tài liệu này mô tả các quyết định có thể kiểm chứng của workflow. Prompt nội bộ,
chain-of-thought và secret không được ghi vào trace hay output.

## 1. System overview

```text
Input → Entity Agent → Coordinator → Customer/Fulfillment/Finance/Policy Agents
             │                │                         │
             └──────────── MCP evidence ────────────────┘
                              │
                    gpt-4o-mini Verifier
                              │
                Local JSON Schema validation → Output

Mọi bước giao việc, tiêu thụ evidence, handoff, policy decision và verification đều
được ghi vào observable trace. Nội dung suy luận riêng không được ghi lại.
```

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Entity | Candidate IDs | Xác minh từng candidate và chọn order có authoritative evidence | `get_order` | Resolved/rejected candidates |
| Coordinator | Case và handoff | Phân công, tập hợp evidence, kiểm tra lifecycle | Không gọi domain tool trực tiếp | Investigation bundle |
| Customer | Customer hint | Lấy lịch sử customer trong đúng case | `get_customer_history` | Customer context |
| Fulfillment | Resolved order | Item, product và shipment timeline | `get_order_items`, `get_product_context`, `get_shipment_summary` | Shipment/entity analysis |
| Finance | Resolved order | Đối soát capture, payment lifecycle và refund | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` | Financial analysis |
| Policy | Policy version | Lấy policy áp dụng cho case | `get_policy` | Policy evidence |
| Verifier | Case và toàn bộ consumed evidence | Resolve conflict, hiệu chỉnh confidence, tạo output đúng schema | OpenAI `gpt-4o-mini`, không gọi MCP | Final output |

Áp dụng least privilege; tool discovery không đồng nghĩa mọi actor đều được gọi mọi tool.

## 3. Entity resolution và A2A protocol

- Giữ thứ tự candidate, loại duplicate và thêm claimed ID nếu chưa có trong danh sách.
- Gọi `get_order` đúng một lần cho mỗi candidate. Gateway hiện trả tool error cho candidate
  không tồn tại; workflow ghi nhận lookup outcome này nhưng không tạo `evidence_ref` giả.
- Chọn candidate có identifier trùng authoritative payload và không mang trạng thái
  `not_found`; fallback chỉ chọn payload có dữ liệu khi server không lặp lại identifier.
- Mọi MCP call luôn mang `case_id`; handoff có `actor`, `target` và `decision_code`.
- Workflow là DAG cố định, không có vòng lặp agent. Retry model chỉ xảy ra tối đa một lần
  khi local schema validation thất bại.
- MCP timeout tổng là 300 giây, connect/write/pool timeout là 30 giây. OpenAI validation
  timeout tổng là 180 giây.

## 4. Evidence và conflict lifecycle

Mỗi MCP response được validate bằng `mcp-evidence-response-v1` trước khi dùng. Workflow
lưu cặp `tool_name` + response, emit `tool_result_consumed` với nguyên bản `evidence_ref`,
và chỉ gửi bundle của case hiện tại cho verifier. Verifier ưu tiên lifecycle tool khi
nguồn mâu thuẫn, ghi conflict vào `data_conflicts`, và map từng claim sang evidence refs.
Output tiếp tục được validate bằng public L3B schema; không sửa hoặc tự tạo evidence ref.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout | 0 automatic retry | Dừng case, không tạo fallback | Runtime error; không finalize |
| Entity not found/ambiguous | 0 broad scan | Chỉ dùng candidate đã cấp; verifier trả `not_found`/`ambiguous` | `ORDER_NOT_FOUND` hoặc confidence thấp |
| Source conflict | Không gọi lại | Ưu tiên authoritative lifecycle/policy, nếu chưa giải được thì ghi conflict | `policy_decided` và output `data_conflicts` |
| Invalid model result | 1 repair call | Gửi lỗi schema để sửa; vẫn lỗi thì dừng | Không emit `verification_completed` |

Budget thông thường là 10 MCP calls/case: hai candidate order calls và tám call chuyên
biệt. Không gọi `get_sellers` vì seller IDs đã có trong item evidence. Không cache hoặc
tái sử dụng evidence giữa các case.

## 6. Verification invariants

Trước finalize, verifier phải bảo đảm:

- đúng `case_id` và `schema_version`;
- resolved/rejected candidates xuất phát từ input và authoritative order evidence;
- mọi entity và evidence ref xuất hiện trong bundle MCP của đúng case;
- mọi claim ID được đánh giá và liên kết evidence;
- shipment verdict khớp timeline, seller responsibility và late seller IDs;
- captured/refunded/refundable totals không âm và khớp lifecycle;
- refund/action chỉ được đề xuất khi policy và evidence hỗ trợ;
- conflict ghi rõ sources, selected source và resolution code;
- confidence nằm trong `[0, 1]` và giảm khi evidence thiếu/mâu thuẫn;
- output pass local JSON Schema trước khi ghi file.

## 7. Reproducibility

- Python: 3.11 trở lên; dependency ranges được pin trong `pyproject.toml`.
- Model verifier: `gpt-4o-mini`, temperature `0`, Structured Outputs; JSON mode là
  compatibility fallback khi endpoint từ chối schema dialect.
- Execution: tuần tự theo thứ tự `case-set.json`, không dùng random seed hoặc concurrency.
- Commands: `day09 validate-inputs`, `day09 run`, `day09 validate`, sau đó
  `day09 package --output dist/submission.zip`.
- Secret chỉ đọc từ `.env`/process environment và không được ghi vào output, trace hoặc ZIP.
