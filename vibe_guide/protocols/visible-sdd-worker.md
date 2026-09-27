# Vibe Guide · 可见 SDD worker 会话协议（visible-sdd topology）

> 本文件随 vibe 包发布，面向被监工派发的 worker 会话（宿主 agent）。
> 当一个 DAG 节点以 `visible-sdd` topology 绑定到单个可见会话时，该会话内的开发/审查流程按本协议执行；监工只读收口证据，不进入会话内代劳。

## 1. 固定流程（双角色，同一会话身份）

worker 会话收到节点合同后，必须在**同一会话身份**内按固定顺序推进，不得更换会话、不得跳过角色：

```text
dev 子代理实现 → review 子代理审查 →（存在 P0–P2）返工回 dev 子代理 → review 子代理复审 → P0–P2 清零后收口
```

1. **dev 子代理**：按节点合同实现，只写文件白名单内的文件，完成后跑合同列明的定向验证。
2. **review 子代理**：**独立上下文**、**只读**、**非作者视角**审查 dev 的 diff，按 P0（资金/数据正确性）、P1（逻辑缺陷/事务边界）、P2（边界条件/行为回归）、P3（建议）分级产出意见。review 子代理不得是 dev 子代理本人，不得复用 dev 的实现上下文。
3. **返工**：存在 P0–P2 时逐条核实（reviewer 意见也要验证，不盲从）→ 由 dev 子代理修复 → 重跑定向验证（须为绿）。
4. **复审**：返工后回到 review 子代理复审，保留审查身份与证据链；最多 3 轮，超限仍存 P0–P2 时转 blocked 并上报监工。
5. **收口**：某轮复审无 P0–P2 且验证绿后，按节点合同收口（提交/开 PR 等以授权卡为准）。

## 2. review 子代理只读红线（git 写禁令清单）

派给 review 子代理的任务提示必须**显式禁止一切 git 写操作**。以下命令清单一字不差写进提示，review 子代理不得执行：

- `git checkout` / `git switch`：禁止（不得切换分支或检出文件）
- `git reset`：禁止（任何模式）
- `git clean`：禁止
- `git stash`：禁止
- 以及一切改变工作区、索引或引用的命令（`git add` / `git commit` / `git push` / `git merge` / `git rebase` / `git cherry-pick` 等）

跨分支查看只允许只读形式：

```bash
git show <ref>:<path>     # 看其他分支上的文件内容
git diff <ref>...<ref>    # 看分支间差异
```

审查所需的一切材料由 worker 会话在派发提示中直接给出（diff、文件内容、验证输出），review 子代理不应需要任何写操作。

## 3. 证据链写回

- 每一轮**实现结论**（dev：改了什么、验证命令与结果）、**审查结论**（review：P0–P3 分级意见）、**返工结论**（逐条处置：接受修复/驳回理由、复验结果）都必须写入本会话的交付记录，不得只留在子代理内部。
- 会话交付中每轮证据需可区分轮次（第 1 轮审查、第 1 轮返工、第 2 轮复审……），旧证据保留，新证据追加。
- 监工负责把会话交付中的结论**收口进 `.vibe/runs/<run-id>/events.jsonl`**；worker 会话不直接改写 events.jsonl，除非节点合同明确授权。
- 无法验证的项记录为 `blocked_unknown`，不得伪造完成。

## 4. reviewer 独立性违规

- review 子代理**代改业务代码**（任何对白名单文件的写入）= **违规**。
- 发现违规时：该轮审查结论作废，节点转 **blocked**，并在会话交付中**记录**违规事实（谁、改了哪些文件、时间）供监工收口。
- 违规后不得以"改得对"为由追认；修复只能由 dev 子代理完成、由 review 子代理复审。

## 5. 交付 payload 契约（监工校验字段）

worker 会话的交付事件由监工**先校验、后落账**：以下字段缺失或形状非法时，本次交付被记为
`acceptance_rejected`（纯审计事件，不入生命周期），会话 handle 保留，监工在同一会话上继续等待——
**worker 修正后重报即可，run 不会因此砖化**。只有节点合同 digest 漂移（篡改/合同被改）仍然
fail-closed，不可重报。

交付事件必须携带两块结构：

1. **`in_session_review`（本会话内 SDD 审查证据，必需）**：

   ```json
   {
     "protocol": "vibe_guide/protocols/visible-sdd-worker.md",
     "evidence_ref": "<本轮审查证据的非空引用，如 会话#轮次>",
     "clearance": {"p0": 0, "p1": 0, "p2": 0}
   }
   ```

   - `protocol` 必须逐字等于本文件路径（`vibe_guide/protocols/visible-sdd-worker.md`），引用别的协议
     等于这次交付没有经过本协议规定的会话内审查；
   - `clearance` 三个键都必须是整数 `0`（布尔不算）——P0–P2 未清零就返工，不要交付；
   - `evidence_ref` 必须是非空字符串，指向第 3 节写入交付记录的那轮审查结论。

2. **`delivery_evidence`（交付证据门，引擎要求时必需）**：一个嵌套对象，含
   `completion_marker`、`delivery_path`、`thread_status`（只认 `complete` / `completed` /
   `DELIVERED`）。摊平成顶层三个字段不算，门读不到。

被拒后如何重报：按上面的形状补齐字段，在同一会话里再次交付即可；旧的 `acceptance_rejected`
记录保留在事件日志里作为审计痕迹，不阻塞后续 acceptance。

## 6. 与本协议无关的事项

- 本协议不定义节点合同格式、授权卡签发或监工调度，那些由 vibe 核心与 prd-guide 协议负责。
- deploy、release、生产数据变更永远不在 worker 会话权限内，无论审查结论如何。
