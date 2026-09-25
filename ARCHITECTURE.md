# Tài liệu kiến trúc L3B

Nhóm phải cập nhật tài liệu này cùng với source code. Mục tiêu là mô tả các
quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. Tổng quan hệ thống

Workflow sử dụng một Coordinator/Router, ba specialist agent chạy song song,
một MCP Evidence Collector dùng chung, một Policy Agent và một Verifier Agent.
Các specialist agent không gọi MCP trực tiếp. Chúng gửi yêu cầu lấy evidence
theo từng case cho collector. Collector chịu trách nhiệm discovery, gọi MCP,
kiểm tra response, cache và theo dõi evidence reference.

```text
Dữ liệu case
    │
    ▼
Coordinator / Router
    │  handoff
    ├───────────────┬────────────────┐
    ▼               ▼                ▼
Order/Item      Payment          Shipment
Agent           Agent            Agent
    │               │                │
    └───────────────┴────────────────┘
                    │ yêu cầu evidence song song
                    ▼
         MCP Evidence Collector
         discovery · gọi · kiểm tra
         cache · quản lý evidence_ref
                    │
                    ▼
              Policy Agent
         quyết định policy và conflict
                    │
                    ▼
             Verifier Agent
      kiểm tra schema · evidence · nhất quán
                    │ output đã kiểm tra
                    ▼
                Kết quả cuối

Tất cả handoff, MCP consumption và lifecycle event có thể quan sát được đều
được ghi vào trace theo từng case.
```

## 2. Quy tắc bất biến của public contract

Public JSON Schema là nguồn chân lý có mức ưu tiên cao nhất. Khi tài liệu,
source code, suy luận của agent hoặc dữ liệu runtime có bất kỳ sai lệch nào so
với schema, phải tuân theo schema.

Phạm vi triển khai là variant `l3b`. Không triển khai hoặc mở rộng output của
variant `l3a`. Schema L3B hiện dùng lại một số `$defs` công khai từ file
`l3a-output-v2.schema.json`; việc tham chiếu này không cho phép thêm field hoặc
thay đổi ý nghĩa của L3B.

Output L3B chỉ được chứa các field top-level đã được contract khai báo:

```text
schema_version
case_id
assessment
affected_entities
claim_assessments       (tùy chọn theo schema)
entity_resolution
customer_context
shipment_analysis
payment_analysis
root_cause_analysis
evidence_refs
data_conflicts
financial_resolution
resolution_actions
```

Mọi object con phải tuân theo đúng `required`, `properties`, `enum`, kiểu dữ
liệu, giới hạn số lượng và giới hạn độ dài trong
`contracts/schemas/l3b-output-v2.schema.json`. Không thêm field phụ để lưu
debug, trạng thái agent, lý do nội bộ, score trung gian hoặc metadata custom.

Các envelope dùng nội bộ cho handoff không được tự động đưa vào output. Trace
chỉ được chứa các field được khai báo trong
`trace-event-v1.schema.json`; MCP response chỉ được chứa các field trong
`mcp-evidence-response-v1.schema.json`; manifest chỉ được chứa các field trong
`submission-manifest-v2.schema.json`.

Không sửa trực tiếp ý nghĩa của public contract đã phát hành. Nếu thật sự cần
thay đổi breaking, phải tạo schema version mới theo quy trình release thay vì
thêm field vào version hiện tại.

## 3. Phân quyền và trách nhiệm của agent

