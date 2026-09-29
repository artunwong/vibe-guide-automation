"""DAG validation, ready-node scheduling, and plan artifact rendering."""

from dataclasses import dataclass, replace
import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
import tempfile
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

# INTEGRATION_REVIEW_NODE_ID lives in models so Plan can validate against it.
# dag imports models, so importing it back from dag would be circular; it is
# re-exported here to keep every `from .dag import INTEGRATION_REVIEW_NODE_ID`
# working unchanged.
from .models import (
    INTEGRATION_REVIEW_NODE_ID,
    DAGNode,
    Plan,
    node_branch,
    node_worktree,
)
from .path_ownership import normalize_project_path


INTEGRATION_REVIEWER_ID = "integration-reviewer"


def is_integration_review_node(node: DAGNode) -> bool:
    """Return whether *node* is the reserved final integration reviewer node."""
    return isinstance(node, DAGNode) and node.id == INTEGRATION_REVIEW_NODE_ID


def _integration_nodes(plan: Plan) -> List[DAGNode]:
    return [node for node in (getattr(plan, "nodes", []) or []) if is_integration_review_node(node)]


INTEGRATION_REVIEW_SCOPE_LIMIT = 256


def integration_review_scope(business_nodes: Sequence[DAGNode]) -> List[str]:
    """The files the final reviewer may read: every business node's, deduped.

    The node owns nothing -- it aggregates -- so its `allowlist` stays empty.
    But dispatch derives a worker profile from `contract["files"]` and
    `validate_child_session_binding` refuses an empty allowlist, so a node with
    no files can never be handed to a reviewer session and every complex run
    stalls on it.  The union is also the honest scope: this reviewer reads the
    whole delivery.

    Entries go through `normalize_project_path`, the same predicate the rest of
    the package uses, and unusable ones are dropped rather than passed through:
    one business node naming `/etc/passwd` must not make this node
    undispatchable again.  Normalising before deduping matters because the union
    is a new list that no validator has seen -- `src/x.ts` and `./src/x.ts` each
    pass on their own node and collide here, and
    `authorization._normalize_files` rejects that collision by refusing to
    publish the plan at all.  `allowlist` is an unvalidated channel
    (`DAGNode.__post_init__` only checks for non-empty strings), so this is the
    only place those spellings are caught.

    The union also has to stay inside that validator's 256-item bound, which
    each node's own list respects but their union need not.  Past the bound the
    scope collapses to the distinct top-level directories: a coarser honest
    scope, rather than a truncated one that would quietly hide files from the
    reviewer.

    An empty union returns empty and the caller omits the key, since `"."` is
    not a legal `files` entry.  `append_integration_review_node` handles that
    case by giving the node an explicit `worker_profile` instead.
    """
    scope: List[str] = []
    for node in business_nodes:
        contract = getattr(node, "contract", None) or {}
        for item in list(contract.get("files") or []) + list(getattr(node, "allowlist", []) or []):
            if not isinstance(item, str):
                continue
            try:
                normalized = normalize_project_path(item)
            except ValueError:
                continue
            if normalized.startswith("~"):
                # `normalize_project_path` keeps a leading `~`; a home-relative
                # path is not project-relative.
                continue
            if normalized not in scope:
                scope.append(normalized)
    if len(scope) > INTEGRATION_REVIEW_SCOPE_LIMIT:
        roots: List[str] = []
        for item in scope:
            root = PurePosixPath(item).parts[0]
            if root not in roots:
                roots.append(root)
        if len(roots) > INTEGRATION_REVIEW_SCOPE_LIMIT:
            # Cutting the list here would put back the very hole coarsening
            # exists to avoid, one level up and losing whole subtrees, while
            # still producing a well-formed contract.  There is no third level
            # to collapse to: `"."` is not a legal `files` entry.
            raise ValueError(
                "integration review scope spans {} top-level directories, over "
                "the {} the authorization contract allows; split the plan".format(
                    len(roots), INTEGRATION_REVIEW_SCOPE_LIMIT
                )
            )
        scope = roots
    return scope


