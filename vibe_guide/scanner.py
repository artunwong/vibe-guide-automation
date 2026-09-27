from dataclasses import dataclass, field
from pathlib import Path
import json
import re
import shutil
import subprocess
from typing import Dict, List, Optional

from .paths import ProjectPaths
from .skills import normalize_github_source, sanitize_git_url_for_display


_AGENT_COMMANDS = (
    'codex',
    'claude',
    'cursor',
    'grok',
    'workbuddy',
    'kimi',
    'deepseek',
)
_CONFIG_LIMIT = 64 * 1024
_SKILL_LIMIT = 64
_FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_SKILL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

CAPABILITY_RULE_MARKER = "Capability and Tool Truth"
CAPABILITY_RULES = """## Capability and Tool Truth

- 不得根据记忆、README、工具未被提及或一次失败判断能力不存在。
- “当前会话未暴露”不等于“平台不具备该能力”。
- 超时、空响应和格式异常统一保持 UNKNOWN；`unknown_timeout` 不得转成 UNAVAILABLE。
- 监工和 worker 的自然语言自报不是能力证据。
- 能力判断必须引用 session contract 的 evidence_ref。
- 没有证据时请求 refresh 或报告 UNKNOWN，不得直接终止。
- 只有 runtime/provider 的结构化结果才能进入能力阻断状态。
"""

PRD_GUIDE_MARKER = "Complex Request Entry"
PRD_GUIDE_RULES = """## Complex Request Entry

- 复杂请求先按 `.vibe/proposals/skills/prd-guide/SKILL.md` 的协议引导：agent 出 PRD 内容与节点拆分，vibe 校验并派生全部工程字段。
- 产品 spec 只写业务字段；工程字段由 `vibe plan --from-prd` 派生，写了会被拒。
- `needs_confirmation` 项未闭合不得发布；产品决策只有产品经理选定后才是 approved。
"""



@dataclass
class ScanReport:
    root: str
    python_version: str
    git_version: str
    git_root: Optional[str]
    git_remote: Optional[str]
    agentsmd_exists: bool
    agentsmd_content: Optional[str]
    knowledge_exists: bool
    vibe_exists: bool
    skills: List[dict]
    agent_commands: Dict[str, bool] = field(default_factory=dict)
    skill_records_error: Optional[str] = None


@dataclass
class PatchProposal:
    proposed: bool
    content: str


