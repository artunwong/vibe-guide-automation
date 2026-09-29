from dataclasses import dataclass, field
from pathlib import Path
import os
import json, tempfile

from .scanner import (
    AGENTSMD_BLOCKS,
    ENGINEERING_PRINCIPLES_CURRENT_SENTINEL,
    ENGINEERING_PRINCIPLES_MARKER,
    VIBE_ENTRY_CURRENT_SENTINEL,
    VIBE_ENTRY_RULE_MARKER,
    build_agentsmd_patch,
    missing_agentsmd_blocks,
    resolve_rules_file,
    scan_project,
)
from .protocols import (
    PRD_GUIDE_NAME,
    PRD_GUIDE_PROPOSAL_RELATIVE,
    VIBE_ENTRY_NAME,
    VIBE_ENTRY_PROPOSAL_RELATIVE,
    load_protocol,
)
from .capability_contract import build_contract, contract_path, load_contract, save_contract
from .migration import migrate_v2_to_v310, migrate_v2_to_v42, _backup, _payload_is_complete
from .workflow_gate import V42_STATE, is_v42_sdd_first_state
from .config import (
    DEFAULT_MAX_ACTIVE_WORKER_SESSIONS,
    FIELD_MAX_ACTIVE_WORKER_SESSIONS,
)

# Newer rule blocks land here when a reviewed proposal.md already exists; both
# the writer (init) and the reader (apply-agentsmd) name it from here.
PENDING_UPDATE_NAME = "proposal.pending-update.md"
# Headings already put to the reviewer, so a section they removed is not
# offered again.  Without it, "missing from AGENTS.md" is the only signal init
# has, and it cannot tell a proposal that predates a section from a reviewer
# who deleted it -- so every re-init re-offers what they already declined.
OFFERED_SECTIONS_NAME = "proposal.offered.json"

#: Heading text of every shipped rule block; the apply gate accepts any of
#: them so a new block can never be stranded by a gate written before it.
_KNOWN_RULE_MARKERS = tuple(
    block.splitlines()[0].strip().lstrip("#").strip() for block in AGENTSMD_BLOCKS
)


@dataclass
class InitResult:
    changed: bool
    paths: list
    #: Things the user should know but that did not stop initialization, such as
    #: a state file that could not be read.  Kept out of `paths`, which lists
    #: what was written.
    notes: list = field(default_factory=list)


def migrate_project(source, destination):
    """Initializer-facing entry point for explicit, backup-first upgrades."""
    return migrate_v2_to_v42(source, destination)


def _is_within(root, path):
    try:
        return os.path.commonpath(
            (str(root), str(path.resolve(strict=False)))
        ) == str(root)
    except (OSError, ValueError):
        return False


def _validate_initialization_paths(root):
    root = root.resolve()
    if not root.is_dir():
        raise ValueError('project root must be a directory')
    directories = (
        root / '.vibe',
        root / '.vibe' / 'knowledge',
        root / '.vibe' / 'proposals',
        root / '.vibe' / 'proposals' / 'agentsmd',
        root / '.vibe' / 'proposals' / 'skills',
        root / '.vibe' / 'proposals' / 'skills' / 'prd-guide',
    )
    files = (
        root / '.vibe' / 'config.json',
        root / '.vibe' / 'state.json',
        root / '.vibe' / 'session-contract.json',
        root / '.vibe' / 'proposals' / 'agentsmd' / 'proposal.md',
        root / '.vibe' / 'proposals' / 'skills' / 'proposal.md',
        root / PRD_GUIDE_PROPOSAL_RELATIVE,
    )
    for path in directories:
        if not _is_within(root, path):
            raise ValueError('initialization path escapes project root')
        if path.is_symlink():
            raise ValueError('initialization directory must not be a symlink')
        if path.exists() and not path.is_dir():
            raise ValueError('initialization directory is not a directory')
    for path in files:
        if not _is_within(root, path):
            raise ValueError('initialization file escapes project root')
        if path.is_symlink():
            raise ValueError('initialization file must not be a symlink')
        if path.exists() and not path.is_file():
            raise ValueError('initialization file is not a regular file')


