# Spec: #91 监工本地预检、地址登记与换班

node_id: supervisor-registry
状态：published
审核：reviewed

输入：run、当前监工会话身份（provider、会话 id、host）与该会话的本地会话记录

输出：① 本地预检命令：只读磁盘，输出三选一 idle（worker 在干活且无新请求）/ work（有待服务请求或 worker 已完工）/ rotate（本会话上下文超过约 6 万 token，读自身会话记录得出；阈值可配）；② CLI 登记/查询 run 的当前监工地址（落 .vibe/runs/<run>/，原子写，换班保留历史）；③ prd-guide §6 写监工纪律：每次心跳第一步只跑预检，idle 只回一字结束，work 按原流程并只从磁盘读状态，rotate 执行换班——新开会话写简短交接（进度在磁盘）、新会话登记自身地址、自建心跳、置顶、改标题，删除旧心跳，旧会话归档自己；Claude Code 以清空自己代替换班；心跳降级为兜底且不得删除（换班时是移交不是删除）

错误行为：会话记录读不到或解析失败时预检返回 unknown（不得当作 idle），监工按 work 处理；未登记时查询返回 unknown 而非空成功；登记内容不含凭据

验收示例：fixture 会话记录 token 用量 70k → rotate；30k 且无 pending、worker 活跃 → idle；有 pending create → work；记录缺失 → unknown；登记 A 后换班登记 B → 查询得 B 且历史含 A