def append_integration_review_node(plan: Plan) -> Plan:
    """Append the deterministic, read-only integration review node to complex plans."""
    if not isinstance(plan, Plan):
        raise TypeError("plan is required")
    if plan.complexity_band != "complex":
        return plan
    existing = _integration_nodes(plan)
    if existing:
        raise ValueError("plan already contains an integration review node")
    # Import lazily to keep the planner/DAG modules independently importable.
    from .planner import build_integration_acceptance_contract

    business_nodes = [node for node in plan.nodes if not is_integration_review_node(node)]
    projected_contract = build_integration_acceptance_contract(plan)
    contract = dict(projected_contract)
    contract.update({
        "input": "all business deliveries, review/rework evidence, and aggregate diff",
        # The claim schema is named here because this contract is the only
        # material the dispatched reviewer is guaranteed to read (ISSUE-83);
        # the full prose contract lives in prd-guide "整合审查节点的 accepted".
        "output": "integration review report: a claim with exactly the keys "
                  "findings, iteration_compatibility, test_runtime_delivery, "
                  "out_of_scope (severity p0-p4, only resolved clears; p3/p4 "
                  "are observations that never count into the clearance)",
        "error_behavior": "malformed claims are rejected as acceptance_rejected "
                          "for re-report on the same session; contract drift, "
                          "unknown, out-of-scope changes, or uncleared findings "
                          "block acceptance",
        "acceptance_example": "all required evidence is present and P0/P1/P2 clearance is zero",
        "risk_tags": ["integration", "read-only"],
        "read_only": True,
        "reviewer": INTEGRATION_REVIEWER_ID,
        "allowlist": [],
        # Who runs it, under the three names dispatch reads, all the ones
        # `complete_node_contracts` gives a business node.  Like every node this
        # one is dispatched twice -- developer first, reviewer after delivery --
        # and each key carries a different path, so dropping any one of them
        # sends a bogus writer identity out (all three verified by ablation):
        #   `worker`           -> the developer pass; `monitor.start` seeds the
        #                         node state's worker from it and `_start_task`
        #                         reads that back for non-reviewer roles.
        #   `reviewer_worker`  -> the reviewer pass; `_start_task` overwrites
        #                         `contract["worker"]` from it for that role.
        #   `writer`           -> the empty-union branch below, whose injected
        #                         `worker_profile` dispatch uses verbatim for
        #                         both passes; without this key that profile's
        #                         own default ships the placeholder `"worker"`.
        "worker": INTEGRATION_REVIEWER_ID,
        "writer": INTEGRATION_REVIEWER_ID,
        "reviewer_worker": INTEGRATION_REVIEWER_ID,
    })
    # What this reviewer may read.  `allowlist` above stays empty because it
    # writes nothing; `files` is what dispatch reads to build the worker
    # profile, and without it the node is undispatchable (see
    # `integration_review_scope`).
    review_scope = integration_review_scope(business_nodes)
    if review_scope:
        contract["files"] = review_scope
    else:
        # No business node names a file -- a legal spec, since
        # `complete_node_contracts` only setdefaults `files`.  Supply the same
        # two things a business node has in that case: an empty `files` and a
        # `worker_profile` scoped to the whole project.
        #
        # Both halves are load-bearing.  `"."` cannot go in `files`
        # (`_normalize_files` rejects it, so the plan would not publish), and it
        # cannot be left to the profile alone either: `_start_task` setdefaults
        # `files` from `worker_profile.allowlist`, so an absent `files` key
        # becomes `["."]` at dispatch and `validate_runtime_contract` refuses it.
        # The empty list is what keeps that setdefault from firing -- which is
        # exactly why business nodes survive this input, their `["."]` allowlist
        # never reaches that validator.
        contract["files"] = []
        contract["worker_profile"] = {
            "worker": str(contract.get("worker", "worker")),
            "model": "default",
            "reasoning": "normal",
            "fallbacks": [],
            "selection_basis": {
                "issue_complexity_ref": INTEGRATION_REVIEW_NODE_ID,
                "complexity_band": "standard",
                "risk_tags": ["integration", "read-only"],
                "availability_evidence": "configured",
            },
            "writer": str(contract.get("writer", "worker")),
            # The node's own tree, from the same derivation
            # `complete_node_contracts` uses for business nodes.  Not
            # `contract.get("worktree", ...)`: that key does not exist yet (the
            # monitor setdefaults it later), so the fallback would always fire
            # and protocol §6.3 has the platform open the session right there --
            # in the main working tree, on a branch vibe never generates.
            "worktree": node_worktree(INTEGRATION_REVIEW_NODE_ID),
            "branch": node_branch(INTEGRATION_REVIEW_NODE_ID),
            "allowlist": ["."],
        }
    # Keep the synthetic integration reviewer on the same verified adapter
    # route as the business nodes so authorization can enforce one binding.
    for node in business_nodes:
        adapter_id = str(node.contract.get("adapter_id", "")).strip()
        if adapter_id:
            contract["adapter_id"] = adapter_id
            break
    # And on the same project.  `task_binding` refuses a visible contract with
    # no `project_id`, so without this the node is rejected before its session
    # is created.  Copied from a business node rather than synthesised: the
    # value only exists when the attested capabilities reported one, and
    # inventing it would route a session at a project nobody verified.
    #
    # Two different values is fail-closed for that same reason.  A spec may
    # declare its own `project_id` on one node and inherit the attested default
    # on another (`complete_node_contracts` setdefaults), and picking either one
    # gives a reviewer that cannot read half the delivery while still being able
    # to report P0-P2 cleared.
    project_ids = []
    for node in business_nodes:
        project_id = str(node.contract.get("project_id", "")).strip()
        if project_id and project_id not in project_ids:
            project_ids.append(project_id)
    if len(project_ids) > 1:
        raise ValueError(
            "business nodes disagree on project_id ({}); the integration "
            "reviewer cannot span projects".format(", ".join(sorted(project_ids)))
        )
    if project_ids:
        contract["project_id"] = project_ids[0]
    integration = DAGNode(
        INTEGRATION_REVIEW_NODE_ID,
        "Final integration review",
        [node.id for node in business_nodes],
        [],
        "integration",
        contract,
        "planned",
        risk_tags=["integration", "read-only"],
        reviewer=INTEGRATION_REVIEWER_ID,
        owned_paths=[],
        allowlist=[],
    )
    return replace(
        plan,
        node_ids=list(plan.node_ids) + [integration.id],
        nodes=list(plan.nodes) + [integration],
    )