def _write_new(path, content):
    with path.open('x', encoding='utf-8') as handle:
        handle.write(content)


def _migrate_state(path):
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise ValueError('state.json is invalid') from error
    if not isinstance(data, dict):
        raise ValueError('state.json must be an object')
    if data == {}:
        _atomic_write(path, {"workflow_version": 2})
        return True
    if is_v42_sdd_first_state(data):
        return False
    if data.get('workflow_version') not in (2, 3, 3.1, 3.10):
        raise ValueError('state.json legacy version is unknown')
    # Preserve the complete legacy snapshot and a verified backup before the
    # new state becomes visible. Unknown legacy fields are never discarded.
    evidence = path.parent / 'migration-evidence.json'
    import hashlib
    source_bytes = json.dumps(data, ensure_ascii=False, sort_keys=True).encode('utf-8')
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    if evidence.exists():
        try:
            recorded = json.loads(evidence.read_text(encoding='utf-8'))
            backup = Path(recorded['backup_path'])
            manifest = recorded['backup_manifest']
            valid = (
                isinstance(recorded, dict)
                and recorded.get('target_version') == '4.2.0'
                and recorded.get('source_state') == data
                and recorded.get('source_sha256') == source_sha256
                and isinstance(manifest, dict)
                and json.loads((backup / 'manifest.json').read_text(encoding='utf-8')) == manifest
                and _payload_is_complete(backup / 'payload', manifest.get('files'))
            )
            if not valid:
                raise ValueError('legacy migration evidence is unverifiable')
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            raise ValueError('legacy migration evidence is unverifiable') from error
    else:
        project_root = path.parent.parent
        backup, manifest = _backup(project_root, project_root.parent)
        if not _payload_is_complete(backup / 'payload', manifest['files']):
            raise ValueError('legacy backup failed integrity check')
        _atomic_write(evidence, {
            'source': str(project_root),
            'destination': str(project_root),
            'source_state': data,
            'source_sha256': source_sha256,
            'target_state': V42_STATE,
            'target_version': '4.2.0',
            'backup_path': str(backup),
            'backup_manifest': manifest,
        })
    data = dict(V42_STATE)
    _atomic_write(path, data)
    return True


def _atomic_write(path, data):
    """Replace `path` with `data` serialized as JSON."""
    _atomic_write_text(
        path, json.dumps(data, ensure_ascii=False, sort_keys=True) + '\n'
    )


