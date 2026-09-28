# Spec: #94 prd-guide §2 方法论引用补丁

node_id: prd-guide-methodology
状态：published
审核：reviewed

输入：prd-guide.md §2 五话题与决策收口

输出：§2 引用表列出 9 个 skill 的项目路径（customer-journey-map 标可选）及安装命令；使用规则：可参考不强制、未装则提示安装后继续、$ARGUMENTS 从对话读主题、中文输出、不执行另存 markdown、来源标记与 needs_confirmation 门不变

错误行为：不改 §5 字段形状，不改变 test_prd_guide_protocol 既有断言

验收示例：测试锚定引用表中 north-star-metric 行与“不得改变来源标记与停等门”那一行；既有 prd-guide 协议测试全绿