def validate_integration_review_node(plan: Plan) -> "DAGValidation":
    """Validate the integration node's uniqueness, scope, lineage and reviewer isolation."""
    if not isinstance(plan, Plan):
        raise TypeError("plan is required")
    if plan.complexity_band != "complex":
        return DAGValidation(True, ())
    nodes = list(plan.nodes or [])
    integration = _integration_nodes(plan)
    errors: List[str] = []
    if len(integration) != 1:
        errors.append("complex plan must contain exactly one integration review node")
        return DAGValidation(False, tuple(errors))
    node = integration[0]
    business = [item for item in nodes if not is_integration_review_node(item)]
    business_ids = [item.id for item in business]
    contract_error = _contract_error(node)
    if contract_error:
        errors.append(contract_error)
    if node.depends_on != business_ids:
        errors.append("integration review depends_on must equal all business node IDs in plan order")
    if node.owned_paths:
        errors.append("integration review node must not own business paths")
    if node.allowlist:
        errors.append("integration review node must have an empty write allowlist")
    if node.contract.get("allowlist"):
        errors.append("integration review contract must have an empty write allowlist")
    explicit_reviewer = node.reviewer
    contract_reviewer = node.contract.get("reviewer")
    if explicit_reviewer and contract_reviewer and explicit_reviewer != contract_reviewer:
        errors.append("integration review reviewer mismatch between node and contract")
    reviewer = explicit_reviewer or contract_reviewer
    business_reviewers = {item.reviewer or item.contract.get("reviewer") for item in business}
    business_writers = {item.writer or item.contract.get("writer") for item in business}
    if reviewer != INTEGRATION_REVIEWER_ID:
        errors.append("integration review reviewer must be the independent integration reviewer")
    if reviewer in business_reviewers or reviewer in business_writers:
        errors.append("integration review reviewer must not be reused by a business node")
    if node.contract.get("read_only") is not True:
        errors.append("integration review contract must be read-only")
    try:
        from .planner import build_integration_acceptance_contract
        expected = build_integration_acceptance_contract(plan)
        if node.contract.get("digest") != expected.get("digest"):
            errors.append("integration review contract digest mismatch")
    except (TypeError, ValueError) as exc:
        errors.append("integration review contract is invalid: {}".format(exc))
    return DAGValidation(not errors, tuple(dict.fromkeys(errors)))


@dataclass(frozen=True)
class DAGValidation:
    valid: bool
    errors: Tuple[str, ...]


@dataclass(frozen=True)
class PlanArtifacts:
    dag_path: Path
    plan_path: Path


@dataclass(frozen=True)
class DAGAuditResult:
    """Evidence-bounded result of auditing a plan's executable DAG."""

    status: str
    ready_nodes: List[str]
    blocked_nodes: List[str]
    reasons: Dict[str, List[str]]
    parallel_groups: Optional[Dict[str, List[str]]] = None

    def __post_init__(self):
        if self.status not in {"ready", "blocked_design", "blocked_dag", "blocked_unknown"}:
            raise ValueError("unsupported DAG audit status")
        object.__setattr__(self, "ready_nodes", list(self.ready_nodes))
        object.__setattr__(self, "blocked_nodes", list(self.blocked_nodes))
        object.__setattr__(self, "reasons", {
            str(key): list(value) for key, value in self.reasons.items()
        })
        object.__setattr__(self, "parallel_groups", {
            str(key): list(value) for key, value in (self.parallel_groups or {}).items()
        })

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "ready_nodes": list(self.ready_nodes),
            "blocked_nodes": list(self.blocked_nodes),
            "reasons": {key: list(value) for key, value in self.reasons.items()},
            "parallel_groups": {key: list(value) for key, value in self.parallel_groups.items()},
        }


_CONTRACT_FIELDS = (
    ("input", "inputs"),
    ("output", "outputs"),
    ("error_behavior", "errors"),
    ("acceptance_example", "acceptance_examples"),
)


