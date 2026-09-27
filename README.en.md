# Vibe Coding Development Guide

[中文 README](README.md)

Vibe Guide is a local CLI for scanning a project, planning work, and running a recoverable development, independent review, rework, and acceptance flow under one explicit authorization. Technical delivery, review acceptance, push, merge, and deploy remain separate states; deploy is never included by default.

## Installation

The current release is **4.9.1**. Treat the matching `v4.9.1` tag, commit, and Release assets as the version source of truth; `dist/` is only a local build cache, not a download entry point. Release validation covers the wheel, source distribution, and source installation separately. Local builds only produce verification artifacts; they do not create tags or releases, push, merge, or deploy.

### Installation and initialization prerequisites

- **Runtime**: Python >= 3.9, zero third-party dependencies (standard library only); install with pip or pipx from wheel, sdist, or source (all three are pinned by tests to ship the protocol files).
- **Post-install verification**: `python3 -c "import vibe_guide; print(vibe_guide.__version__)"` and `vibe --help`.
- **Initialization order**: in a project, run read-only `vibe scan --json` first; run `vibe init --confirm` only when initialization is confirmed; AGENTS.md rule blocks are merged by `vibe apply-agentsmd --confirm` after human review.
- **No host-framework requirement**: the entry protocol is fully self-contained. When the host runs its own mandatory gate/status-line protocol (e.g. an alignment card), the S1 score merges into its first mandatory status line as an `S1：` field -- no second scoring pass and no second gate; otherwise the agent emits one standalone S1 line.
- **Upgrading existing projects**: materialized files (`.vibe/proposals/skills/*/SKILL.md` and already-merged AGENTS.md blocks) are never rewritten; a newer init prints a drift note when they differ from the shipped content, and a human merges manually after a manual diff.

### Installation protocol for Agents

When a user asks to install or upgrade, the Agent must:

1. Query the newest tag instead of trusting README text, `dist/` filenames, or memory:
   `git ls-remote --tags --sort='-v:refname' https://github.com/halfmoon82/vibe-guide-automation.git 'refs/tags/v*'`
2. Select the newest stable tag (currently `v4.9.1`) and verify that the tag, commit, and downloadable Release assets agree. If that cannot be verified, keep the result `unknown`.
3. Install that version and verify both:
   `python3 -c "import vibe_guide; print(vibe_guide.__version__)"`
   `vibe --help`
4. In the user-selected project, run the read-only `vibe scan --json` first. Run `vibe init --confirm --json` only after the user explicitly confirms initialization.
5. Report the initialization result and `.vibe/session-contract.json`. Package installation does not initialize a project or authorize monitor, push, merge, or deploy.

An Agent must not assume that downloading or installing the package automatically starts the wizard or writes to a project. Initialization is a separate, visible, traceable step.

Install the published wheel:

```bash
python3 -m pip install --user --upgrade vibe_guide-4.9.1-py3-none-any.whl
vibe --help
```

For an isolated environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/vibe --help
```

The package supports Python 3.9 and later paths covered by the repository's release contract. Installation does not change the system Python.

## Commands

```text
vibe scan                         Read-only project scan; does not create .vibe/
vibe init --confirm               Initialize the minimum project state after confirmation
vibe doctor                       Report observable environment, Skill, and Agent facts
vibe plan --request <request>     Run S0 and, when needed, explicit S1 and node-spec checks
vibe monitor --plan <ID>         Start only with exact authorization and valid evidence
vibe status --plan <ID>          Read the current snapshot without polling providers
vibe resume --plan <ID>          Continue from snapshots, task records, and event evidence
```

Every command supports `--json`. Exit codes are `0` for success, `2` for argument errors, `3` for confirmation or design blocking, and `4` for unknown external or runtime state.

New-session entry: the host agent self-screens S0/S1 in-session following `.vibe/proposals/skills/vibe-entry/SKILL.md` (materialized by `vibe init`, never rewritten). Requests scored <=8 are executed directly and 9-15 get a light plan, neither touching vibe; every scored request emits one S1 score-and-band line; only requests scored >15 (or uncertain) enter `vibe scan` + `vibe plan --request --s1`. The protocol is fully self-contained and needs no external skill.

## V4.2 engine evidence

Complex-plan publication records a real provider engine attestation containing the plan, revision, provider, content digest, and freshness checks. Monitor start and resume validate that evidence before dispatch. Missing, altered, mismatched, expired, future-dated, or symlinked plan/evidence files fail closed.

## V4.6 dispatch topology

Since V4.6, true DAG parallelism is carried by one visible worker session per node (Codex: `create_thread`, user-owned); the supervisor only dispatches, waits, and closes out, and is never the writer of any node. The task registry records each node's dispatch topology in its `topology` field:

- `visible-sdd`: one visible session per node running in-session SDD — a dev subagent implements while an independent-context, read-only review subagent audits (protocol: `vibe_guide/protocols/visible-sdd-worker.md`); rework and re-review close the loop inside the same session identity;
- `dual-visible`: the conservative default, with two distinct visible tasks for developer and reviewer; UNKNOWN platform evidence fails closed to this topology and never upgrades to `visible-sdd`;
- `background`: the explicit downgrade when a platform has no visible bridge. The downgrade and its limitations (not visible, not directly enterable, limited rework continuation) must be disclosed in the capability report, the authorization card, and the delivery; a `mode=background` worker without disclosure fails authorization-card validation.

Platform topology is ruled by the adapter registry's `DISPATCH_TOPOLOGY_MATRIX` from each platform's `in_session_sdd` probe evidence. `in_session_sdd` and `visible-sdd` live on different layers and are not interchangeable: the former is an adapter-layer name — both the manifest capability-probe field name and the ruling value in `DISPATCH_TOPOLOGY_MATRIX`; the latter is an enum value of the `topology` field (dispatch layer), translated from that ruling by the supervisor and describing how the node is actually dispatched; a passing probe does not by itself mean the node's topology is `visible-sdd` (the matrix ruling decides).

Concurrency cap: `max_active_worker_sessions` in `.vibe/config.json` caps simultaneously active worker sessions (default 5, valid range 1–64) and takes effect as the minimum with the authorization-card snapshot; an explicit but invalid value is a configuration error, never silently replaced. Accepted and archived sessions release capacity for later ready nodes.

## Governance boundaries

- `scan` is read-only.
- Existing `AGENTS.md` files are never overwritten; missing rules produce a proposal only (four rule blocks: capability truth, complex-request entry, `New Session Entry`, and `Engineering Principles`), merged by `vibe apply-agentsmd --confirm` after human review.
- Provider identity, login, task visibility, permissions, push, merge, and deploy are verified independently.
- Tests, `PASS`, or `CANMERGE` markers are not approval or release truth.
- Secrets, tokens, passwords, and private business data must not be stored in `.vibe/`, logs, plans, or chat.

See the Chinese README for the full workflow and governance contract.
