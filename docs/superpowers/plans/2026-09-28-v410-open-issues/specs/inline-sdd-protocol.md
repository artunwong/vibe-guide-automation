# Spec: #93 派发时把 visible-sdd 协议全文放进 worker 指令

node_id: inline-sdd-protocol
状态：published
审核：reviewed

输入：visible-sdd developer 的 create 派发

输出：create 请求 prompt 含随包协议全文；VISIBLE_SDD_PROTOCOL_REF 逐字不变仅作版本号；契约测试在非 vibeguide 的临时项目中断言 prompt 含协议正文

错误行为：随包协议读取失败时派发前 fail-closed，不创建会话

验收示例：临时消费项目中生成 visible-sdd create 请求 → request.prompt 包含协议 §1 标题与 §5 protocol 规则原文；在途 run 的 protocol 比对不变
