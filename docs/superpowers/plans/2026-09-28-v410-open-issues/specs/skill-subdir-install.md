# Spec: #92 阶段 0：skill 安装命令（subdir）+ 物化进项目 + config 登记

node_id: skill-subdir-install
状态：published
审核：reviewed

输入：GitHub 源、完整 commit SHA、可选 subdir、skill 名

输出：新 CLI 子命令（需 --confirm）按 SHA 拉取、subdir 归档、校验根 SKILL.md、物化到 .vibe/proposals/skills/<名>/、写 .vibe/config.json 记录（含 subdir）；scan 解析并校验 subdir；README 说明

错误行为：subdir 含 ..、绝对路径、首尾 /、非法字符或符号链接 → pending，不落盘不写记录；网络失败 → pending；已存在同名不覆盖

验收示例：对无根 SKILL.md 的本地 fixture 仓库 + 合法 subdir → installed 且 scan valid；subdir=../x → pending 无落盘；不带 subdir 的既有安装测试逐字通过；同仓两个 subdir → 两条记录
