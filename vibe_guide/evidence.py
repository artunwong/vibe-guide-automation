"""Append-only generation evidence and repeated-rework classification."""

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Sequence
from copy import deepcopy

from .paths import ProjectPaths
from .state import _atomic_bytes, run_dir


@dataclass(frozen=True)
class DeliveryEvidence:
    status: str
    reasons: List[str]

    @property
    def complete(self) -> bool:
        return self.status == "DELIVERED"

    def to_dict(self) -> Dict[str, Any]:
        return {"status": self.status, "reasons": list(self.reasons)}


def evaluate_delivery_evidence(node: Any, binding: Any, artifacts: Any) -> DeliveryEvidence:
    """Require complete, current delivery evidence before dependency unlock."""
    get = lambda value, key, default=None: value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)
    reasons: List[str] = []
    if get(node, "status") not in {"DELIVERED", "delivered"}:
        reasons.append("canonical delivered state is missing")
    if get(binding, "task_id") in (None, "") and get(binding, "threadId") in (None, ""):
        reasons.append("task identity is missing")
    if get(binding, "host") in (None, "") and get(binding, "hostId") in (None, ""):
        reasons.append("host identity is missing")
    if get(binding, "worktree") in (None, "") or get(binding, "branch") in (None, ""):
        reasons.append("worktree or branch is missing")
    if get(binding, "cursor") in (None, ""):
        reasons.append("current cursor is missing")
    if not isinstance(artifacts, dict) or not artifacts.get("completion_marker"):
        reasons.append("completion marker is missing")
    if not isinstance(artifacts, dict) or not artifacts.get("delivery_path"):
        reasons.append("delivery path is missing")
    thread_status = get(artifacts, "thread_status")
    if thread_status not in {"complete", "completed", "DELIVERED"}:
        reasons.append("terminal thread status is missing")
    return DeliveryEvidence("DELIVERED" if not reasons else "blocked_unknown", reasons)


@dataclass(frozen=True)
class GenerationEvidence:
    run_id: str
    issue_id: str
    generation: int
    task_id: str
    cursor: str
    worktree: str
    branch: str
    base_sha: str
    status: str

    def to_dict(self):
        return {"run_id": self.run_id, "issue_id": self.issue_id, "generation": self.generation,
                "task_id": self.task_id, "cursor": self.cursor, "worktree": self.worktree,
                "branch": self.branch, "base_sha": self.base_sha, "status": self.status}


@dataclass(frozen=True)
class IssueSummary:
    generations: List[int]
    original_task_id: str
    original_worktree: str = ""
    original_branch: str = ""


class ReworkDecision(str, Enum):
    CONTINUE_SAME_WORKER = "continue_same_worker"
    CONTRACT_OR_CALL_CHAIN_REVIEW_REQUIRED = "contract_or_call_chain_review_required"


@dataclass(frozen=True)
class ReviewResult:
    severity: str
    root_cause: str
    status: str


@dataclass(frozen=True)
class CloseoutDecision:
    """Evidence-bounded decision for a run-level V4.1 closeout."""

    allowed: bool
    status: str
    reasons: List[str]

    @property
    def can_complete(self) -> bool:
        return self.allowed

    @property
    def complete(self) -> bool:
        return self.allowed

    def to_dict(self) -> Dict[str, Any]:
        return {"allowed": self.allowed, "status": self.status, "reasons": list(self.reasons)}


_HEX_DIGEST = lambda value: isinstance(value, str) and len(value) == 64 and all(
    character in "0123456789abcdef" for character in value.lower()
)

# The only fields an integration reviewer may report.  Everything else in the
# run-level package is derived; see `build_integration_review_evidence`.
INTEGRATION_REVIEW_CLAIM_KEYS = frozenset({
    "findings", "iteration_compatibility", "test_runtime_delivery", "out_of_scope",
})


def _snapshot_value(snapshot: Any, key: str, default: Any = None) -> Any:
    if isinstance(snapshot, dict):
        return snapshot.get(key, default)
    return getattr(snapshot, key, default)


def _integration_node(snapshot: Any):
    nodes = _snapshot_value(snapshot, "nodes", {})
    if not isinstance(nodes, dict):
        return None, None
    for node_id, node in nodes.items():
        if node_id == "integration-review" or (
            isinstance(node, dict)
            and node.get("integration_review") is True
        ):
            return node_id, node
    return None, None


