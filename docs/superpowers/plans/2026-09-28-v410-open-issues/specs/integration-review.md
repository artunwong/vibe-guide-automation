# Spec: Final integration review

node_id: integration-review
状态：published
审核：reviewed

输入：all business deliveries, review/rework evidence, and aggregate diff

输出：integration review report: a claim with exactly the keys findings, iteration_compatibility, test_runtime_delivery, out_of_scope (severity p0-p4, only resolved clears; p3/p4 are observations that never count into the clearance)

错误行为：malformed claims are rejected as acceptance_rejected for re-report on the same session; contract drift, unknown, out-of-scope changes, or uncleared findings block acceptance

验收示例：all required evidence is present and P0/P1/P2 clearance is zero
