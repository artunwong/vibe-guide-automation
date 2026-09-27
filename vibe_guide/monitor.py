"""Authorization-bound, write-ahead monitor for developer/reviewer DAG tasks."""

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from datetime import datetime, timezone
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple
import uuid
import copy

from .authorization import (
    AuthorizationRecord,
    affected_node_closure,
    canonical_node_contracts,
    digest_integration_contract,
    executable_contract_digest,
    integration_contract_projection,
    is_authorization_valid,
    validate_runtime_contract,
)
from .contracts import RunEvent, RunHandle, Runner
from .models import (
    DAGNode,
    Plan,
    BindingIntent,
    BindingObservation,
    BindingVerification,
    IssueComplexity,
    LocalModel,
    HealingResult,
    ObservationDisposition,
    node_branch,
    node_worktree,
)
from .paths import ProjectPaths
from .path_ownership import validate_path_ownership
from .binding_lifecycle import VisibleSddAcceptance
from .planner import resolve_consistency
from . import dag as _dag_module
from .dag import audit_dag, node_scoped_ready, ready_nodes
from .adapters.task_provider import ProviderActionStore, ProviderPending, ProviderUnavailable
from .state import (
    CONSISTENCY_CORRECTION_KEYS,
    RunSnapshot,
    _atomic_bytes,
    acquire_writer_lease,
    append_event,
    load_events,
    load_snapshot,
    map_user_status,
    quarantine_writer_lease,
    read_writer_lease,
    redact_provider_text,
    release_writer_lease,
    run_dir,
    save_snapshot,
    validate_run_id,
)
from .task_registry import (
    TaskBinding,
    binding_contract_enabled,
    load_task_binding,
    runtime_binding_gate,
    save_task_binding,
)
from .config import load_project_config
from .workflow_gate import require_capability_contract, require_entry, verify_workflow
from .authorize_entry import load_live_workflow, select_plan_workflow, verify_workflow_artifacts
from .diagnostics import validate_child_session_binding
from .models import WorkerProfile
from .model_router import ModelRouter
from .change_requests import (
    ChangeRequest,
    ChangeRequestEvidence,
    LocalMergeEvidence,
    execute_change_request,
    merge_local,
)
from .authorization import can_create_change_request, can_auto_merge
from .checkpoint import (
    ContextBudgetEstimator,
    ContextBudgetPolicy,
    MonitorCheckpoint,
    resume_from_checkpoint,
    write_checkpoint,
)
from .brief import ImplementationBrief, validate_implementation_brief
from .manifest import RunManifest
from .evidence import (
    build_integration_review_evidence,
    evaluate_v41_closeout,
    evaluate_delivery_evidence,
    record_integration_review as _record_integration_review,
)
from .engine_attestation import validate_engine_attestation


# --- V4.6 ISSUE-04: topology-aware dispatch of visible worker sessions ---

#: Dispatch topologies a node can be ruled into.  ``visible-sdd`` binds one
#: visible worker session per node (review is in-session evidence, never a
#: second task); ``dual-visible`` keeps the developer/reviewer pair;
#: ``background`` is the disclosed downgrade when no visible bridge exists.
TOPOLOGY_VISIBLE_SDD = "visible-sdd"
TOPOLOGY_DUAL_VISIBLE = "dual-visible"
TOPOLOGY_BACKGROUND = "background"
_DISPATCH_TOPOLOGIES = {TOPOLOGY_VISIBLE_SDD, TOPOLOGY_DUAL_VISIBLE, TOPOLOGY_BACKGROUND}

#: Manifest capability-probe field name / ``DISPATCH_TOPOLOGY_MATRIX`` ruling
#: value (adapter layer, from the ISSUE-07 probe matrix).  The supervisor
#: translates it into the topology enum value ``visible-sdd`` (dispatch
#: layer); the adapter-layer name itself is never a topology value.
_RULING_IN_SESSION_SDD = "in_session_sdd"

#: Protocol pointer carried by every visible-sdd create request so the worker
#: session can resolve the in-session SDD protocol from the installed package.
VISIBLE_SDD_PROTOCOL_REF = "vibe_guide/protocols/visible-sdd-worker.md"

#: Disclosure recorded when a node is dispatched without a visible bridge.
BACKGROUND_TOPOLOGY_DISCLOSURE = (
    "平台无可见任务桥：降级为 background subagent，不可见、不可直接进入、返工续接受限"
)

#: Identities reserved for the supervisor itself.  A node whose writer
#: resolves to one of these is structurally rejected: the monitor must never
#: become the writer of any node, regardless of authorization-card text.
_SUPERVISOR_WRITER_IDENTITIES = frozenset(
    {"monitor", "vibeguide_monitor", "vibe_guide_monitor", "supervisor", "vibe_supervisor"}
)


def reconcile_pending_binding(snapshot: Any, node_id: str, runner: Any) -> bool:
    """Reconcile a provider setup placeholder without creating a successor.

    A ``clientThreadId`` is only a setup handle.  Until the provider returns an
    exact real task identity this function keeps the existing generation and
    writer lease, marks the node retryable, and returns ``False``.
    """
    nodes = snapshot.get("nodes") if isinstance(snapshot, dict) else getattr(snapshot, "nodes", None)
    if not isinstance(nodes, dict) or not isinstance(node_id, str):
        return False
    node = nodes.get(node_id)
    if not isinstance(node, dict):
        return False
    binding = node.get("binding") or node.get("active_binding") or node.get("task")
    client_id = None
    if isinstance(binding, dict):
        client_id = binding.get("clientThreadId") or binding.get("client_thread_id")
    client_id = client_id or node.get("clientThreadId") or node.get("client_thread_id")
    thread_id = binding.get("threadId") if isinstance(binding, dict) else node.get("threadId")
    if not client_id or thread_id:
        return False
    # Only a verified provider locate/list operation may promote the identity.
    try:
        resolver = getattr(runner, "resolve_pending", None)
        resolved = resolver(binding) if callable(resolver) else None
    except Exception:
        resolved = None
    if not isinstance(resolved, dict) and resolved is not None and hasattr(resolved, "__dict__"):
        resolved = dict(resolved.__dict__)
    if isinstance(resolved, dict) and resolved.get("threadId"):
        expected_host = binding.get("hostId") or binding.get("host") if isinstance(binding, dict) else None
        observed_host = resolved.get("hostId") or resolved.get("host")
        required_identity = ("provider", "status", "clientThreadId", "issue_id", "role", "generation")
        if any(resolved.get(key) in (None, "") for key in required_identity) or not expected_host or not observed_host:
            resolved = None
        elif resolved.get("clientThreadId") != client_id:
            resolved = None
        if resolved is None:
            pass
        elif not isinstance(binding, dict) or not binding.get("provider") or resolved.get("provider") != binding.get("provider"):
            resolved = None
        elif expected_host and observed_host != expected_host:
            resolved = None
        elif isinstance(binding, dict) and resolved.get("clientThreadId") != client_id:
            # A locate result for a different setup request must never be
            # promoted in place: doing so would silently change the writer
            # identity while retaining the old generation and lease.
            resolved = None
        elif isinstance(binding, dict) and resolved.get("issue_id") != binding.get("issue_id"):
            resolved = None
        elif isinstance(binding, dict) and resolved.get("role") != binding.get("role"):
            resolved = None
        elif isinstance(binding, dict) and resolved.get("generation") != binding.get("generation"):
            resolved = None
        elif resolved.get("status") not in ("verified", "visible"):
            resolved = None
    if isinstance(resolved, dict) and resolved.get("threadId"):
        target = binding if isinstance(binding, dict) else node
        target["threadId"] = resolved["threadId"]
        target["clientThreadId"] = client_id
        return True
    node["status"] = "retry_pending"
    node["retryable_action"] = {"action": "reconcile_pending_binding", "clientThreadId": client_id}
    node.setdefault("generation", 1)
    node["successor"] = None
    return False


def _user_status(record: Dict[str, Any]) -> str:
    """Keep internal recovery detail behind the four public statuses."""
    reason = str(record.get("reason") or "").casefold()
    if any(marker in reason for marker in ("product", "scope", "permission", "credential", "irreversible", "security", "产品", "范围", "权限", "凭据", "不可逆", "安全")):
        return "需要你决定"
    if record.get("binding_phase") in {"retry_pending", "binding_probe_pending", "binding_repair_pending", "binding_repairing", "blocked_unknown", "unknown", "timeout", "failed", "stopped"}:
        return "自动修复中"
    if isinstance(record.get("retryable_action"), dict) and not record.get("active_task"):
        return "自动修复中"
    status = record.get("status")
    if status in {"blocked_design", "blocked_deploy"}:
        return "需要你决定"
    if status in {"initialized", "planned", "brief_pending", "start_pending"}:
        return "准备中"
    if status in {"running", "review", "rework", "delivered", "accepted", "complete"}:
        return "已启动"
    return "自动修复中"


def classify_observation(observation: Any) -> ObservationDisposition:
    """Classify provider drift without claiming unavailable capability."""
    if not isinstance(observation, dict):
        return ObservationDisposition("unknown", "isolate", "observation is malformed")
    kind = str(observation.get("kind", observation.get("type", "unknown"))).strip().lower()
    repairable = {"worktree_branch_drift", "worktree_drift", "branch_drift", "stale_cursor"}
    uncertain = {
        "missing_visible_locate_evidence", "provider_timeout", "timeout",
        "permission_denied", "missing_locate_evidence", "unknown",
    }
    if kind in repairable and observation.get("repairable", True) is not False:
        return ObservationDisposition("repairable", "repair", kind)
    if kind in uncertain or not kind:
        return ObservationDisposition("degraded", "isolate", kind or "unknown observation")
    return ObservationDisposition("unknown", "isolate", kind)


def classify_provider_failure(observation: Any) -> Dict[str, str]:
    """Classify provider outcomes without turning missing evidence into failure.

    Provider bridges expose a mixture of structured observations and exception
    text.  Transient/repairable outcomes are engineering conditions and may be
    retried on the same task identity; explicit user/system authorization
    boundaries are external and must remain blocked.  Anything else is kept
    ``unknown`` so callers fail closed rather than inventing a category.
    """
    class _ClassificationKind(str):
        """Current V4.5 kind with equality aliases for V4.2 callers."""
        _aliases = {
            "retryable": "engineering",
            "capacity_wait": "engineering",
            "repairable": "engineering",
            "external_decision": "external",
            "binding_unknown": "unknown",
        }
        def __eq__(self, other):
            return str.__eq__(self, other) or self._aliases.get(str(self)) == other

        __hash__ = str.__hash__

    def _result(kind: str, legacy: str) -> Dict[str, str]:
        return {"kind": _ClassificationKind(kind), "legacy_kind": legacy}

    if isinstance(observation, BaseException):
        text = str(observation)
        kind = getattr(observation, "kind", None) or getattr(observation, "type", None)
    elif isinstance(observation, dict):
        values = [observation.get(key) for key in ("kind", "type", "status", "reason", "error", "message", "detail")]
        text = " ".join(str(value) for value in values if value is not None)
        kind = observation.get("kind") or observation.get("type")
        if observation.get("credential_required") or observation.get("login_required") or observation.get("permission_required"):
            return _result("external_decision", "external")
    else:
        text = str(observation or "")
        kind = None
    normalized = (str(kind or "") + " " + text).casefold()
    external_markers = (
        "login required", "log in required", "credential", "credentials",
        "auth required", "authentication required", "permission required",
        "permission denied", "access denied",
        "system permission", "remote approval", "approval required",
        "external approval", "sudo", "凭据", "登录", "系统权限", "远程批准",
        "product scope", "scope change", "deploy", "deployment", "irreversible",
        "security", "产品范围", "范围变更", "部署", "不可逆", "安全",
        "external decision", "external_decision", "authorization", "授权", "产品决策",
    )
    if any(marker in normalized for marker in external_markers):
        return _result("external_decision", "external")
    binding_markers = (
        "identity conflict", "identity mismatch", "writer conflict", "lease conflict",
        "binding conflict", "binding mismatch", "task identity", "contract digest",
        "身份冲突", "租约冲突", "绑定冲突",
    )
    if any(marker in normalized for marker in binding_markers):
        return _result("binding_unknown", "unknown")
    capacity_markers = ("429", "rate limit", "too many requests", "capacity", "quota", "限流", "容量", "配额")
    if any(marker in normalized for marker in capacity_markers):
        return _result("capacity_wait", "engineering")
    retryable_markers = (
        "task-create", "task create", "task creation", "create task failed", "创建任务失败",
        "timeout", "timed out", "disconnect", "disconnected", "connection reset",
        "empty response", "no response", "pending", "clientthreadid", "setup",
        "worker exit", "worker exited", "worker crash", "snapshot interruption",
        "snapshot interrupted", "interrupted", "temporarily unavailable",
        "provider unavailable", "超时", "断开", "空响应", "创建中", "工作进程退出", "快照中断",
    )
    if any(marker in normalized for marker in retryable_markers):
        return _result("retryable", "engineering")
    repairable_markers = (
        "dirty checkout", "detached head", "detached", "branch drift", "worktree drift",
        "stale cursor", "dirty 工作树", "游离头", "分支漂移", "工作树漂移", "游标过期",
    )
    if any(marker in normalized for marker in repairable_markers):
        return _result("repairable", "engineering")
    engineering_markers = (
        "timeout", "timed out", "429", "rate limit", "too many requests",
        "disconnect", "disconnected", "connection reset", "empty response",
        "no response", "pending", "clientthreadid", "setup", "worker exit",
        "worker exited", "worker crash", "snapshot interruption", "snapshot interrupted",
        "interrupted", "binding drift", "branch drift", "worktree drift",
        "stale cursor", "temporarily unavailable", "provider unavailable",
        "超时", "限流", "断开", "空响应", "创建中", "工作进程退出", "快照中断",
    )
    if any(marker in normalized for marker in engineering_markers):
        return _result("retryable", "engineering")
    return _result("binding_unknown", "unknown")


def self_heal_binding(snapshot: Any, node_id: str, observation: Any) -> HealingResult:
    """Repair only fields proven by the frozen node contract, in place.

    This deliberately accepts a plain snapshot mapping so recovery and replay
    callers can use the same fail-closed operation before persistence.
    """
    nodes = snapshot.get("nodes") if isinstance(snapshot, dict) else getattr(snapshot, "nodes", None)
    if not isinstance(nodes, dict):
        return HealingResult(False, degraded=True, isolated=True, reason="snapshot is malformed")
    node = nodes.get(node_id)
    if not isinstance(node, dict):
        return HealingResult(False, degraded=True, isolated=True, reason="node is unknown")
    disposition = classify_observation(observation)
    if disposition.status != "repairable" or not isinstance(observation, dict):
        isolate_affected_action(snapshot, node_id, disposition.reason)
        return HealingResult(False, degraded=True, isolated=True, reason=disposition.reason,
                             task_id=node.get("task_id") or node.get("developer_identity"))
    # Current facts may identify drift, but never become the new contract.
    expected = node.get("binding")
    if not isinstance(expected, dict):
        expected = node.get("frozen_binding")
    if not isinstance(expected, dict):
        expected = node.get("binding_contract")
    if not isinstance(expected, dict) and isinstance(node.get("contract"), dict):
        nested = node["contract"].get("binding")
        expected = nested if isinstance(nested, dict) else node["contract"]
    if not isinstance(expected, dict):
        isolate_affected_action(snapshot, node_id, "frozen binding evidence is missing")
        return HealingResult(False, degraded=True, isolated=True,
                             reason="frozen binding evidence is missing",
                             task_id=node.get("task_id") or node.get("developer_identity"))
    for field in ("task_id", "worktree", "branch", "allowlist", "cursor",
                  "token", "continuation_token", "continuation_digest"):
        if field in expected:
            node[field] = expected[field]
    node["healing"] = {"status": "repaired", "observation_kind": disposition.reason}
    return HealingResult(True, reason=disposition.reason, task_id=node.get("task_id"))


def isolate_affected_action(snapshot: Any, node_id: str, reason: str) -> None:
    """Quarantine one node/action while leaving all other nodes untouched."""
    nodes = snapshot.get("nodes") if isinstance(snapshot, dict) else getattr(snapshot, "nodes", None)
    if not isinstance(nodes, dict):
        return
    node = nodes.get(node_id)
    if not isinstance(node, dict):
        return
    node["status"] = "blocked_unknown"
    node["reason"] = str(reason)
    node["isolated"] = True
    node["retryable_action"] = None