| Vai trò | Dữ liệu vào | Trách nhiệm | Quyền dùng tool | Kết quả/chuyển giao |
| --- | --- | --- | --- | --- |
| Coordinator / Router | Case input và trạng thái specialist | Tạo context cho case, phân công công việc độc lập, kiểm soát phạm vi case và tổng hợp kết quả | Không gọi MCP theo domain nghiệp vụ; có thể yêu cầu collector discovery tool | Handoff tới ba specialist; investigation package tới Policy Agent |
| Order/Item Agent | Candidate order ID, order được claim và customer claim | Resolve entity order/item/product/seller, báo candidate được hỗ trợ và bị loại | Evidence order, item, product và seller thông qua collector | Kết quả order/item và evidence ref tới Policy Agent |
| Payment Agent | Order/item ID và payment claim | Đối soát capture, duplicate charge, refund và tổng tiền có thể refund | Evidence payment và refund thông qua collector | Phân tích payment và evidence ref tới Policy Agent |
| Shipment Agent | Order/item ID và delivery claim | Dựng timeline giao hàng và xác định trách nhiệm delay | Evidence shipment, order và logistics thông qua collector | Phân tích shipment và evidence ref tới Policy Agent |
| MCP Evidence Collector | Yêu cầu evidence theo từng case | Discovery tool, gọi MCP, kiểm tra evidence envelope, giữ nguyên evidence ref, cache trong phạm vi case và ghi event consumption | Tất cả MCP tool được discovery, luôn kèm case ID | Evidence envelope đã kiểm tra tới specialist agent yêu cầu |
| Policy Agent | Kết quả specialist, evidence ref và policy version | Áp dụng policy, xử lý conflict nguồn, xác định primary issue, trách nhiệm, refund và action | Evidence policy thông qua collector; không thay đổi trực tiếp kết quả specialist | L3B assessment dự kiến tới Verifier Agent |
| Verifier Agent | Assessment dự kiến và toàn bộ evidence được tham chiếu | Kiểm tra schema, ownership của evidence, phạm vi entity, timeline, tổng tiền, tính nhất quán và confidence | Quyền đọc investigation package; có thể yêu cầu evidence bổ sung có mục tiêu thông qua collector | Output cuối đã kiểm tra hoặc yêu cầu sửa có giới hạn |

Áp dụng nguyên tắc quyền tối thiểu. Discovery tool không có nghĩa là mọi actor
được phép gọi mọi tool.

## 4. Entity resolution và giao thức A2A

Coordinator tạo một case context bất biến, được định danh bằng `case_id`.
Mỗi handoff sử dụng một envelope có cấu trúc nhỏ gồm:

```text
case_id
correlation_id
sender
recipient
task_type
input_entity_ids
requested_domains
deadline
```

Order/Item Agent xếp hạng candidate order dựa trên các match có evidence với
order được claim, customer hint, claim và quan hệ order/item trả về. Agent
đánh dấu candidate không được hỗ trợ là rejected và chỉ resolve order khi
evidence vượt ngưỡng confidence đã cấu hình. Nếu các candidate đầu bảng không
thể phân biệt, kết quả là `ambiguous`; nếu không có candidate nào được hỗ trợ,
kết quả là `not_found`.

Coordinator gửi cùng case context, bao gồm candidate ID và claim, cho
Order/Item, Payment và Shipment Agent cùng lúc. Order/Item Agent resolve entity,
trong khi Payment và Shipment Agent điều tra bằng context candidate hiện có.
Nếu một specialist cần ID đã resolve để thực hiện follow-up có mục tiêu,
specialist đó yêu cầu follow-up thông qua collector sau khi nhận kết quả từ
Order/Item Agent. Mỗi specialist trả về finding có cấu trúc và các evidence ref
đã sử dụng. Policy Agent chỉ bắt đầu sau khi đã tổng hợp xong kết quả của ba
specialist.

Handoff được liên kết bằng `case_id` và `correlation_id`. Mỗi handoff có một
owner, một deadline có giới hạn và không tự động chuyển vòng lặp. Timeout và
result không hợp lệ được trả về Coordinator dưới dạng lỗi rõ ràng, không được
chuyển thành dữ liệu phỏng đoán. Trace chỉ ghi task, handoff và completion
metadata có thể quan sát; không ghi suy luận riêng tư.

## 5. Vòng đời evidence và conflict

MCP Evidence Collector là component duy nhất được phép gọi MCP. Khi khởi động,
collector discovery danh sách tool có sẵn. Với mỗi request, collector:

1. bắt buộc có `case_id` hiện tại;
2. chỉ gọi tool đã discovery;
3. kiểm tra response theo `mcp-evidence-response-v1.schema.json`;
4. giữ nguyên `evidence_ref` và result hash do server trả về;
5. chỉ cache request giống nhau trong case và run hiện tại;
6. emit `tool_result_consumed` khi specialist sử dụng result.