def _atomic_write_text(path, text):
    """Replace `path` with `text` verbatim.

    Prose and already-serialized payloads go through here: handing them to the
    JSON writer stores a quoted, escaped copy of the text instead of the text,
    which turns a proposal into an unreadable one-line string.
    """
    descriptor, temporary_name = tempfile.mkstemp(prefix=f'.{path.name}.', dir=str(path.parent))
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(text); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def init_project(paths, confirm):
    if not confirm:
        return InitResult(False, [])
    root = Path(paths.root).resolve()
    _validate_initialization_paths(root)
    _migrate_state(root / '.vibe' / 'state.json')
    report = scan_project(paths)
    created = []
    notes = []
    for relative in (
        '.vibe',
        '.vibe/knowledge',
        '.vibe/proposals',
        '.vibe/proposals/agentsmd',
        '.vibe/proposals/skills',
    ):
        path = root / relative
        if not path.exists():
            path.mkdir()
            if relative in ('.vibe/knowledge', '.vibe/proposals/agentsmd', '.vibe/proposals/skills'):
                created.append(relative)
    default_config = json.dumps(
        {FIELD_MAX_ACTIVE_WORKER_SESSIONS: DEFAULT_MAX_ACTIVE_WORKER_SESSIONS},
        ensure_ascii=False, sort_keys=True,
    ) + '\n'
    for relative in ('.vibe/config.json', '.vibe/state.json'):
        path = root / relative
        if not path.exists():
            # Existing files are never rewritten: a config that predates a
            # newer field keeps its bytes and the reader applies defaults.
            _write_new(path, (json.dumps(V42_STATE, ensure_ascii=False, sort_keys=True) + '\n') if relative == '.vibe/state.json' else default_config)
            created.append(relative)
    capability_target = contract_path(paths)
    if capability_target.exists():
        load_contract(paths)
    else:
        runtime_status = 'verified_available' if report.python_version and report.git_version else 'probe_failed'
        facts = {
            'runtime.exec': {'status': runtime_status, 'scope': 'init', 'route': 'runtime.exec' if runtime_status == 'verified_available' else '', 'evidence_ref': 'init:scan:runtime'},
            'task.terminal': {'status': 'unknown', 'scope': 'task', 'route': '', 'evidence_ref': 'init:unobserved:task.terminal'},
            'task.browser.control': {'status': 'unknown', 'scope': 'task', 'route': '', 'evidence_ref': 'init:unobserved:task.browser.control'},
            'task.visible_session': {'status': 'unknown', 'scope': 'task', 'route': '', 'evidence_ref': 'init:unobserved:task.visible_session'},
        }
        save_contract(paths, build_contract(Path(paths.root), facts=facts))
        created.append('.vibe/session-contract.json')
    proposal = build_agentsmd_patch(report.agentsmd_content, report)
    proposal_path = root / '.vibe/proposals/agentsmd/proposal.md'
    if proposal.proposed:
        existing_proposal = None
        if proposal_path.is_file() and not proposal_path.is_symlink():
            try:
                existing_proposal = proposal_path.read_text(encoding='utf-8')
            except (OSError, UnicodeDecodeError):
                existing_proposal = None
        if existing_proposal is None:
            _write_new(proposal_path, proposal.content)
            created.append(str(proposal_path.relative_to(root)))
            # Record the first proposal's own headings too: a section deleted
            # from it has been declined just as plainly as one deleted from an
            # increment, and only this record can tell that from a proposal
            # written before the section existed.
            offered_path = proposal_path.with_name(OFFERED_SECTIONS_NAME)
            if _record_offered(
                offered_path,
                {
                    section.splitlines()[0].strip()
                    for section in _proposal_sections(proposal.content)
                },
            ):
                created.append(str(offered_path.relative_to(root)))
        else:
            # An existing proposal is awaiting human review, and a reviewer may
            # have deliberately deleted a section they do not want.  Rewriting
            # it in place would restore that section and discard their notes, so
            # a proposal that predates a newer rule block goes to a side file
            # and the reviewed bytes are left alone.
            offered_path = proposal_path.with_name(OFFERED_SECTIONS_NAME)
            offered, damaged = _offered_headings(offered_path)
            if damaged:
                notes.append(damaged)
            # Every question here is answered by a section's own heading, and
            # the proposal is asked about itself.  Matching a whole block
            # verbatim would read a reviewer's edit as the section going
            # missing, so it would become pending again and rewrite the
            # increment, discarding whatever still awaited review; borrowing the
            # AGENTS.md gate would answer with that file's extra tokens, so a
            # proposal holding the section would still be offered it -- and a
            # section sitting in both files is past the reviewer's reach, since
            # deleting it from the proposal no longer declines it.
            already_proposed = {
                section.splitlines()[0].strip()
                for section in _proposal_sections(existing_proposal)
            }
            pending_blocks = [
                block for block in missing_agentsmd_blocks(report.agentsmd_content)
                if block.splitlines()[0].strip() not in already_proposed
                and block.splitlines()[0].strip() not in offered
            ]
            # Everything the reviewer has now seen, whether it reached them
            # through the proposal or through the increment.  Recording only what
            # the increment carried would leave the proposal's own sections
            # unrecorded, so deleting one would read as "this release added a
            # section" and offer it straight back.
            if _record_offered(
                offered_path,
                offered | already_proposed
                | {block.splitlines()[0].strip() for block in pending_blocks},
            ):
                created.append(str(offered_path.relative_to(root)))
            if pending_blocks:
                update_path = proposal_path.with_name(PENDING_UPDATE_NAME)
                update = (
                    '# 提案增量（本次未合入 proposal.md）\n\n'
                    '现有 proposal.md 正在等待人工评审，可能已被有意修改，因此不改写它。\n'
                    '以下小节是当前版本新增、proposal.md 里还没有的内容。\n'
                    'vibe apply-agentsmd --confirm 会连同本文件一起合入 AGENTS.md；\n'
                    '不想采纳某一节就在这里删掉它。\n\n'
                    + '\n'.join(pending_blocks)
                )
                if not update_path.exists():
                    _write_new(update_path, update)
                    created.append(str(update_path.relative_to(root)))
                elif update_path.is_file() and not update_path.is_symlink():
                    if update_path.read_text(encoding='utf-8') != update:
                        _atomic_write_text(update_path, update)
                        created.append(str(update_path.relative_to(root)))
    skill_proposal = root / '.vibe/proposals/skills/proposal.md'
    if not skill_proposal.exists() and not any(item.get('valid') and item.get('name') == 'architecture-skill-pack' for item in report.skills):
        _write_new(
            skill_proposal,
            '# Skill proposal\n\n'
            '- name: architecture-skill-pack\n'
            '- source: https://github.com/lov-team/architecture-skill-pack\n'
            '- source_status: remote\n',
        )
        created.append(str(skill_proposal.relative_to(root)))
    # The PRD-guide protocol is vibe's own, shipped with the package; it is
    # proposed into the project so the host agent finds it, and never
    # rewritten once present so user edits survive re-initialization.
    prd_guide = root / PRD_GUIDE_PROPOSAL_RELATIVE
    if not prd_guide.exists():
        prd_guide.parent.mkdir(parents=True, exist_ok=True)
        _write_new(prd_guide, load_protocol(PRD_GUIDE_NAME))
        created.append(PRD_GUIDE_PROPOSAL_RELATIVE)
    # The session-entry protocol shares prd-guide's semantics: proposed once,
    # never rewritten, so user edits survive re-initialization.
    vibe_entry = root / VIBE_ENTRY_PROPOSAL_RELATIVE
    if not vibe_entry.exists():
        vibe_entry.parent.mkdir(parents=True, exist_ok=True)
        _write_new(vibe_entry, load_protocol(VIBE_ENTRY_NAME))
        created.append(VIBE_ENTRY_PROPOSAL_RELATIVE)
    elif vibe_entry.is_file() and not vibe_entry.is_symlink():
        try:
            current_entry = vibe_entry.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            current_entry = None
        if current_entry is None:
            notes.append(
                VIBE_ENTRY_PROPOSAL_RELATIVE
                + ' 读不出来，本次跳过一致性比对；vibe 永不改写该文件，请人工核对。'
            )
        elif current_entry != load_protocol(VIBE_ENTRY_NAME):
            # The copy is never rewritten, so a difference stays silent
            # forever unless surfaced here.  The cause is ambiguous -- a
            # local edit or a shipped-protocol update -- so the note says
            # both and leaves the merge to a human.
            notes.append(
                VIBE_ENTRY_PROPOSAL_RELATIVE
                + ' 与随包协议不一致：可能是本地修改，也可能是随包协议已更新；'
                + 'vibe 永不改写该文件，请人工 diff 后手动合并。'
            )
    # The AGENTS.md entry block has the same silent-staleness gap: marker
    # detection only answers "a block is present", never "it is current",
    # and the heading-level dedup never re-proposes a same-named section.
    if (
        report.agentsmd_content
        and VIBE_ENTRY_RULE_MARKER in report.agentsmd_content
        and VIBE_ENTRY_CURRENT_SENTINEL not in report.agentsmd_content
    ):
        notes.append(
            'AGENTS.md 的「' + VIBE_ENTRY_RULE_MARKER + '」块与当前版本不一致：'
            '可能是旧版块，也可能是本地改写；init 不会重复提案同名小节，'
            '.vibe/proposals/agentsmd/ 的既有提案也可能同样是旧版。'
            '请人工对照包内最新块（python -c "from vibe_guide.scanner import VIBE_ENTRY_RULES; print(VIBE_ENTRY_RULES)"）手动更新。'
        )
    if (
        report.agentsmd_content
        and ENGINEERING_PRINCIPLES_MARKER in report.agentsmd_content
        and ENGINEERING_PRINCIPLES_CURRENT_SENTINEL not in report.agentsmd_content
    ):
        notes.append(
            'AGENTS.md 的「' + ENGINEERING_PRINCIPLES_MARKER + '」块与当前版本不一致：'
            '可能是旧版块，也可能是本地改写；init 不会重复提案同名小节，'
            '.vibe/proposals/agentsmd/ 的既有提案也可能同样是旧版。'
            '请人工对照包内最新块（python -c "from vibe_guide.scanner import ENGINEERING_PRINCIPLES; print(ENGINEERING_PRINCIPLES)"）手动更新。'
        )
    return InitResult(bool(created), created, notes)


