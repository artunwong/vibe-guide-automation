# 开发计划：v410-open-issues

版本：1

状态：confirmed_pending_authorization

PRD：.vibe/plans/v410-open-issues/prd.md

DAG 审计：blocked_dag

可启动节点：暂无

## 节点

- delivery-gate-rereport [blocked]
- remove-dead-upgrade [blocked]
- inline-sdd-protocol [blocked]
- skill-subdir-install [blocked]
- review-methodology [blocked]
- prd-guide-methodology [blocked]
- supervisor-registry [blocked]
- worker-push-delivery [blocked]
- integration-review [blocked]

## 审计理由

- delivery-gate-rereport：node delivery-gate-rereport contract is missing risk_tags
- remove-dead-upgrade：node remove-dead-upgrade contract is missing risk_tags；parallel_group 'wave-1': nodes remove-dead-upgrade and skill-subdir-install have overlapping write scope (vibe_guide/cli.py); relabel integration_after or split the group；parallel_group 'wave-1': nodes remove-dead-upgrade and supervisor-registry have overlapping write scope (vibe_guide/cli.py); relabel integration_after or split the group；parallel_group 'wave-1': nodes remove-dead-upgrade and worker-push-delivery have overlapping write scope (vibe_guide/cli.py); relabel integration_after or split the group
- inline-sdd-protocol：node inline-sdd-protocol contract is missing risk_tags；parallel_group 'wave-1': nodes inline-sdd-protocol and skill-subdir-install have overlapping write scope (README.en.md, README.md); relabel integration_after or split the group；parallel_group 'wave-1': nodes inline-sdd-protocol and supervisor-registry have overlapping write scope (README.md); relabel integration_after or split the group；parallel_group 'wave-1': nodes inline-sdd-protocol and worker-push-delivery have overlapping write scope (vibe_guide/runners/provider_action.py); relabel integration_after or split the group
- skill-subdir-install：node skill-subdir-install contract is missing risk_tags；parallel_group 'wave-1': nodes remove-dead-upgrade and skill-subdir-install have overlapping write scope (vibe_guide/cli.py); relabel integration_after or split the group；parallel_group 'wave-1': nodes inline-sdd-protocol and skill-subdir-install have overlapping write scope (README.en.md, README.md); relabel integration_after or split the group；parallel_group 'wave-1': nodes skill-subdir-install and supervisor-registry have overlapping write scope (README.md, vibe_guide/cli.py); relabel integration_after or split the group；parallel_group 'wave-1': nodes skill-subdir-install and worker-push-delivery have overlapping write scope (vibe_guide/cli.py); relabel integration_after or split the group
- review-methodology：node review-methodology contract is missing risk_tags；parallel_group 'wave-1': nodes review-methodology and worker-push-delivery have overlapping write scope (vibe_guide/protocols/visible-sdd-worker.md); relabel integration_after or split the group
- prd-guide-methodology：node prd-guide-methodology contract is missing risk_tags；parallel_group 'wave-1': nodes prd-guide-methodology and supervisor-registry have overlapping write scope (vibe_guide/protocols/prd-guide.md); relabel integration_after or split the group
- supervisor-registry：node supervisor-registry contract is missing risk_tags；parallel_group 'wave-1': nodes remove-dead-upgrade and supervisor-registry have overlapping write scope (vibe_guide/cli.py); relabel integration_after or split the group；parallel_group 'wave-1': nodes inline-sdd-protocol and supervisor-registry have overlapping write scope (README.md); relabel integration_after or split the group；parallel_group 'wave-1': nodes skill-subdir-install and supervisor-registry have overlapping write scope (README.md, vibe_guide/cli.py); relabel integration_after or split the group；parallel_group 'wave-1': nodes prd-guide-methodology and supervisor-registry have overlapping write scope (vibe_guide/protocols/prd-guide.md); relabel integration_after or split the group；parallel_group 'wave-1': nodes supervisor-registry and worker-push-delivery have overlapping write scope (vibe_guide/cli.py); relabel integration_after or split the group
- worker-push-delivery：node worker-push-delivery contract is missing risk_tags；parallel_group 'wave-1': nodes remove-dead-upgrade and worker-push-delivery have overlapping write scope (vibe_guide/cli.py); relabel integration_after or split the group；parallel_group 'wave-1': nodes inline-sdd-protocol and worker-push-delivery have overlapping write scope (vibe_guide/runners/provider_action.py); relabel integration_after or split the group；parallel_group 'wave-1': nodes skill-subdir-install and worker-push-delivery have overlapping write scope (vibe_guide/cli.py); relabel integration_after or split the group；parallel_group 'wave-1': nodes review-methodology and worker-push-delivery have overlapping write scope (vibe_guide/protocols/visible-sdd-worker.md); relabel integration_after or split the group；parallel_group 'wave-1': nodes supervisor-registry and worker-push-delivery have overlapping write scope (vibe_guide/cli.py); relabel integration_after or split the group
- integration-review：hard dependencies not complete (accepted or delivered): delivery-gate-rereport, remove-dead-upgrade, inline-sdd-protocol, skill-subdir-install, review-methodology, prd-guide-methodology, supervisor-registry, worker-push-delivery
