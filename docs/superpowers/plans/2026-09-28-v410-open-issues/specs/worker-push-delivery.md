# Spec: #91 worker 完工自报交付并唤醒监工

node_id: worker-push-delivery
状态：published
审核：reviewed

输入：worker 完工时的交付 payload

输出：CLI 子命令供 worker 自报交付：当场按现有交付门做格式校验并把错误返回给 worker，合法则落盘供监工下一次 resume 消费（幂等）；派发指令追加完工步骤：先自报落盘→查询监工地址→发固定格式唤醒信号；监工侧校验门位置不变、不信任信号内容；无推送能力时退化为纯拉取

错误行为：自报格式错误当场报错不落盘；查不到监工地址时仍落盘并依赖心跳兜底；重复自报幂等

验收示例：worker 自报缺 completion_marker → 命令非零退出且不落盘；合法自报后 resume 一次即消费；同 payload 重复自报不产生第二个事件；在 Claude Code 上实测一次 worker→监工唤醒（结果登记为实测或未验证）