class Monitor:
    @staticmethod
    def _prospective_intent_digest(snapshot: RunSnapshot, node_id: str, role: str, generation: int, current: Dict[str, Any]) -> str:
        payload = {"run_id": snapshot.run_id, "node_id": node_id, "role": role, "generation": generation, "task_id": current.get(role + "_identity") or f"{role}:{node_id}", "writer": current.get("writer") or current.get("worker") or role, "worktree": current.get("worktree", ""), "branch": current.get("branch", ""), "authorization_digest": snapshot.authorization_digest, "node_contract_digest": current.get("contract_digest") or snapshot.node_contract_digest}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _build_binding_intent(self, snapshot: RunSnapshot, node_id: str, role: str,
                               generation: int, contract: Optional[Dict[str, Any]] = None):
        """Build (or reuse) the immutable pre-dispatch identity envelope.

        This helper deliberately contains no provider lease or cursor.  It is
        safe to call repeatedly after an interrupted start and therefore keeps
        the original task/generation stable during retries.
        """
        current = snapshot.nodes[node_id]
        contract = dict(contract or {})
        task_id = current.get(role + "_identity") or contract.get("task_id") or f"{role}:{node_id}"
        payload = {
            "run_id": snapshot.run_id,
            "plan_id": getattr(self.plan, "plan_id", snapshot.plan_id),
            "plan_revision": getattr(self.plan, "version", snapshot.plan_version),
            "node_id": node_id,
            "task_id": task_id,
            "role": role,
            "generation": int(generation),
            "writer": contract.get("writer") or current.get("writer") or current.get("worker") or role,
            "worktree": str(contract.get("worktree") or current.get("worktree", "")),
            "branch": str(contract.get("branch") or current.get("branch", "")),
            "base_sha": str(contract.get("base_sha") or current.get("base_sha", "")),
            "authorization_digest": snapshot.authorization_digest,
            "node_contract_digest": current.get("contract_digest") or snapshot.node_contract_digest,
        }
        existing = current.get("binding_intent") or current.get("start_intent")
        if isinstance(existing, dict) and all(existing.get(k) == payload.get(k) for k in payload):
            payload = existing
        try:
            from .binding_contract import BindingIntent as V44BindingIntent
            return V44BindingIntent.from_dict(payload)
        except (ImportError, TypeError, ValueError):
            return payload

    def _dispatch_with_intent(self, runner: Runner, contract: Dict[str, Any], intent: Any):
        """Dispatch one provider request and accept only a matching proof.

        The provider response must be structured; prose or a partial envelope
        is retained as a retry/binding observation and never confirmed.
        """
        payload = dict(contract)
        # The immutable intent is a V3.9/V4.4 binding contract field.  Do not
        # inject a plain JSON intent into legacy contracts: its mere presence
        # opts the provider runner into the protected binding gate, where the
        # downgraded mapping is correctly rejected as untrusted evidence.
        if binding_contract_enabled(contract):
            payload["binding_intent"] = (
                intent.to_dict() if hasattr(intent, "to_dict") else dict(intent)
            )
        try:
            worktree = Path(str(payload.get("worktree", ".")))
            project_root = payload.get("project_root")
            if not worktree.is_absolute() and isinstance(project_root, str) and project_root:
                worktree = Path(project_root) / worktree
            handle = runner.start(payload, worktree)
        except Exception as error:
            return {"kind": "retryable", "reason": type(error).__name__, "intent": payload.get("binding_intent") or (intent.to_dict() if hasattr(intent, "to_dict") else dict(intent))}
        # Existing runners expose RunHandle; only V4.4 contracts require
        # structured proof validation.  The call still traverses this helper.
        if contract.get("binding_contract_version") != "4.4" and hasattr(handle, "run_id"):
            return handle
        response = handle if isinstance(handle, dict) else getattr(handle, "__dict__", None)
        if not isinstance(response, dict):
            return {"kind": "binding_unknown", "reason": "provider response is unstructured", "intent": payload["binding_intent"]}
        expected = payload.get("binding_intent") or (
            intent.to_dict() if hasattr(intent, "to_dict") else dict(intent)
        )
        strict_v44 = contract.get("binding_contract_version") == "4.4"
        if strict_v44 and (not isinstance(payload.get("provider"), str) or not payload.get("provider") or not isinstance(payload.get("host") or payload.get("host_id"), str) or not (payload.get("host") or payload.get("host_id"))):
            return {"kind": "binding_unknown", "reason": "provider/host proof expectation is missing", "intent": expected}
        checks = {"provider": payload.get("provider"), "host": payload.get("host") or payload.get("host_id"),
                  "task_id": expected.get("task_id"), "issue_id": expected.get("node_id"), "role": expected.get("role"),
                  "generation": expected.get("generation"), "worktree": expected.get("worktree"),
                  "branch": expected.get("branch"), "writer": expected.get("writer"),
                  "authorization_digest": expected.get("authorization_digest"),
                  "node_contract_digest": expected.get("node_contract_digest")}
        # Cursor is runtime proof; it may be absent only while a provider
        # reports an explicitly pending setup, never on a confirmed start.
        required = dict(checks)
        required["cursor"] = None
        if strict_v44 and any(key not in response or not response.get(key) for key in required if key != "cursor"):
            return {"kind": "binding_unknown", "reason": "provider proof field missing", "intent": expected, "proof": response}
        if any(key not in response or (value is not None and response.get(key) != value) for key, value in checks.items()) or not response.get("cursor"):
            return {"kind": "binding_unknown", "reason": "provider proof mismatch", "intent": expected, "proof": response}
        return response

    def _v42_retry_status(self) -> str:
        """Use the explicit retry phase only for V4.2 initialized projects.

        Older snapshots retain their historical ``blocked_unknown`` label;
        V4.2 exposes the durable engineering phase as ``retry_pending``.
        """
        try:
            state_path = self.paths.vibe / "state.json"
            if state_path.is_file():
                state = json.loads(state_path.read_text(encoding="utf-8"))
                if isinstance(state, dict) and state.get("workflow_version") == 4:
                    return "retry_pending"
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
        return "blocked_unknown"

    @staticmethod
    def _legacy_run_allowed(state_data: Dict[str, Any]) -> bool:
        """Accept only an explicitly evidenced historical-run marker."""
        if not isinstance(state_data, dict) or state_data.get("legacy_run") is not True:
            return False
        evidence = state_data.get("legacy_evidence")
        if not isinstance(evidence, dict):
            return False
        source = evidence.get("source")
        run_id = evidence.get("run_id")
        if not isinstance(source, str) or not source.strip() or not isinstance(run_id, str) or not run_id.strip():
            return False
        try:
            validate_run_id(run_id)
        except ValueError:
            return False
        return True

    @staticmethod
    def _legacy_evidence_digest(evidence: Dict[str, Any]) -> str:
        payload = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @classmethod
    def _legacy_binding_valid(cls, legacy_run: Any, evidence: Any, digest: Any) -> bool:
        return legacy_run is True and cls._legacy_run_allowed({"legacy_run": legacy_run, "legacy_evidence": evidence}) and digest == cls._legacy_evidence_digest(evidence)

    @staticmethod
    def _lineage_matches_snapshot(snapshot: RunSnapshot, prd_digest: str, spec_digest: str) -> bool:
        return bool(prd_digest and spec_digest and snapshot.prd_digest == prd_digest and snapshot.spec_digest == spec_digest)

    def _execution_engine_binding(self, record: AuthorizationRecord) -> Tuple[str, str]:
        """Require the V4.1 complex plan to run through the DAG Monitor."""
        if getattr(self.plan, "complexity_band", "") != "complex":
            return "", ""
        override = getattr(record, "explicit_execution_mode_override", None)
        if isinstance(override, dict) and override:
            if not all(isinstance(override.get(key), str) and override.get(key).strip() for key in ("original_instruction", "alternative_mode")):
                raise PermissionError("execution_engine_unverified: malformed explicit execution override")
            raise PermissionError("execution_engine_unverified: explicit execution mode override has no V4.1 DAG guarantees")
        if record.execution_engine != "vibeguide_monitor" or record.engine_mode != "dag":
            raise PermissionError("execution_engine_unverified: V4.1 complex runs require vibeguide_monitor DAG")
        if not record.engine_evidence_ref or record.engine_evidence_ref.startswith("unverified:"):
            raise PermissionError("execution_engine_unverified: engine evidence is not verified")
        if record.dag_revision != self.plan.version:
            raise PermissionError("blocked_by_execution_topology_mismatch: DAG revision mismatch")
        # The authorization card's reference must resolve to fresh, intact
        # engine evidence before any provider dispatch is considered.  This
        # check is deliberately separate from authorization validation: an
        # attestation can prove engine identity, never grant execution rights.
        try:
            raw_path = self.paths.vibe / "plans" / self.plan.plan_id / "engine-attestation.json"
            if any(part.is_symlink() for part in (self.paths.vibe / "plans", self.paths.vibe / "plans" / self.plan.plan_id, raw_path)):
                raise ValueError("engine attestation must be a regular file")
            path = self.paths.resolve_vibe_path(
                Path("plans") / self.plan.plan_id / "engine-attestation.json"
            )
            if path.is_symlink() or not path.is_file():
                raise ValueError("engine attestation must be a regular file")
            attestation = json.loads(path.read_text(encoding="utf-8"))
            validate_engine_attestation(
                attestation, self.plan.plan_id, self.plan.version,
                now=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            )
            if attestation.get("evidence_ref") != record.engine_evidence_ref:
                raise ValueError("engine evidence reference mismatch")
            if record.agent_id and attestation.get("provider") != record.agent_id:
                raise ValueError("engine attestation provider mismatch")
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            raise PermissionError(
                "execution_engine_unverified: engine attestation is missing or invalid"
            ) from error
        return record.execution_engine, record.engine_mode

    def _topology_projection(self, snapshot: RunSnapshot) -> Tuple[List[str], str]:
        """Return the current ready-set and a stable dependency projection digest."""
        expected = []
        for node in self.nodes.values():
            expected.append({"id": node.id, "depends_on": list(node.depends_on), "parallel_group": node.parallel_group, "allowlist": list(node.allowlist), "owned_paths": list(node.owned_paths), "writer": node.writer, "reviewer": node.reviewer, "worktree": node.worktree})
        digest = hashlib.sha256(json.dumps(expected, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        blocked_ids = {
            node_id for node_id, current in snapshot.nodes.items()
            if current.get("status") in {"blocked_unknown", "blocked_design", "blocked_deploy"}
        }
        # Use the same node-scoped hard-dependency projection as DAG audit.
        # A repair/waiting node remains active for identity/capacity accounting,
        # while unrelated planned nodes stay eligible to start.
        projected = []
        legacy_topology = False
        for node in self.nodes.values():
            current = snapshot.nodes.get(node.id, {})
            runtime_status = current.get("status", getattr(node, "status", "planned"))
            # Snapshot recovery phases (retry_pending, timeout, failed, ...)
            # are intentionally richer than the DAG model. They are not
            # rewritten into the plan; use the node's last valid plan status
            # for readiness while retaining the snapshot phase elsewhere.
            if runtime_status not in {"planned", "ready", "running", "delivered", "review", "accepted", "rework", "blocked_design", "blocked_deploy", "blocked_unknown", "brief_pending"}:
                # Recovery phases such as retry_pending, timeout and failed
                # must stay non-ready until the same binding is confirmed.
                runtime_status = "running"
            if hasattr(node, "__dataclass_fields__"):
                projected.append(replace(node, status=runtime_status))
            else:
                projected_node = copy.copy(node)
                if not hasattr(projected_node, "contract"):
                    # Compatibility snapshots from V4.1 carried only the
                    # topology fields. Treat their absent contract as empty
                    # so the DAG gate can report not-ready without raising.
                    projected_node.contract = {}
                    legacy_topology = True
                projected_node.status = runtime_status
                projected.append(projected_node)
        if legacy_topology:
            by_id = {node.id: node for node in projected}
            ready = [
                node.id for node in projected
                if node.id not in blocked_ids
                and node.status in {"planned", "ready"}
                and all(by_id.get(dep) is not None and by_id[dep].status == "accepted" for dep in node.depends_on)
            ]
        else:
            ready = node_scoped_ready(projected, blocked_ids=blocked_ids)
        return ready, digest

    def _refresh_execution_projection(self, snapshot: RunSnapshot, entry: str = "monitor.unknown") -> None:
        ready, digest = self._topology_projection(snapshot)
        snapshot.ready_set = ready
        snapshot.topology_digest = digest
        snapshot.started_nodes = [n for n, c in snapshot.nodes.items() if c.get("active_task") or n in snapshot.handles]
        snapshot.active_concurrency = len(snapshot.started_nodes)
        snapshot.capacity = self._effective_worker_session_limit(
            self._snapshot_record(snapshot)
        )
        snapshot.monitor_entry_evidence = entry
        snapshot.parallel_groups = {n: ([self.nodes[n].parallel_group] if self.nodes[n].parallel_group else []) for n in snapshot.started_nodes}

    def _record_topology_projection(self, snapshot: RunSnapshot, entry: str) -> None:
        self._refresh_execution_projection(snapshot, entry)
        self._record(snapshot, "execution_topology_observed", {
            "run_id": snapshot.run_id, "plan_revision": snapshot.plan_version,
            "node_ids": sorted(self.nodes), "started_nodes": list(snapshot.started_nodes),
            "active_concurrency": snapshot.active_concurrency, "capacity": snapshot.capacity,
            "parallel_groups": snapshot.parallel_groups,
            "monitor_entry_evidence": entry,
        })

    def _validate_execution_topology(self, snapshot: RunSnapshot) -> None:
        if getattr(self.plan, "complexity_band", "") != "complex":
            return
        ready, digest = self._topology_projection(snapshot)
        if snapshot.dag_revision != self.plan.version or snapshot.topology_digest != digest:
            raise PermissionError("blocked_by_execution_topology_mismatch: topology projection drift")
        if list(snapshot.ready_set) != ready:
            raise PermissionError("blocked_by_execution_topology_mismatch: ready-set drift")
        started = [n for n, c in snapshot.nodes.items() if c.get("active_task") or n in snapshot.handles]
        if list(snapshot.started_nodes) != started:
            raise PermissionError("blocked_by_execution_topology_mismatch: started-set drift")
        if snapshot.active_concurrency != len(started) or (snapshot.capacity and snapshot.active_concurrency > snapshot.capacity):
            raise PermissionError("blocked_by_execution_topology_mismatch: capacity drift")
        expected_groups = {n: ([self.nodes[n].parallel_group] if self.nodes[n].parallel_group else []) for n in started}
        if snapshot.parallel_groups != expected_groups:
            raise PermissionError("blocked_by_execution_topology_mismatch: parallel-group drift")
        if not snapshot.monitor_entry_evidence.startswith("monitor."):
            raise PermissionError("blocked_by_execution_topology_mismatch: monitor entry evidence missing")
        if not hasattr(self, "paths"):
            return
        events = load_events(self.paths, snapshot.run_id)
        observed = [e for e in events if e.get("event") == "execution_topology_observed"]
        if not observed:
            raise PermissionError("blocked_by_execution_topology_mismatch: topology evidence missing")
        latest = observed[-1].get("data", {})
        expected = {"run_id": snapshot.run_id, "plan_revision": snapshot.plan_version, "node_ids": sorted(self.nodes), "started_nodes": snapshot.started_nodes, "active_concurrency": snapshot.active_concurrency, "capacity": snapshot.capacity, "parallel_groups": snapshot.parallel_groups}
        for key, value in expected.items():
            if latest.get(key) != value:
                raise PermissionError("blocked_by_execution_topology_mismatch: topology evidence drift")
        if latest.get("monitor_entry_evidence") != snapshot.monitor_entry_evidence:
            raise PermissionError("blocked_by_execution_topology_mismatch: monitor entry evidence drift")

    def __init__(
        self,
        paths: ProjectPaths,
        plan: Plan,
        nodes: List[DAGNode],
        context_policy: Optional[ContextBudgetPolicy] = None,
        topology_rulings: Optional[Any] = None,
    ):
        self.paths = paths
        self.plan = plan
        self.nodes = {node.id: node for node in nodes}
        # Platform dispatch-topology rulings (adapter_id -> ruling), normally
        # derived from the ISSUE-07 probe matrix.  Anything missing or
        # unrecognized is fail-closed to the conservative dual-visible
        # topology; UNKNOWN never upgrades a node to visible-sdd.
        self._topology_rulings: Dict[str, str] = {}
        if isinstance(topology_rulings, dict):
            for adapter_id, ruling in topology_rulings.items():
                if isinstance(adapter_id, str) and isinstance(ruling, str):
                    self._topology_rulings[adapter_id] = ruling
        # Binding reads are scoped to one public monitor operation.  The
        # registry is still the durable source of truth; clearing this cache
        # at each operation boundary prevents stale cross-tick identities.
        self._binding_cache: Dict[Tuple[str, str, str], TaskBinding] = {}
        self.context_policy = context_policy

    def _reset_binding_cache(self) -> None:
        self._binding_cache.clear()

    def _node_adapter_id(self, node: DAGNode) -> str:
        """Return the platform adapter id a node is ruled under, or ""."""
        contract = node.contract if isinstance(node.contract, dict) else {}
        adapter_id = contract.get("adapter_id")
        if isinstance(adapter_id, str) and adapter_id.strip():
            return adapter_id.strip()
        profile = contract.get("worker_profile")
        if isinstance(profile, dict):
            worker = profile.get("worker")
            if isinstance(worker, str) and worker.strip():
                return worker.strip()
        return ""

    def _node_dispatch_topology(self, node: DAGNode) -> str:
        """Rule the dispatch topology for one node.

        Resolution order, all fail-closed:

        1. an authorization-bound ``dispatch_topology`` persisted in the node
           contract (stamped when the plan was materialized);
        2. the live platform ruling injected at monitor construction
           (``in_session_sdd`` is the manifest capability-probe field name /
           ``DISPATCH_TOPOLOGY_MATRIX`` ruling value at the adapter layer; the
           supervisor translates it into the ``visible-sdd`` topology enum
           value at the dispatch layer);
        3. the conservative ``dual-visible`` default.  UNKNOWN evidence never
           yields ``visible-sdd``.
        """
        contract = node.contract if isinstance(node.contract, dict) else {}
        persisted = contract.get("dispatch_topology")
        if isinstance(persisted, str) and persisted in _DISPATCH_TOPOLOGIES:
            return persisted
        if getattr(node, "id", "") == "integration-review":
            # The integration-review node is a reviewer-role closeout node by
            # construction; the single-session developer topology can never
            # apply to it.
            return TOPOLOGY_DUAL_VISIBLE
        rulings = getattr(self, "_topology_rulings", None) or {}
        ruling = rulings.get(self._node_adapter_id(node))
        if ruling == _RULING_IN_SESSION_SDD:
            return TOPOLOGY_VISIBLE_SDD
        if ruling == TOPOLOGY_BACKGROUND:
            return TOPOLOGY_BACKGROUND
        return TOPOLOGY_DUAL_VISIBLE

    def _parallel_group_blocked(self, snapshot: RunSnapshot) -> set:
        """Node ids refused by the V4.6 parallel-group audit.

        The audit runs on the same status projection the scheduler uses for
        readiness, so ``running`` siblings hold their write scope against new
        dispatches.  ``_parallel_group_errors`` arrives with ISSUE-06; before
        that dag.py lands, the blocked set is empty (no behavior change).
        """
        audit = getattr(_dag_module, "_parallel_group_errors", None)
        if not callable(audit):
            return set()
        projected = self._projection_nodes(snapshot)
        try:
            return set(audit(projected))
        except (TypeError, ValueError):
            # Unverifiable audit metadata is fail-closed inside the audit
            # itself; a broken projection must not silently widen dispatch.
            return set(self.nodes)

    _OWNERSHIP_IN_FLIGHT = ("running", "rework", "review", "brief_pending")

    def _refuse_path_ownership_conflicts(self, snapshot: RunSnapshot) -> None:
        """Refuse the whole dispatch round when candidate writers collide.

        ``validate_path_ownership`` (V3.8) is the existing owned_paths check;
        until V4.7 nothing on the dispatch path called it.  The candidates
        are the planned nodes eligible for a first write this round plus the
        in-flight writers they would run beside, regardless of
        parallel_group.  Any overlap, missing ownership metadata or invalid
        path fails closed: every planned candidate becomes blocked_unknown,
        the conflict is recorded as an event and written to the run-scoped
        ``.vibe/runs/<run-id>/dag-audit.json`` (the plan-level dag-audit.json
        is digest-locked after authorization and must never be rewritten).
        """
        planned = []
        in_flight = []
        for node_id, node in self.nodes.items():
            current = snapshot.nodes[node_id]
            status = current.get("status")
            if status == "planned" and all(
                snapshot.nodes[dependency].get("status") == "accepted"
                for dependency in node.depends_on
            ):
                planned.append(node_id)
            elif status in self._OWNERSHIP_IN_FLIGHT:
                in_flight.append(node_id)
        if not planned:
            return
        candidates = planned + in_flight
        try:
            result = validate_path_ownership([self.nodes[node_id] for node_id in candidates])
        except (TypeError, ValueError) as error:
            conflicts, missing = [], []
            reason = "path ownership unverifiable: {}".format(error)
        else:
            if result.valid:
                return
            conflicts = [{"path": item.path, "nodes": list(item.nodes)} for item in result.conflicts]
            missing = list(result.missing_nodes)
            details = ["{} owned by {}".format(item["path"], ", ".join(item["nodes"])) for item in conflicts]
            if missing:
                details.append("ownership metadata missing on " + ", ".join(missing))
            reason = "path ownership conflict: " + "; ".join(details)
        affected = sorted({node_id for item in conflicts for node_id in item["nodes"]} | set(missing))
        for node_id in planned:
            self._mark_blocked_unknown(snapshot, node_id, reason, quarantine_lease=False)
        self._record(
            snapshot,
            "path_ownership_conflict",
            {
                "run_id": snapshot.run_id,
                "node_ids": list(candidates),
                "affected_nodes": affected,
                "reason": reason,
            },
        )
        audit = {
            "status": "blocked_unknown",
            "run_id": snapshot.run_id,
            "plan_revision": str(snapshot.plan_version),
            "node_count": len(candidates),
            "node_ids": list(candidates),
            "path_ownership": {
                "valid": False,
                "conflicts": conflicts,
                "missing_nodes": missing,
                "blocked_nodes": list(planned),
                "reason": reason,
            },
        }
        audit_path = run_dir(self.paths, snapshot.run_id, create=True) / "dag-audit.json"
        # Append rather than overwrite: multiple conflict rounds in the same run
        # each contribute a record so the full ownership history survives.
        existing: list = []
        if audit_path.is_file():
            try:
                raw = json.loads(audit_path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    existing = raw
                elif isinstance(raw, dict):
                    existing = [raw]
            except (OSError, ValueError):
                existing = []
        existing.append(audit)
        _atomic_bytes(
            audit_path,
            (json.dumps(existing, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"),
        )

    def _projection_nodes(self, snapshot: RunSnapshot) -> List[DAGNode]:
        """The status-projected node list used for readiness decisions."""
        projected = []
        for node in self.nodes.values():
            current = snapshot.nodes.get(node.id, {})
            runtime_status = current.get(
                "status", getattr(node, "status", "planned")
            )
            if runtime_status not in {"planned", "ready", "running", "delivered", "review", "accepted", "rework", "blocked_design", "blocked_deploy", "blocked_unknown", "brief_pending"}:
                runtime_status = "running"
            projected.append(replace(node, status=runtime_status))
        return projected

    def _effective_worker_session_limit(self, record: AuthorizationRecord) -> int:
        """Concurrency cap = min(authorization card snapshot, project config).

        The project config (``.vibe/config.json``,
        ``max_active_worker_sessions``) is validated by ``load_project_config``;
        an explicit but illegal value is a configuration error and propagates
        instead of being silently replaced.
        """
        card_limit = int(getattr(record, "active_pair_limit", 0) or 0)
        config_limit = load_project_config(self.paths.root).max_active_worker_sessions
        if card_limit < 1:
            return config_limit
        return min(card_limit, config_limit)

    def _supervisor_writer_rejection(
        self, node: DAGNode, current: Dict[str, Any]
    ) -> Optional[str]:
        """Return a reason when the node's writer is the supervisor itself.

        Structural guard: the monitor/supervisor identity must never be the
        writer of any node.  This does not rely on authorization-card text;
        every dispatch path passes through it.
        """
        contract = node.contract if isinstance(node.contract, dict) else {}
        candidates = [
            contract.get("worker"),
            contract.get("reviewer_worker"),
            current.get("worker"),
            getattr(node, "writer", ""),
            getattr(node, "reviewer", ""),
        ]
        profile = contract.get("worker_profile")
        if isinstance(profile, dict):
            candidates.extend([profile.get("worker"), profile.get("writer")])
        for value in candidates:
            if (
                isinstance(value, str)
                and value.strip().casefold() in _SUPERVISOR_WRITER_IDENTITIES
            ):
                return (
                    "supervisor identity cannot be a node writer: "
                    + value.strip()
                )
        return None

    def _current_plan_digests(self) -> Tuple[str, str]:
        values = []
        for ref in ("prd", "spec"):
            supplied = getattr(self.plan, ref + "_digest", "")
            if supplied:
                values.append(str(supplied))
                continue
            path_value = getattr(self.plan, ref + "_path", "")
            path = Path(path_value) if path_value else Path("")
            if path_value and not path.is_absolute():
                path = self.paths.root / path
            try:
                values.append(hashlib.sha256(path.read_bytes()).hexdigest() if path_value else "")
            except (OSError, ValueError):
                values.append("")
        return values[0], values[1]

    def _load_task_binding(
        self, snapshot: RunSnapshot, node_id: str, role: str
    ) -> TaskBinding:
        key = (snapshot.run_id, node_id, role)
        binding = self._binding_cache.get(key)
        if binding is None:
            binding = load_task_binding(
                self.paths, node_id, role, run_id=snapshot.run_id
            )
            self._binding_cache[key] = binding
        return binding

    def _save_task_binding(self, binding: TaskBinding) -> None:
        save_task_binding(self.paths, binding)
        if binding.run_id is not None:
            self._binding_cache[(binding.run_id, binding.issue_id, binding.role)] = binding

    def _require_binding_gate(
        self, contract: Dict[str, Any], binding: TaskBinding, runner: Runner
    ) -> None:
        """Fail closed before a real runner can start or write.

        ProviderActionRunner exposes the same gate so both boundaries execute
        it.  A generic runner is checked here as well, preserving legacy
        contracts that do not opt into V3.9.
        """
        if not binding_contract_enabled(contract):
            return
        verification = runtime_binding_gate(contract, binding)
        if not isinstance(verification, BindingVerification):
            raise ValueError("local provider binding gate returned invalid verification")
        if verification.binding_state != "binding_verified" or not verification.business_write_allowed:
            raise ValueError(
                "provider binding gate blocked_unknown: missing={} conflicts={}".format(
                    ",".join(verification.missing), ",".join(verification.conflicts)
                )
            )
        # Refresh the supervisor-owned lease at the write boundary.  A cached
        # observation is not sufficient for generic runners that expose no
        # provider hook; a released/expired lease must fail closed.
        node_id = str(contract.get("node_id") or binding.issue_id)
        fresh_lease = read_writer_lease(self.paths, node_id, binding.worktree)
        observed = getattr(binding, "binding_observation", None)
        if fresh_lease is None or observed is None or observed.lease != fresh_lease:
            raise ValueError("supervisor lease is stale or unavailable")
        refreshed = runtime_binding_gate(contract, binding)
        if (
            not isinstance(refreshed, BindingVerification)
            or refreshed.binding_state != "binding_verified"
            or not refreshed.business_write_allowed
        ):
            raise ValueError("provider binding gate blocked after lease refresh")
        # Provider hooks are advisory consistency checks only.  A provider
        # cannot manufacture permission by returning an object with
        # ``verified=True`` or another untrusted shape.
        probe = getattr(runner, "provider_binding_probe", None)
        if callable(probe):
            provider_probe = probe(contract, binding)
            if (
                not isinstance(provider_probe, BindingVerification)
                or provider_probe.binding_state != "binding_verified"
                or not provider_probe.verified
                or provider_probe != refreshed
            ):
                raise ValueError("provider binding probe returned invalid verification")
        gate = getattr(runner, "binding_gate", None)
        if callable(gate):
            provider_verification = gate(contract, binding)
            if not isinstance(provider_verification, BindingVerification):
                raise ValueError("provider binding gate returned invalid verification")
            if provider_verification != refreshed:
                raise ValueError("provider binding gate disagrees with local evidence")

    def _preflight_binding_before_provider(self, contract: Dict[str, Any]) -> None:
        """Gate provider actions before ``task_binding`` can emit a request.

        Unknown provider task ids are the sole exception: an explicit
        ``binding_probe`` may be emitted, and is marked non-business in its
        request.  Any ordinary V3.9 start requires complete protected live
        evidence first.
        """
        if not binding_contract_enabled(contract):
            return
        verification = runtime_binding_gate(contract)
        if not isinstance(verification, BindingVerification):
            raise ValueError("provider binding preflight returned invalid verification")
        if contract.get("binding_probe") is True:
            # An unknown provider task id is the sole controlled exception:
            # invoke the local gate first, then permit a non-business probe
            # only when no untrusted evidence was supplied.
            if verification.verified:
                return
            if (
                verification.missing == ["binding_provenance"]
                and "binding_intent" not in contract
                and "binding_observation" not in contract
            ):
                return
            raise ValueError("provider binding probe preflight blocked_unknown")
        if not verification.verified:
            raise ValueError("provider binding preflight blocked_unknown")

    @staticmethod
    def _binding_recovery_requested(contract: Dict[str, Any]) -> bool:
        """Identify the narrow JSON-restored continuation recovery path."""
        if not binding_contract_enabled(contract):
            return False
        if contract.get("continuation") is not True or contract.get("binding_bootstrap") is not True:
            return False
        return not (
            isinstance(contract.get("binding_intent"), BindingIntent)
            and isinstance(contract.get("binding_observation"), BindingObservation)
        )

    def _preflight_binding_recovery(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        role: str,
        contract: Dict[str, Any],
        current: Dict[str, Any],
    ) -> None:
        """Validate only the durable identity needed to enter bootstrap.

        A persisted binding has intentionally lost its private lease/cursor
        provenance.  This preflight therefore never returns a verified gate;
        it only proves that recovery will address the same task and the same
        constrained worktree.  The runner bootstrap performs the fresh Git
        observation and the subsequent full protected-evidence gate.
        """
        try:
            binding = self._load_task_binding(snapshot, node_id, role)
            root = self._worktree_path(current).resolve(strict=True)
            bound = Path(str(binding.worktree))
            if not bound.is_absolute():
                bound = self.paths.root / bound
            bound = bound.resolve(strict=True)
            managed_root = contract.get("managed_root")
            branch = contract.get("branch")
            base_sha = contract.get("base_sha")
            if not isinstance(managed_root, str) or not managed_root:
                raise ProviderUnavailable("binding recovery managed_root is missing")
            if not isinstance(branch, str) or not branch:
                raise ProviderUnavailable("binding recovery branch is missing")
            if not isinstance(base_sha, str) or not base_sha:
                raise ProviderUnavailable("binding recovery base_sha is missing")
            managed = Path(managed_root).resolve(strict=True)
            root.relative_to(managed)
            if bound != root:
                raise ProviderUnavailable("binding recovery worktree drift")
            if binding.issue_id != node_id or binding.role != role:
                raise ProviderUnavailable("binding recovery task scope drift")
            if binding.run_id != snapshot.run_id:
                raise ProviderUnavailable("binding recovery run drift")
            if binding.task_id != contract.get("task_id"):
                raise ProviderUnavailable("binding recovery task identity drift")
            if binding.branch != branch:
                raise ProviderUnavailable("binding recovery branch drift")
            expected_host = contract.get("host_id")
            if expected_host not in (None, "") and binding.host != expected_host:
                raise ProviderUnavailable("binding recovery host drift")
            expected_project = contract.get("project_id")
            if expected_project not in (None, ""):
                persisted_intent = binding.binding_intent
                if isinstance(persisted_intent, BindingIntent) and persisted_intent.project_id != expected_project:
                    raise ProviderUnavailable("binding recovery project drift")
        except ProviderUnavailable:
            raise
        except (FileNotFoundError, OSError, TypeError, ValueError) as error:
            raise ProviderUnavailable("binding recovery preflight blocked_unknown") from error

    def _run_binding_bootstrap(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        role: str,
        contract: Dict[str, Any],
        runner: Runner,
        current: Dict[str, Any],
    ) -> None:
        """Invoke the real runner bootstrap only for an explicit recovery."""
        if not binding_contract_enabled(contract) or (
            contract.get("binding_bootstrap") is not True
            and contract.get("continuation") is not True
        ):
            return
        bootstrap = getattr(runner, "binding_bootstrap", None)
        if not callable(bootstrap):
            raise ProviderUnavailable("binding bootstrap is unavailable")
        try:
            binding = self._load_task_binding(snapshot, node_id, role)
        except (FileNotFoundError, OSError, TypeError, ValueError) as error:
            raise ProviderUnavailable("binding bootstrap has no same-task binding") from error
        result = bootstrap(contract, binding, self._worktree_path(current))
        if not isinstance(result, BindingVerification) or not result.verified:
            raise ProviderUnavailable("binding bootstrap remains blocked_unknown")
        contract["binding_intent"] = binding.binding_intent
        contract["binding_observation"] = binding.binding_observation

    @staticmethod
    def classify_observation(observation: Any) -> ObservationDisposition:
        return classify_observation(observation)

    def self_heal_binding(self, snapshot: RunSnapshot, node_id: str, observation: Any) -> HealingResult:
        result = self_heal_binding(snapshot, node_id, observation)
        node_state = snapshot.nodes[node_id]
        frozen_binding = node_state.get("binding")
        if not isinstance(frozen_binding, dict):
            frozen_binding = {
                key: node_state.get(key)
                for key in ("task_id", "worktree", "branch", "allowlist", "cursor", "continuation_digest")
                if key in node_state
            }
        self._record(snapshot, "binding_self_healed" if result.repaired else "binding_degraded", {
            "run_id": snapshot.run_id, "node_id": node_id,
            "observation_kind": classify_observation(observation).reason,
            "disposition": "repaired" if result.repaired else "degraded",
            "action": "repair" if result.repaired else "isolate",
            "repaired": result.repaired, "isolated": result.isolated,
            "binding": frozen_binding,
        })
        save_snapshot(self.paths, snapshot)
        return result

    def isolate_affected_action(self, snapshot: RunSnapshot, node_id: str, reason: str) -> None:
        isolate_affected_action(snapshot, node_id, reason)
        self._record(snapshot, "action_isolated", {
            "run_id": snapshot.run_id, "node_id": node_id,
            "reason": reason, "disposition": "degraded", "action": "isolate", "isolated": True,
        })
        save_snapshot(self.paths, snapshot)

    def merge_change_request_local(
        self,
        run_id: str,
        change_request: ChangeRequest,
        local_facts: Optional[Dict[str, Any]] = None,
    ) -> LocalMergeEvidence:
        """Record a local-only Change Request outcome for an existing run.

        The method never invokes Git or a provider.  It only validates the
        persisted authorization and appends the evidence returned by
        :func:`merge_local` to the run event log.
        """
        snapshot = load_snapshot(self.paths, run_id)
        self._validate_execution_topology(snapshot)
        record = self._require_snapshot_authorization(snapshot)
        evidence = merge_local(change_request, record, local_facts)
        self._record(
            snapshot,
            "change_request_" + evidence.status,
            {
                "run_id": snapshot.run_id,
                "change_request": change_request.to_dict(),
                "evidence": evidence.to_dict(),
            },
        )
        self._record_topology_projection(snapshot, "monitor.merge_local")
        save_snapshot(self.paths, snapshot)
        return evidence

    # Short alias for callers that already operate in the Monitor boundary.
    merge_local = merge_change_request_local

    def record_change_request_action(
        self,
        run_id: str,
        action: str,
        target_contract: Any,
        evidence: Optional[Dict[str, Any]] = None,
        node_id: Optional[str] = None,
    ) -> ChangeRequestEvidence:
        """Record a target-bound PR/MR or merge action without calling a provider.

        A failed precondition quarantines only this external action.  When a
        node observation is supplied, callers may invoke ``self_heal_binding``
        first; this method never widens the frozen contract or authorization.
        """
        snapshot = load_snapshot(self.paths, run_id)
        record = self._require_snapshot_authorization(snapshot)
        prepared = execute_change_request(action, target_contract)
        payload = dict(evidence or {})
        payload.setdefault("action", action)
        payload.setdefault("target_contract", prepared.target_contract)
        allowed = (
            can_create_change_request(payload, record)
            if str(action).casefold() in {"create_pr", "create_mr"}
            else can_auto_merge(payload, record)
            if str(action).casefold() in {"merge_local", "merge_remote"}
            else False
        )
        status = "authorized" if allowed and prepared.status == "prepared" else "blocked_unknown"
        if not allowed:
            prepared = ChangeRequestEvidence(
                prepared.action, status, prepared.target_contract,
                prepared.target_digest, prepared.provider,
                remote_mutated=False,
                details={"reason": "authorization or evidence precondition failed"},
            )
            if node_id:
                self.isolate_affected_action(snapshot, node_id, "change request action evidence is incomplete")
        self._record(snapshot, "change_request_" + prepared.status, {
            "run_id": run_id, "action": prepared.action,
            "target_contract": prepared.target_contract,
            "target_digest": prepared.target_digest,
            "evidence": prepared.to_dict(),
            "external_execution": "not_performed",
        })
        save_snapshot(self.paths, snapshot)
        return prepared

    def start(
        self, record: Optional[AuthorizationRecord], runner: Runner
    ) -> RunSnapshot:
        self._reset_binding_cache()
        self._require_record(record)
        state = self.paths.vibe / "state.json"
        capability_contract_digest = ""
        task_workflow = None
        state_data = {}
        if state.is_file():
            try:
                require_entry(self.paths, "monitor:" + self.plan.plan_id, "monitor")
            except (OSError, ValueError, json.JSONDecodeError) as error:
                raise PermissionError("session_gate_blocked") from error
            capability_contract_digest = require_capability_contract(
                self.paths
            ).contract_digest
            try:
                state_data = json.loads(state.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError) as error:
                raise PermissionError("session_gate_blocked") from error
            # Evidence is selected by plan identity: a token supplied for one
            # plan must not start a different one, so unrelated evidence reads
            # as absent and the required-workflow gate below still blocks.
            task_workflow = select_plan_workflow(state_data, self.plan.plan_id)
            if task_workflow is not None:
                workflow_result = verify_workflow(task_workflow)
                if workflow_result.get("status") != "complete":
                    raise PermissionError("required_workflow_blocked: {}".format(workflow_result.get("node", "unknown")))
                # Recorded digests are otherwise never re-checked, so an
                # artifact edited after authorization would execute on stale
                # evidence.
                verify_workflow_artifacts(self.paths, task_workflow)
        elif self.paths.vibe.exists():
            raise PermissionError("session_gate_blocked: V2 state.json is missing")
        assert record is not None
        integration_run = any(node.id == "integration-review" for node in self.nodes.values())
        legacy_run = self._legacy_run_allowed(state_data)
        if integration_run and task_workflow is None and not legacy_run:
            raise PermissionError("required_workflow_blocked: workflow")
        prd_digest, spec_digest = self._current_plan_digests()
        if integration_run and (not prd_digest or not spec_digest):
            raise PermissionError("integration lineage blocked_unknown: PRD/Spec digest is missing")
        run_id = "run-" + uuid.uuid4().hex
        node_state: Dict[str, Dict[str, Any]] = {}
        for node_id, node in self.nodes.items():
            node_state[node_id] = {
                "status": "delivered" if node.status == "delivered" else "planned",
                "worker": node.contract.get("worker"),
                "worktree": node.contract.get("worktree", node_worktree(node_id)),
                "branch": node.contract.get("branch", node_branch(node_id)),
                "status_file": node.contract.get("status_file", ""),
                "handoff_file": node.contract.get("handoff_file", ""),
                "evidence": [],
                "active_role": None,
                "active_task": None,
                "start_intent": None,
                "quarantine": None,
                "developer_identity": None,
                "reviewer_identity": None,
                "developer_generation": 0,
                "review_generation": 0,
                "reviewer_started": False,
                "pair_archived": False,
                "old_task_reconciled": False,
                "retryable_action": None,
                "binding_phase": None,
                "contract_overrides": {},
                "corrections": [],
                "contract_digest": executable_contract_digest([node]),
                "acceptance": None,
            }
        snapshot = RunSnapshot(
            run_id=run_id,
            plan_id=self.plan.plan_id,
            plan_version=self.plan.version,
            status="initialized",
            nodes=node_state,
            handles={},
            tasks={},
            authorization=record.to_dict(),
            authorization_digest=record.digest,
            node_contract_digest=record.node_contract_digest,
            capability_contract_digest=capability_contract_digest,
            event_sequence=0,
            workflow=task_workflow,
            prd_digest=prd_digest,
            spec_digest=spec_digest,
            legacy_run=legacy_run,
            legacy_evidence=state_data.get("legacy_evidence") if legacy_run else None,
            legacy_evidence_digest=self._legacy_evidence_digest(state_data.get("legacy_evidence")) if legacy_run else "",
            execution_engine=record.execution_engine,
            engine_mode=record.engine_mode,
            engine_evidence_ref=record.engine_evidence_ref,
            dag_revision=record.dag_revision,
        )
        self._refresh_execution_projection(snapshot, "monitor.start")
        self._record(
            snapshot,
            "run_started",
            {
                "run_id": run_id,
                "authorization_digest": record.digest,
                "node_contract_digest": record.node_contract_digest,
                "capability_contract_digest": capability_contract_digest,
                "node_ids": sorted(self.nodes),
                "legacy_evidence_digest": snapshot.legacy_evidence_digest,
            },
        )
        # _schedule_ready() validates the execution topology, and that check
        # requires an `execution_topology_observed` event in this run's log.  A
        # brand-new complex run has none yet, and the projection below was only
        # recorded after dispatch, so the very first dispatch of every complex
        # plan failed with "topology evidence missing".  Publish the observation
        # now: the projection was refreshed above, so this records exactly the
        # state the validator is about to read.
        self._record_topology_projection(snapshot, "monitor.start")
        save_snapshot(self.paths, snapshot)
        if not self._context_allows_dispatch(snapshot, runner):
            save_snapshot(self.paths, snapshot)
            return snapshot
        self._schedule_ready(snapshot, runner)
        self._refresh_run_status(snapshot)
        self._record_topology_projection(snapshot, "monitor.start")
        save_snapshot(self.paths, snapshot)
        return snapshot

    def resume(self, run_id: str, runner: Runner, poll_handles: bool = True) -> RunSnapshot:
        self._reset_binding_cache()
        require_entry(self.paths, "resume:" + str(run_id), "resume")
        snapshot = load_snapshot(self.paths, run_id)
        if isinstance(snapshot.workflow, dict):
            result = verify_workflow(snapshot.workflow)
            if result.get("status") != "complete":
                raise PermissionError("required_workflow_blocked: {}".format(result.get("node", "unknown")))
            # Symmetric with start(): the lineage check below covers prd.md and
            # the spec path only, so without this a post-authorization edit to
            # nodes.json, dag-audit.json, plan-confirmation.json or the card
            # would go undetected on every tick after the first.  The digests
            # must be read from `.vibe/state.json`, not from
            # `snapshot.workflow`: `evidence` is one of the redacted keys, so
            # the copy saved with the run has every `ref` and `sha256` replaced
            # by a placeholder and could never match.
            live_workflow = load_live_workflow(self.paths, snapshot.plan_id)
            if live_workflow is None:
                raise PermissionError("required_workflow_blocked: authorizing evidence is no longer present")
            verify_workflow_artifacts(self.paths, live_workflow, run_started=True)
        legacy_allowed = self._legacy_binding_valid(snapshot.legacy_run, snapshot.legacy_evidence, snapshot.legacy_evidence_digest)
        if "integration-review" in snapshot.nodes and snapshot.workflow is None and not legacy_allowed:
            raise PermissionError("required_workflow_blocked: workflow")
        if "integration-review" in snapshot.nodes and not legacy_allowed:
            current_prd, current_spec = self._current_plan_digests()
            if not self._lineage_matches_snapshot(snapshot, current_prd, current_spec):
                raise PermissionError("integration lineage blocked_unknown: PRD/Spec source drift")
        checkpoint_path = self.paths.root / ".vibe" / "runs" / run_id / "monitor_checkpoint.json"
        if checkpoint_path.is_file():
            # Validate the recovery package before touching leases or polling.
            resume_from_checkpoint(self.paths, run_id)
        current_capability_contract = require_capability_contract(self.paths)
        if (
            not snapshot.capability_contract_digest
            or snapshot.capability_contract_digest
            != current_capability_contract.contract_digest
        ):
            raise PermissionError("capability_contract_unknown: run binding mismatch")
        self._require_snapshot_authorization(snapshot)
        self._reconcile_unapplied_events(snapshot)
        for node_id, current in snapshot.nodes.items():
            if current.get("status") == "start_pending":
                self._mark_blocked_unknown(
                    snapshot,
                    node_id,
                    "start intent has no provider-confirmed result",
                )
            elif current.get("status") in {"running", "review", "rework"}:
                if node_id not in snapshot.handles and not isinstance(
                    current.get("retryable_action"), dict
                ):
                    self._mark_blocked_unknown(
                        snapshot,
                        node_id,
                        "active task handle is missing during resume",
                    )
        # Poll durable handles before considering any new dispatch.  This is
        # the recovery ordering that prevents chat/context loss from creating
        # a second writer for an already-running provider task.
        if poll_handles:
            self._poll_active_handles(snapshot, runner)
        if self._context_allows_dispatch(snapshot, runner):
            replay_retry_pending = any(
                isinstance(current.get("retryable_action"), dict)
                and current.get("binding_phase") != "retry_pending"
                for current in snapshot.nodes.values()
            )
            # A caller explicitly replaying events without polling must see
            # the recovered snapshot before scheduling a new cycle. This is
            # especially important after an interrupted timeout event.
            if not (poll_handles is False and replay_retry_pending):
                self._schedule_ready(snapshot, runner, recover_missing_reviewer=True)
        self._refresh_run_status(snapshot)
        self._record_topology_projection(snapshot, "monitor.resume")
        save_snapshot(self.paths, snapshot)
        return snapshot

    def reauthorize(
        self,
        run_id: str,
        record: AuthorizationRecord,
        runner: Runner,
        change_reason: str,
    ) -> RunSnapshot:
        """Continue the same run under a newly confirmed same-plan contract."""

        self._reset_binding_cache()
        self._require_record(record)
        snapshot = load_snapshot(self.paths, run_id)
        state = self.paths.vibe / "state.json"
        current_capability_contract_digest = snapshot.capability_contract_digest
        capability_contract_changed = False
        if state.is_file():
            current_capability_contract = require_capability_contract(self.paths)
            current_capability_contract_digest = current_capability_contract.contract_digest
            capability_contract_changed = (
                snapshot.capability_contract_digest
                != current_capability_contract_digest
            )
            if capability_contract_changed and change_reason not in {
                "capability_contract_changed",
                "executable_contract_changed",
            }:
                raise PermissionError(
                    "capability_contract_unknown: explicit recovery reason required"
                )
        self._reconcile_unapplied_events(snapshot)
        # The replay may already have applied this exact authorization and
        # capability transition.  Re-read the live contract after replay so
        # an interrupted call cannot append the same transition again.
        if state.is_file():
            current_capability_contract = require_capability_contract(self.paths)
            current_capability_contract_digest = current_capability_contract.contract_digest
            capability_contract_changed = (
                snapshot.capability_contract_digest
                != current_capability_contract_digest
            )
        if (
            snapshot.plan_id != self.plan.plan_id
            or snapshot.plan_version != self.plan.version
            or set(snapshot.nodes) != set(self.nodes)
        ):
            raise PermissionError("reauthorization must remain on the same plan revision")
        if snapshot.authorization_digest == record.digest and not capability_contract_changed:
            self._require_snapshot_authorization(snapshot)
            self._schedule_ready(snapshot, runner)
            self._refresh_run_status(snapshot)
            save_snapshot(self.paths, snapshot)
            return snapshot

        previous = self._snapshot_record(snapshot)
        previous_node_contract_digests = {
            node_id: snapshot.nodes[node_id].get("contract_digest")
            for node_id in snapshot.nodes
        }
        new_node_contract_digests = {
            node_id: executable_contract_digest([self.nodes[node_id]])
            for node_id in snapshot.nodes
        }
        changed_nodes = sorted(
            node_id
            for node_id in snapshot.nodes
            if previous_node_contract_digests[node_id]
            != new_node_contract_digests[node_id]
        )
        affected_nodes = affected_node_closure(
            list(self.nodes.values()), changed_nodes
        )
        authorized_node_contracts = {
            item["id"]: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for item in canonical_node_contracts(list(self.nodes.values()))
        }
        accepted_nodes = sorted(
            node_id
            for node_id, current in snapshot.nodes.items()
            if current.get("status") == "accepted"
        )
        retained_acceptances = {}
        invalidated_acceptances = {}
        for node_id, current in snapshot.nodes.items():
            if current.get("status") != "accepted":
                continue
            acceptance = current.get("acceptance")
            if not isinstance(acceptance, dict):
                raise RuntimeError("accepted node acceptance evidence is missing")
            evidence = {
                "contract_digest": acceptance.get("contract_digest"),
                "authorization_epoch": acceptance.get("authorization_epoch"),
            }
            if (
                node_id not in affected_nodes
                and previous_node_contract_digests.get(node_id)
                == new_node_contract_digests[node_id]
                and evidence["contract_digest"]
                == previous_node_contract_digests[node_id]
            ):
                retained_acceptances[node_id] = evidence
            else:
                invalidated_acceptances[node_id] = evidence
        for node_id, handle_id in list(snapshot.handles.items()):
            current = snapshot.nodes[node_id]
            active = current.get("active_task")
            if not isinstance(active, dict) or active.get("handle_id") != handle_id:
                raise RuntimeError("active task cannot be reconciled for reauthorization")
            if not self._stop_active_for_transition(
                snapshot,
                node_id,
                str(active["role"]),
                handle_id,
                runner,
                "stopped",
            ):
                save_snapshot(self.paths, snapshot)
                raise RuntimeError("active task stop cannot be proven for reauthorization")
            snapshot.nodes[node_id]["old_task_reconciled"] = True

        continuation: Dict[str, Any] = {}
        for node_id in sorted(snapshot.nodes):
            for role in ("developer", "reviewer"):
                key = "{}:{}".format(node_id, role)
                try:
                    binding = self._load_task_binding(snapshot, node_id, role)
                except FileNotFoundError:
                    continue
                snapshot.tasks[key] = binding.to_dict()
                continuation[key] = {
                    "task_id": binding.task_id,
                    "cursor": binding.cursor,
                }

        transition = {
            "run_id": snapshot.run_id,
            "previous_authorization": previous.to_dict(),
            "previous_authorization_digest": previous.digest,
            "previous_node_contract_digest": previous.node_contract_digest,
            "previous_capability_contract_digest": snapshot.capability_contract_digest,
            "new_authorization": record.to_dict(),
            "authorization_digest": record.digest,
            "node_contract_digest": record.node_contract_digest,
            "capability_contract_digest": current_capability_contract_digest,
            "previous_node_contract_digests": previous_node_contract_digests,
            "node_contract_digests": new_node_contract_digests,
            "authorized_node_contracts": authorized_node_contracts,
            "changed_nodes": changed_nodes,
            "affected_nodes": affected_nodes,
            "accepted_nodes": accepted_nodes,
            "retained_acceptances": retained_acceptances,
            "invalidated_acceptances": invalidated_acceptances,
            "change_reason": change_reason,
            "continuation": continuation,
        }
        self._record(snapshot, "authorization_reauthorized", transition)
        self._apply_reauthorization_transition(snapshot, transition)
        self._schedule_ready(snapshot, runner)
        self._refresh_run_status(snapshot)
        self._refresh_execution_projection(snapshot, "monitor.reauthorize")
        save_snapshot(self.paths, snapshot)
        return snapshot

    def tick(self, run_id: str, runner: Runner) -> RunSnapshot:
        self._reset_binding_cache()
        snapshot = load_snapshot(self.paths, run_id)
        self._require_snapshot_authorization(snapshot)
        self._validate_execution_topology(snapshot)
        self._reconcile_unapplied_events(snapshot)
        self._poll_active_handles(snapshot, runner)
        self._record_topology_projection(snapshot, "monitor.tick")
        if self._context_allows_dispatch(snapshot, runner):
            self._schedule_ready(snapshot, runner)
        self._record_topology_projection(snapshot, "monitor.tick")
        self._refresh_run_status(snapshot)
        save_snapshot(self.paths, snapshot)
        return snapshot

    def record_integration_review(self, run_id: str, evidence: Dict[str, Any]) -> RunSnapshot:
        """Persist a validated integration review and refresh run closeout."""
        self._reset_binding_cache()
        snapshot = load_snapshot(self.paths, run_id)
        self._require_snapshot_authorization(snapshot)
        self._validate_execution_topology(snapshot)
        _record_integration_review(snapshot, evidence)
        self._record(snapshot, "integration_review_recorded", {
            "run_id": snapshot.run_id,
            "node_id": "integration-review",
            "evidence": evidence,
            "clearance": evidence.get("clearance"),
        })
        self._refresh_run_status(snapshot)
        self._record_topology_projection(snapshot, "monitor.record_integration_review")
        save_snapshot(self.paths, snapshot)
        return snapshot

    def reconcile_evidence(self, run_id: str, package: Dict[str, Any]) -> RunSnapshot:
        """Promote a verified, same-run evidence package through normal events.

        This path is deliberately provider-free: it only reuses the original
        task bindings and appends ordinary ``delivered``/``accepted`` events.
        Any malformed, stale, mixed, or unverifiable package is rejected before
        the first event is appended.
        """
        self._reset_binding_cache()
        snapshot = load_snapshot(self.paths, run_id)
        self._require_snapshot_authorization(snapshot)
        self._validate_reconciliation_package(snapshot, package)
        if all(node.get("status") == "accepted" for node in snapshot.nodes.values()):
            return snapshot
        for item in package["nodes"]:
            current = snapshot.nodes[item["node_id"]]
            if current.get("active_task") is not None or item["node_id"] in snapshot.handles:
                raise ValueError("reconciliation target has an active writer")

        for item in package["nodes"]:
            node_id = item["node_id"]
            current = snapshot.nodes[node_id]
            developer = self._load_task_binding(snapshot, node_id, "developer")
            reviewer = self._load_task_binding(snapshot, node_id, "reviewer")
            # Establish an in-memory active registration for event application;
            # this is not a new task or writer and is never dispatched.
            dev_handle = str(item["developer"].get("handle_id") or "reconcile:" + node_id + ":developer")
            current["active_role"] = "developer"
            current["active_task"] = {
                "role": "developer", "task_id": developer.task_id,
                "generation": developer.generation, "handle_id": dev_handle,
            }
            snapshot.handles[node_id] = dev_handle
            previous_sequence = snapshot.event_sequence
            self._record(
                snapshot,
                "delivered",
                {"run_id": snapshot.run_id, "node_id": node_id,
                 "evidence": item["developer"].get("evidence_ref", "reconciliation")},
                current["active_task"],
            )
            snapshot.event_sequence = previous_sequence
            self._reconcile_unapplied_events(snapshot)
            current.setdefault("evidence", []).append(
                redact_provider_text(item["developer"].get("evidence_ref", "reconciliation"))
            )

            reviewer_handle = str(item["reviewer"].get("handle_id") or "reconcile:" + node_id + ":reviewer")
            current["active_role"] = "reviewer"
            current["active_task"] = {
                "role": "reviewer", "task_id": reviewer.task_id,
                "generation": reviewer.generation, "handle_id": reviewer_handle,
            }
            snapshot.handles[node_id] = reviewer_handle
            previous_sequence = snapshot.event_sequence
            self._record(
                snapshot,
                "accepted",
                {"run_id": snapshot.run_id, "node_id": node_id,
                 "contract_digest": current["contract_digest"],
                 "authorization_epoch": snapshot.authorization_digest,
                 "evidence": {"source": "reconciliation", "clearance": item["reviewer"]["clearance"]}},
                current["active_task"],
            )
            snapshot.event_sequence = previous_sequence
            self._reconcile_unapplied_events(snapshot)
            current.setdefault("evidence", []).append(
                redact_provider_text(item["reviewer"].get("evidence_ref", "reconciliation"))
            )
            previous_sequence = snapshot.event_sequence
            self._record(
                snapshot,
                "pair_archived",
                {"run_id": snapshot.run_id, "node_id": node_id,
                 "clearance": current.get("review_clearance", {"p0": 0, "p1": 0, "p2": 0})},
            )
            snapshot.event_sequence = previous_sequence
            self._reconcile_unapplied_events(snapshot)

        self._refresh_run_status(snapshot)
        save_snapshot(self.paths, snapshot)
        return snapshot

    def _validate_reconciliation_package(
        self, snapshot: RunSnapshot, package: Dict[str, Any]
    ) -> None:
        if not isinstance(package, dict):
            raise ValueError("reconciliation package is invalid")
        required = {
            "schema_version", "run_id", "plan_id", "plan_revision",
            "authorization_digest", "node_contract_digest", "nodes",
        }
        if set(package) != required or package.get("schema_version") != 1:
            raise ValueError("reconciliation package schema is invalid")
        if (
            package["run_id"] != snapshot.run_id
            or package["plan_id"] != self.plan.plan_id
            or package["plan_revision"] != self.plan.version
            or package["authorization_digest"] != snapshot.authorization_digest
            or package["node_contract_digest"] != snapshot.node_contract_digest
        ):
            raise ValueError("reconciliation run or contract digest mismatch")
        records = package["nodes"]
        if not isinstance(records, list):
            raise ValueError("reconciliation package nodes are invalid")
        ids = [item.get("node_id") if isinstance(item, dict) else None for item in records]
        all_accepted = all(current.get("status") == "accepted" for current in snapshot.nodes.values())
        expected_ids = {
            node_id for node_id, current in snapshot.nodes.items()
            if current.get("status") != "accepted"
        }
        if all_accepted and not records:
            return
        if all_accepted:
            expected_ids = set(snapshot.nodes)
        if any(not isinstance(node_id, str) for node_id in ids) or len(ids) != len(set(ids)) or set(ids) != expected_ids:
            raise ValueError("reconciliation package node set is invalid")
        if all_accepted:
            for item in records:
                if not isinstance(item, dict) or set(item) != {"node_id", "developer", "reviewer"}:
                    raise ValueError("reconciliation node record is invalid")
            return
        for item in records:
            node_id = item["node_id"]
            if set(item) != {"node_id", "developer", "reviewer"}:
                raise ValueError("reconciliation node record is invalid")
            current = snapshot.nodes[node_id]
            for role in ("developer", "reviewer"):
                claim = item[role]
                if not isinstance(claim, dict):
                    raise ValueError("reconciliation task evidence is invalid")
                binding = self._load_task_binding(snapshot, node_id, role)
                for key in ("task_id", "generation", "worktree", "branch", "status"):
                    if claim.get(key) != getattr(binding, key):
                        raise ValueError("reconciliation {} identity mismatch".format(role))
                self._validate_head(binding, claim.get("head"))
                if binding.task_id != current.get(role + "_identity"):
                    raise ValueError("reconciliation {} task identity is stale".format(role))
                if binding.worktree != str(current.get("worktree")) or binding.branch != str(current.get("branch")):
                    raise ValueError("reconciliation {} worktree or branch mismatch".format(role))
                self._validate_evidence_file(binding, claim.get("status_file"), "status_file")
                self._validate_evidence_file(binding, claim.get("handoff_file"), "handoff_file")
            reviewer_claim = item["reviewer"]
            clearance = reviewer_claim.get("clearance")
            if not isinstance(clearance, dict) or set(clearance) != {"p0", "p1", "p2"} or any(clearance[key] != 0 for key in clearance):
                raise ValueError("reviewer P0-P2 clearance is not zero")

    def _validate_evidence_file(self, binding: TaskBinding, value: Any, field: str) -> None:
        if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
            raise ValueError("reconciliation {} evidence is invalid".format(field))
        path_value = value["path"]
        digest = value["sha256"]
        if not isinstance(path_value, str) or not path_value or Path(path_value).is_absolute() or "\x00" in path_value:
            raise ValueError("reconciliation {} path is invalid".format(field))
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("reconciliation {} hash is invalid".format(field))
        expected = getattr(binding, field)
        if expected and path_value != expected:
            raise ValueError("reconciliation {} path does not match binding".format(field))
        root = self._worktree_path({"worktree": binding.worktree})
        candidate = (root / path_value).resolve(strict=False)
        try:
            candidate.relative_to(root.resolve())
        except ValueError as error:
            raise ValueError("reconciliation evidence path escapes worktree") from error
        if candidate.is_symlink() or not candidate.is_file():
            raise ValueError("reconciliation evidence file is unavailable")
        if hashlib.sha256(candidate.read_bytes()).hexdigest() != digest:
            raise ValueError("reconciliation evidence hash mismatch")

    def _validate_head(self, binding: TaskBinding, claimed: Any) -> None:
        if not isinstance(claimed, str) or len(claimed) != 40 or any(c not in "0123456789abcdef" for c in claimed.lower()):
            raise ValueError("reconciliation HEAD is invalid")
        worktree = self._worktree_path({"worktree": binding.worktree})
        try:
            observed = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "--verify", "HEAD"],
                check=True, capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            observed_branch = subprocess.run(
                ["git", "-C", str(worktree), "branch", "--show-current"],
                check=True, capture_output=True, text=True, timeout=5,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError) as error:
            raise ValueError("reconciliation HEAD is unavailable") from error
        if observed.lower() != claimed.lower():
            raise ValueError("reconciliation HEAD mismatch")
        if observed_branch != binding.branch:
            raise ValueError("reconciliation branch mismatch: {} != {}".format(observed_branch, binding.branch))

    def _poll_active_handles(self, snapshot: RunSnapshot, runner: Runner) -> None:
        for node_id, handle_id in list(snapshot.handles.items()):
            self._require_snapshot_authorization(snapshot)
            current = snapshot.nodes.get(node_id, {})
            try:
                events = runner.poll(RunHandle(handle_id))
            except Exception as error:
                reason = "runner poll failed ({})".format(type(error).__name__)
                if classify_provider_failure({"reason": reason}).get("legacy_kind") == "engineering":
                    active = snapshot.nodes[node_id].get("active_task") or {}
                    self._queue_active_retry(snapshot, node_id, reason)
                    if active.get("role"):
                        self._set_binding_status(snapshot, node_id, str(active["role"]), "blocked_unknown")
                    snapshot.nodes[node_id]["status"] = self._v42_retry_status()
                    snapshot.nodes[node_id]["reason"] = reason
                else:
                    self._mark_blocked_unknown(
                        snapshot, node_id, reason,
                        quarantine_lease=False,
                        retryable_same_task=True,
                    )
                continue
            for event in events:
                if event.event in {"context_overflow", "context_exhausted", "overflow"}:
                    active = snapshot.nodes[node_id].get("active_task")
                    self._record_runner_event(snapshot, node_id, event, active or {})
                    self._checkpoint_context(snapshot, "provider reported context overflow", exhausted=True)
                    continue
                self._apply_event(snapshot, node_id, handle_id, event, runner)

    def _require_record(self, record: Optional[AuthorizationRecord]) -> None:
        if record is None or not is_authorization_valid(
            record, self.plan, list(self.nodes.values())
        ):
            raise PermissionError("a valid executable-contract authorization is required")
        if record.node_ids != tuple(sorted(self.nodes)):
            raise PermissionError("authorization node scope does not match monitor scope")
        self._execution_engine_binding(record)

    def _snapshot_record(self, snapshot: RunSnapshot) -> AuthorizationRecord:
        try:
            return AuthorizationRecord.from_dict(snapshot.authorization)
        except (TypeError, ValueError) as error:
            raise PermissionError("snapshot authorization record is invalid") from error

    def _consistency_binding(
        self, record: AuthorizationRecord, node: DAGNode
    ) -> Dict[str, Any]:
        project_root = str(self.paths.root.resolve())
        return {
            "schema_version": 1,
            "project_digest": hashlib.sha256(project_root.encode("utf-8")).hexdigest(),
            "plan_id": self.plan.plan_id,
            "plan_version": self.plan.version,
            "decision_digest": record.decision_digest,
            "authorization_digest": record.digest,
            "issue_contract_digest": executable_contract_digest([node]),
        }

    def _require_snapshot_authorization(self, snapshot: RunSnapshot) -> AuthorizationRecord:
        record = self._snapshot_record(snapshot)
        if snapshot.plan_id != self.plan.plan_id or snapshot.plan_version != self.plan.version:
            raise PermissionError("snapshot plan no longer matches monitor plan")
        if snapshot.authorization_digest != record.digest:
            raise PermissionError("snapshot authorization digest is inconsistent")
        if snapshot.node_contract_digest != record.node_contract_digest:
            raise PermissionError("snapshot contract digest is inconsistent")
        self._require_record(record)
        return record

    def _apply_reauthorization_transition(
        self, snapshot: RunSnapshot, data: Dict[str, Any]
    ) -> None:
        previous = AuthorizationRecord.from_dict(data.get("previous_authorization"))
        replacement = AuthorizationRecord.from_dict(data.get("new_authorization"))
        if (
            previous.to_dict() != snapshot.authorization
            or data.get("previous_authorization_digest")
            != snapshot.authorization_digest
            or data.get("previous_node_contract_digest")
            != snapshot.node_contract_digest
            or data.get("previous_capability_contract_digest")
            != snapshot.capability_contract_digest
        ):
            raise ValueError("reauthorization previous lineage is inconsistent")
        self._require_record(replacement)
        if (
            data.get("authorization_digest") != replacement.digest
            or data.get("node_contract_digest")
            != replacement.node_contract_digest
            or data.get("change_reason")
            not in {"executable_contract_changed", "capability_contract_changed"}
        ):
            raise ValueError("reauthorization replacement lineage is inconsistent")
        replacement_capability_digest = data.get("capability_contract_digest")
        if replacement_capability_digest:
            if (
                not isinstance(replacement_capability_digest, str)
                or len(replacement_capability_digest) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in replacement_capability_digest
                )
            ):
                raise ValueError("reauthorization capability contract lineage is invalid")
        elif snapshot.capability_contract_digest:
            raise ValueError("reauthorization capability contract lineage is missing")
        continuation = data.get("continuation")
        if not isinstance(continuation, dict):
            raise ValueError("reauthorization continuation evidence is invalid")
        for key, evidence in continuation.items():
            if not isinstance(key, str) or ":" not in key or not isinstance(evidence, dict):
                raise ValueError("reauthorization continuation evidence is invalid")
            node_id, role = key.rsplit(":", 1)
            if node_id not in snapshot.nodes or role not in {"developer", "reviewer"}:
                raise ValueError("reauthorization continuation identity is invalid")
            binding = self._load_task_binding(snapshot, node_id, role)
            if (
                set(evidence) != {"task_id", "cursor"}
                or evidence["task_id"] != binding.task_id
                or evidence["cursor"] != binding.cursor
            ):
                raise ValueError("reauthorization continuation cursor is inconsistent")
            snapshot.tasks[key] = binding.to_dict()

        previous_node_contract_digests = data.get("previous_node_contract_digests")
        node_contract_digests = data.get("node_contract_digests")
        if (
            not isinstance(previous_node_contract_digests, dict)
            or not isinstance(node_contract_digests, dict)
            or set(previous_node_contract_digests) != set(snapshot.nodes)
            or set(node_contract_digests) != set(snapshot.nodes)
            or any(
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
                for mapping in (previous_node_contract_digests, node_contract_digests)
                for value in mapping.values()
            )
        ):
            raise ValueError("reauthorization node contract lineage is invalid")
        retained_acceptances = data.get("retained_acceptances")
        invalidated_acceptances = data.get("invalidated_acceptances")
        changed_nodes = data.get("changed_nodes")
        affected_nodes = data.get("affected_nodes")
        accepted_nodes = data.get("accepted_nodes")
        authorized_node_contracts = data.get("authorized_node_contracts")
        if not isinstance(retained_acceptances, dict) or not isinstance(
            invalidated_acceptances, dict
        ) or not isinstance(authorized_node_contracts, dict) or set(
            authorized_node_contracts
        ) != set(snapshot.nodes) or any(
            not isinstance(value, str)
            for value in authorized_node_contracts.values()
        ) or any(
            not isinstance(items, list)
            or any(not isinstance(node_id, str) for node_id in items)
            or len(items) != len(set(items))
            or items != sorted(items)
            or any(node_id not in snapshot.nodes for node_id in items)
            for items in (changed_nodes, affected_nodes, accepted_nodes)
        ):
            raise ValueError("reauthorization acceptance lineage is invalid")
        if set(retained_acceptances) & set(invalidated_acceptances):
            raise ValueError("reauthorization acceptance lineage overlaps")
        expected_changed_nodes = sorted(
            node_id
            for node_id in snapshot.nodes
            if previous_node_contract_digests[node_id]
            != node_contract_digests[node_id]
        )
        if changed_nodes != expected_changed_nodes:
            raise ValueError("reauthorization changed node lineage is invalid")
        expected_authorized_node_contracts = {
            item["id"]: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for item in canonical_node_contracts(list(self.nodes.values()))
        }
        if authorized_node_contracts != expected_authorized_node_contracts:
            raise ValueError("reauthorization authorized DAG proof is invalid")
        if affected_nodes != affected_node_closure(
            list(self.nodes.values()), changed_nodes
        ):
            raise ValueError("reauthorization affected suffix lineage is invalid")
        retained_ids = set(retained_acceptances)
        invalidated_ids = set(invalidated_acceptances)
        current_accepted_ids = {
            node_id
            for node_id, current in snapshot.nodes.items()
            if current.get("status") == "accepted"
        }
        if (
            accepted_nodes != sorted(retained_ids | invalidated_ids)
            or current_accepted_ids != set(accepted_nodes)
            or set(changed_nodes) - set(affected_nodes)
            or retained_ids & set(affected_nodes)
            or not invalidated_ids.issubset(set(affected_nodes))
            or retained_ids | invalidated_ids != set(accepted_nodes)
        ):
            raise ValueError("reauthorization affected suffix disposition is invalid")

        for node_id, evidence in retained_acceptances.items():
            if (
                not isinstance(evidence, dict)
                or set(evidence) != {"contract_digest", "authorization_epoch"}
                or evidence["contract_digest"]
                != previous_node_contract_digests[node_id]
                or evidence["contract_digest"] != node_contract_digests[node_id]
                or evidence["authorization_epoch"] != previous.digest
            ):
                raise ValueError("retained acceptance evidence is invalid")
        for node_id, evidence in invalidated_acceptances.items():
            if (
                not isinstance(evidence, dict)
                or set(evidence) != {"contract_digest", "authorization_epoch"}
                or evidence["contract_digest"]
                != previous_node_contract_digests[node_id]
                or evidence["authorization_epoch"] != previous.digest
            ):
                raise ValueError("invalidated acceptance evidence is invalid")

        snapshot.authorization = replacement.to_dict()
        snapshot.authorization_digest = replacement.digest
        snapshot.node_contract_digest = replacement.node_contract_digest
        snapshot.capability_contract_digest = replacement_capability_digest or ""
        snapshot.status = "running"
        snapshot.handles.clear()
        for node_id, current in snapshot.nodes.items():
            current["contract_digest"] = node_contract_digests[node_id]
            current["active_role"] = None
            current["active_task"] = None
            current["start_intent"] = None
            current["quarantine"] = None
            if current.get("status") == "accepted":
                if node_id in retained_acceptances:
                    evidence = retained_acceptances[node_id]
                    current["acceptance"] = {
                        "contract_digest": evidence["contract_digest"],
                        "authorization_epoch": replacement.digest,
                    }
                    continue
                if node_id in invalidated_acceptances:
                    current["acceptance"] = None
                    self._queue_reauthorization_continuation(
                        current,
                        self._continuation_requires_successor(
                            snapshot, node_id, "developer"
                        ),
                    )
                    continue
                raise ValueError("accepted node lacks reauthorization disposition")
            if int(current.get("developer_generation", 0)) > 0:
                self._queue_reauthorization_continuation(
                    current,
                    self._continuation_requires_successor(
                        snapshot, node_id, "developer"
                    ),
                )
            else:
                current["status"] = "planned"
                current["retryable_action"] = None

    def _continuation_requires_successor(
        self, snapshot: RunSnapshot, node_id: str, role: str
    ) -> bool:
        """Return whether reauthorization needs a new task identity.

        A provider-confirmed terminal binding is still a canonical task that
        can be continued.  Only a missing binding (or a non-terminal binding
        whose state must be reconciled by the scheduler) is a successor
        candidate.  Malformed registry data remains fail-closed: the
        scheduler will keep the node blocked rather than guessing.
        """
        try:
            binding = self._load_task_binding(snapshot, node_id, role)
        except FileNotFoundError:
            return True
        except (OSError, TypeError, ValueError):
            return True
        # A provider-confirmed delivery is a terminal developer handoff: the
        # same visible task can be resumed for authorized rework.  Treating
        # it as a successor candidate would require a stop/absence proof even
        # though no active writer remains, leaving ordinary delivered work
        # permanently blocked.  Unknown/running/review states remain
        # successor candidates and still require the fail-closed proof below.
        return binding.status not in {"delivered", "stopped", "failed", "archived"}

    @staticmethod
    def _queue_reauthorization_continuation(
        current: Dict[str, Any], successor_candidate: bool
    ) -> None:
        current["pair_archived"] = True
        current["status"] = "blocked_unknown"
        current["retryable_action"] = {
            "role": "developer",
            "phase": "rework",
            "continuation": True,
            "pending_schedule": True,
            "successor_candidate": successor_candidate,
        }

    def _recover_quarantined_delivered_developer(
        self, snapshot: RunSnapshot, node_id: str, record_event: bool = True
    ) -> bool:
        """Rebuild a lost continuation marker from a delivered binding.

        An interrupted reauthorization can persist the quarantine transition
        after the retry marker was lost.  A current-run delivered developer
        binding is sufficient evidence to continue that same visible task;
        it is not evidence for creating a successor or another writer.
        """
        current = snapshot.nodes[node_id]
        if self._node_dispatch_topology(self.nodes[node_id]) == TOPOLOGY_VISIBLE_SDD:
            # A delivered visible-sdd binding only reaches blocked_unknown
            # when its in-session review evidence was rejected.  That is a
            # fail-closed terminal doubt, not a recoverable continuation.
            return False
        if (
            current.get("status") != "blocked_unknown"
            or not isinstance(current.get("quarantine"), dict)
            or current.get("active_task") is not None
            or current.get("active_role") is not None
            or current.get("start_intent") is not None
            or node_id in snapshot.handles
            or current.get("retryable_action") is not None
            # If reviewer dispatch already began, a delivered developer is
            # not a rework candidate.  Recovery must reconstruct the missing
            # reviewer binding and keep the node fail-closed until then.
            or current.get("reviewer_started") is True
        ):
            return False
        try:
            binding = self._load_task_binding(snapshot, node_id, "developer")
            generation = int(current.get("developer_generation", 0))
        except (FileNotFoundError, OSError, TypeError, ValueError):
            return False
        if (
            binding.status != "delivered"
            or not binding.task_id
            or binding.task_id != current.get("developer_identity")
            or generation <= 0
            or binding.generation <= 0
            or binding.generation > generation
        ):
            return False
        current["retryable_action"] = {
            "role": "developer",
            "phase": "rework",
            "continuation": True,
            "pending_schedule": True,
            "successor_candidate": False,
        }
        current["pair_archived"] = True
        current["quarantine"] = None
        current["reason"] = "quarantined delivered continuation recovered"
        if record_event:
            self._record(
                snapshot,
                "quarantine_continuation_recovered",
                {
                    "run_id": snapshot.run_id,
                    "node_id": node_id,
                    "role": "developer",
                    "task_id": binding.task_id,
                    "generation": generation,
                },
            )
        return True

    @staticmethod
    def _identity_from_contract(node: DAGNode, role: str) -> Optional[str]:
        for key in (
            role + "_task_id",
            role + "_platform_task_id",
            role + "_threadId",
            role + "_thread_id",
        ):
            value = node.contract.get(key)
            if value:
                return str(value)
        nested = node.contract.get(role + "_task")
        if isinstance(nested, dict):
            for key in ("task_id", "platform_task_id", "threadId", "thread_id"):
                if nested.get(key):
                    return str(nested[key])
        if role == "developer":
            for key in ("task_id", "platform_task_id", "threadId", "thread_id"):
                if node.contract.get(key):
                    return str(node.contract[key])
        return None

    def _prove_old_task_stopped_or_absent(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        retry: Dict[str, Any],
    ) -> Optional[str]:
        """Return the predecessor identity only when a successor is safe.

        ``None`` means the old task remains ambiguous.  An empty string is a
        durable absence proof: there is no current-run registry binding and
        no provider request that could have created a task.  A terminal
        binding is accepted only for an already provider-confirmed terminal
        state; running/blocked bindings never authorize a new writer.
        """
        current = snapshot.nodes[node_id]
        if retry.get("successor_candidate") is not True:
            return None
        if (
            current.get("active_task") is not None
            or node_id in snapshot.handles
            or current.get("start_intent") is not None
        ):
            return None
        role = retry.get("role")
        if role not in {"developer", "reviewer"}:
            return None
        try:
            binding = self._load_task_binding(snapshot, node_id, str(role))
        except FileNotFoundError:
            try:
                requested = ProviderActionStore(self.paths).has_request(
                    snapshot.run_id, node_id, str(role)
                )
            except (OSError, TypeError, ValueError):
                return None
            return None if requested else ""
        except (OSError, TypeError, ValueError):
            return None
        if binding.status in {"stopped", "failed", "archived"}:
            return binding.task_id or ""
        return None

    def _live_node_contract_digest(self, node_id: str) -> str:
        """Recompute the node contract digest from the live node contract.

        V4.7 ISSUE-02: every path that turns a visible-sdd accepted event
        into an accepted node compares the digest the event carries against
        this recomputation, not merely against the snapshot copy stamped at
        dispatch.  An empty or non-dict contract is unreadable and raises;
        ``executable_contract_digest`` itself would happily digest ``None``,
        so the refusal has to happen here.
        """
        node = self.nodes[node_id]
        contract = getattr(node, "contract", None)
        if not isinstance(contract, dict) or not contract:
            raise ValueError("node contract content is unreadable")
        return executable_contract_digest([node])

    def _replay_visible_sdd_acceptance(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        data: Dict[str, Any],
        provenance: Dict[str, Any],
    ) -> None:
        """Apply a durable visible-sdd acceptance lost with the snapshot.

        The single-session acceptance binds the developer session identity
        (the in-session reviewer role shares it); there is no reviewer
        binding to load.  Stale identity, lineage or digest evidence raises,
        exactly like the dual-visible replay; missing clearance evidence
        fails closed to ``blocked_unknown`` without archiving.  The carried
        ``contract_digest`` must also equal the digest recomputed from the
        live node contract (ISSUE-02): a tampered contract invalidates the
        event and the node never turns accepted.
        """
        current = snapshot.nodes[node_id]
        if provenance["role"] != "reviewer":
            raise ValueError("unapplied acceptance lacks reviewer provenance")
        generation = int(current.get("developer_generation", 0))
        if (
            provenance["task_id"] != current.get("developer_identity")
            or provenance["generation"] != generation
            or generation <= 0
        ):
            raise ValueError(
                "unapplied visible-sdd acceptance identity or generation is stale"
            )
        try:
            registered = self._load_task_binding(snapshot, node_id, "developer")
        except (FileNotFoundError, OSError, TypeError, ValueError) as error:
            raise ValueError(
                "unapplied visible-sdd acceptance developer binding is unavailable"
            ) from error
        if (
            registered.task_id != provenance["task_id"]
            or registered.generation != generation
        ):
            raise ValueError(
                "unapplied visible-sdd acceptance registry generation is stale"
            )
        contract_digest = data.get("contract_digest")
        authorization_epoch = data.get("authorization_epoch")
        try:
            live_digest = self._live_node_contract_digest(node_id)
        except ValueError as error:
            raise ValueError(
                "unapplied visible-sdd acceptance node contract is unreadable"
            ) from error
        if (
            not isinstance(contract_digest, str)
            or len(contract_digest) != 64
            or any(
                character not in "0123456789abcdef" for character in contract_digest
            )
            or contract_digest != current.get("contract_digest")
            or contract_digest != live_digest
            or authorization_epoch != snapshot.authorization_digest
        ):
            raise ValueError("unapplied acceptance contract epoch is stale")
        self._set_binding_status(snapshot, node_id, "developer", "accepted")
        current["status"] = "accepted"
        current["acceptance"] = {
            "contract_digest": contract_digest,
            "authorization_epoch": authorization_epoch,
        }
        # Match the live path: a replayed acceptance must not resurrect the
        # rejection text the lost snapshot still carried.
        current["reason"] = None
        current["reviewer_identity"] = provenance["task_id"]
        current["review_generation"] = generation
        current["active_role"] = None
        current["active_task"] = None
        current["quarantine"] = None
        snapshot.handles.pop(node_id, None)
        if not data.get("evidence"):
            current["status"] = "blocked_unknown"
            current["reason"] = (
                "visible-sdd acceptance has no registered in-session review evidence"
            )
        else:
            current["review_clearance"] = {"p0": 0, "p1": 0, "p2": 0}
            self._set_binding_status(snapshot, node_id, "developer", "archived")
            current["pair_archived"] = True
        self._release_node_lease(snapshot, node_id)

    def _reject_replayed_old_task_reconciliation(
        self, snapshot: RunSnapshot, node_id: str, data: Dict[str, Any]
    ) -> None:
        """Consume an untrusted reconciliation event without starting a writer."""
        current = snapshot.nodes[node_id]
        current["status"] = "blocked_unknown"
        current["reason"] = "replayed old task reconciliation proof is invalid"
        current["quarantine"] = {
            "run_id": snapshot.run_id,
            "handle_id": snapshot.handles.get(node_id),
            "reason": current["reason"],
        }
        current["old_task_reconciled"] = False
        current["retryable_action"] = None

    def _schedule_ready(
        self,
        snapshot: RunSnapshot,
        runner: Runner,
        recover_missing_reviewer: bool = False,
    ) -> None:
        record = self._require_snapshot_authorization(snapshot)
        self._validate_execution_topology(snapshot)
        # V4.6: capacity counts active *worker sessions* (one visible-sdd
        # session or one dev/reviewer pair slot per occupied node), not
        # logical pairs.  Unknown states keep occupying their slot: when the
        # evidence is ambiguous the monitor dispatches less, never more.
        session_limit = self._effective_worker_session_limit(record)
        # The V4.6 parallel-group audit (ISSUE-06, wired into
        # ``node_scoped_ready`` in dag.py) refuses nodes whose group
        # membership is conflicting or unverifiable.  Consume the same audit
        # signal on the same readiness projection before any writer lease is
        # acquired; on a dag.py without the gate the audit set is empty and
        # dispatch behavior is unchanged.
        group_blocked = self._parallel_group_blocked(snapshot)
        # V4.7 ISSUE-01: explicit writer ownership is checked over every
        # candidate of this round (same group, cross group or ungrouped)
        # before any writer lease is acquired.  A refused round leaves its
        # candidates blocked_unknown, so the planned branch below never sees
        # them.
        self._refuse_path_ownership_conflicts(snapshot)
        active_sessions = sum(
            1
            for node_id, current in snapshot.nodes.items()
            if current.get("status") not in {"stopped", "failed"}
            and not current.get("pair_archived")
            and (
                int(current.get("developer_generation", 0)) > 0
                or current.get("retryable_action") is not None
                or isinstance(current.get("active_task"), dict)
                or node_id in snapshot.handles
            )
        )
        for node_id, node in self.nodes.items():
            current = snapshot.nodes[node_id]
            was_active_session = not current.get("pair_archived") and (
                int(current.get("developer_generation", 0)) > 0
                or current.get("retryable_action") is not None
            )
            if self._recover_quarantined_delivered_developer(snapshot, node_id):
                if was_active_session:
                    active_sessions -= 1
            if recover_missing_reviewer:
                self._recover_missing_reviewer_successor(snapshot, node_id)
            retry = current.get("retryable_action")
            if isinstance(retry, dict) and retry.get("same_task") is True:
                # Unknown side effects are never eligible for successor
                # creation, even if an older persisted marker said so.
                if retry.get("successor") is not False or retry.get("successor_candidate") is True:
                    retry = dict(retry)
                    retry["successor"] = False
                    retry["successor_candidate"] = False
                    current["retryable_action"] = retry
            if (
                current.get("status") in {"blocked_unknown", "retry_pending", "running", "rework"}
                and isinstance(retry, dict)
                and node_id not in snapshot.handles
            ):
                # A delivered developer handoff is a verified continuation
                # even when an interrupted reauthorization left a stale
                # quarantine marker behind.  Normalize the legacy marker
                # before applying the fail-closed quarantine branch so the
                # original visible task can be resumed in place.
                resumable_developer = (
                    retry.get("continuation")
                    and retry.get("role") == "developer"
                    and not self._continuation_requires_successor(
                        snapshot, node_id, "developer"
                    )
                )
                if resumable_developer:
                    retry = dict(retry)
                    retry["successor_candidate"] = False
                    current["retryable_action"] = retry
                    if (
                        isinstance(current.get("quarantine"), dict)
                        and current.get("active_task") is None
                    ):
                        current["quarantine"] = None
                if (
                    current.get("status") == "blocked_unknown"
                    and isinstance(current.get("quarantine"), dict)
                    and retry.get("successor_candidate") is not True
                    and retry.get("same_task") is not True
                    and current.get("active_task") is None
                    and not resumable_developer
                ):
                    current["retryable_action"] = None
                    current["reason"] = (
                        "quarantined task has no proven active binding to resume"
                    )
                    continue
                if retry.get("successor_candidate") is True and retry.get(
                    "continuation"
                ):
                    predecessor = self._prove_old_task_stopped_or_absent(
                        snapshot, node_id, retry
                    )
                    if predecessor is None:
                        current["status"] = "blocked_unknown"
                        current["reason"] = (
                            "old task stop or absence cannot be proven for successor"
                        )
                        continue
                    retry = dict(retry)
                    retry["continuation"] = False
                    retry["successor"] = True
                    retry["predecessor_task_id"] = predecessor or None
                    retry["old_task_reconciled"] = True
                    current["retryable_action"] = retry
                    current["old_task_reconciled"] = True
                    self._record(
                        snapshot,
                        "old_task_reconciled",
                        {
                            "run_id": snapshot.run_id,
                            "node_id": node_id,
                            "role": retry["role"],
                            "predecessor_task_id": predecessor or None,
                            "proof": "absent" if not predecessor else "stopped",
                        },
                    )
                pending_schedule = retry.get("pending_schedule") is True
                reviewer_recovery = retry.get("missing_binding_recovery") is True
                # Respect the durable bounded-backoff deadline for V4.2
                # engineering retries.  The marker is consumed by the
                # scheduler; polling/starting again before this point would
                # turn an intended retry budget into a hot loop.
                next_retry_at = retry.get("next_retry_at")
                if (
                    self._v42_retry_status() == "retry_pending"
                    and retry.get("same_task") is True
                    and isinstance(next_retry_at, (int, float))
                    and time.time() < float(next_retry_at)
                ):
                    continue
                # Legacy V3.x replay defers one scheduling pass after an
                # interrupted timeout. The next heartbeat can then resume
                # the same handle/task; this preserves historical recovery
                # ordering while V4.2 uses the explicit retry_pending phase.
                if (
                    self._v42_retry_status() == "blocked_unknown"
                    and retry.get("same_task") is True
                    and retry.get("defer_once") is True
                    and not pending_schedule
                    and isinstance(current.get("quarantine"), dict)
                ):
                    retry = dict(retry)
                    retry["defer_once"] = False
                    current["retryable_action"] = retry
                    continue
                if pending_schedule and (
                    active_sessions >= session_limit
                    or not all(
                        snapshot.nodes[dependency].get("status") == "accepted"
                        for dependency in node.depends_on
                    )
                ) and not reviewer_recovery:
                    continue
                retry_role = str(retry["role"])
                retry_generation = int(current.get("review_generation" if retry_role == "reviewer" else "developer_generation", 0)) + 1
                retry_digest = str((current.get("start_intent") or {}).get("intent_digest") or self._prospective_intent_digest(snapshot, node_id, retry_role, retry_generation, current))
                if not acquire_writer_lease(self.paths, node_id, str(current["worktree"]), snapshot.run_id, retry_digest, retry_role, retry_generation):
                    self._mark_blocked_unknown(
                        snapshot,
                        node_id,
                        "writer lease is already owned by another run",
                    )
                    continue
                if pending_schedule:
                    current["pair_archived"] = False
                    active_sessions += 1
                if self._start_task(
                    snapshot,
                    node_id,
                    str(retry["role"]),
                    str(retry["phase"]),
                    runner,
                    bool(retry.get("continuation")),
                    bool(retry.get("successor")),
                ):
                    if current.get("status") != "blocked_unknown":
                        current["retryable_action"] = None
                continue
            if current.get("status") == "delivered" and not current.get("reviewer_started"):
                if self._node_dispatch_topology(node) == TOPOLOGY_VISIBLE_SDD:
                    # visible-sdd review is in-session evidence consumed at
                    # delivery time; no reviewer task is ever dispatched.  A
                    # delivered state reaching the scheduler means the
                    # evidence was lost (interrupted recovery), never that it
                    # was accepted: fail closed instead of stranding the node
                    # with its session slot held forever.
                    self._mark_blocked_unknown(
                        snapshot,
                        node_id,
                        "visible-sdd delivery has no consumable in-session review evidence after recovery",
                        quarantine_lease=False,
                    )
                    continue
                retry_role = "reviewer"
                retry_generation = int(current.get("review_generation", 0)) + 1
                retry_digest = str((current.get("start_intent") or {}).get("intent_digest") or self._prospective_intent_digest(snapshot, node_id, retry_role, retry_generation, current))
                if not acquire_writer_lease(self.paths, node_id, str(current["worktree"]), snapshot.run_id, retry_digest, retry_role, retry_generation):
                    self._mark_blocked_unknown(
                        snapshot,
                        node_id,
                        "writer lease is already owned by another run",
                    )
                    continue
                self._start_task(snapshot, node_id, "reviewer", "review", runner, False)
                continue
            if current.get("status") != "planned":
                continue
            if node_id in group_blocked:
                # Refused by the parallel-group audit: never acquire a
                # writer lease for a conflicting or unverifiable group
                # member.
                continue
            supervisor_rejection = self._supervisor_writer_rejection(node, current)
            if supervisor_rejection is not None:
                self._mark_blocked_unknown(snapshot, node_id, supervisor_rejection)
                self._record(
                    snapshot,
                    "supervisor_writer_rejected",
                    {
                        "run_id": snapshot.run_id,
                        "node_id": node_id,
                        "reason": supervisor_rejection,
                    },
                )
                continue
            if not self._brief_allows_first_write(snapshot, node_id, node):
                continue
            if active_sessions >= session_limit:
                continue
            if not all(
                snapshot.nodes[dependency].get("status") == "accepted"
                for dependency in node.depends_on
            ):
                continue
            developer_generation = int(current.get("developer_generation", 0)) + 1
            developer_digest = str((current.get("start_intent") or {}).get("intent_digest") or self._prospective_intent_digest(snapshot, node_id, "developer", developer_generation, current))
            if not acquire_writer_lease(self.paths, node_id, str(current["worktree"]), snapshot.run_id, developer_digest, "developer", developer_generation):
                self._mark_blocked_unknown(
                    snapshot,
                    node_id,
                    "writer lease is already owned by another run",
                )
                continue
            if self._start_task(
                snapshot, node_id, "developer", "develop", runner, False
            ):
                active_sessions += 1

    def _brief_allows_first_write(
        self, snapshot: RunSnapshot, node_id: str, node: DAGNode
    ) -> bool:
        """Require a valid V3.8 implementation brief before a first write.

        The gate is deliberately checked before acquiring a writer lease or
        invoking the provider.  Nodes without the opt-in contract retain the
        legacy scheduling path.
        """
        if node.contract.get("brief_required") is not True:
            return True
        current = snapshot.nodes[node_id]
        raw = node.contract.get("implementation_brief")
        try:
            brief = ImplementationBrief.from_mapping(raw)
            validation = validate_implementation_brief(
                brief, RunManifest.from_mapping({
                    "plan_id": snapshot.plan_id,
                    "plan_revision": snapshot.plan_version,
                    "run_id": snapshot.run_id,
                    # A missing contract base is itself a mismatch.  Use a
                    # non-zero sentinel only to let the validator report the
                    # concrete ``base_sha`` difference without accepting it.
                    "base_sha": node.contract.get("base_sha", "f" * 40),
                    "target_branch": str(current.get("branch", "")),
                    "execution_epoch": int(node.contract.get("execution_epoch", 0)),
                    "authorization_digest": snapshot.authorization_digest,
                    "evidence_ref": "brief-gate",
                }),
                node,
                project_root=self.paths.root,
            )
        except (TypeError, ValueError, OSError) as error:
            validation = None
            missing = ["schema:" + type(error).__name__]
            evidence = {"status": "brief_pending", "checks": {}}
        else:
            missing = validation.missing
            evidence = validation.evidence
        if missing:
            current["status"] = "brief_pending"
            current["brief_evidence"] = {
                "missing": sorted(set(missing)),
                "checks": evidence.get("checks", {}),
            }
            current["reason"] = "implementation brief is not valid"
            return False
        return True

    def _recover_missing_reviewer_successor(
        self, snapshot: RunSnapshot, node_id: str
    ) -> None:
        """Queue a reviewer successor only from a fully proven recovery state.

        A historical ``reviewer_started`` flag is not proof that this run has
        a reviewer task to continue. Recovery may create a successor only when
        the delivered developer binding is current, the reviewer binding is
        explicitly absent, and neither a provider request nor an active
        task/handle/start intent could represent an unresolved side effect.
        """
        current = snapshot.nodes[node_id]
        if self._node_dispatch_topology(self.nodes[node_id]) == TOPOLOGY_VISIBLE_SDD:
            # visible-sdd binds a single worker session; there is never a
            # reviewer binding to recover.
            return
        if current.get("status") != "blocked_unknown":
            return
        if current.get("reviewer_started") is not True:
            return
        if current.get("retryable_action") is not None:
            return
        if (
            current.get("active_task") is not None
            or current.get("active_role") is not None
            or current.get("start_intent") is not None
            or node_id in snapshot.handles
        ):
            return

        try:
            developer = self._load_task_binding(snapshot, node_id, "developer")
        except (FileNotFoundError, OSError, TypeError, ValueError):
            return

        try:
            self._load_task_binding(snapshot, node_id, "reviewer")
        except FileNotFoundError:
            pass
        except (OSError, TypeError, ValueError):
            return
        else:
            return

        try:
            requested = ProviderActionStore(self.paths).has_request(
                snapshot.run_id, node_id, "reviewer"
            )
        except (OSError, TypeError, ValueError):
            return
        if requested:
            return

        try:
            developer_generation = int(current.get("developer_generation", 0))
        except (TypeError, ValueError):
            return
        if (
            developer.status != "delivered"
            or developer.task_id != current.get("developer_identity")
            or developer.generation != developer_generation
        ):
            return

        predecessor = current.get("reviewer_identity")
        if predecessor is not None and (
            not isinstance(predecessor, str) or not predecessor
        ):
            return
        current["retryable_action"] = {
            "role": "reviewer",
            "phase": "review",
            "continuation": False,
            "successor": True,
            "successor_candidate": True,
            "pending_schedule": True,
            "missing_binding_recovery": True,
            "predecessor_task_id": predecessor,
        }
        current["reason"] = "reviewer successor queued after missing binding recovery"

    def _prepare_worker_profile(
        self, node: DAGNode, contract: Dict[str, Any], current: Dict[str, Any], role: str
    ) -> Optional[WorkerProfile]:
        """Resolve an evidence-bound profile for the real dispatch path.

        Legacy nodes keep their configured profile/default behavior.  A node
        that declares routing inputs must provide a complete IssueComplexity
        and structured LocalModel probes; no default model is synthesized.
        """
        routing_keys = {"routing_required", "issue_complexity", "model_probes", "models", "required_capabilities"}
        routing_requested = any(key in contract for key in routing_keys)
        raw = contract.get("worker_profile")
        if raw is None and isinstance(contract.get("child_binding"), dict):
            raw = contract["child_binding"].get("worker_profile")
        if role != "developer" and raw is None:
            raw = current.get("contract_overrides", {}).get("worker_profile")
        if raw is not None:
            try:
                profile = raw if isinstance(raw, WorkerProfile) else WorkerProfile.from_dict(raw)
            except (TypeError, ValueError) as error:
                raise ValueError("worker model profile is invalid") from error
            issue_data = contract.get("issue_complexity")
            probes = contract.get("model_probes", contract.get("models"))
            if routing_requested and contract.get("routing_required") is True:
                # An explicitly routed node cannot silently accept an
                # unrelated preconfigured/default profile.
                if issue_data is None or probes is None:
                    raise ValueError("worker model routing evidence is incomplete")
            if routing_requested and issue_data is not None and probes is not None:
                issue = issue_data if isinstance(issue_data, IssueComplexity) else IssueComplexity.from_dict(issue_data)
                if not isinstance(probes, (list, tuple)):
                    raise TypeError("model probes must be a list")
                models = [item if isinstance(item, LocalModel) else LocalModel.from_dict(item) for item in probes]
                expected = ModelRouter(worker=profile.worker).select(
                    issue, contract.get("required_capabilities", []), models
                )
                if (
                    profile.model != expected.model
                    or profile.reasoning != expected.reasoning
                    or profile.route_digest != expected.route_digest
                ):
                    raise ValueError("worker model profile conflicts with routing evidence")
            return profile
        if not routing_requested:
            return None
        issue_data = contract.get("issue_complexity")
        probes = contract.get("model_probes", contract.get("models"))
        if issue_data is None or probes is None:
            raise ValueError("worker model routing evidence is incomplete")
        issue = issue_data if isinstance(issue_data, IssueComplexity) else IssueComplexity.from_dict(issue_data)
        if not isinstance(probes, (list, tuple)):
            raise TypeError("model probes must be a list")
        models = [item if isinstance(item, LocalModel) else LocalModel.from_dict(item) for item in probes]
        profile = ModelRouter(worker=str(contract.get("worker") or node.contract.get("worker") or "developer")).select(
            issue, contract.get("required_capabilities", []), models
        )
        profile = WorkerProfile(
            profile.worker,
            profile.model,
            profile.reasoning,
            profile.fallbacks,
            profile.selection_basis,
            worktree=str(contract.get("worktree") or current.get("worktree", "")),
            branch=str(contract.get("branch") or current.get("branch", "")),
            allowlist=list(contract.get("files", [])),
            writer=str(contract.get("writer") or profile.worker),
            route_digest=profile.route_digest,
        )
        current.setdefault("contract_overrides", {})["worker_profile"] = profile.to_dict()
        return profile

    def _start_task(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        role: str,
        phase: str,
        runner: Runner,
        continuation: bool,
        successor: bool = False,
    ) -> bool:
        record = self._require_snapshot_authorization(snapshot)
        node = self.nodes[node_id]
        current = snapshot.nodes[node_id]
        topology = self._node_dispatch_topology(node)
        supervisor_rejection = self._supervisor_writer_rejection(node, current)
        if supervisor_rejection is not None:
            self._mark_blocked_unknown(snapshot, node_id, supervisor_rejection)
            self._record(
                snapshot,
                "supervisor_writer_rejected",
                {
                    "run_id": snapshot.run_id,
                    "node_id": node_id,
                    "role": role,
                    "reason": supervisor_rejection,
                },
            )
            return False
        if topology == TOPOLOGY_VISIBLE_SDD and role == "reviewer":
            # Structural rule (not authorization-card text): a visible-sdd
            # node binds exactly one worker session; review is in-session
            # evidence and is never dispatched as a reviewer task.
            self._mark_blocked_unknown(
                snapshot,
                node_id,
                "visible-sdd topology never dispatches a reviewer task",
            )
            return False
        identity_key = role + "_identity"
        predecessor_identity = current.get(identity_key) or "{}:{}".format(
            role, node_id
        )
        identity = predecessor_identity
        generation_key = "review_generation" if role == "reviewer" else "developer_generation"
        prior_intent = current.get("start_intent")
        if isinstance(prior_intent, dict) and prior_intent.get("role") == role:
            generation = int(prior_intent.get("generation", current.get(generation_key, 0)))
            identity = str(prior_intent.get("task_id") or identity)
        else:
            generation = int(current.get(generation_key, 0)) + 1
        if successor:
            identity = "{}:successor:{}".format(predecessor_identity, generation)
        current[generation_key] = generation
        if role == "reviewer":
            current["reviewer_started"] = True

        contract = dict(node.contract)
        contract.update(current.get("contract_overrides", {}))
        contract.update(
            {
                "node_id": node_id,
                "role": role,
                "worker": (
                    node.contract.get("reviewer_worker")
                    if role == "reviewer"
                    else current.get("worker")
                ),
                "phase": phase,
                "task_id": identity,
                "generation": generation,
                "continuation": continuation,
                "successor": successor,
                "action": phase,
                "run_id": snapshot.run_id,
                # Explicit provenance consumed by the shared complex-dispatch
                # guard in runners and adapters.  Only Monitor creates this
                # proof-bearing contract.
                "dispatcher": "monitor",
                "project_root": str(self.paths.root.resolve()),
                "plan_id": getattr(self.plan, "plan_id", ""),
                "plan_version": getattr(self.plan, "version", 0),
                "complexity_band": getattr(self.plan, "complexity_band", ""),
                "execution_engine": getattr(record, "execution_engine", "monitor"),
                "engine_mode": getattr(record, "engine_mode", "parallel"),
                "engine_evidence_ref": getattr(record, "engine_evidence_ref", ""),
                "authorization_digest": getattr(record, "digest", snapshot.authorization_digest),
                "authorized_actions": list(getattr(record, "allowed_actions", [])),
                "consistency_binding": self._consistency_binding(record, node),
            }
        )
        # The create request carries the dispatch topology with the node
        # contract, file allowlist and worktree/branch; visible-sdd sessions
        # additionally receive the SDD protocol pointer they must follow.
        contract["topology"] = topology
        if topology == TOPOLOGY_VISIBLE_SDD:
            contract["sdd_protocol"] = VISIBLE_SDD_PROTOCOL_REF
        elif topology == TOPOLOGY_BACKGROUND:
            limitations = contract.get("dispatch_limitations")
            if not (
                isinstance(limitations, list)
                and any(isinstance(item, str) and item.strip() for item in limitations)
            ):
                limitations = [BACKGROUND_TOPOLOGY_DISCLOSURE]
            contract["dispatch_limitations"] = list(limitations)
        retry = current.get("retryable_action")
        if successor:
            predecessor_task_id = None
            if isinstance(retry, dict):
                predecessor_task_id = retry.get("predecessor_task_id")
            if not predecessor_task_id:
                predecessor_task_id = current.get(identity_key)
            contract["predecessor_task_id"] = predecessor_task_id
        contract.setdefault("worktree", str(current["worktree"]))
        contract.setdefault("branch", str(current["branch"]))
        contract.setdefault(
            "files", list((contract.get("worker_profile") or {}).get("allowlist", []))
        )
        routing_requested = any(
            key in contract
            for key in ("routing_required", "issue_complexity", "model_probes", "models", "required_capabilities")
        )
        profile = self._prepare_worker_profile(node, contract, current, role)
        if profile is not None:
            contract["worker_profile"] = profile.to_dict()
            if routing_requested:
                contract["routing_required"] = True
        if binding_contract_enabled(contract):
            has_live_evidence = isinstance(contract.get("binding_intent"), BindingIntent) and isinstance(
                contract.get("binding_observation"), BindingObservation
            )
            if not has_live_evidence:
                if continuation:
                    contract.setdefault("binding_bootstrap", True)
                    current["binding_phase"] = "binding_repair_pending"
                else:
                    contract.setdefault("binding_probe", True)
                    current["binding_phase"] = "binding_probe_pending"
            else:
                current["binding_phase"] = "binding_verified"
        if snapshot.capability_contract_digest:
            contract["capability_contract_digest"] = snapshot.capability_contract_digest
        if (self.paths.vibe / "state.json").is_file():
            try:
                workflow_version = json.loads(
                    (self.paths.vibe / "state.json").read_text(encoding="utf-8")
                ).get("workflow_version")
                v2 = workflow_version in (2, 4)
            except (OSError, ValueError, json.JSONDecodeError):
                v2 = True
            if v2:
                profile_data = contract.get("worker_profile") or {}
                if not profile_data:
                    profile_data = {"worker": str(contract.get("worker", "worker")), "model": "default", "reasoning": "normal", "fallbacks": [], "selection_basis": {"issue_complexity_ref": node_id, "complexity_band": "standard", "risk_tags": [], "availability_evidence": "runtime"}, "writer": str(contract.get("worker", "writer")), "worktree": str(contract.get("worktree", ".")), "branch": str(contract.get("branch", "branch-" + node_id)), "allowlist": list(contract.get("files", [node_id + ".py"]))}
                profile = WorkerProfile(**profile_data)
                validate_child_session_binding(snapshot.run_id, str(self.plan.version), record.digest, node_id, role, profile)
                contract["child_origin"] = "worker_dispatch"
                contract["child_binding"] = {"parent_run_id": snapshot.run_id, "plan_revision": str(self.plan.version), "authorization_digest": record.digest, "node_id": node_id, "role": role, "writer": profile.writer, "worktree": profile.worktree, "branch": profile.branch, "allowlist": profile.allowlist, "worker_profile": profile.to_dict(), "model": profile.model, "reasoning": profile.reasoning, "route_digest": profile.route_digest, "capability_contract_digest": snapshot.capability_contract_digest}
        try:
            contract = validate_runtime_contract(
                contract,
                authorized_actions=record.allowed_actions,
                authorized_files=record.file_scope,
            )
            if self._binding_recovery_requested(contract):
                self._preflight_binding_recovery(
                    snapshot, node_id, role, contract, current
                )
            else:
                self._preflight_binding_before_provider(contract)
            self._run_binding_bootstrap(snapshot, node_id, role, contract, runner, current)
            binding = self._binding_for(
                snapshot,
                node_id,
                role,
                identity,
                generation,
                "start_pending",
                runner,
                contract,
                continuation,
                successor,
            )
            self._require_binding_gate(contract, binding, runner)
            if binding_contract_enabled(contract):
                # Registry persistence intentionally strips private provenance
                # tokens.  Carry the live, verified objects through this
                # in-process call so ProviderActionRunner.start() rechecks the
                # same evidence instead of trusting a downgraded JSON row.
                contract["binding_intent"] = binding.binding_intent
                contract["binding_observation"] = binding.binding_observation
            identity = str(binding.task_id)
            current[identity_key] = identity
            contract["task_id"] = identity
            self._save_task_binding(binding)
        except ProviderPending as error:
            current[generation_key] = generation - 1
            if role == "reviewer" and generation == 1:
                current["reviewer_started"] = False
            # Binding/setup pending is not a provider-confirmed task.  Clear
            # any write-ahead active projection created before the probe so a
            # setup token can never be exposed as a formal task identity.
            current["active_task"] = None
            current["active_role"] = None
            current["start_intent"] = None
            setup_identity = contract.get("setup_identity") or contract.get("clientThreadId") or contract.get("client_thread_id")
            current["retryable_action"] = {
                "role": role,
                "phase": phase,
                "continuation": continuation,
                "successor": False,
                "same_task": True,
                "same_task_required": True,
                "attempt": 1,
                "reason_class": "engineering",
                "next_retry_at": time.time(),
                "binding_digest": current.get("contract_digest", ""),
                "last_observation_ref": "provider-pending",
            }
            if setup_identity:
                current["retryable_action"]["setup_identity"] = str(setup_identity)
            # An asynchronous bridge response is a retry condition, not
            # evidence that the provider capability is unavailable.
            current["status"] = "blocked_unknown" if successor else "running"
            if binding_contract_enabled(contract):
                current["binding_phase"] = "retry_pending"
            current["reason"] = str(error)
            # A pending client handle is only a setup observation.  Do not
            # expose it as an active/formal task; replay the durable action
            # with the same contract and writer once the provider resolves it.
            current["active_task"] = None
            current["active_role"] = None
            # Preserve the write-ahead intent for same-task retry.
            if successor:
                self._mark_blocked_unknown(snapshot, node_id, str(error), quarantine_lease=False)
                current["retryable_action"] = {
                    "role": role, "phase": phase, "continuation": continuation,
                    "successor": False, "same_task": True, "same_task_required": True,
                    "attempt": 1, "reason_class": "engineering", "next_retry_at": time.time(),
                    "binding_digest": current.get("contract_digest", ""),
                    "last_observation_ref": "provider-pending",
                }
                save_snapshot(self.paths, snapshot)
            return False
        except ProviderUnavailable as error:
            current[generation_key] = generation - 1
            if role == "reviewer" and generation == 1:
                current["reviewer_started"] = False
            failure = classify_provider_failure(error)
            if failure["kind"] == "external_decision":
                current["retryable_action"] = None
            else:
                current["retryable_action"] = {
                    "role": role,
                    "phase": phase,
                    "continuation": continuation,
                    "successor": False,
                    "same_task": True,
                    "same_task_required": True,
                    "attempt": 1,
                    "reason_class": failure["kind"],
                    "next_retry_at": time.time(),
                    "binding_digest": current.get("contract_digest", ""),
                    "last_observation_ref": hashlib.sha256(str(error).encode("utf-8")).hexdigest(),
                }
            current["active_task"] = None
            current["active_role"] = None
            # Preserve the write-ahead intent for same-task retry.
            self._mark_blocked_unknown(snapshot, node_id, str(error))
            if binding_contract_enabled(contract):
                current["binding_phase"] = "binding_repair_pending"
            save_snapshot(self.paths, snapshot)
            return False
        except (OSError, TypeError, ValueError) as error:
            self._mark_blocked_unknown(
                snapshot,
                node_id,
                "task binding rejected before start ({})".format(
                    type(error).__name__
                ),
            )
            return False

        snapshot.tasks["{}:{}".format(node_id, role)] = binding.to_dict()
        # Write-ahead intent is immutable across provider retries.  Keep the
        # legacy fields while adding the V4.4 identity digest.
        intent = dict(current.get("start_intent") or {})
        if intent.get("role") != role or intent.get("task_id") != identity or int(intent.get("generation", generation)) != generation:
            intent = {"intent_id": uuid.uuid4().hex, "role": role, "phase": phase, "task_id": identity, "generation": generation}
        intent["intent_digest"] = hashlib.sha256(json.dumps({"run_id": snapshot.run_id, "node_id": node_id, "role": role, "task_id": identity, "generation": generation, "writer": contract.get("writer") or current.get("worker"), "worktree": contract.get("worktree") or current.get("worktree"), "branch": contract.get("branch") or current.get("branch"), "authorization_digest": snapshot.authorization_digest, "node_contract_digest": current.get("contract_digest") or getattr(snapshot, "node_contract_digest", "")}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        contract["intent_digest"] = intent["intent_digest"]
        contract["start_intent"] = dict(intent)
        current["status"] = "start_pending"
        if binding_contract_enabled(contract) and current.get("binding_phase") is None:
            current["binding_phase"] = "binding_verified"
        current["active_role"] = role
        current["active_task"] = {
            "role": role,
            "task_id": identity,
            "generation": generation,
            "handle_id": None,
        }
        current["start_intent"] = intent
        self._record(
            snapshot,
            "start_intent",
            {"run_id": snapshot.run_id, "node_id": node_id, **intent},
            current["active_task"],
        )
        # The intent and lease are durable before the external side effect.
        save_snapshot(self.paths, snapshot)
        self._require_snapshot_authorization(snapshot)
        try:
            # All starts pass through the intent transaction.  V4.4 providers
            # must return a structured proof; legacy runners retain RunHandle
            # compatibility until their contract opts into 4.4.
            handle = self._dispatch_with_intent(runner, contract, intent)
            if isinstance(handle, dict) and handle.get("kind") in {"binding_unknown", "retryable", "capacity_wait", "repairable"}:
                current["status"] = "blocked_unknown"
                current["reason"] = handle.get("reason", handle.get("kind"))
                current["binding_phase"] = handle.get("kind")
                current["active_task"] = None
                current["active_role"] = None
                current["start_intent"] = None
                current["retryable_action"] = {
                    "role": role, "phase": phase,
                    "continuation": continuation, "successor": False,
                    "same_task": True, "same_task_required": True,
                    "attempt": 1, "reason_class": handle.get("kind"),
                    "next_retry_at": time.time(),
                    "binding_digest": current.get("contract_digest", ""),
                    "last_observation_ref": "provider-pending",
                }
                self._record(snapshot, handle.get("kind", "binding_unknown"), {"run_id": snapshot.run_id, "node_id": node_id, "intent_digest": intent.get("intent_digest"), "proof": handle.get("proof")})
                save_snapshot(self.paths, snapshot)
                return False
            if isinstance(handle, dict):
                provider_run_id = handle.get("run_id") or handle.get("provider_task_id") or handle.get("task_id")
                if not isinstance(provider_run_id, str) or not provider_run_id:
                    current["status"] = "blocked_unknown"; current["reason"] = "provider proof has no task handle"; save_snapshot(self.paths, snapshot); return False
                handle = RunHandle(provider_run_id)
        except ProviderPending as error:
            # A bridge setup token (clientThreadId) is not a formal task. Keep
            # the writer lease and durable retry metadata, but clear the
            # active/start intent until a real provider identity is observed.
            setup_identity = contract.get("setup_identity") or contract.get("clientThreadId") or contract.get("client_thread_id")
            current["retryable_action"] = {
                "role": role,
                "phase": phase,
                "continuation": continuation,
                "successor": False,
                "same_task": True,
                "same_task_required": True,
                "attempt": 1,
                "reason_class": "engineering",
                "next_retry_at": time.time(),
                "binding_digest": current.get("contract_digest", ""),
                "last_observation_ref": "provider-pending",
                "setup_identity": str(setup_identity or "pending"),
            }
            current["status"] = self._v42_retry_status()
            current["active_task"] = None
            current["active_role"] = None
            current["start_intent"] = None
            # Preserve the write-ahead intent for same-task retry.
            current["reason"] = str(error)
            save_snapshot(self.paths, snapshot)
            return False
        except Exception as error:
            if successor:
                current["retryable_action"] = {
                    "role": role,
                    "phase": phase,
                    "continuation": continuation,
                    "successor": False,
                    "same_task": True,
                }
            self._mark_blocked_unknown(
                snapshot,
                node_id,
                "worker start outcome is unknown ({})".format(
                    type(error).__name__
                ),
            )
            save_snapshot(self.paths, snapshot)
            return False

        if (
            not isinstance(handle.run_id, str)
            or not handle.run_id
            or handle.run_id in snapshot.handles.values()
        ):
            self._mark_blocked_unknown(
                snapshot,
                node_id,
                "provider returned a duplicate or invalid active handle",
            )
            save_snapshot(self.paths, snapshot)
            return False

        current["status"] = self._running_status(role, phase)
        if binding_contract_enabled(contract):
            current["binding_phase"] = "business_work_allowed"
        current["active_task"]["handle_id"] = handle.run_id
        current["start_intent"] = None
        current["quarantine"] = None
        snapshot.handles[node_id] = handle.run_id
        self._record(
            snapshot,
            "start_confirmed",
            {
                "run_id": snapshot.run_id,
                "node_id": node_id,
                "role": role,
                "phase": phase,
                "task_id": identity,
                "generation": generation,
                "handle_id": handle.run_id,
            },
            current["active_task"],
        )
        try:
            self._set_binding_status(
                snapshot, node_id, role, self._running_status(role, phase)
            )
        except (FileNotFoundError, OSError, TypeError, ValueError) as error:
            self._mark_blocked_unknown(
                snapshot,
                node_id,
                "confirmed task binding cannot be updated ({})".format(
                    type(error).__name__
                ),
            )
            save_snapshot(self.paths, snapshot)
            return False
        save_snapshot(self.paths, snapshot)
        pending = getattr(runner, "is_pending", None)
        if callable(pending) and pending(handle):
            # Keep the active lease, but expose the unresolved bridge action
            # as unknown until a provider-confirmed result is observed.  The
            # handle remains durable so the next tick can poll it; no second
            # writer is scheduled while this side effect is pending.
            # A continuation after reauthorization carries an already
            # accepted task identity.  Until the provider confirms the
            # resume action, keep that continuation fail-closed so the CLI
            # cannot report a successful reauthorization while the native
            # action is still only queued.
            current["status"] = (
                "blocked_unknown" if continuation else self._v42_retry_status()
            )
            current["quarantine"] = {
                "run_id": snapshot.run_id,
                "handle_id": handle.run_id,
                "reason": "provider action pending; result not yet confirmed",
            }
            current["reason"] = "provider action pending; result not yet confirmed"
            if continuation:
                current["retryable_action"] = {
                    "role": role,
                    "phase": phase,
                    "continuation": True,
                    "successor": False,
                    "same_task": True,
                    "same_task_required": True,
                    "attempt": 1,
                    "reason_class": "engineering",
                    "next_retry_at": time.time(),
                    "binding_digest": current.get("contract_digest", ""),
                    "last_observation_ref": "provider-pending",
                }
            save_snapshot(self.paths, snapshot)
        return True

    @staticmethod
    def _running_status(role: str, phase: str) -> str:
        if role == "reviewer":
            return "review"
        if phase == "rework":
            return "rework"
        return "running"

    def _binding_for(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        role: str,
        identity: str,
        generation: int,
        status: str,
        runner: Runner,
        contract: Dict[str, Any],
        continuation: bool,
        successor: bool = False,
    ) -> TaskBinding:
        current = snapshot.nodes[node_id]
        previous_binding = None
        if continuation:
            try:
                previous_binding = self._load_task_binding(snapshot, node_id, role)
            except FileNotFoundError:
                previous_binding = None
            if previous_binding is None or previous_binding.task_id != identity:
                raise ValueError("continuation task identity is stale")
        observed_binding = getattr(runner, "task_binding", None)
        if callable(observed_binding):
            binding = observed_binding(
                contract,
                self._worktree_path(current),
                snapshot.run_id,
                status,
            )
            if not isinstance(binding, TaskBinding):
                raise ValueError("runner returned an invalid observed task binding")
            if (
                binding.issue_id != node_id
                or binding.role != role
                or binding.run_id != snapshot.run_id
                or binding.generation != generation
                or (continuation and binding.task_id != identity)
            ):
                raise ValueError("observed task binding does not match runtime scope")
        else:
            binding = TaskBinding(
                provider="runner",
                mode="background",
                issue_id=node_id,
                role=role,
                task_id=identity,
                host=None,
                worktree=str(current["worktree"]),
                branch=str(current["branch"]),
                status_file=str(current.get("status_file", "")),
                handoff_file=str(current.get("handoff_file", "")),
                run_id=snapshot.run_id,
                status=status,
                generation=generation,
                allowlist=list(contract.get("files", [])),
                capability_contract_digest=contract.get("capability_contract_digest"),
                successor_of=contract.get("predecessor_task_id") if successor else None,
                topology=str(contract.get("topology") or TOPOLOGY_DUAL_VISIBLE),
                limitations=list(contract.get("dispatch_limitations", [])),
            )
        # The registered binding must match the dispatch ruling.  A bridge
        # that cannot honor the ruled topology (e.g. no visible session for a
        # visible-sdd node) is fail-closed, never silently downgraded.
        expected_topology = str(contract.get("topology") or TOPOLOGY_DUAL_VISIBLE)
        if binding.topology != expected_topology:
            raise ValueError(
                "task binding topology does not match the dispatch ruling"
            )
        if successor:
            predecessor = contract.get("predecessor_task_id")
            if predecessor and binding.task_id == predecessor:
                raise ValueError("successor task identity reused the predecessor")
            if predecessor and binding.successor_of not in {None, predecessor}:
                raise ValueError("successor task predecessor does not match contract")
            if binding.successor_of is None:
                binding.successor_of = predecessor
        expected_capability_digest = contract.get("capability_contract_digest")
        if expected_capability_digest:
            if binding.capability_contract_digest not in {
                None,
                expected_capability_digest,
            } and not continuation:
                raise ValueError("task binding capability contract digest is stale")
            binding.capability_contract_digest = expected_capability_digest
        expected_allowlist = list(contract.get("files", []))
        if expected_allowlist:
            if binding.allowlist:
                if binding.allowlist != expected_allowlist:
                    if not continuation or not set(binding.allowlist).issubset(
                        set(expected_allowlist)
                    ):
                        raise ValueError("task binding allowlist does not match contract")
                    binding.allowlist = expected_allowlist
            else:
                binding.allowlist = expected_allowlist
        profile_data = contract.get("worker_profile")
        if profile_data is not None:
            profile = profile_data if isinstance(profile_data, WorkerProfile) else WorkerProfile.from_dict(profile_data)
            for field_name, expected in (
                ("route_digest", profile.route_digest),
                ("model", profile.model),
                ("reasoning", profile.reasoning),
            ):
                current_value = getattr(binding, field_name, None)
                if current_value not in (None, "", expected):
                    raise ValueError("task binding {} does not match worker profile".format(field_name))
                setattr(binding, field_name, expected)
        expected_worktree = self._worktree_path(current)
        observed_worktree = Path(str(binding.worktree))
        if not observed_worktree.is_absolute():
            observed_worktree = self.paths.root / observed_worktree
        if (
            observed_worktree.resolve() != expected_worktree.resolve()
            or binding.branch != str(current["branch"])
        ):
            raise ValueError("task binding worktree or branch does not match contract")
        if continuation and previous_binding is not None:
            if (
                binding.cursor is not None
                and binding.cursor != previous_binding.cursor
            ):
                raise ValueError("continuation cursor is stale")
            binding.cursor = previous_binding.cursor
        return binding

    def _set_binding_status(
        self, snapshot: RunSnapshot, node_id: str, role: str, status: str
    ) -> TaskBinding:
        binding = self._load_task_binding(snapshot, node_id, role)
        current = snapshot.nodes[node_id]
        generation_key = (
            "review_generation" if role == "reviewer" else "developer_generation"
        )
        if (
            binding.task_id != current.get(role + "_identity")
            or binding.generation != current.get(generation_key)
        ):
            raise ValueError("task binding identity or generation is stale")
        binding.status = status
        self._save_task_binding(binding)
        snapshot.tasks["{}:{}".format(node_id, role)] = binding.to_dict()
        return binding

    def _registered_active_binding(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        provenance: Dict[str, Any],
    ) -> TaskBinding:
        current = snapshot.nodes[node_id]
        active = current.get("active_task")
        if not isinstance(active, dict):
            raise ValueError("event has no active registered task")
        if any(
            provenance.get(key) != active.get(key)
            for key in ("role", "task_id", "handle_id", "generation")
        ):
            raise ValueError("event provenance is stale or unregistered")
        if snapshot.handles.get(node_id) != active.get("handle_id"):
            raise ValueError("event handle is not the active run handle")
        binding = self._load_task_binding(snapshot, node_id, str(active["role"]))
        if (
            binding.task_id != active.get("task_id")
            or binding.generation != active.get("generation")
        ):
            raise ValueError("event task binding is stale")
        return binding

    def _derive_integration_acceptance(
        self,
        snapshot: RunSnapshot,
        claim: Any,
        contract: Any = None,
        refs: Any = None,
        excluded: Any = None,
    ):
        """Derive the run-level package without writing anything.

        Returns ``(package, None, False)`` on success and
        ``(None, reason, recoverable)`` otherwise.  Splitting derivation from
        the write is what lets the live acceptance path validate first: a
        malformed claim (recoverable) is a format error the same reviewer can
        correct and re-report on its still-live handle, while a contract
        digest mismatch or open P0-P2 findings stay fail-closed.  Only a
        schema/derivation failure is recoverable -- everything else is an
        evidence or scope problem a re-report cannot fix.
        """
        if contract is None or refs is None or excluded is None:
            contract = integration_contract_projection(self.plan, list(self.nodes.values()))
            record = self._snapshot_record(snapshot)
            if digest_integration_contract(contract) != record.integration_contract_digest:
                return None, "integration contract no longer matches the authorized digest", False
            if contract:
                refs = contract.get("agentsmd_acceptance_refs") or []
                excluded = contract.get("unverified_or_excluded") or []
            else:
                refs = getattr(self.plan, "agentsmd_acceptance_refs", [])
                excluded = getattr(self.plan, "unverified_or_excluded", [])
        try:
            package = build_integration_review_evidence(
                snapshot, claim, list(refs or []), list(excluded or [])
            )
        except (TypeError, ValueError) as error:
            return None, "integration review evidence cannot be derived ({})".format(error), True
        if any(package["clearance"][severity] for severity in ("p0", "p1", "p2")):
            return None, "integration review acceptance still reports open P0-P2 findings", False
        return package, None, False

    def _reject_acceptance_format(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        active: Dict[str, Any],
        reason: str,
    ) -> None:
        """Record a validate-first rejection as pure audit and keep the handle.

        A malformed acceptance/delivery payload is a format error in the same
        task's report, not an unknown side effect: the event is not recorded
        as a lifecycle transition, the binding is not flipped, and the handle
        stays in ``snapshot.handles`` so the next poll re-issues the provider
        wait on the same session -- the reviewer/worker can correct the
        payload and report again.  Replay treats ``acceptance_rejected`` as a
        no-op for the same reason: nothing here mutated lifecycle state.

        The node ``reason`` is a fixed, decision-marker-free text: the raw
        validator reason stays only in the redacted audit event, because
        free-form reasons (e.g. key names like ``out_of_scope``) otherwise
        trip the user-facing "needs your decision" mapping for an error the
        same session recovers from on its own.  Crash-window trade-off: if
        the process dies after this event is appended but before the
        snapshot is saved, the replayed no-op restores neither the evidence
        entry nor this reason -- the durable trail is the audit event alone.
        """
        current = snapshot.nodes[node_id]
        current["reason"] = (
            "acceptance report format error; "
            "the same session can correct and re-report"
        )
        self._record(
            snapshot,
            "acceptance_rejected",
            {
                "run_id": snapshot.run_id,
                "node_id": node_id,
                "reason": redact_provider_text(reason),
                "recoverable": True,
            },
            active,
        )

    def _apply_event(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        handle_id: str,
        event: RunEvent,
        runner: Runner,
    ) -> None:
        self._require_snapshot_authorization(snapshot)
        current = snapshot.nodes[node_id]
        active = current.get("active_task")
        if not isinstance(active, dict) or active.get("handle_id") != handle_id:
            self._mark_blocked_unknown(
                snapshot, node_id, "event arrived from an unregistered handle"
            )
            return
        if not self._event_claims_match(event, node_id, active):
            self._mark_blocked_unknown(
                snapshot, node_id, "event provenance conflicts with the registered task"
            )
            return

        evidence = event.data.get("evidence") or event.data.get("finding")
        if evidence is not None:
            current.setdefault("evidence", []).append(redact_provider_text(evidence))
        role = active["role"]
        if event.event not in {
            "unknown",
            "timeout",
            "state_unknown",
            "visibility_unknown",
        }:
            # A provider-confirmed event consumes any reauthorization retry
            # marker; the active handle now carries the durable continuation.
            current["retryable_action"] = None

        if event.event in {"delivered", "complete"}:
            if snapshot.execution_engine == "vibeguide_monitor":
                delivery = event.data.get("delivery_evidence")
                gate = evaluate_delivery_evidence(
                    {**current, "status": "DELIVERED"},
                    self._load_task_binding(snapshot, node_id, role),
                    delivery,
                )
                if not gate.complete:
                    self._mark_blocked_unknown(snapshot, node_id, "; ".join(gate.reasons))
                    return
            visible_sdd_delivery = (
                role == "developer"
                and self._node_dispatch_topology(self.nodes[node_id]) == TOPOLOGY_VISIBLE_SDD
            )
            if visible_sdd_delivery:
                # Validate first (ISSUE-83): the in-session review evidence is
                # checked before the delivery event is recorded, the binding is
                # flipped, or the handle is popped.  A missing/malformed field
                # or a foreign protocol pointer is a format error the same
                # worker session can correct and re-report; only contract
                # digest drift stays fail-closed.
                review_failure = self._validate_visible_sdd_review(
                    snapshot, node_id, event, current
                )
                if review_failure is not None:
                    reason, recoverable = review_failure
                    if recoverable:
                        self._reject_acceptance_format(
                            snapshot, node_id, active, reason
                        )
                        return
                    self._mark_blocked_unknown(snapshot, node_id, reason)
                    return
            self._record_runner_event(snapshot, node_id, event, active)
            if role != "developer":
                try:
                    self._set_binding_status(snapshot, node_id, role, "review")
                except (FileNotFoundError, OSError, TypeError, ValueError) as error:
                    self._mark_blocked_unknown(
                        snapshot,
                        node_id,
                        "task binding cannot record review state ({})".format(
                            type(error).__name__
                        ),
                    )
                    return
                current["status"] = "review"
                return
            try:
                self._set_binding_status(snapshot, node_id, role, "delivered")
            except (FileNotFoundError, OSError, TypeError, ValueError) as error:
                self._mark_blocked_unknown(
                    snapshot,
                    node_id,
                    "task binding cannot record delivery ({})".format(
                        type(error).__name__
                    ),
                )
                return
            current["status"] = "delivered"
            current["active_role"] = None
            current["active_task"] = None
            snapshot.handles.pop(node_id, None)
            if self._node_dispatch_topology(self.nodes[node_id]) == TOPOLOGY_VISIBLE_SDD:
                self._accept_visible_sdd_delivery(snapshot, node_id, event, current)
                return
            reviewer_continuation = False
            reviewer_successor = False
            if current.get("reviewer_started"):
                try:
                    reviewer_binding = self._load_task_binding(snapshot, node_id, "reviewer")
                except FileNotFoundError:
                    reviewer_successor = True
                except (OSError, TypeError, ValueError) as error:
                    self._mark_blocked_unknown(
                        snapshot,
                        node_id,
                        "reviewer task binding cannot be verified ({})".format(
                            type(error).__name__
                        ),
                    )
                    return
                else:
                    expected_reviewer_identity = current.get("reviewer_identity")
                    if (
                        not expected_reviewer_identity
                        or reviewer_binding.task_id != expected_reviewer_identity
                    ):
                        self._mark_blocked_unknown(
                            snapshot,
                            node_id,
                            "reviewer task binding identity is stale",
                        )
                        return
                    reviewer_continuation = True
            self._start_task(
                snapshot,
                node_id,
                "reviewer",
                "review",
                runner,
                reviewer_continuation,
                reviewer_successor,
            )
        elif event.event == "review_finding":
            if role != "reviewer":
                self._mark_blocked_unknown(
                    snapshot, node_id, "review finding did not come from the reviewer"
                )
                return
            self._record_runner_event(snapshot, node_id, event, active)
            if node_id == "integration-review":
                # The integration node is read-only: findings never create a
                # business writer. Preserve the reviewer lineage and queue the
                # same visible reviewer task for re-review.
                current["status"] = "rework"
                current["active_role"] = None
                current["active_task"] = None
                snapshot.handles.pop(node_id, None)
                current["retryable_action"] = {
                    "role": "reviewer", "phase": "review", "continuation": True,
                    "pending_schedule": True, "successor": False,
                }
                current.setdefault("integration_findings", []).append(redact_provider_text(event.data))
                self._release_node_lease(snapshot, node_id)
                return
            if self._is_implementation_finding(event.data):
                current["active_role"] = None
                current["active_task"] = None
                snapshot.handles.pop(node_id, None)
                self._start_task(
                    snapshot, node_id, "developer", "rework", runner, True, False
                )
                return
            if not event.data.get("in_contract", False):
                record = self._snapshot_record(snapshot)
                resolution = resolve_consistency(
                    event.data.get("consistency"),
                    self.plan.decisions,
                    self.nodes[node_id].contract,
                    list(record.allowed_actions),
                    list(record.file_scope),
                    self._consistency_binding(record, self.nodes[node_id]),
                )
                if resolution is not None:
                    if not self._stop_active_for_transition(
                        snapshot, node_id, role, handle_id, runner, "stopped"
                    ):
                        return
                    current.setdefault("contract_overrides", {})[
                        resolution.field
                    ] = resolution.value
                    correction_values = {
                        "field": resolution.field,
                        "value": resolution.value,
                        "source": resolution.source,
                        "action": resolution.action,
                        "files": list(resolution.files),
                        "consistency_binding": dict(
                            resolution.consistency_binding
                        ),
                        "decision": (
                            dict(resolution.decision)
                            if resolution.decision is not None
                            else None
                        ),
                    }
                    correction = {
                        key: correction_values[key]
                        for key in CONSISTENCY_CORRECTION_KEYS
                    }
                    current.setdefault("corrections", []).append(correction)
                    self._record(
                        snapshot,
                        "consistency_corrected",
                        {
                            "run_id": snapshot.run_id,
                            "node_id": node_id,
                            **correction,
                        },
                    )
                    self._start_task(
                        snapshot, node_id, "developer", "rework", runner, True
                    )
                    return
                if not self._stop_active_for_transition(
                    snapshot,
                    node_id,
                    role,
                    handle_id,
                    runner,
                    "blocked_design",
                ):
                    return
                current["status"] = "blocked_design"
                current["reason"] = redact_provider_text(
                    event.data.get("finding", "out-of-contract finding")
                )
                current["old_task_reconciled"] = True
                current["active_role"] = None
                current["active_task"] = None
                snapshot.handles.pop(node_id, None)
                self._release_node_lease(snapshot, node_id)
                self._record(
                    snapshot,
                    "blocked_design",
                    {
                        "run_id": snapshot.run_id,
                        "node_id": node_id,
                        "reason": current["reason"],
                        "old_task_reconciled": True,
                    },
                )
                return
            current["active_role"] = None
            current["active_task"] = None
            snapshot.handles.pop(node_id, None)
            self._start_task(snapshot, node_id, "developer", "rework", runner, True)
        elif event.event == "accepted":
            if role != "reviewer":
                self._mark_blocked_unknown(
                    snapshot, node_id, "acceptance did not come from the reviewer"
                )
                return
            try:
                registered = self._load_task_binding(snapshot, node_id, "reviewer")
            except (FileNotFoundError, OSError, TypeError, ValueError) as error:
                self._mark_blocked_unknown(
                    snapshot,
                    node_id,
                    "reviewer binding cannot be verified ({})".format(
                        type(error).__name__
                    ),
                )
                return
            if (
                registered.task_id != active["task_id"]
                or registered.generation != active["generation"]
            ):
                self._mark_blocked_unknown(
                    snapshot, node_id, "reviewer identity or generation is stale"
                )
                return
            # ISSUE-02 (dual-visible): recompute the live contract digest before
            # recording the acceptance, matching the guard already in
            # _accept_visible_sdd_delivery.  A drifted or unreadable contract
            # must not silently bind a stale snapshot digest.
            try:
                live_digest = self._live_node_contract_digest(node_id)
            except ValueError as error:
                self._mark_blocked_unknown(
                    snapshot,
                    node_id,
                    "dual-visible acceptance refused: node contract is unreadable ({})".format(
                        error
                    ),
                )
                return
            if current.get("contract_digest") != live_digest:
                self._mark_blocked_unknown(
                    snapshot,
                    node_id,
                    "dual-visible acceptance refused: contract digest does not match the live node contract",
                )
                return
            integration_package = None
            if node_id == "integration-review":
                # Validate first (ISSUE-83): derive the run-level package
                # before the event is recorded, the binding is flipped, or
                # the handle is popped.  A malformed claim is a format error
                # the same reviewer can correct and re-report on its live
                # handle; bricking here destroyed every recovery path first.
                integration_package, rejection, recoverable = (
                    self._derive_integration_acceptance(snapshot, evidence)
                )
                if rejection is not None:
                    if recoverable:
                        self._reject_acceptance_format(
                            snapshot, node_id, active, rejection
                        )
                        return
                    self._mark_blocked_unknown(snapshot, node_id, rejection)
                    return
            acceptance_event = RunEvent(
                event.event,
                {
                    **event.data,
                    "contract_digest": current["contract_digest"],
                    "authorization_epoch": snapshot.authorization_digest,
                },
            )
            self._record_runner_event(snapshot, node_id, acceptance_event, active)
            try:
                self._set_binding_status(snapshot, node_id, role, "accepted")
            except (FileNotFoundError, OSError, TypeError, ValueError) as error:
                self._mark_blocked_unknown(
                    snapshot,
                    node_id,
                    "task binding cannot record acceptance ({})".format(
                        type(error).__name__
                    ),
                )
                return
            current["status"] = "accepted"
            current["acceptance"] = {
                "contract_digest": current["contract_digest"],
                "authorization_epoch": snapshot.authorization_digest,
            }
            # A corrected re-report must not keep the rejection text.
            current["reason"] = None
            current["active_role"] = None
            current["active_task"] = None
            current["quarantine"] = None
            snapshot.handles.pop(node_id, None)
            if evidence is None:
                self._mark_blocked_unknown(
                    snapshot,
                    node_id,
                    "review acceptance has no registered P0-P2 clearance evidence",
                )
                return
            if node_id == "integration-review":
                # The acceptance of this node is the run-level closeout, so the
                # clearance is whatever the derived package says rather than an
                # assumed zero.  The package was already derived and validated
                # before any state changed; this write cannot fail validation.
                _record_integration_review(snapshot, integration_package)
            else:
                current["review_clearance"] = {"p0": 0, "p1": 0, "p2": 0}
            self._archive_pair(snapshot, node_id)
            self._release_node_lease(snapshot, node_id)
        elif event.event in {"unknown", "timeout", "state_unknown", "visibility_unknown"}:
            raw_reason = event.data.get("reason", event.event)
            reason = str(redact_provider_text(raw_reason))
            failure = classify_provider_failure(event.data)
            # A mailbox wait with no result is an evidence gap, rather than
            # an engineering failure.  Keep the active handle and its wait
            # request for reconciliation, but expose the node as unknown
            # until the provider returns a terminal observation.
            if (
                event.event == "visibility_unknown"
                and str(raw_reason) == "provider wait is pending"
            ):
                failure = {"kind": "binding_unknown", "legacy_kind": "unknown"}
            if failure.get("legacy_kind") == "engineering":
                self._queue_active_retry(snapshot, node_id, raw_reason)
                self._set_binding_status(snapshot, node_id, role, "blocked_unknown")
                current["status"] = self._v42_retry_status()
                current["reason"] = reason
                current["quarantine"] = None
            else:
                # Unknown evidence and external boundaries are fail-closed;
                # neither is allowed to consume an engineering retry budget.
                self._mark_blocked_unknown(
                    snapshot, node_id, reason,
                    quarantine_lease=False,
                    retryable_same_task=False,
                )
        elif event.event in {"failed", "stopped", "terminal_failed"}:
            failure = classify_provider_failure(event.data)
            if failure["kind"] == "external_decision":
                current["retryable_action"] = None
                self._mark_blocked_unknown(
                    snapshot, node_id,
                    str(redact_provider_text(event.data.get("reason", event.event))),
                )
                return
            if failure.get("legacy_kind") == "engineering" and event.event != "stopped":
                raw_reason = event.data.get("reason", event.event)
                reason = str(redact_provider_text(raw_reason))
                self._queue_active_retry(snapshot, node_id, raw_reason)
                self._set_binding_status(snapshot, node_id, role, "blocked_unknown")
                current["status"] = self._v42_retry_status()
                current["reason"] = reason
                current["quarantine"] = None
                return
            self._record_runner_event(snapshot, node_id, event, active)
            terminal_status = "failed" if event.event != "stopped" else "stopped"
            try:
                self._set_binding_status(snapshot, node_id, role, terminal_status)
            except (FileNotFoundError, OSError, TypeError, ValueError) as error:
                self._mark_blocked_unknown(
                    snapshot,
                    node_id,
                    "task binding cannot record terminal state ({})".format(
                        type(error).__name__
                    ),
                )
                return
            current["status"] = terminal_status
            current["reason"] = redact_provider_text(
                event.data.get("reason", event.event)
            )
            current["active_role"] = None
            current["active_task"] = None
            current["quarantine"] = None
            snapshot.handles.pop(node_id, None)
            self._release_node_lease(snapshot, node_id)
        else:
            self._mark_blocked_unknown(
                snapshot, node_id, "unrecognized runner event: " + event.event
            )

    def _stop_active_for_transition(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        role: str,
        handle_id: str,
        runner: Runner,
        terminal_status: str,
    ) -> bool:
        try:
            runner.stop(RunHandle(handle_id))
            self._set_binding_status(snapshot, node_id, role, terminal_status)
        except Exception as error:
            self._mark_blocked_unknown(
                snapshot,
                node_id,
                "old task stop cannot be proven ({})".format(
                    type(error).__name__
                ),
            )
            return False
        snapshot.handles.pop(node_id, None)
        snapshot.nodes[node_id]["active_role"] = None
        snapshot.nodes[node_id]["active_task"] = None
        return True

    def _validate_visible_sdd_review(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        event: RunEvent,
        current: Dict[str, Any],
    ):
        """Pure validation of the in-session review evidence; no writes.

        Returns ``None`` when the delivery may be accepted, otherwise
        ``(reason, recoverable)``.  Missing or malformed evidence and a
        foreign protocol pointer are format errors (recoverable -- the same
        worker session can correct and re-report on its live handle, per
        ISSUE-83); an unreadable or drifted node contract is an evidence
        problem and stays fail-closed.
        """
        review = event.data.get("in_session_review")
        clearance = review.get("clearance") if isinstance(review, dict) else None
        evidence_ref = review.get("evidence_ref") if isinstance(review, dict) else None
        valid = (
            isinstance(review, dict)
            and isinstance(clearance, dict)
            and all(
                isinstance(clearance.get(severity), int)
                and not isinstance(clearance.get(severity), bool)
                and clearance.get(severity) == 0
                for severity in ("p0", "p1", "p2")
            )
            and isinstance(evidence_ref, str)
            and bool(evidence_ref.strip())
        )
        if not valid:
            return "visible-sdd delivery lacks verifiable in-session review evidence", True
        if review.get("protocol") != VISIBLE_SDD_PROTOCOL_REF:
            # The session must have followed the protocol shipped with this
            # package; a different or missing pointer is not the visible-sdd
            # review this acceptance certifies.
            return "visible-sdd delivery cites an unknown in-session review protocol", True
        # ISSUE-02: the acceptance evidence is bound to the node contract it
        # reviewed.  The carried digest is the dispatch-time node digest and
        # must still equal the digest recomputed from the live contract; an
        # unreadable contract or a drifted one refuses the acceptance instead
        # of writing an empty, placeholder or stale digest.
        try:
            live_digest = self._live_node_contract_digest(node_id)
        except ValueError as error:
            return "visible-sdd acceptance refused: {}".format(error), False
        if current.get("contract_digest") != live_digest:
            return (
                "visible-sdd acceptance refused: contract digest does not match the live node contract",
                False,
            )
        return None

    def _accept_visible_sdd_delivery(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        event: RunEvent,
        current: Dict[str, Any],
    ) -> None:
        """Accept a visible-sdd node from its in-session review evidence.

        The single worker session carries the whole SDD loop (implement,
        independent read-only review, rework, re-review); the delivery event
        must therefore cite the in-session review clearance.  The live
        delivery path validates this evidence before any state changes
        (ISSUE-83) and only reaches this function with a payload that passed;
        the fail-closed checks are kept here so a direct or replay-adjacent
        caller can never write an acceptance the validator would refuse.
        """
        review = event.data.get("in_session_review")
        evidence_ref = review.get("evidence_ref") if isinstance(review, dict) else None
        failure = self._validate_visible_sdd_review(snapshot, node_id, event, current)
        if failure is not None:
            reason, _recoverable = failure
            self._mark_blocked_unknown(snapshot, node_id, reason)
            return
        try:
            acceptance = VisibleSddAcceptance(
                contract_digest=current.get("contract_digest"),
                protocol_ref=VISIBLE_SDD_PROTOCOL_REF,
                evidence_ref=evidence_ref.strip(),
            )
        except (AttributeError, ValueError) as error:
            self._mark_blocked_unknown(
                snapshot,
                node_id,
                "visible-sdd acceptance refused: {}".format(error),
            )
            return
        # The in-session reviewer acts under the same single session
        # identity (ISSUE-01/05 semantics): the acceptance provenance names
        # the reviewer role of that session, and the review proof travels in
        # the event payload (evidence keeps the durable, replay-checked
        # clearance reference; topology/protocol/clearance annotate it).
        review_identity = current.get("developer_identity")
        review_generation = int(current.get("developer_generation", 0))
        try:
            # The binding transition lands before the durable accepted event:
            # a failure here must not leave an acceptance the registry never
            # confirmed.
            self._set_binding_status(snapshot, node_id, "developer", "accepted")
        except (FileNotFoundError, OSError, TypeError, ValueError) as error:
            self._mark_blocked_unknown(
                snapshot,
                node_id,
                "task binding cannot record acceptance ({})".format(
                    type(error).__name__
                ),
            )
            return
        current["reviewer_identity"] = review_identity
        current["review_generation"] = review_generation
        self._record(
            snapshot,
            "accepted",
            {
                "run_id": snapshot.run_id,
                "node_id": node_id,
                "evidence": evidence_ref.strip(),
                "topology": TOPOLOGY_VISIBLE_SDD,
                "clearance": {"p0": 0, "p1": 0, "p2": 0},
                **acceptance.to_dict(),
                "authorization_epoch": snapshot.authorization_digest,
            },
            {
                "role": "reviewer",
                "task_id": review_identity,
                "handle_id": None,
                "generation": review_generation,
            },
        )
        current["status"] = "accepted"
        current["acceptance"] = {
            "contract_digest": current["contract_digest"],
            "authorization_epoch": snapshot.authorization_digest,
        }
        # A corrected re-report must not keep the rejection text.
        current["reason"] = None
        current["review_clearance"] = {"p0": 0, "p1": 0, "p2": 0}
        current["quarantine"] = None
        self._archive_pair(snapshot, node_id)
        self._release_node_lease(snapshot, node_id)

    def _archive_pair(self, snapshot: RunSnapshot, node_id: str) -> None:
        current = snapshot.nodes[node_id]
        roles = ("developer", "reviewer")
        if self._node_dispatch_topology(self.nodes[node_id]) == TOPOLOGY_VISIBLE_SDD:
            # A visible-sdd node holds exactly one worker-session binding.
            roles = ("developer",)
        for role in roles:
            self._set_binding_status(snapshot, node_id, role, "archived")
        current["pair_archived"] = True
        self._record(
            snapshot,
            "pair_archived",
            {
                "run_id": snapshot.run_id,
                "node_id": node_id,
                "clearance": current["review_clearance"],
            },
        )

    @staticmethod
    def _event_claims_match(
        event: RunEvent, node_id: str, active: Dict[str, Any]
    ) -> bool:
        claims = {
            "node_id": node_id,
            "role": active.get("role"),
            "task_id": active.get("task_id"),
            "handle_id": active.get("handle_id"),
            "generation": active.get("generation"),
        }
        return all(event.data.get(key) == value for key, value in claims.items())

    @staticmethod
    def _is_implementation_finding(data: Dict[str, Any]) -> bool:
        """Recognize an explicit P0-P2 implementation defect from review data."""
        values = []
        if isinstance(data, dict):
            values.append(data.get("severity"))
            for key in ("finding", "review_finding"):
                value = data.get(key)
                if isinstance(value, dict):
                    values.append(value.get("severity"))
                    nested = value.get("finding")
                    if isinstance(nested, dict):
                        values.append(nested.get("severity"))
        for value in values:
            if isinstance(value, str):
                severity = value.strip().upper()
                if severity in {"P0", "P1", "P2"}:
                    return True
        return False

    def _record_runner_event(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        event: RunEvent,
        active: Dict[str, Any],
    ) -> None:
        data = dict(event.data)
        data.update({"run_id": snapshot.run_id, "node_id": node_id})
        self._record(snapshot, event.event, data, active)

    def _queue_active_retry(
        self, snapshot: RunSnapshot, node_id: str, reason: str, *, record_event: bool = True
    ) -> None:
        """Record a retry intent without allocating a new task identity.

        A transient provider result is still unknown, but an active handle is
        durable evidence that the same task may be polled again.  Persist the
        logical retry alongside that handle so snapshot recovery can resume
        the original task even when the next poll happens in a new process.
        """
        current = snapshot.nodes[node_id]
        active = current.get("active_task")
        handle_id = snapshot.handles.get(node_id)
        if not isinstance(active, dict) or not isinstance(handle_id, str):
            return
        role = active.get("role")
        task_id = active.get("task_id")
        generation = active.get("generation")
        if role not in {"developer", "reviewer"} or not isinstance(task_id, str):
            return
        classification = classify_provider_failure({"reason": reason})
        if classification["kind"] == "external_decision":
            current["retryable_action"] = None
            return
        phase = "review" if role == "reviewer" else (
            "rework" if current.get("status") == "rework" else "develop"
        )
        previous = current.get("retryable_action")
        attempt = 1
        if isinstance(previous, dict) and previous.get("same_task") is True:
            try:
                attempt = int(previous.get("attempt", 0)) + 1
            except (TypeError, ValueError):
                attempt = 1
        observation_ref = hashlib.sha256(str(reason).encode("utf-8")).hexdigest()
        cursor = None
        try:
            binding = self._load_task_binding(snapshot, node_id, str(role))
            cursor = binding.cursor
        except (FileNotFoundError, OSError, TypeError, ValueError):
            pass
        retry = {
            "role": role,
            "phase": phase,
            "continuation": True,
            "successor": False,
            "successor_candidate": False,
            "same_task": True,
            "same_task_required": True,
            "task_id": task_id,
            "handle_id": handle_id,
            "generation": generation,
            "cursor": cursor,
            "attempt": attempt,
            "reason_class": classification["kind"],
            "next_retry_at": time.time() + min(60.0, float(2 ** min(attempt - 1, 5))),
            "binding_digest": str(current.get("contract_digest") or snapshot.node_contract_digest or ""),
            "last_observation_ref": observation_ref,
            "reason": redact_provider_text(reason),
        }
        current["retryable_action"] = retry
        if not record_event:
            retry["defer_once"] = True
        if record_event:
            self._record(
                snapshot,
                "retry_scheduled",
                {
                    "run_id": snapshot.run_id,
                    "node_id": node_id,
                    "role": role,
                    "task_id": task_id,
                    "generation": generation,
                    "cursor": cursor,
                    "attempt": attempt,
                    "reason_class": classification["kind"],
                    "next_retry_at": retry["next_retry_at"],
                    "same_task_required": True,
                    "binding_digest": retry["binding_digest"],
                    "last_observation_ref": observation_ref,
                    "reason": reason,
                },
                active,
            )

    def _recovery_snapshot(self, node_id: str, run_id: Optional[str] = None) -> RunSnapshot:
        """Load the uniquely bound durable run snapshot for a node."""
        runs = self.paths.vibe / "runs"
        if run_id:
            return load_snapshot(self.paths, run_id)
        candidates = []
        for path in runs.glob("*/state.json"):
            try:
                snapshot = load_snapshot(self.paths, path.parent.name)
                if node_id in snapshot.nodes:
                    candidates.append(snapshot)
            except (OSError, TypeError, ValueError):
                continue
        if len(candidates) != 1:
            raise ValueError("binding_unknown: run id is required for ambiguous node binding")
        return candidates[0]

    def schedule_retry(self, node_id: str, reason: str, next_retry_at: Optional[float] = None, run_id: Optional[str] = None) -> None:
        """Persist same-task retry state with exponential backoff."""
        snapshot = self._recovery_snapshot(node_id, run_id)
        current = snapshot.nodes[node_id]
        classification = classify_provider_failure({"reason": reason})
        if classification["kind"] == "external_decision":
            current["status"] = "blocked_design"
            current["reason"] = redact_provider_text(reason)
            current["retryable_action"] = None
        else:
            previous = current.get("retryable_action") if isinstance(current.get("retryable_action"), dict) else {}
            attempt = int(previous.get("attempt", 0) or 0) + 1
            delay = min(60.0, float(2 ** min(attempt - 1, 5)))
            retry = dict(previous)
            retry.update({"same_task": True, "same_task_required": True, "successor": False,
                          "attempt": attempt, "reason_class": classification["kind"],
                          "reason": redact_provider_text(reason),
                          "next_retry_at": float(next_retry_at if next_retry_at is not None else time.time() + delay)})
            current["retryable_action"] = retry
            current["status"] = "retry_pending" if classification["kind"] in {"retryable", "capacity_wait"} else "blocked_unknown"
            current["reason"] = redact_provider_text(reason)
        self._record(snapshot, "retry_scheduled", {"run_id": snapshot.run_id, "node_id": node_id,
            "reason_class": classification["kind"], "reason": reason,
            "same_task_required": True, "next_retry_at": current.get("retryable_action", {}).get("next_retry_at") if isinstance(current.get("retryable_action"), dict) else None})
        save_snapshot(self.paths, snapshot)

    def repair_node(self, node_id: str, run_id: Optional[str] = None) -> Dict[str, Any]:
        """Repair a node in place; identity fields are immutable across repair."""
        snapshot = self._recovery_snapshot(node_id, run_id)
        current = snapshot.nodes[node_id]
        before = {key: current.get(key) for key in ("task_id", "generation", "writer", "lease", "cursor", "intent_digest", "contract_digest")}
        reason = str(current.get("reason") or "repair requested")
        classification = classify_provider_failure({"reason": reason})
        if classification["kind"] == "binding_unknown":
            current["status"] = "blocked_unknown"
        elif classification["kind"] == "external_decision":
            current["status"] = "blocked_design"
        else:
            current["status"] = "retry_pending"
            self.schedule_retry(node_id, reason, run_id=snapshot.run_id)
            snapshot = self._recovery_snapshot(node_id, snapshot.run_id)
            current = snapshot.nodes[node_id]
        after = {key: current.get(key) for key in before}
        preserved = before == after
        repair_action = "git/provider_reconcile" if classification["kind"] == "repairable" else "same_task_retry"
        reconciliation = {"git": {"observed": False}, "provider": {"observed": False, "status": "unavailable"}}
        if classification["kind"] == "repairable":
            worktree = current.get("worktree")
            if isinstance(worktree, str) and worktree:
                try:
                    reconciliation["git"] = {
                        "observed": True,
                        "worktree": worktree,
                        "head": subprocess.check_output(["git", "-C", worktree, "rev-parse", "HEAD"], text=True, stderr=subprocess.STDOUT, timeout=5).strip(),
                        "branch": subprocess.check_output(["git", "-C", worktree, "branch", "--show-current"], text=True, stderr=subprocess.STDOUT, timeout=5).strip(),
                        "dirty": bool(subprocess.check_output(["git", "-C", worktree, "status", "--porcelain"], text=True, stderr=subprocess.STDOUT, timeout=5).strip()),
                    }
                except (OSError, subprocess.SubprocessError) as error:
                    reconciliation["git"] = {"observed": False, "error": type(error).__name__}
        self._record(snapshot, "node_repaired", {"run_id": snapshot.run_id, "node_id": node_id,
            "kind": classification["kind"], "preserved_identity": preserved,
            "repair_action": repair_action, "same_task": True,
            "reconciliation": reconciliation,
            "task_id": current.get("task_id"), "generation": current.get("generation")})
        save_snapshot(self.paths, snapshot)
        return {"node_id": node_id, "kind": classification["kind"], "preserved_identity": preserved,
                "status": current.get("status"), "task_id": current.get("task_id"),
                "generation": current.get("generation"), "repair_action": repair_action,
                "run_id": snapshot.run_id, "reconciliation": reconciliation}

    def _mark_blocked_unknown(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        reason: str,
        quarantine_lease: bool = True,
        retryable_same_task: bool = False,
    ) -> None:
        current = snapshot.nodes[node_id]
        active = current.get("active_task")
        handle_id = snapshot.handles.get(node_id)
        current["status"] = "blocked_unknown"
        current["reason"] = reason
        current["quarantine"] = {
            "run_id": snapshot.run_id,
            "handle_id": handle_id,
            "reason": reason,
        }
        if isinstance(active, dict) and active.get("role") in {"developer", "reviewer"}:
            try:
                self._set_binding_status(
                    snapshot, node_id, str(active["role"]), "blocked_unknown"
                )
            except (FileNotFoundError, OSError, TypeError, ValueError):
                pass
        if quarantine_lease:
            quarantine_writer_lease(
                self.paths,
                node_id,
                str(current.get("worktree", "")),
                snapshot.run_id,
                reason,
            )
        event_data = {
            "run_id": snapshot.run_id,
            "node_id": node_id,
            "reason": reason,
        }
        if retryable_same_task:
            event_data["retryable_same_task"] = True
        self._record(
            snapshot,
            "blocked_unknown",
            event_data,
            active if isinstance(active, dict) else None,
        )

    def _record(
        self,
        snapshot: RunSnapshot,
        event_name: str,
        data: Dict[str, Any],
        active: Optional[Dict[str, Any]] = None,
    ) -> None:
        provenance = {
            "role": active.get("role", "system") if active else "system",
            "task_id": active.get("task_id") if active else None,
            "handle_id": active.get("handle_id") if active else None,
            "generation": active.get("generation", 0) if active else 0,
            "authorization_digest": snapshot.authorization_digest,
            "node_contract_digest": snapshot.node_contract_digest,
        }
        snapshot.event_sequence = append_event(
            self.paths, RunEvent(event_name, data), provenance
        )

    def _context_estimate(self, snapshot: RunSnapshot, runner: Runner):
        """Obtain an evidence-bound context estimate when the runner exposes one.

        V1 runners do not expose a context limit and therefore retain the
        existing scheduling behavior.  V2 adapters may provide either a
        ``context_budget`` mapping or ``context_limit_tokens`` plus optional
        text fields; absence is intentionally not treated as zero.
        """
        policy = self.context_policy
        supplied = getattr(runner, "context_budget", None)
        if callable(supplied):
            supplied = supplied()
        if isinstance(supplied, dict):
            policy = policy or ContextBudgetPolicy(
                supplied.get("context_limit_tokens", "observed-model-limit"),
                supplied.get("reserve_tokens"),
                supplied.get("warning_ratio", 0.70),
                supplied.get("checkpoint_ratio", 0.80),
                supplied.get("hard_stop_ratio", 0.90),
            )
            fields = dict(getattr(runner, "context_input", {}) or {})
            fields.update(supplied)
        else:
            limit = getattr(runner, "context_limit_tokens", None)
            if limit is None and policy is None:
                return None
            policy = policy or ContextBudgetPolicy(limit)
            fields = getattr(runner, "context_input", {})
            if not isinstance(fields, dict):
                fields = {}
        estimator = ContextBudgetEstimator(policy)
        if "event_summary" not in fields:
            try:
                recent = load_events(self.paths, snapshot.run_id)[-5:]
                fields["event_summary"] = json.dumps(recent, ensure_ascii=False, sort_keys=True)
            except (OSError, ValueError, TypeError):
                fields["event_summary"] = ""
        if "checkpoint" not in fields:
            checkpoint_path = self.paths.root / ".vibe" / "runs" / snapshot.run_id / "monitor_checkpoint.json"
            try:
                fields["checkpoint"] = checkpoint_path.read_text(encoding="utf-8") if checkpoint_path.is_file() else ""
            except OSError:
                fields["checkpoint"] = ""
        return estimator.estimate(
            str(fields.get("system_prompt", "")),
            str(fields.get("current_input", "")),
            str(fields.get("event_summary", "")),
            str(fields.get("checkpoint", "")),
            str(fields.get("expected_output", "")),
            fields.get("tokenizer"),
        )

    def _checkpoint_context(self, snapshot: RunSnapshot, reason: str, exhausted: bool = False, estimate: Any = None) -> None:
        profiles = {}
        for node_id, node in self.nodes.items():
            profile = node.contract.get("worker_profile")
            if isinstance(profile, dict):
                profiles[node_id] = profile
        checkpoint = MonitorCheckpoint(
            run_id=snapshot.run_id,
            plan_revision="{}@{}".format(snapshot.plan_id, snapshot.plan_version),
            state_version=snapshot.schema_version,
            last_event_seq=snapshot.event_sequence,
            next_action="resume",
            stop_conditions=["preserve one run lease", "do not duplicate writer"],
            authorization_digest=snapshot.authorization_digest,
            node_contract_digest=snapshot.node_contract_digest,
            capability_contract_digest=snapshot.capability_contract_digest,
            nodes=snapshot.nodes,
            handles=snapshot.handles,
            worker_profiles=profiles,
            evidence=[{"reason": reason}],
            estimate=(estimate.__dict__ if estimate is not None else None),
        )
        write_checkpoint(self.paths, checkpoint)
        event_name = "monitor_context_exhausted" if exhausted else "monitor_context_checkpoint"
        self._record(
            snapshot,
            event_name,
            {
                "run_id": snapshot.run_id,
                "reason": reason,
                "checkpoint_sha": checkpoint.sha256,
                "last_event_seq": snapshot.event_sequence,
            },
        )
        if exhausted:
            active_nodes = [node_id for node_id, current in snapshot.nodes.items() if current.get("active_task")]
            for node_id in active_nodes:
                snapshot.nodes[node_id]["status"] = "blocked_unknown"
            snapshot.status = "blocked_unknown"

    def _context_allows_dispatch(self, snapshot: RunSnapshot, runner: Runner) -> bool:
        estimate = self._context_estimate(snapshot, runner)
        if estimate is None:
            return True
        if estimate.status == "normal":
            return True
        events = load_events(self.paths, snapshot.run_id)
        if estimate.status == "warning":
            if not any(item.get("event") == "monitor_context_warning" for item in events):
                self._record(snapshot, "monitor_context_warning", {
                    "run_id": snapshot.run_id,
                    "ratio": estimate.ratio,
                    "total_tokens": estimate.total_tokens,
                    "limit_tokens": estimate.limit_tokens,
                    "source": estimate.source,
                })
            return True
        if estimate.status in {"checkpoint", "hard_stop", "blocked_unknown"}:
            self._checkpoint_context(
                snapshot,
                "context budget status: " + estimate.status,
                exhausted=estimate.status in {"hard_stop", "blocked_unknown"},
                estimate=estimate,
            )
            return False
        return True

    def _reconcile_start_intent(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        data: Dict[str, Any],
        provenance: Dict[str, Any],
    ) -> None:
        current = snapshot.nodes[node_id]
        role = data.get("role")
        phase = data.get("phase")
        if role not in {"developer", "reviewer"}:
            raise ValueError("start intent role is invalid")
        if (role == "reviewer" and phase != "review") or (
            role == "developer" and phase not in {"develop", "rework"}
        ):
            raise ValueError("start intent phase is invalid")
        identity = data.get("task_id")
        expected_identity = (
            current.get(role + "_identity")
            or self._identity_from_contract(self.nodes[node_id], role)
            or "{}:{}".format(role, node_id)
        )
        generation_key = (
            "review_generation" if role == "reviewer" else "developer_generation"
        )
        expected_generation = int(current.get(generation_key, 0)) + 1
        if identity != expected_identity or data.get("generation") != expected_generation:
            raise ValueError("start intent identity or generation is stale")
        if (
            provenance.get("role") != role
            or provenance.get("task_id") != identity
            or provenance.get("generation") != expected_generation
            or provenance.get("handle_id") is not None
        ):
            raise ValueError("start intent provenance is invalid")
        binding = self._load_task_binding(snapshot, node_id, role)
        if (
            binding.task_id != identity
            or binding.generation != expected_generation
            or binding.status != "start_pending"
        ):
            raise ValueError("start intent task binding is stale")
        current[role + "_identity"] = identity
        current[generation_key] = expected_generation
        if role == "reviewer":
            current["reviewer_started"] = True
        current["status"] = "start_pending"
        current["active_role"] = role
        current["start_intent"] = {
            key: data[key]
            for key in ("intent_id", "role", "phase", "task_id", "generation")
        }
        current["active_task"] = {
            "role": role,
            "task_id": identity,
            "generation": expected_generation,
            "handle_id": None,
        }

    def _reconcile_start_confirmed(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        data: Dict[str, Any],
        provenance: Dict[str, Any],
    ) -> None:
        current = snapshot.nodes[node_id]
        active = current.get("active_task")
        if not isinstance(active, dict) or active.get("handle_id") is not None:
            raise ValueError("start confirmation has no matching intent")
        expected = {
            "role": data.get("role"),
            "task_id": data.get("task_id"),
            "generation": data.get("generation"),
        }
        if any(active.get(key) != value for key, value in expected.items()):
            raise ValueError("start confirmation identity or generation is stale")
        handle_id = data.get("handle_id")
        if not isinstance(handle_id, str) or not handle_id:
            raise ValueError("start confirmation handle is invalid")
        if (
            provenance.get("role") != expected["role"]
            or provenance.get("task_id") != expected["task_id"]
            or provenance.get("generation") != expected["generation"]
            or provenance.get("handle_id") != handle_id
        ):
            raise ValueError("start confirmation provenance is invalid")
        if any(
            other_node != node_id and other_handle == handle_id
            for other_node, other_handle in snapshot.handles.items()
        ):
            raise ValueError("start confirmation duplicates an active handle")
        binding = self._load_task_binding(snapshot, node_id, str(expected["role"]))
        desired_status = self._running_status(str(expected["role"]), str(data.get("phase")))
        if binding.status not in {"start_pending", desired_status}:
            raise ValueError("start confirmation task binding status is invalid")
        self._set_binding_status(
            snapshot, node_id, str(expected["role"]), desired_status
        )
        active["handle_id"] = handle_id
        current["active_role"] = expected["role"]
        current["start_intent"] = None
        current["status"] = desired_status
        snapshot.handles[node_id] = handle_id

    def _reconcile_unapplied_events(self, snapshot: RunSnapshot) -> None:
        records = load_events(self.paths, snapshot.run_id)
        pending = records[snapshot.event_sequence :]
        for record in pending:
            if record.get("event") == "supervisor_heartbeat":
                # Heartbeats are supervisor lease telemetry, not node
                # lifecycle events.  They may be appended between snapshot
                # writes and must be consumed before provider polling.
                snapshot.event_sequence = record["sequence"]
                continue
            provenance = record["provenance"]
            if (
                provenance["authorization_digest"] != snapshot.authorization_digest
                or provenance["node_contract_digest"] != snapshot.node_contract_digest
            ):
                raise ValueError("unapplied event lineage is inconsistent")
            data = record["data"]
            if data.get("run_id") != snapshot.run_id:
                raise ValueError("unapplied event run lineage is inconsistent")
            if record["event"] == "execution_topology_observed":
                # System projection evidence is read-only metadata; legacy
                # V3.9 replay must not interpret it as a node lifecycle event.
                snapshot.event_sequence = record["sequence"]
                continue
            if record["event"] == "authorization_reauthorized":
                self._apply_reauthorization_transition(snapshot, data)
                snapshot.event_sequence = record["sequence"]
                continue
            if str(record["event"]).startswith("change_request_"):
                # Change-request evidence is a system-level, target-bound
                # observation.  Replay must be idempotent and preserve it
                # even when the originating node was removed from a newer
                # snapshot; unlike worker events this is not an unknown-node
                # failure.
                if provenance.get("role") != "system":
                    raise ValueError("change request replay lacks system provenance")
                evidence = data.get("evidence")
                if not isinstance(evidence, dict):
                    raise ValueError("change request replay evidence is invalid")
                try:
                    validated = ChangeRequestEvidence.from_dict(evidence)
                    prepared_check = execute_change_request(validated.action, validated.target_contract)
                    if prepared_check.target_digest != validated.target_digest or validated.remote_mutated:
                        raise ValueError("change request replay target evidence is inconsistent")
                except (TypeError, ValueError) as error:
                    raise ValueError("change request replay evidence is invalid") from error
                if not str(record["event"]).startswith("change_request_") or str(record["event"])[len("change_request_"):] != validated.status:
                    raise ValueError("change request replay status is inconsistent")
                node_id = data.get("node_id")
                if isinstance(node_id, str) and node_id in snapshot.nodes:
                    bucket = snapshot.nodes[node_id].setdefault("change_request_evidence", [])
                    if evidence not in bucket:
                        bucket.append(evidence)
                else:
                    bucket = snapshot.tasks.setdefault("_change_request_evidence", {"records": []})
                    if isinstance(bucket, dict):
                        records_bucket = bucket.setdefault("records", [])
                        if evidence not in records_bucket:
                            records_bucket.append(evidence)
                snapshot.event_sequence = record["sequence"]
                continue
            node_id = data.get("node_id")
            if node_id not in snapshot.nodes:
                raise ValueError("unapplied event references an unknown node")
            current = snapshot.nodes[node_id]
            if record["event"] == "start_intent":
                self._reconcile_start_intent(
                    snapshot, node_id, data, provenance
                )
            elif record["event"] == "start_confirmed":
                self._reconcile_start_confirmed(
                    snapshot, node_id, data, provenance
                )
            elif record["event"] in {"blocked_unknown", "unknown", "timeout"}:
                if provenance["role"] != "system":
                    binding = self._registered_active_binding(
                        snapshot, node_id, provenance
                    )
                    self._set_binding_status(
                        snapshot, node_id, str(provenance["role"]), "blocked_unknown"
                    )
                if (
                    record["event"] in {"unknown", "timeout"}
                    or data.get("retryable_same_task") is True
                ):
                    self._queue_active_retry(
                        snapshot,
                        node_id,
                        str(data.get("reason", record["event"])),
                        record_event=False,
                    )
                current["status"] = self._v42_retry_status() if data.get("retryable_same_task") is True or record["event"] in {"unknown", "timeout"} else "blocked_unknown"
                current["reason"] = data.get("reason", record["event"])
                current["quarantine"] = {
                    "run_id": snapshot.run_id,
                    "handle_id": snapshot.handles.get(node_id),
                    "reason": current["reason"],
                }
            elif record["event"] == "retry_scheduled":
                if provenance.get("role") not in {"developer", "reviewer"}:
                    raise ValueError("retry event lacks task provenance")
                active = current.get("active_task")
                if not isinstance(active, dict) or active.get("task_id") != data.get("task_id"):
                    raise ValueError("retry event task identity is stale")
                for field in ("task_id", "handle_id", "generation"):
                    if provenance.get(field) != active.get(field):
                        raise ValueError("retry event provenance is stale")
                if data.get("generation") != active.get("generation"):
                    raise ValueError("retry event generation is stale")
                # The retry marker is durable supervisor state, but the
                # provider binding must also be quarantined during replay.
                # Without this projection the registry remains ``running``
                # after an interrupted snapshot write, allowing a stale
                # binding to be treated as live on the next resume.
                self._set_binding_status(
                    snapshot,
                    node_id,
                    str(provenance["role"]),
                    "blocked_unknown",
                )
                current["retryable_action"] = {
                    "role": data.get("role"),
                    "phase": "review" if data.get("role") == "reviewer" else "develop",
                    "continuation": True,
                    "successor": False,
                    "successor_candidate": False,
                    "same_task": True,
                    "same_task_required": bool(data.get("same_task_required", True)),
                    "task_id": data.get("task_id"),
                    "handle_id": active.get("handle_id"),
                    "generation": data.get("generation"),
                    "cursor": data.get("cursor"),
                    "attempt": data.get("attempt", 1),
                    "reason_class": data.get("reason_class", "unknown"),
                    "next_retry_at": data.get("next_retry_at"),
                    "binding_digest": data.get("binding_digest", ""),
                    "last_observation_ref": data.get("last_observation_ref", ""),
                    "reason": data.get("reason", "retry scheduled"),
                    "defer_once": True,
                }
                current["status"] = self._v42_retry_status()
                current["quarantine"] = {
                    "run_id": snapshot.run_id,
                    "handle_id": active.get("handle_id"),
                    "reason": data.get("reason", "retry scheduled"),
                }
            elif record["event"] == "old_task_reconciled":
                if provenance["role"] != "system":
                    raise ValueError("old task reconciliation lacks system provenance")
                role = data.get("role")
                retry = current.get("retryable_action")
                valid = (
                    isinstance(retry, dict)
                    and retry.get("role") == role
                    and retry.get("successor_candidate") is True
                    and data.get("proof") in {"absent", "stopped"}
                )
                predecessor = data.get("predecessor_task_id")
                proof_identity = None
                if valid:
                    proof_identity = self._prove_old_task_stopped_or_absent(
                        snapshot,
                        node_id,
                        retry,
                    )
                    if data.get("proof") == "absent":
                        valid = proof_identity == "" and predecessor in {None, ""}
                    else:
                        valid = (
                            isinstance(predecessor, str)
                            and bool(predecessor)
                            and proof_identity == predecessor
                        )
                if not valid:
                    self._reject_replayed_old_task_reconciliation(
                        snapshot, node_id, data
                    )
                    snapshot.event_sequence = record["sequence"]
                    continue
                current["old_task_reconciled"] = True
                if isinstance(retry, dict):
                    retry = dict(retry)
                    retry["old_task_reconciled"] = True
                    retry["successor"] = True
                    retry["continuation"] = False
                    retry["predecessor_task_id"] = predecessor
                    current["retryable_action"] = retry
            elif record["event"] == "accepted":
                if self._node_dispatch_topology(self.nodes[node_id]) == TOPOLOGY_VISIBLE_SDD:
                    self._replay_visible_sdd_acceptance(
                        snapshot, node_id, data, provenance
                    )
                    snapshot.event_sequence = record["sequence"]
                    continue
                if provenance["role"] != "reviewer":
                    raise ValueError("unapplied acceptance lacks reviewer provenance")
                self._registered_active_binding(snapshot, node_id, provenance)
                if (
                    provenance["task_id"] != current.get("reviewer_identity")
                    or provenance["generation"] != current.get("review_generation")
                    or provenance["handle_id"] != snapshot.handles.get(node_id)
                ):
                    raise ValueError(
                        "unapplied acceptance reviewer identity or generation is stale"
                    )
                try:
                    registered = self._load_task_binding(snapshot, node_id, "reviewer")
                except (FileNotFoundError, OSError, TypeError, ValueError) as error:
                    raise ValueError(
                        "unapplied acceptance reviewer binding is unavailable"
                    ) from error
                if (
                    registered.task_id != provenance["task_id"]
                    or registered.generation != provenance["generation"]
                ):
                    raise ValueError("unapplied acceptance registry generation is stale")
                self._set_binding_status(
                    snapshot, node_id, "reviewer", "accepted"
                )
                current["status"] = "accepted"
                contract_digest = data.get("contract_digest")
                authorization_epoch = data.get("authorization_epoch")
                # ISSUE-02 (dual-visible replay): recompute the live contract
                # digest and require it to match, identical to the visible-sdd
                # replay path at _replay_visible_sdd_acceptance.  A snapshot
                # copy alone cannot detect a drifted contract.
                try:
                    live_digest = self._live_node_contract_digest(node_id)
                except ValueError as error:
                    raise ValueError(
                        "unapplied dual-visible acceptance node contract is unreadable"
                    ) from error
                if (
                    not isinstance(contract_digest, str)
                    or len(contract_digest) != 64
                    or any(character not in "0123456789abcdef" for character in contract_digest)
                    or contract_digest != current.get("contract_digest")
                    or contract_digest != live_digest
                    or authorization_epoch != snapshot.authorization_digest
                ):
                    raise ValueError("unapplied acceptance contract epoch is stale")
                current["acceptance"] = {
                    "contract_digest": contract_digest,
                    "authorization_epoch": authorization_epoch,
                }
                # Match the live path: a replayed acceptance must not
                # resurrect the rejection text the lost snapshot carried.
                current["reason"] = None
                current["active_role"] = None
                current["active_task"] = None
                current["quarantine"] = None
                snapshot.handles.pop(node_id, None)
                if not data.get("evidence"):
                    current["status"] = "blocked_unknown"
                    current["reason"] = (
                        "review acceptance has no registered P0-P2 clearance evidence"
                    )
                elif node_id == "integration-review":
                    # The appended event keeps only redacted provider text, so the
                    # reviewer's structured claim cannot be rebuilt here.  Rather
                    # than close the run out on a package we would have to invent,
                    # expose the gap and let the reviewer report the acceptance
                    # again on a live handle.
                    current["status"] = "blocked_unknown"
                    current["reason"] = (
                        "integration review acceptance must be reported again after recovery"
                    )
                else:
                    current["review_clearance"] = {
                        "p0": 0,
                        "p1": 0,
                        "p2": 0,
                    }
                    for role in ("developer", "reviewer"):
                        self._set_binding_status(
                            snapshot, node_id, role, "archived"
                        )
                    current["pair_archived"] = True
                self._release_node_lease(snapshot, node_id)
            elif record["event"] in {"failed", "stopped", "terminal_failed"}:
                self._registered_active_binding(snapshot, node_id, provenance)
                terminal_status = (
                    "failed" if record["event"] != "stopped" else "stopped"
                )
                self._set_binding_status(
                    snapshot, node_id, str(provenance["role"]), terminal_status
                )
                current["status"] = terminal_status
                current["reason"] = data.get("reason", record["event"])
                current["active_role"] = None
                current["active_task"] = None
                snapshot.handles.pop(node_id, None)
                self._release_node_lease(snapshot, node_id)
            elif record["event"] in {"delivered", "complete"}:
                self._registered_active_binding(snapshot, node_id, provenance)
                if provenance["role"] != "developer":
                    current["status"] = "blocked_unknown"
                    current["reason"] = "unapplied reviewer delivery needs reconciliation"
                else:
                    self._set_binding_status(
                        snapshot, node_id, "developer", "delivered"
                    )
                    current["status"] = "delivered"
                    current["active_role"] = None
                    current["active_task"] = None
                    snapshot.handles.pop(node_id, None)
            elif record["event"] == "quarantine_continuation_recovered":
                if provenance["role"] != "system":
                    raise ValueError(
                        "quarantine continuation recovery lacks system provenance"
                    )
                if (
                    data.get("role") != "developer"
                    or data.get("task_id") != current.get("developer_identity")
                    or data.get("generation") != current.get("developer_generation")
                ):
                    raise ValueError("quarantine continuation recovery is stale")
                if not self._recover_quarantined_delivered_developer(
                    snapshot, node_id, record_event=False
                ):
                    raise ValueError(
                        "quarantine continuation recovery cannot be verified"
                    )
            elif record["event"] == "review_finding":
                # The following transition event/start intent is authoritative.
                self._registered_active_binding(snapshot, node_id, provenance)
            elif record["event"] in {"binding_self_healed", "binding_degraded", "action_isolated"}:
                if provenance["role"] != "system":
                    raise ValueError("self-healing event lacks system provenance")
                if record["event"] == "binding_self_healed":
                    binding = data.get("binding", {})
                    frozen = current.get("binding")
                    if not isinstance(binding, dict):
                        raise ValueError("self-healing binding evidence is invalid")
                    if isinstance(frozen, dict):
                        for field in ("task_id", "worktree", "branch", "allowlist", "cursor", "token", "continuation_token", "continuation_digest"):
                            if field in binding and field in frozen and binding[field] != frozen[field]:
                                raise ValueError("self-healing replay binding drift")
                    # Restore the frozen values after validating the event;
                    # observations are never used as replacement facts.
                    if isinstance(frozen, dict):
                        for field in ("task_id", "worktree", "branch", "allowlist", "cursor", "token", "continuation_token", "continuation_digest"):
                            if field in frozen:
                                current[field] = frozen[field]
                    else:
                        current["binding"] = dict(binding)
                        for field in ("task_id", "worktree", "branch", "allowlist", "cursor", "continuation_digest"):
                            if field in binding:
                                current[field] = binding[field]
                    current["healing"] = {"status": "repaired", "observation_kind": data.get("observation_kind", "unknown")}
                else:
                    current["status"] = "blocked_unknown"
                    current["reason"] = data.get("reason", data.get("observation_kind", record["event"]))
                    current["isolated"] = True
                    current["retryable_action"] = None
                    current["quarantine"] = {"run_id": snapshot.run_id, "handle_id": snapshot.handles.get(node_id), "reason": current["reason"]}
            elif record["event"] == "consistency_corrected":
                correction = {
                    key: data[key]
                    for key in CONSISTENCY_CORRECTION_KEYS
                }
                current.setdefault("contract_overrides", {})[
                    correction["field"]
                ] = correction["value"]
                if correction not in current.setdefault("corrections", []):
                    current["corrections"].append(correction)
                current["active_role"] = None
                current["active_task"] = None
                snapshot.handles.pop(node_id, None)
                current["status"] = "blocked_unknown"
                current["retryable_action"] = {
                    "role": "developer",
                    "phase": "rework",
                    "continuation": True,
                }
            elif record["event"] == "blocked_design":
                current["status"] = "blocked_design"
                current["reason"] = data.get("reason", "design decision required")
                current["old_task_reconciled"] = bool(
                    data.get("old_task_reconciled")
                )
                current["active_role"] = None
                current["active_task"] = None
                snapshot.handles.pop(node_id, None)
                self._release_node_lease(snapshot, node_id)
            elif record["event"] == "pair_archived":
                current["pair_archived"] = True
            elif record["event"] == "acceptance_rejected":
                # Validate-first rejections (ISSUE-83) are pure audit: the
                # live pass mutated no lifecycle state -- the handle, binding
                # and node status were all left for the same task's corrected
                # re-report -- so replay must mutate nothing either.
                pass
            else:
                current["status"] = "blocked_unknown"
                current["reason"] = "unapplied event needs manual reconciliation"
            snapshot.event_sequence = record["sequence"]

    def _release_node_lease(self, snapshot: RunSnapshot, node_id: str) -> None:
        current = snapshot.nodes[node_id]
        release_writer_lease(
            self.paths,
            node_id,
            str(current.get("worktree", "")),
            snapshot.run_id,
        )

    def _worktree_path(self, current: Dict[str, Any]) -> Path:
        worktree_path = Path(str(current.get("worktree", ".")))
        if not worktree_path.is_absolute():
            worktree_path = self.paths.root / worktree_path
        return worktree_path

    @staticmethod
    def _refresh_run_status(snapshot: RunSnapshot) -> None:
        for current in snapshot.nodes.values():
            current["user_status"] = map_user_status(current)
        statuses = [node.get("status") for node in snapshot.nodes.values()]
        closeout = evaluate_v41_closeout(snapshot)
        if closeout.allowed:
            snapshot.status = "complete"
        elif "blocked_unknown" in statuses:
            snapshot.status = "blocked_unknown"
        elif "blocked_design" in statuses:
            snapshot.status = "blocked_design"
        elif "failed" in statuses or "stopped" in statuses:
            snapshot.status = "failed"
        else:
            snapshot.status = "running"