def _run(*args):
    try:
        completed = subprocess.run(
            args,
            text=True,
            capture_output=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ''
    if completed.returncode != 0:
        return ''
    return completed.stdout.strip()


def _configured_skills(vibe):
    config = vibe / 'config.json'
    if not config.exists():
        return [], None
    if config.is_symlink() or not config.is_file():
        return [], 'config is not a regular file'
    try:
        with config.open('rb') as handle:
            raw = handle.read(_CONFIG_LIMIT + 1)
    except OSError:
        return [], 'config unreadable'
    if len(raw) > _CONFIG_LIMIT:
        return [], 'config too large'
    try:
        document = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return [], 'config invalid'
    records = document.get('skills', []) if isinstance(document, dict) else None
    if not isinstance(records, list):
        return [], 'skills must be a list'
    if len(records) > _SKILL_LIMIT:
        return [], 'too many skill records'

    result = []
    for record in records:
        if not isinstance(record, dict):
            result.append(
                {
                    'name': '',
                    'source': '<invalid-source>',
                    'commit': '',
                    'valid': False,
                }
            )
            continue
        name = record.get('name') if isinstance(record.get('name'), str) else ''
        source = (
            record.get('source') if isinstance(record.get('source'), str) else ''
        )
        commit = (
            record.get('commit') if isinstance(record.get('commit'), str) else ''
        )
        try:
            canonical_source = normalize_github_source(source)
            source_valid = True
        except ValueError:
            canonical_source = sanitize_git_url_for_display(source)
            source_valid = False
        valid = bool(
            _SKILL_NAME.fullmatch(name)
            and _FULL_SHA.fullmatch(commit)
            and source_valid
        )
        result.append(
            {
                'name': name[:128],
                'source': canonical_source,
                'commit': commit.lower() if _FULL_SHA.fullmatch(commit) else '',
                'valid': valid,
            }
        )
    return result, None


def scan_project(paths):
    root = paths.root
    git_root = _run('git', '-C', str(root), 'rev-parse', '--show-toplevel') or None
    remote = (
        _run('git', '-C', str(root), 'config', '--get', 'remote.origin.url')
        or None
    )
    python_version = _run('python3', '--version')
    git_version = _run('git', '--version')
    agentsmd = root / 'AGENTS.md'
    vibe = root / '.vibe'
    agentsmd_exists = agentsmd.is_file() and not agentsmd.is_symlink()
    if not vibe.exists() or vibe.is_symlink() or not vibe.is_dir():
        skills, skill_records_error = [], 'invalid .vibe directory'
    else:
        skills, skill_records_error = _configured_skills(vibe)
    commands = {
        command: shutil.which(command) is not None for command in _AGENT_COMMANDS
    }
    return ScanReport(
        root=str(root),
        python_version=python_version,
        git_version=git_version,
        git_root=git_root,
        git_remote=sanitize_git_url_for_display(remote) if remote else None,
        agentsmd_exists=agentsmd_exists,
        agentsmd_content=(
            agentsmd.read_text(encoding='utf-8') if agentsmd_exists else None
        ),
        knowledge_exists=(
            (vibe / 'knowledge').is_dir()
            and not (vibe / 'knowledge').is_symlink()
        ),
        vibe_exists=vibe.is_dir() and not vibe.is_symlink(),
        skills=skills,
        agent_commands=commands,
        skill_records_error=skill_records_error,
    )


# The threshold prose inside this block (<=15 / >15) must stay in step with
# planner.ROUTE_SIMPLE_MAX_SCORE / ROUTE_LIGHT_PLAN_MAX_SCORE; the protocol
# threshold test derives its tokens from those constants and turns red on
# drift, forcing a sync here.
VIBE_ENTRY_RULE_MARKER = "New Session Entry"
VIBE_ENTRY_RULES = """## New Session Entry

- 开发/改动/排查类请求（「排查」含只读日志/生产数据分析）先按 `.vibe/proposals/skills/vibe-entry/SKILL.md` 的入口协议在会话内自评 S0/S1，不逢任务必过 vibe。
- 凡经 S1 评分的请求，评分后输出一行评分与档位（形如 `S1：11→轻规划（不触碰 vibe）`）；>15 或拿不准时才 `vibe scan` 并 `vibe plan --request --s1` 进入正式路由；<=15 直接执行或轻规划。
- 宿主另有强制入口/状态行协议时，S1 评分并入其首个强制状态行作为 `S1：` 字段输出，不另起一轮评分或第二个门。
- 会话门阻塞必须停下报告，不得伪造或跳过。
"""

#: Substring unique to the current entry-block wording.  The marker only
#: answers "a block is present"; this answers "it is the current block".
VIBE_ENTRY_CURRENT_SENTINEL = "首个强制状态行"

ENGINEERING_PRINCIPLES_MARKER = "Engineering Principles"
ENGINEERING_PRINCIPLES = """## Engineering Principles

- 改动旧接口、数据结构或行为前，先确认兼容要求；只有明确不需要兼容时才移除旧路径，不自行删除迁移或回退方案。
- 选择能满足当前需求的最简单实现；不为尚未出现的需求增加抽象、配置或间接层。
- 先做出能从头到尾跑通的最小版本，再逐步添加能力；不为尚未完成的复杂设计拆掉现有可用功能。
- 保持组件职责清楚，相关代码放在合适的位置。
- 成熟且维护良好的库能降低复杂度或提高可靠性时，优先使用；没有明确理由不重写常见功能。
- 写新实现或加新依赖前，先检查项目已有依赖的文档、类型和现成能力。
- 做架构选择时考虑后续维护，不用明知很快要推倒的临时方案糊弄过去。
- 设计方案前先看成熟产品怎样解决同类问题；适合当前需求时沿用已验证的做法。
"""

#: Substring unique to the current principles-block wording.  Same lesson as
#: VIBE_ENTRY_CURRENT_SENTINEL (PR #81): the marker proving "a block is
#: present" must itself be versioned, or an outdated block reads as current.
ENGINEERING_PRINCIPLES_CURRENT_SENTINEL = "不自行删除迁移或回退方案"

#: Every rule block this release ships, in document order.
AGENTSMD_BLOCKS = (
    CAPABILITY_RULES,
    PRD_GUIDE_RULES,
    VIBE_ENTRY_RULES,
    ENGINEERING_PRINCIPLES,
)


def missing_agentsmd_blocks(existing):
    '''Return the rule blocks an AGENTS.md still lacks, in document order.

    Each block is judged on its own marker.  A single combined check would
    make any later block unreachable for every project that already applied
    an earlier one.
    '''
    if existing is None:
        return list(AGENTSMD_BLOCKS)
    blocks = []
    has_capability_rules = (
        'Vibe Guide' in existing
        and 'project' in existing.lower()
        and CAPABILITY_RULE_MARKER in existing
        and 'evidence_ref' in existing
        and 'unknown_timeout' in existing
    )
    if not has_capability_rules:
        blocks.append(CAPABILITY_RULES)
    if PRD_GUIDE_MARKER not in existing:
        blocks.append(PRD_GUIDE_RULES)
    if VIBE_ENTRY_RULE_MARKER not in existing:
        blocks.append(VIBE_ENTRY_RULES)
    if ENGINEERING_PRINCIPLES_MARKER not in existing:
        blocks.append(ENGINEERING_PRINCIPLES)
    return blocks


def build_agentsmd_patch(existing, report):
    blocks = missing_agentsmd_blocks(existing)
    if not blocks:
        return PatchProposal(False, '')
    body = '\n'.join(blocks)
    if existing is None:
        content = '# Vibe Guide\n\nProject guidance is maintained through the Vibe Guide.\n\n' + body
    else:
        content = '# Vibe Guide capability contract proposal\n\n' + body
    return PatchProposal(
        True,
        content,
    )