def _has_content(value: object) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return bool(value) and all(_has_content(key) and _has_content(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return bool(value) and all(_has_content(item) for item in value)
    return bool(value)


def _has_value(contract: Mapping[str, object], names: Sequence[str]) -> bool:
    return any(name in contract and _has_content(contract[name]) for name in names)


def _contract_error(node: DAGNode) -> str:
    contract = node.contract
    if not isinstance(contract, Mapping):
        return "node {} contract must be a mapping".format(node.id)
    if contract.get("design_change") or contract.get("status") == "blocked_design":
        return "node {} is blocked by a design change".format(node.id)
    missing = [names[0] for names in _CONTRACT_FIELDS if not _has_value(contract, names)]
    if missing:
        return "node {} contract is missing {}".format(node.id, ", ".join(missing))
    return ""


def _metadata_sources(node: DAGNode, name: str) -> List[Tuple[str, Any]]:
    """Return explicit, legacy, and authoritative metadata claims."""
    contract = node.contract if isinstance(node.contract, Mapping) else {}
    profile = contract.get("worker_profile")
    profile_value = profile.get(name) if isinstance(profile, Mapping) else None
    return [
        ("explicit", getattr(node, name, None)),
        ("contract", contract.get(name)),
        ("worker_profile", profile_value),
    ]


def _node_metadata(node: DAGNode, name: str) -> Any:
    for _source, value in _metadata_sources(node, name):
        if value not in (None, "", []):
            return value
    return None


def _audit_contract_errors(node: DAGNode) -> List[str]:
    """Validate executable contract and its writer identity claims."""
    errors: List[str] = []
    contract_error = _contract_error(node)
    if contract_error:
        errors.append(contract_error)
    contract = node.contract if isinstance(node.contract, Mapping) else {}
    aliases = {
        "input": ("input", "inputs"),
        "output": ("output", "outputs"),
        "error_behavior": ("error_behavior", "errors"),
        "acceptance_examples": ("acceptance_example", "acceptance_examples"),
    }
    for label, names in aliases.items():
        if not _has_value(contract, names):
            errors.append("node {} contract is missing {}".format(node.id, label))

    profile = contract.get("worker_profile")
    if profile is not None and not isinstance(profile, Mapping):
        errors.append("node {} worker_profile must be a mapping".format(node.id))
        profile = None
    if isinstance(profile, Mapping):
        for field_name in ("writer", "allowlist"):
            if field_name not in profile or not _has_content(profile[field_name]):
                errors.append("node {} worker_profile is missing {}".format(node.id, field_name))

    risk_tags = _node_metadata(node, "risk_tags")
    if not isinstance(risk_tags, list) or not risk_tags or not all(
        isinstance(item, str) and item.strip() for item in risk_tags
    ):
        errors.append("node {} contract is missing risk_tags".format(node.id))
    writer = _node_metadata(node, "writer")
    if not isinstance(writer, str) or not writer.strip():
        errors.append("node {} contract is missing writer".format(node.id))
    worktree = _node_metadata(node, "worktree")
    if not isinstance(worktree, str) or not worktree.strip():
        errors.append("node {} contract is missing worktree".format(node.id))
    allowlist = _node_metadata(node, "allowlist")
    if not isinstance(allowlist, list) or not allowlist or not all(
        isinstance(item, str) and item.strip() for item in allowlist
    ):
        errors.append("node {} contract is missing allowlist".format(node.id))
    elif any(Path(item).is_absolute() or ".." in Path(item).parts for item in allowlist):
        errors.append("node {} allowlist escapes the project".format(node.id))

    # If multiple representations are present, they must make the same claim.
    for name in ("risk_tags", "writer", "worktree", "allowlist"):
        populated = [
            (source, value) for source, value in _metadata_sources(node, name)
            if value not in (None, "", [])
        ]
        for index, (left_source, left_value) in enumerate(populated):
            for right_source, right_value in populated[index + 1:]:
                if left_value != right_value:
                    errors.append("node {} {} mismatch ({} vs {})".format(
                        node.id, name, left_source, right_source
                    ))
    if contract.get("design_change") or contract.get("status") == "blocked_design":
        errors.append("node {} is blocked by a design change".format(node.id))
    return list(dict.fromkeys(errors))


def _structural_errors(nodes: List[DAGNode]) -> List[str]:
    errors = []
    identifiers = [node.id for node in nodes]
    if len(identifiers) != len(set(identifiers)):
        errors.append("DAG node IDs must be unique")

    node_ids = set(identifiers)
    graph = {}
    for node in nodes:
        graph[node.id] = list(node.depends_on)
        missing = [dependency for dependency in node.depends_on if dependency not in node_ids]
        if missing:
            errors.append("node {} has unknown hard dependencies: {}".format(node.id, ", ".join(missing)))

    visiting = set()
    visited = set()

    def visit(node_id: str) -> bool:
        if node_id in visiting:
            return True
        if node_id in visited:
            return False
        visiting.add(node_id)
        for dependency in graph.get(node_id, []):
            if dependency in graph and visit(dependency):
                return True
        visiting.remove(node_id)
        visited.add(node_id)
        return False

    if any(visit(node_id) for node_id in graph if node_id not in visited):
        errors.append("DAG hard dependencies contain a cycle")
    return errors


def validate_dag(nodes: List[DAGNode]) -> DAGValidation:
    errors = _structural_errors(nodes)
    errors.extend(error for node in nodes for error in (_contract_error(node),) if error)
    return DAGValidation(not errors, tuple(errors))


def ready_nodes(nodes: List[DAGNode]) -> List[DAGNode]:
    if _structural_errors(nodes):
        return []
    by_id: Dict[str, DAGNode] = {node.id: node for node in nodes}
    ready = []
    for node in nodes:
        if node.status not in ("planned", "ready"):
            continue
        if _contract_error(node):
            continue
        if all(by_id[dependency].status == "accepted" for dependency in node.depends_on):
            ready.append(node)
    return ready


def dependency_closure(nodes: Sequence[DAGNode], blocked_ids: Sequence[str]) -> set:
    """Return the hard-dependency descendants of blocked nodes, including roots.

    ``integration_after`` is deliberately excluded: it is review/coordination
    metadata and never makes a node part of a blocked execution closure.
    """
    by_id = {node.id: node for node in nodes}
    closure = {node_id for node_id in blocked_ids if node_id in by_id}
    changed = True
    while changed:
        changed = False
        for node in nodes:
            if node.id in closure:
                continue
            if any(dep in closure for dep in node.depends_on):
                closure.add(node.id)
                changed = True
    return closure


def node_scoped_ready(
    nodes: Sequence[DAGNode], blocked_ids: Optional[Sequence[str]] = None
) -> List[str]:
    """Compute ready node IDs while isolating blocked dependency closures.

    Only ``depends_on`` is consulted. Repair/wait states therefore consume
    their existing identity/capacity but do not suppress unrelated ready nodes.

    This is also the runtime hard gate for the parallel-group audit: nodes
    carrying ``_parallel_group_errors`` violations (overlapping write scope,
    artifact references, or unverifiable metadata inside their group) are
    never dispatch-eligible.  ``audit_dag`` reports the same violations with
    ``blocked_dag`` reasons; the rendered dag.yaml is evidence, not the
    enforcement point.
    """
    nodes = list(nodes)
    if _structural_errors(nodes):
        return []
    by_id = {node.id: node for node in nodes}
    closure = dependency_closure(nodes, blocked_ids or ())
    group_blocked = set(_parallel_group_errors(nodes))
    ready: List[str] = []
    for node in nodes:
        if node.id in closure or node.id in group_blocked or node.status not in ("planned", "ready"):
            continue
        if _contract_error(node):
            continue
        if all(
            dependency in by_id
            and by_id[dependency].status == "accepted"
            for dependency in node.depends_on
        ):
            ready.append(node.id)
    return ready


def _cycle_nodes(nodes: List[DAGNode]) -> List[str]:
    """Return nodes participating in hard-dependency cycles."""
    graph = {node.id: list(node.depends_on) for node in nodes}
    state: Dict[str, int] = {}
    stack: List[str] = []
    found = set()

    def visit(node_id: str) -> None:
        state[node_id] = 1
        stack.append(node_id)
        for dependency in graph.get(node_id, []):
            if dependency not in graph:
                continue
            if state.get(dependency, 0) == 0:
                visit(dependency)
            elif state.get(dependency) == 1:
                try:
                    found.update(stack[stack.index(dependency):])
                except ValueError:
                    found.add(dependency)
        stack.pop()
        state[node_id] = 2

    for node_id in graph:
        if state.get(node_id, 0) == 0:
            visit(node_id)
    return sorted(found)


# V4.8: CJK connectives 和/或/与/及 added as token delimiters.
_PATH_TOKEN_PATTERN = re.compile(r"[^\s，。；、：:\"'()（）\[\]<>《》〈〉“”‘’「」『』和或与及]+")
# A file path glued to prose without whitespace (``reports/测试输出.json供下游``)
# still ends where its extension ends: one or more ``/``-separated segments,
# then a final segment with an ASCII extension that is not itself continued by
# alphanumerics, ``_``/``-`` (``spec.md_v2``), another ``.ext`` (``spec.md.bak``)
# or ``/``; the continuation class mirrors ``_references_path`` so both sides
# agree on where a path ends.  Segments may hold non-ASCII characters so CJK
# file names survive.  Only the tail is cut: prose glued *before* a path, or
# after an extension-less directory path (``docs/specs后续``), is undecidable
# and the token is left whole rather than guessed (it then matches nothing).
_PATH_SPAN_PATTERN = re.compile(r"^(?:[^/]+/)+[^/]+?\.[A-Za-z0-9]{1,10}(?![A-Za-z0-9_/-]|\.[A-Za-z0-9])")


def _write_scope_paths(node: DAGNode) -> Optional[List[str]]:
    """Return the node's normalized write scope, or None when it is unverifiable.

    The scope is the union of the write allowlist and owned paths; ``"."``
    (project root) is kept as a root marker that overlaps everything.  Missing
    or malformed metadata returns None so the parallel-group audit can fail
    closed instead of guessing whether two writers collide.
    """
    allowlist = _node_metadata(node, "allowlist")
    if allowlist is None:
        return None
    if not isinstance(allowlist, (list, tuple)) or not all(
        isinstance(item, str) and item.strip() for item in allowlist
    ):
        return None
    raw = [str(item).strip() for item in allowlist]
    contract = node.contract if isinstance(node.contract, Mapping) else {}
    for source in (getattr(node, "owned_paths", None), contract.get("owned_paths")):
        if source is None:
            continue
        if not isinstance(source, list) or not all(
            isinstance(item, str) and item.strip() for item in source
        ):
            return None
        raw.extend(str(item).strip() for item in source)
    normalized: List[str] = []
    for item in raw:
        if item == ".":
            candidate = item
        else:
            try:
                candidate = normalize_project_path(item)
            except ValueError:
                return None
        if candidate not in normalized:
            normalized.append(candidate)
    return normalized


def _write_paths_overlap(left: str, right: str) -> bool:
    if left == "." or right == ".":
        return True
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def _contract_texts(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [text for item in value.values() for text in _contract_texts(item)]
    if isinstance(value, (list, tuple)):
        return [text for item in value for text in _contract_texts(item)]
    return []


def _path_like_token(token: str) -> Optional[str]:
    """Normalize a whitespace-delimited token that names a project path.

    CJK prose carries no whitespace, so the token may continue past the path
    (``reports/测试输出.json供下游校验``); ``_PATH_SPAN_PATTERN`` cuts the
    token at the file extension when one is present.  Tokens without an
    extension (``docs/specs``) are taken whole as before.
    """
    # Trailing sentence punctuation only: a leading dot (`.vibe/...`) is part
    # of the path and must survive normalization.
    candidate = token.strip().rstrip(",;.")
    if not candidate or candidate == ".":
        return None
    # Free-text tokens only count as paths when they name a directory path.
    # Bare tokens with a dot ("v1.2", "SKILL.md") are usually versions or
    # filenames mentioned in prose, not project paths; authoritative file
    # identity belongs to the declared write scope.
    if "/" not in candidate:
        return None
    span = _PATH_SPAN_PATTERN.match(candidate)
    if span:
        candidate = span.group(0)
    try:
        return normalize_project_path(candidate)
    except ValueError:
        return None


def _references_path(text: str, path: str) -> bool:
    """Return whether *text* references *path* as a whole path token.

    A bare substring check would let ``docs/spec.md`` match
    ``docs/spec.md.bak``, so the path must be delimited by characters that
    cannot extend its spelling.  ``/`` stays legal *after* the match: a
    directory in the write scope covers references to files inside it
    (``docs/specs`` must match a reference to ``docs/specs/v2.md``), while
    ``.``/alphanumerics still reject ``docs/spec.md.bak``.  ``/`` before the
    match stays rejected so ``xdocs/spec.md`` prefix attacks fail.
    """
    pattern = r"(?<![A-Za-z0-9._/-])" + re.escape(path) + r"(?![A-Za-z0-9._-])"
    return re.search(pattern, text) is not None


def _produced_paths(node: DAGNode, scope: Sequence[str]) -> List[str]:
    """Paths a node may produce: its write scope plus paths named in outputs.

    The write scope itself counts as produced output: a sibling whose contract
    references a file inside it plans to consume (or at least read) what this
    writer may be rewriting, which is a soft dependency either way.
    """
    produced = list(scope)
    contract = node.contract if isinstance(node.contract, Mapping) else {}
    for key in ("output", "outputs"):
        for text in _contract_texts(contract.get(key)):
            for token in _PATH_TOKEN_PATTERN.findall(text):
                normalized = _path_like_token(token)
                if normalized and normalized not in produced:
                    produced.append(normalized)
    return produced


def _parallel_group_errors(nodes: List[DAGNode]) -> Dict[str, List[str]]:
    """Audit intra-group soft dependencies before parallel dispatch.

    Two nodes may share a parallel group only when their write scopes are
    verifiably disjoint and neither contract consumes a path the other
    produces.  Overlap, artifact references, or unverifiable metadata refuse
    the grouping: the plan must relabel the relation as ``integration_after``
    or split the group.

    Membership covers every status that holds or will hold a writer
    (``planned``/``ready``/``running``/``rework``/``review``/``brief_pending``):
    a ``running`` sibling keeps writing, so pairing only planned/ready nodes
    would let a freshly dispatched node collide with an in-flight writer.
    ``delivered``/``accepted`` nodes no longer write and are exempt.  Reasons
    are only attached to dispatch-eligible (``planned``/``ready``) members:
    in-flight work is not retro-blocked, but nothing new may start alongside
    a conflicting or unverifiable sibling.
    """
    _WRITER_HOLDING = ("planned", "ready", "running", "rework", "review", "brief_pending")
    _DISPATCH_ELIGIBLE = ("planned", "ready")
    groups: Dict[str, List[DAGNode]] = {}
    for node in nodes:
        if node.status not in _WRITER_HOLDING:
            continue
        group = _node_metadata(node, "parallel_group")
        if group is not None and str(group).strip():
            groups.setdefault(str(group), []).append(node)
    errors: Dict[str, List[str]] = {}

    def attach(member: DAGNode, message: str) -> None:
        if member.status in _DISPATCH_ELIGIBLE:
            errors.setdefault(member.id, []).append(message)

    for group, members in groups.items():
        if len(members) < 2:
            continue
        scopes: Dict[str, Optional[List[str]]] = {}
        for member in members:
            # The integration reviewer is read-only: its write scope is
            # verifiably empty even though it carries no allowlist metadata.
            scope = [] if is_integration_review_node(member) else _write_scope_paths(member)
            scopes[member.id] = scope
            if scope is None:
                attach(
                    member,
                    "parallel_group '{}': write scope of node {} is missing or invalid; "
                    "refusing unverifiable group membership".format(group, member.id),
                )
        for index, left in enumerate(members):
            for right in members[index + 1:]:
                if left.status not in _DISPATCH_ELIGIBLE and right.status not in _DISPATCH_ELIGIBLE:
                    continue
                left_scope = scopes[left.id]
                right_scope = scopes[right.id]
                if left_scope is None or right_scope is None:
                    unverifiable = left if left_scope is None else right
                    peer = right if left_scope is None else left
                    attach(
                        peer,
                        "parallel_group '{}': write scope of group peer {} is missing or "
                        "invalid; refusing dispatch alongside an unverifiable member".format(
                            group, unverifiable.id
                        ),
                    )
                    continue
                overlaps = sorted({
                    path for path in left_scope for other in right_scope
                    if _write_paths_overlap(path, other)
                })
                if overlaps:
                    message = (
                        "parallel_group '{}': nodes {} and {} have overlapping write scope "
                        "({}); relabel integration_after or split the group".format(
                            group, left.id, right.id, ", ".join(overlaps[:3])
                        )
                    )
                    attach(left, message)
                    attach(right, message)
                produced_left = _produced_paths(left, left_scope)
                produced_right = _produced_paths(right, right_scope)
                for consumer, producer, produced in (
                    (right, left, produced_left),
                    (left, right, produced_right),
                ):
                    contract = consumer.contract if isinstance(consumer.contract, Mapping) else {}
                    inputs = _contract_texts(contract.get("input")) + _contract_texts(contract.get("inputs"))
                    referenced = sorted({
                        path for path in produced
                        if path != "." and any(_references_path(text, path) for text in inputs)
                    })
                    for path in referenced[:3]:
                        message = (
                            "parallel_group '{}': node {} contract references {}'s produced "
                            "path {}; relabel integration_after or split the group".format(
                                group, consumer.id, producer.id, path
                            )
                        )
                        attach(consumer, message)
                        attach(producer, message)
    return errors


def _conflict_write_scope(node: DAGNode) -> Optional[List[str]]:
    """Collect every declared writable path for conflict audit.

    Authorization-era contracts may declare ``files`` while monitor-era
    worker profiles declare ``allowlist``/``owned_paths``; all of them are
    write scope for collision purposes.  Malformed sources return ``None``
    (unverifiable) rather than silently shrinking the scope.
    """
    contract = node.contract if isinstance(node.contract, Mapping) else {}
    raw: List[str] = []
    sources = (
        _node_metadata(node, "allowlist"),
        getattr(node, "owned_paths", None),
        contract.get("allowlist"),
        contract.get("owned_paths"),
        contract.get("files"),
    )
    for source in sources:
        if source is None:
            continue
        if not isinstance(source, (list, tuple)) or not all(
            isinstance(item, str) and item.strip() for item in source
        ):
            return None
        raw.extend(item.strip() for item in source)
    normalized: List[str] = []
    for item in raw:
        if item == ".":
            candidate = item
        else:
            try:
                candidate = normalize_project_path(item)
            except ValueError:
                return None
        if candidate not in normalized:
            normalized.append(candidate)
    return normalized or None


def _write_scope_conflict_errors(nodes: List[DAGNode]) -> Dict[str, List[str]]:
    """Audit overlapping write scopes between separately dispatchable nodes.

    ``_parallel_group_errors`` covers members of one explicit
    ``parallel_group``.  This complementary plan/authorization gate covers
    every unordered node pair: overlapping write scope is legal only when a
    ``depends_on`` edge serializes the pair.  Otherwise both may be
    dispatched independently while editing the same paths.  Pairs already
    inside one parallel group are skipped because the group audit emits the
    more specific message.
    """
    _WRITER_HOLDING = ("planned", "ready", "running", "rework", "review", "brief_pending")
    _DISPATCH_ELIGIBLE = ("planned", "ready")
    by_id = {node.id: node for node in nodes}
    scopes: Dict[str, Optional[List[str]]] = {}
    groups: Dict[str, str] = {}
    for node in nodes:
        if node.status not in _WRITER_HOLDING or is_integration_review_node(node):
            continue
        group = _node_metadata(node, "parallel_group")
        groups[node.id] = str(group) if group is not None else ""
        scopes[node.id] = _conflict_write_scope(node)

    errors: Dict[str, List[str]] = {}

    def attach(member: DAGNode, message: str) -> None:
        if member.status in _DISPATCH_ELIGIBLE:
            errors.setdefault(member.id, []).append(message)

    for index, left in enumerate(by_id.values()):
        if left.id not in scopes:
            continue
        for right in list(by_id.values())[index + 1:]:
            if right.id not in scopes:
                continue
            if left.id in right.depends_on or right.id in left.depends_on:
                continue
            left_scope = scopes[left.id]
            right_scope = scopes[right.id]
            if left_scope is None or right_scope is None:
                continue
            overlaps = sorted({
                path for path in left_scope for other in right_scope
                if _write_paths_overlap(path, other)
            })
            if overlaps:
                message = (
                    "nodes {} and {} have overlapping write scope ({}); "
                    "add depends_on, relabel integration_after, or split the paths".format(
                        left.id, right.id, ", ".join(overlaps[:3])
                    )
                )
                attach(left, message)
                attach(right, message)
    return errors


def audit_dag(plan: Plan) -> DAGAuditResult:
    """Audit executable readiness; only hard dependencies block startup."""
    nodes = list(getattr(plan, "nodes", []) or [])
    if not nodes:
        return DAGAuditResult(
            "blocked_dag", [], list(plan.node_ids),
            {"__dag__": ["plan has no executable DAG nodes"]}, {}
        )

    reasons: Dict[str, List[str]] = {node.id: [] for node in nodes}
    if plan.complexity_band == "complex":
        global_validation = validate_integration_review_node(plan)
        if not global_validation.valid:
            reasons["__dag__"] = list(global_validation.errors)
    by_id: Dict[str, DAGNode] = {}
    duplicate_ids = set()
    for node in nodes:
        if node.id in by_id:
            duplicate_ids.add(node.id)
        by_id[node.id] = node
    for node in nodes:
        if node.id in duplicate_ids:
            reasons[node.id].append("DAG node IDs must be unique")

    plan_ids = set(plan.node_ids)
    actual_ids = set(by_id)
    for missing in sorted(plan_ids - actual_ids):
        reasons.setdefault(missing, []).append("plan references missing node")
    for extra in sorted(actual_ids - plan_ids):
        reasons[extra].append("node is outside the plan node_ids")

    structural = _structural_errors(nodes)
    cycles = _cycle_nodes(nodes)
    for error in structural:
        if error.startswith("node "):
            node_id = error.split(" ", 2)[1]
            if node_id in reasons:
                reasons[node_id].append(error)
        else:
            for node_id in cycles or by_id:
                reasons[node_id].append(error)
    for node_id in cycles:
        reasons[node_id].append("hard dependencies contain a cycle")

    writer_bindings: Dict[Tuple[str, str], List[str]] = {}
    for node in nodes:
        if is_integration_review_node(node):
            # Integration reviewers are read-only and intentionally have no
            # writer/worktree/write allowlist; validate their dedicated
            # contract instead of applying the business-writer audit.
            reasons[node.id].extend(validate_integration_review_node(plan).errors)
        else:
            reasons[node.id].extend(_audit_contract_errors(node))
        writer = _node_metadata(node, "writer")
        worktree = _node_metadata(node, "worktree")
        if isinstance(writer, str) and writer.strip() and isinstance(worktree, str) and worktree.strip():
            writer_bindings.setdefault((writer, worktree), []).append(node.id)
    for (writer, _worktree), node_ids in writer_bindings.items():
        if len(node_ids) > 1:
            allowlists = [tuple(_node_metadata(by_id[node_id], "allowlist") or ()) for node_id in node_ids]
            reason = (
                "writer {} allowlist mismatch".format(writer)
                if len(set(allowlists)) > 1 else "duplicate writer {}".format(writer)
            )
            for node_id in node_ids:
                reasons[node_id].append(reason)

    for node_id, group_reasons in _parallel_group_errors(nodes).items():
        reasons.setdefault(node_id, []).extend(group_reasons)

    for node_id, write_scope_errors in _write_scope_conflict_errors(nodes).items():
        reasons.setdefault(node_id, []).extend(write_scope_errors)

    ready: List[str] = []
    for node in nodes:
        if node.status not in ("planned", "ready") or reasons[node.id]:
            continue
        unmet = [dependency for dependency in node.depends_on if dependency not in by_id]
        required_statuses = ("accepted",) if is_integration_review_node(node) else ("accepted", "delivered")
        unmet.extend(
            dependency for dependency in node.depends_on
            if dependency in by_id and by_id[dependency].status not in required_statuses
        )
        if unmet:
            reasons[node.id].append(
                "hard dependencies not complete (accepted or delivered): {}".format(
                    ", ".join(dict.fromkeys(unmet))
                )
            )
            continue
        # integration_after is intentionally non-blocking for startup.
        ready.append(node.id)

    for node_id in list(reasons):
        reasons[node_id] = list(dict.fromkeys(reasons[node_id]))

    blocked = [
        node.id for node in nodes
        if reasons[node.id] or node.status in ("blocked_design", "blocked_dag", "blocked_unknown")
    ]
    blocked.extend(missing for missing in sorted(plan_ids - actual_ids) if missing not in blocked)
    has_design = any("design change" in reason for values in reasons.values() for reason in values)
    fatal = [
        reason for values in reasons.values() for reason in values
        if not reason.startswith("hard dependencies not complete")
    ]
    if has_design or any(node.status == "blocked_design" for node in nodes):
        status = "blocked_design"
    elif fatal or structural or any(node.status == "blocked_dag" for node in nodes):
        status = "blocked_dag"
    elif any(node.status == "blocked_unknown" for node in nodes):
        status = "blocked_unknown"
    else:
        status = "ready"

    groups: Dict[str, List[str]] = {}
    for node in nodes:
        group = _node_metadata(node, "parallel_group")
        if node.id in ready and group:
            groups.setdefault(str(group), []).append(node.id)
    return DAGAuditResult(status, ready, blocked, reasons, groups)


def render_plan_artifacts(plan: Plan, output_dir: Path) -> PlanArtifacts:
    """Publish a new machine-readable DAG and plain-language plan without overwriting."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    dag_path = output_dir / "dag.yaml"
    plan_path = output_dir / "plan.md"

    collisions = [path for path in (dag_path, plan_path) if os.path.lexists(str(path))]
    if collisions:
        raise FileExistsError("plan artifact already exists: {}".format(", ".join(str(path) for path in collisions)))

    audit = audit_dag(plan)
    node_data = []
    for node in getattr(plan, "nodes", []) or []:
        node_data.append({
            "id": node.id,
            "title": node.title,
            "depends_on": list(node.depends_on),
            "integration_after": list(node.integration_after),
            "parallel_group": node.parallel_group,
            "contract": dict(node.contract),
            "risk_tags": _node_metadata(node, "risk_tags"),
            "writer": _node_metadata(node, "writer"),
            "worktree": _node_metadata(node, "worktree"),
            "allowlist": _node_metadata(node, "allowlist"),
            "status": node.status,
            "audit_reasons": audit.reasons.get(node.id, []),
        })
    dag_data = {
        "plan_id": plan.plan_id,
        "version": plan.version,
        "status": plan.status,
        "prd": plan.prd_path,
        "nodes": node_data or [{"id": node_id} for node_id in plan.node_ids],
        "audit": audit.to_dict(),
    }
    artifact_basis = json.dumps(dag_data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    dag_data["artifact_hashes"] = {
        "content_basis_sha256": hashlib.sha256(artifact_basis.encode("utf-8")).hexdigest()
    }
    dag_content = json.dumps(dag_data, ensure_ascii=False, indent=2) + "\n"
    node_lines = "\n".join(
        "- {} [{}]".format(node_id, "ready" if node_id in audit.ready_nodes else "blocked")
        for node_id in plan.node_ids
    ) or "- 暂无节点"
    reason_lines = []
    for node_id, node_reasons in audit.reasons.items():
        if node_reasons and node_id != "__dag__":
            reason_lines.append("- {}：{}".format(node_id, "；".join(node_reasons)))
    if audit.reasons.get("__dag__"):
        reason_lines.extend("- DAG：{}".format(reason) for reason in audit.reasons["__dag__"])
    rationale = "\n".join(reason_lines) or "- 无阻塞理由"
    plan_content = "# 开发计划：{}\n\n版本：{}\n\n状态：{}\n\nPRD：{}\n\nDAG 审计：{}\n\n可启动节点：{}\n\n## 节点\n\n{}\n\n## 审计理由\n\n{}\n".format(
        plan.plan_id,
        plan.version,
        plan.status,
        plan.prd_path,
        audit.status,
        ", ".join(audit.ready_nodes) or "暂无",
        node_lines,
        rationale,
    )

    published = []
    with tempfile.TemporaryDirectory(prefix=".plan-artifacts-", dir=str(output_dir)) as staging:
        staging_dir = Path(staging)
        staged_dag = staging_dir / dag_path.name
        staged_plan = staging_dir / plan_path.name
        staged_dag.write_text(dag_content, encoding="utf-8")
        staged_plan.write_text(plan_content, encoding="utf-8")
        try:
            for staged, destination in ((staged_dag, dag_path), (staged_plan, plan_path)):
                os.link(str(staged), str(destination))
                published.append(destination)
        except BaseException:
            for path in reversed(published):
                path.unlink()
            raise
    return PlanArtifacts(dag_path, plan_path)