def _lineage_reasons(snapshot: Any, evidence: Dict[str, Any]) -> List[str]:
    reasons = []
    expected = {
        "run_id": _snapshot_value(snapshot, "run_id"),
        "plan_id": _snapshot_value(snapshot, "plan_id"),
        "plan_revision": _snapshot_value(snapshot, "plan_version"),
        "authorization_digest": _snapshot_value(snapshot, "authorization_digest"),
        "node_contract_digest": _snapshot_value(snapshot, "node_contract_digest"),
    }
    for key, value in expected.items():
        if value in (None, ""):
            reasons.append("integration current lineage {} is missing".format(key))
        elif evidence.get(key) != value:
            reasons.append("integration evidence {} mismatch".format(key))
    # PRD/Spec digests are supplied by the plan projection when available.
    for key in ("prd_digest", "spec_digest"):
        expected_value = _snapshot_value(snapshot, key)
        if expected_value in (None, ""):
            reasons.append("integration current lineage {} is missing".format(key))
        elif evidence.get(key) != expected_value:
            reasons.append("integration evidence {} mismatch".format(key))
    return reasons


def _open_finding_counts(findings: Any) -> Dict[str, int]:
    """Count the still-open P0/P1/P2 findings, rejecting malformed entries.

    Both the validator and the derivation below read the clearance out of the
    findings through this one function, so a package can never be built with a
    clearance the validator would then compute differently.

    Only `resolved` clears a finding.  `waived` and `accepted` are registered
    spellings because reviewers do report them, and counting them as open is a
    format convention rather than a permission boundary: nothing here
    cross-checks the `review_finding` events the same reviewer filed earlier, so
    a reviewer that writes `resolved` over its own unfixed P0 does clear it.
    What the convention buys is a trace -- a self-served exemption cannot stay
    spelled `waived`; it has to be restated as a claim that the finding was
    fixed, in the package a human later reads.

    `p3`/`p4` are observation severities (ISSUE-83): a reviewer can register
    advisory notes without them counting into the P0-P2 clearance.  The
    whitelist is extended *by name, not by shape* -- anything else
    (`critical`, `po`, `p00`, `high`) still raises, because a mistyped P0
    must never be silently dropped from the clearance.
    """
    open_counts = {"p0": 0, "p1": 0, "p2": 0}
    observation_severities = {"p3", "p4"}
    for finding in findings:
        if not isinstance(finding, dict):
            raise ValueError("integration finding schema is invalid")
        severity = str(finding.get("severity", "")).lower()
        if severity not in open_counts and severity not in observation_severities:
            raise ValueError("integration finding schema is invalid")
        status = str(finding.get("status", finding.get("resolution", ""))).lower()
        if status not in {"open", "resolved", "accepted", "waived"}:
            raise ValueError("integration finding status is invalid")
        if severity in open_counts and status != "resolved":
            open_counts[severity] += 1
    return open_counts


def validate_integration_review_evidence(snapshot: Any, evidence: Dict[str, Any]) -> None:
    """Validate the immutable, run-bound integration reviewer evidence package."""
    if not isinstance(evidence, dict):
        raise ValueError("integration review evidence must be an object")
    required = {
        "schema_version", "run_id", "plan_id", "plan_revision",
        "prd_digest", "spec_digest", "authorization_digest", "node_contract_digest",
        "aggregated_scope", "iteration_compatibility", "agentsmd_acceptance_refs",
        "test_runtime_delivery", "unverified_or_excluded", "findings", "clearance",
    }
    if set(evidence) != required or evidence.get("schema_version") != 1:
        raise ValueError("integration review evidence schema is invalid")
    lineage_errors = _lineage_reasons(snapshot, evidence)
    if lineage_errors:
        raise ValueError(lineage_errors[0])
    for key in ("prd_digest", "spec_digest", "authorization_digest", "node_contract_digest"):
        if not _HEX_DIGEST(evidence.get(key)):
            raise ValueError("integration evidence {} is invalid".format(key))
    scope = evidence["aggregated_scope"]
    if not isinstance(scope, dict) or not isinstance(scope.get("nodes"), list):
        raise ValueError("integration aggregated scope is invalid")
    nodes = _snapshot_value(snapshot, "nodes", {})
    expected_nodes = [node_id for node_id in nodes if node_id != "integration-review"] if isinstance(nodes, dict) else []
    if scope.get("nodes") != expected_nodes or len(set(scope["nodes"])) != len(scope["nodes"]):
        raise ValueError("integration aggregated scope conflicts with run")
    if scope.get("out_of_scope"):
        raise ValueError("integration aggregated scope contains out-of-scope changes")
    compatibility = evidence["iteration_compatibility"]
    if not isinstance(compatibility, dict) or compatibility.get("status") in {"unknown", "expired", "stale"}:
        raise ValueError("integration compatibility evidence is unknown or expired")
    if compatibility.get("status") not in {"verified", "compatible", "reviewed"}:
        raise ValueError("integration compatibility evidence is incomplete")
    if not compatibility.get("evidence"):
        raise ValueError("integration compatibility evidence is missing")
    refs = evidence["agentsmd_acceptance_refs"]
    if not isinstance(refs, list) or not refs or any(not isinstance(item, (str, dict)) for item in refs):
        raise ValueError("integration AGENTS.md evidence is missing")
    runtime = evidence["test_runtime_delivery"]
    if not isinstance(runtime, dict) or runtime.get("status") in {"unknown", "expired", "stale"}:
        raise ValueError("integration test/runtime evidence is unknown or expired")
    if runtime.get("status") not in {"verified", "reviewed"} or not runtime.get("evidence"):
        raise ValueError("integration test/runtime evidence is incomplete")
    if not isinstance(evidence["unverified_or_excluded"], list):
        raise ValueError("integration unverified/excluded field is invalid")
    if not isinstance(evidence["findings"], list):
        raise ValueError("integration findings field is invalid")
    clearance = evidence["clearance"]
    if not isinstance(clearance, dict) or set(clearance) != {"p0", "p1", "p2"}:
        raise ValueError("integration P0/P1/P2 clearance is invalid")
    if any(type(clearance[key]) is not int or clearance[key] < 0 for key in clearance):
        raise ValueError("integration P0/P1/P2 clearance is invalid")
    open_counts = _open_finding_counts(evidence["findings"])
    for key in open_counts:
        if clearance[key] != open_counts[key]:
            raise ValueError("{} clearance does not match finding count".format(key.upper()))