def _offered_headings(path):
    """Return the section headings already put to the reviewer.

    Returns the headings plus a note when the record could not be read.  An
    unreadable record reads as "nothing offered yet": the cost is re-offering a
    section, which the reviewer can decline again, whereas treating it as
    "everything offered" would silently withhold new rules.  The note is what
    keeps that fallback from being silent -- otherwise a declined section
    reappears with nothing on screen to explain why.
    """
    if path.is_symlink() or not path.is_file():
        return frozenset(), None
    damaged = '{} 读不出来，本次按“尚未提出过任何小节”处理；已被删除的小节可能会再次出现在增量里。'.format(path.name)
    try:
        recorded = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, ValueError):
        return frozenset(), damaged
    if not isinstance(recorded, dict):
        return frozenset(), damaged
    headings = recorded.get('offered_headings')
    if not isinstance(headings, list):
        return frozenset(), damaged
    return frozenset(item for item in headings if isinstance(item, str)), None


def _record_offered(path, headings):
    """Persist the headings the reviewer has now seen.

    Returns whether the file changed, so `init` can report the write.  An
    unchanged record is left alone: this runs on every pass, and rewriting
    identical bytes would report a change on a run that decided nothing.
    """
    payload = json.dumps(
        {'offered_headings': sorted(headings)}, ensure_ascii=False, indent=2
    ) + '\n'
    if not path.exists():
        _write_new(path, payload)
        return True
    if not path.is_file() or path.is_symlink():
        return False
    try:
        if path.read_text(encoding='utf-8') == payload:
            return False
    except (OSError, UnicodeDecodeError):
        pass
    _atomic_write_text(path, payload)
    return True


