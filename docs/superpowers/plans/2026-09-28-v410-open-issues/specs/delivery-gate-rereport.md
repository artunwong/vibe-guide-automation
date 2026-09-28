# Spec: #88 生产引擎交付证据门纳入可重报通道

node_id: delivery-gate-rereport
状态：published
审核：reviewed

输入：vibeguide_monitor 引擎下 developer 的 delivered/complete 事件，delivery_evidence 缺失或缺 completion_marker/delivery_path/thread_status

输出：worker 上报格式类缺失记 acceptance_rejected 纯审计、保留 handle 与 binding，可同会话重报；身份、worktree/branch、cursor 等绑定证据类缺失保持 blocked_unknown；补生产引擎排序测试

错误行为：判不清类别时按 fail-closed 处理，不得把未知当成功

验收示例：生产引擎 fixture：第一次上报缺 delivery_evidence → 节点非 blocked_unknown、事件含 acceptance_rejected；同会话补齐重报 → 节点进入交付后续流程；缺 task identity → 仍 blocked_unknown
