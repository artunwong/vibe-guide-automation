# Spec: #80 删除不可达的 upgrade_project 分支与 upgrade.py

node_id: remove-dead-upgrade
状态：published
审核：reviewed

输入：cli.py 中 install/upgrade 分发块之后不可达的 upgrade 分支与 upgrade.py shim

输出：死分支、import 与 upgrade.py 删除；vibe upgrade 行为不变

错误行为：若发现任何可达调用方则停止删除并报告

验收示例：grep upgrade_project 全仓为零；vibe upgrade --json 输出与删除前一致；全量测试绿
