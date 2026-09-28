# Spec: #92 阶段 2：visible-sdd worker 协议加入中文审查方法与严重度映射

node_id: review-methodology
状态：published
审核：reviewed

输入：visible-sdd-worker.md §1.2 与 pm-ai-shipping code-review/intended-vs-implemented 方法论（只读参考，不复制原文）

输出：协议含：约定→可行执行→反驳后才成 finding；节点合同即 intent；未反驳不计 P0–P2；未知落 blocked_unknown；fan-out 上限一层；严重度映射表；完整 skill 以项目主目录 .vibe/proposals/skills/<名>/SKILL.md 作可参考、读不到时按硬规则继续

错误行为：不改 protocol 标识、不改 §5 交付字段

验收示例：测试锚定协议中映射表 Refutation 行与“未完成反驳不得计入 P0–P2”那一行；VISIBLE_SDD_PROTOCOL_REF 相关测试全部不变通过