def _read_proposal(path):
    """Return a proposal file's text, or None when there is nothing to read.

    Both the reviewed proposal and the increment beside it are files a human
    may have edited, so each gets the same bounds: a regular file, within the
    size limit, valid UTF-8.
    """
    if path.is_symlink() or not path.is_file():
        return None
    raw = path.read_bytes()
    if len(raw) > 64 * 1024:
        raise ValueError("%s exceeds the size bound" % path.name)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("%s is not valid UTF-8" % path.name) from error


def _proposal_sections(proposal):
    """Split a reviewed proposal into its `## ` sections, heading included.

    Sections are the unit of application: each one is appended or skipped on
    its own so a project that already carries an earlier section can still
    receive a later one.  Any preamble before the first heading is dropped,
    since it only titles the proposal document.

    A `## ` inside a fenced block is example text, not a boundary.  This
    protocol's own rules are written with fenced examples, so splitting on one
    would tear the fence apart and strand its closing line -- and everything
    after it -- in a section whose heading AGENTS.md may already carry.  A fence
    closes only on the same marker that opened it (` ``` ` or `~~~`, at least as
    long), and one left unclosed is treated as a typo rather than as a reason to
    swallow every heading after it.
    """
    lines = proposal.splitlines(keepends=True)
    boundaries = set()
    fence = None
    for index, line in enumerate(lines):
        marker = _fence_marker(line)
        if fence is None:
            if marker:
                fence = marker
            elif line.startswith("## "):
                boundaries.add(index)
        elif marker and marker[0] == fence[0] and len(marker) >= len(fence):
            fence = None
    if fence is not None:
        # The document ends inside a fence, so the opening line was a typo.
        # Honouring it would drop every section after it without a word.
        boundaries = {
            index for index, line in enumerate(lines) if line.startswith("## ")
        }
    sections = []
    current = []
    for index, line in enumerate(lines):
        if index in boundaries:
            if current:
                sections.append("".join(current))
            current = [line]
        elif current:
            current.append(line)
    if current:
        sections.append("".join(current))
    return [section for section in (item.strip("\n") for item in sections) if section.strip()]