def build_integration_review_evidence(
    snapshot: Any,
    claim: Any,
    agentsmd_acceptance_refs: Sequence[Any],
    unverified_or_excluded: Sequence[Any],
) -> Dict[str, Any]:
    """Derive the run-bound review package from the reviewer's judgement.

    The reviewer supplies only what it alone can know -- the findings, the two
    compatibility/runtime verdicts, and whether it saw changes outside the
    aggregated scope.  Everything else is lineage the run already owns, so the
    reviewer can neither forge it nor narrow it; supplying any of it is a
    schema error rather than an override.  The acceptance references and the
    permanent exclusions come from the plan's integration contract for the
    same reason: a reviewer must not be able to shrink the scope it is being
    held to.
    """
    if not isinstance(claim, dict) or set(claim) != INTEGRATION_REVIEW_CLAIM_KEYS:
        raise ValueError(
            "integration review claim must be an object with exactly these keys: "
            + ", ".join(sorted(INTEGRATION_REVIEW_CLAIM_KEYS))
        )
    if not isinstance(claim["findings"], list) or not isinstance(claim["out_of_scope"], list):
        raise ValueError("integration review claim findings and out_of_scope must be lists")
    for key in ("iteration_compatibility", "test_runtime_delivery"):
        verdict = claim[key]
        # The justification has to be text: persistence redacts any value under
        # an `evidence` key, and for a nested object it drops the keys that look
        # sensitive -- so `{"secret_scan": "clean"}` was written as `{}` and the
        # reload-time validator then rejected the whole completed run.
        if not isinstance(verdict, dict) or not isinstance(verdict.get("evidence"), str) or not verdict["evidence"].strip():
            raise ValueError(
                "integration review claim {} needs a status and a non-empty evidence string".format(key)
            )
    nodes = _snapshot_value(snapshot, "nodes", {})
    package = {
        "schema_version": 1,
        "run_id": _snapshot_value(snapshot, "run_id"),
        "plan_id": _snapshot_value(snapshot, "plan_id"),
        "plan_revision": _snapshot_value(snapshot, "plan_version"),
        "prd_digest": _snapshot_value(snapshot, "prd_digest"),
        "spec_digest": _snapshot_value(snapshot, "spec_digest"),
        "authorization_digest": _snapshot_value(snapshot, "authorization_digest"),
        "node_contract_digest": _snapshot_value(snapshot, "node_contract_digest"),
        "aggregated_scope": {
            "nodes": [
                node_id for node_id in (nodes if isinstance(nodes, dict) else {})
                if node_id != "integration-review"
            ],
            "out_of_scope": deepcopy(claim["out_of_scope"]),
        },
        "iteration_compatibility": deepcopy(claim["iteration_compatibility"]),
        "agentsmd_acceptance_refs": list(agentsmd_acceptance_refs),
        "test_runtime_delivery": deepcopy(claim["test_runtime_delivery"]),
        "unverified_or_excluded": list(unverified_or_excluded),
        "findings": deepcopy(claim["findings"]),
        "clearance": _open_finding_counts(claim["findings"]),
    }
    validate_integration_review_evidence(snapshot, package)
    return package