Evidence ref được sao chép nguyên trạng vào claim assessment, các phần phân
tích, danh sách `evidence_refs` cuối cùng và trace. Không được tự tạo, sửa đổi
hoặc dùng lại evidence ref giữa các case. Response lỗi hoặc bị thiếu vẫn được
giữ là missing evidence; không được thay bằng một sự kiện suy luận.

Policy Agent so sánh các record nguồn theo policy đang áp dụng. Khi các nguồn
mâu thuẫn, output phải ghi rõ field, toàn bộ source liên quan, source được chọn
(hoặc `null`) và resolution code. Conflict chưa giải quyết dẫn tới kết quả có
giới hạn như `conflicting`, `insufficient_evidence` hoặc
`needs_investigation`, tùy domain bị ảnh hưởng.

## 6. Chính sách lỗi và hiệu quả

| Lỗi | Ngân sách retry | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout | Tối đa 2 lần retry, có backoff giới hạn | Trả về missing evidence rõ ràng; không phỏng đoán | Chỉ emit `tool_result_consumed` cho result hợp lệ đã được dùng; lỗi được biểu diễn trong task status |
| Entity không tìm thấy hoặc mơ hồ | Không retry trừ khi có query candidate khác | Đánh dấu `not_found` hoặc `ambiguous`, hạ confidence và chuyển cho Policy/Verifier | `handoff` với `entity_unresolved` |
| Conflict giữa các nguồn | Không tự động retry với conflict dữ liệu thực sự | Áp dụng thứ tự ưu tiên của policy; giữ conflict trong `data_conflicts` | `policy_decided` với conflict decision code |
| Specialist result không hợp lệ | Một correction request có giới hạn | Từ chối result và dùng `insufficient_evidence` nếu không sửa được | `handoff` với `invalid_result` |

Collector sử dụng cache theo từng case, với key gồm tên tool và arguments đã
được chuẩn hóa. Evidence không được chia sẻ giữa các case. Specialist chỉ yêu
cầu domain cần thiết cho case và tránh quét rộng. Coordinator giới hạn mức
đồng thời theo năng lực runtime; mọi retry phải có giới hạn và idempotent.
Mọi MCP call đều tính vào efficiency, kể cả call có result không xuất hiện
trong output cuối.

## 7. Bất biến cần kiểm tra

Trước khi finalize, Verifier Agent kiểm tra:

- output đúng schema và `case_id` chính xác;
- entity đã resolve được hỗ trợ bằng evidence của case hiện tại;
- candidate bị reject không xuất hiện như entity đã resolve;
- mọi evidence ref trong output đều tồn tại, thuộc đúng team/run/case và được
  liên kết với claim hoặc phần phân tích;
- trace có đủ lifecycle event bắt buộc và đúng thứ tự;
- ngày shipment và trách nhiệm delay nhất quán;
- tổng captured, refunded, refundable và recommended refund đều không âm và
  nhất quán với nhau;
- thứ tự ưu tiên source và conflict chưa giải quyết được biểu diễn rõ ràng;
- assessment, root cause, responsibility và resolution action thống nhất;
- confidence nằm trong giới hạn và phản ánh phần evidence chưa chắc chắn;
- không có action trùng hoặc evidence ref trùng.

Nếu verification thất bại, Verifier gửi correction request có giới hạn tới
Coordinator hoặc Policy Agent. Verifier không tự động sửa các dữ kiện không có
evidence hỗ trợ. Chỉ output vừa hợp lệ về schema vừa liên kết đầy đủ với
evidence mới được phát hành làm end output.

## 8. Khả năng tái lập

Workflow được triển khai trong `src/student_agent/workflow.py` và chạy thông
qua CLI `day09`. Dependency được giới hạn trong `pyproject.toml`. Các lần chạy
lặp lại sử dụng cùng input case, danh sách tool đã discovery, policy version và
chiến lược request theo từng case. Mức đồng thời được giới hạn bởi Coordinator
và collector; retry có giới hạn và mang tính deterministic, ngoại trừ timestamp
và trace event ID. Output được kiểm tra bằng public contract trước khi ghi ra
file.

Các lệnh chạy:

```bash
day09 validate-inputs
day09 mcp-tools
day09 run
day09 validate
day09 package --output dist/submission.zip
```

Khóa API của team và các secret khác phải nằm trong `.env`, không được ghi vào tài
liệu kiến trúc, output, trace hoặc submission archive.