def _fence_marker(line):
    """Return a code-fence marker (`` ``` `` or `~~~`) opening or closing a block."""
    stripped = line.lstrip()
    for char in ("`", "~"):
        if stripped.startswith(char * 3):
            return char * (len(stripped) - len(stripped.lstrip(char)))
    return ""


def _rules_target(root):
    """Pick the rule file `apply-agentsmd` appends to.

    A project already carrying a host-loaded rule file gets the rules there,
    so a WorkBuddy project's CODEBUDDY.md -- the only file its host reads --
    actually receives them.  A project with none still gets AGENTS.md, which
    is what every host loads and what older releases always wrote.
    """
    return root / (resolve_rules_file(root) or "AGENTS.md")


def apply_agentsmd_proposal(paths, confirm):
    """Apply the generated capability rules only after explicit confirmation.

    Initialization remains proposal-only.  This separate operation preserves
    the existing AGENTS.md bytes and appends the reviewed proposal exactly
    once, so the rules become part of the supervisor's project context without
    silently overwriting user instructions.
    """
    if not confirm:
        return InitResult(False, [])
    root = Path(paths.root).resolve()
    if not root.is_dir():
        raise ValueError("project root must be a directory")
    proposal_path = root / ".vibe" / "proposals" / "agentsmd" / "proposal.md"
    proposal = _read_proposal(proposal_path)
    if proposal is None:
        raise ValueError("AGENTS.md proposal is missing or not a regular file")
    increment = _read_proposal(proposal_path.with_name(PENDING_UPDATE_NAME))
    # Accept every shipped block, from proposal and increment alike: a project
    # that applied (or declined) the older blocks gets a proposal whose only
    # section is the newest one, and a gate keyed to legacy markers would
    # strand it.  Deriving from AGENTSMD_BLOCKS keeps the gate in step with
    # whatever this release ships.
    offered = "\n".join(part for part in (proposal, increment) if part)
    if not offered.strip() or not any(
        marker in offered for marker in _KNOWN_RULE_MARKERS
    ):
        raise ValueError("AGENTS.md proposal does not contain capability rules")
    # A project that reviewed an earlier proposal keeps those bytes, so newer
    # rule blocks were written beside it rather than into it.  Applying only
    # proposal.md would leave that increment on disk forever: the rule would
    # never reach AGENTS.md and nothing would say so.  Section-level dedup
    # below makes reading both safe.
    target = _rules_target(root)
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise ValueError(target.name + " must be a regular file")
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    # Append only the sections this AGENTS.md still lacks, judging each by its
    # own heading.  Keying the whole append on one marker would strand every
    # later section on any project that already applied an earlier one.
    sections = _proposal_sections(proposal)
    if increment:
        seen = {section.splitlines()[0].strip() for section in sections}
        sections += [
            section for section in _proposal_sections(increment)
            if section.splitlines()[0].strip() not in seen
        ]
    pending = [
        section for section in sections
        if section.splitlines()[0].strip() not in existing
    ]
    if not pending:
        return InitResult(False, [])
    proposal = "\n\n".join(pending) + "\n"

    if existing:
        separator = "" if existing.endswith("\n") else "\n"
        content = existing + separator + "\n" + proposal.lstrip("\n")
    else:
        content = proposal
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="." + target.name + ".", dir=str(root)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(target))
    finally:
        if temporary.exists():
            temporary.unlink()
    return InitResult(True, [target.name])