def record_integration_review(snapshot: Any, evidence: Dict[str, Any]) -> None:
    """Append a review result to a snapshot while retaining prior evidence."""
    validate_integration_review_evidence(snapshot, evidence)
    prior = _snapshot_value(snapshot, "integration_review_evidence", {})
    history = []
    if isinstance(prior, dict) and isinstance(prior.get("history"), list):
        history = deepcopy(prior["history"])
    elif prior:
        history = [deepcopy(prior)]
    history.append(deepcopy(evidence))
    stored = deepcopy(evidence)
    stored["history"] = history
    if isinstance(snapshot, dict):
        snapshot["integration_review_evidence"] = stored
        node = snapshot.get("nodes", {}).get("integration-review")
    else:
        snapshot.integration_review_evidence = stored
        node = getattr(snapshot, "nodes", {}).get("integration-review")
    if isinstance(node, dict):
        node["review_clearance"] = dict(evidence["clearance"])
        node.setdefault("evidence", []).append(deepcopy(evidence))
        node["status"] = "accepted" if all(evidence["clearance"][key] == 0 for key in ("p0", "p1", "p2")) else "rework"


def evaluate_v41_closeout(snapshot: Any) -> CloseoutDecision:
    """Evaluate V4.1 closeout without mutating the supplied snapshot."""
    nodes = _snapshot_value(snapshot, "nodes", {})
    if not isinstance(nodes, dict) or not nodes:
        return CloseoutDecision(False, "blocked_unknown", ["snapshot nodes are unavailable"])
    integration_id, integration = _integration_node(snapshot)
    if integration_id is None:
        accepted = all(isinstance(node, dict) and node.get("status") == "accepted" for node in nodes.values())
        return CloseoutDecision(accepted, "complete" if accepted else "running", [] if accepted else ["business nodes are not all accepted"])
    reasons = []
    business = [node for node_id, node in nodes.items() if node_id != integration_id]
    if not all(isinstance(node, dict) and node.get("status") == "accepted" for node in business):
        reasons.append("business nodes are not all accepted")
    if not isinstance(integration, dict) or integration.get("status") != "accepted":
        reasons.append("integration review is not accepted")
    evidence = _snapshot_value(snapshot, "integration_review_evidence", {})
    if not isinstance(evidence, dict) or not evidence:
        reasons.append("integration review evidence is missing")
    else:
        try:
            current_evidence = {key: value for key, value in evidence.items() if key != "history"}
            validate_integration_review_evidence(snapshot, current_evidence)
        except ValueError as error:
            reasons.append(str(error))
        clearance = evidence.get("clearance")
        if not isinstance(clearance, dict) or any(clearance.get(key) != 0 for key in ("p0", "p1", "p2")):
            reasons.append("integration P0/P1/P2 clearance is not zero")
    return CloseoutDecision(not reasons, "complete" if not reasons else "running", reasons)


def _path(paths: ProjectPaths, evidence: GenerationEvidence) -> Path:
    directory = run_dir(paths, evidence.run_id, create=True) / "evidence" / evidence.issue_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ("generation-%d.json" % evidence.generation)
    if path.is_symlink():
        raise ValueError("generation evidence may not be a symlink")
    return path


def write_generation_evidence(paths: ProjectPaths, evidence: GenerationEvidence) -> None:
    path = _path(paths, evidence)
    payload = (json.dumps(evidence.to_dict(), sort_keys=True, separators=(",", ":")) + "\n").encode()
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError("historical generation evidence is immutable")
        return
    _atomic_bytes(path, payload)


def replay_summary(paths: ProjectPaths, run_id: str, issue_id: str) -> IssueSummary:
    directory = run_dir(paths, run_id, create=False) / "evidence" / issue_id
    entries = []
    for path in sorted(directory.glob("generation-*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        entries.append(data)
    if not entries:
        raise FileNotFoundError(str(directory))
    entries.sort(key=lambda item: item["generation"])
    first = entries[0]
    return IssueSummary([item["generation"] for item in entries], first["task_id"],
                        first["worktree"], first["branch"])


def classify_rework(history: Sequence[ReviewResult]) -> ReworkDecision:
    counts = {}
    for result in history:
        key = (result.severity, result.root_cause)
        counts[key] = counts.get(key, 0) + 1
    return (ReworkDecision.CONTRACT_OR_CALL_CHAIN_REVIEW_REQUIRED
            if any(count >= 2 for count in counts.values())
            else ReworkDecision.CONTINUE_SAME_WORKER)
