"""V2 non-blocking diagnostics and execution boundary contracts."""
from dataclasses import dataclass, asdict
import json
import hashlib
import os
import tempfile
from ._wincompat import flock_exclusive, flock_release
from pathlib import Path
from typing import Any, Dict, List

from .models import WorkerProfile


@dataclass(frozen=True)
class Proposal:
    proposed: bool
    content: str = ""
    path: str = ""
    reason: str = ""


@dataclass(frozen=True)
class SkillDiagnostic:
    name: str
    status: str
    reason: str
    proposal: Proposal = Proposal(False)


@dataclass(frozen=True)
class ContractCheck:
    ok: bool
    missing: List[str]
    extra: List[str] = None

    def __post_init__(self):
        if self.extra is None:
            object.__setattr__(self, "extra", [])


@dataclass(frozen=True)
class PlanningGate:
    status: str
    plan_id: str
    missing: List[str]


@dataclass(frozen=True)
class SessionGate:
    status: str
    session_id: str
    request: str
    origin: str
    reason: str = ""


def _valid_plan_confirmation_binding(
    paths,
    plan_root: Path,
    plan: Dict[str, Any],
    card: Dict[str, Any],
    confirmation: Dict[str, Any],
) -> bool:
    """Validate current-run publication provenance fail-closed.

    Legacy plan confirmations are compatible only when neither current-run
    metadata field is present. Once either field appears, the publication must
    bind plan, run, event sequence, authorization and durable event lineage.
    """
    has_run_id = "run_id" in confirmation
    has_event_sequence = "event_sequence" in confirmation
    has_current_metadata = has_run_id or has_event_sequence
    if has_current_metadata and "plan_id" not in confirmation:
        return False
    if "plan_id" in confirmation and confirmation.get("plan_id") != plan.get("plan_id"):
        return False
    if not has_run_id and not has_event_sequence:
        return True
    if not has_run_id or not has_event_sequence:
        return False
    run_id = confirmation.get("run_id")
    event_sequence = confirmation.get("event_sequence")
    if (
        not isinstance(run_id, str)
        or not run_id
        or isinstance(event_sequence, bool)
        or not isinstance(event_sequence, int)
        or event_sequence < 1
    ):
        return False
    try:
        from .state import load_events, load_snapshot

        current_run = json.loads(
            (plan_root / "current-run.json").read_text(encoding="utf-8")
        )
        if not isinstance(current_run, dict) or current_run.get("run_id") != run_id:
            return False
        snapshot = load_snapshot(paths, run_id)
        if (
            snapshot.run_id != run_id
            or snapshot.plan_id != plan.get("plan_id")
            or snapshot.plan_version != int(plan.get("version"))
            or event_sequence > snapshot.event_sequence
            or snapshot.authorization_digest != card.get("digest")
        ):
            return False
        events = load_events(paths, run_id)
        if event_sequence > len(events):
            return False
        publication_event = events[event_sequence - 1]
        publication_provenance = publication_event.get("provenance", {})
        if publication_provenance.get("authorization_digest") != snapshot.authorization_digest:
            # A reauthorization event is recorded under the previous epoch;
            # its data must explicitly name the snapshot's replacement digest
            # and link back to that previous provenance.
            if not (
                publication_event.get("event") == "authorization_reauthorized"
                and publication_event.get("data", {}).get("authorization_digest")
                == snapshot.authorization_digest
                and publication_provenance.get("authorization_digest")
                == publication_event.get("data", {}).get("previous_authorization_digest")
            ):
                return False
        lineage_digests = {events[0]["data"].get("authorization_digest")}
        current_reauthorization = False
        for event in events[:event_sequence]:
            if event["event"] != "authorization_reauthorized":
                continue
            data = event.get("data", {})
            lineage_digests.add(data.get("previous_authorization_digest"))
            lineage_digests.add(data.get("authorization_digest"))
            if data.get("authorization_digest") == card.get("digest"):
                current_reauthorization = True
        confirmation_digest = confirmation.get("authorization_digest")
        return confirmation_digest in lineage_digests and (
            current_reauthorization
            or confirmation_digest == snapshot.authorization_digest
        )
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return False


def diagnose_skill(name: str, report, config: dict) -> SkillDiagnostic:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("skill name is required")
    project = {item.get("name") for item in getattr(report, "skills", []) if item.get("valid")}
    global_skills = config.get("global_skills", []) if isinstance(config, dict) else []
    present_global = False
    if isinstance(config, dict):
        configured = config.get("skills", [])
        present_global = name in global_skills or name in configured
    if name in project:
        return SkillDiagnostic(name, "ready", "project reference present")
    if present_global:
        return SkillDiagnostic(name, "attention", "global Skill present; project reference missing", Proposal(True, "Add a project Skill reference after confirmation."))
    return SkillDiagnostic(name, "attention", "Skill is not configured", Proposal(True, "Configure this Skill after confirmation."))


def build_skill_reference_proposal(diagnostic: SkillDiagnostic) -> Proposal:
    if diagnostic.status != "attention":
        return Proposal(False)
    return Proposal(True, "# Skill proposal\n\n- name: %s\n- action: add project reference\n" % diagnostic.name)


def check_agents_contract(content: str, required_rules: List[str]) -> ContractCheck:
    if not isinstance(content, str) or not isinstance(required_rules, list):
        raise TypeError("content and required_rules are required")
    missing = [rule for rule in required_rules if rule not in content]
    return ContractCheck(not missing, missing)


def build_agentsmd_proposal(check: ContractCheck) -> Proposal:
    if check.ok:
        return Proposal(False)
    return Proposal(True, "# Vibe Guide contract proposal\n\n" + "\n".join("- %s" % item for item in check.missing) + "\n")


def assert_planning_gate(paths, plan_id: str) -> PlanningGate:
    root = paths.resolve_vibe_path(Path("plans") / plan_id)
    required = ["prd.md", "plan.json", "nodes.json", "authorization-card.json"]
    missing = [item for item in required if not (root / item).is_file()]
    if not missing:
        try:
            prd = (root / "prd.md").read_text(encoding="utf-8")
            plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))
            if "approved" not in prd.lower() or "review" not in prd.lower():
                missing.append("prd.reviewed")
            # A freshly published complex plan is executable after the caller
            # supplies the separate exact authorization token; publication
            # completeness must not require that token to be persisted yet.
            if plan.get("status") not in ("authorized", "confirmed_pending_authorization", "running", "complete"):
                missing.append("plan.published")
            nodes = json.loads((root / "nodes.json").read_text(encoding="utf-8"))
            if not isinstance(nodes, list) or not nodes:
                missing.append("nodes")
            if not any((root / "specs").glob("*.md")):
                missing.append("specs.reviewed")
            else:
                spec_names = {item.stem for item in (root / "specs").glob("*.md")}
                missing.extend("spec-missing:" + str(x.get("id")) for x in nodes if x.get("id") not in spec_names)
                for item in (root / "specs").glob("*.md"):
                    text = item.read_text(encoding="utf-8")
                    node_id = item.stem
                    if node_id not in {x.get("id") for x in nodes} or "node_id: " + node_id not in text or "published" not in text.lower() or "review" not in text.lower() or not text.strip(): missing.append("spec:" + item.name)
            if not any((root / "issues").glob("*.md")):
                missing.append("issues.reviewed")
            else:
                issue_names = {item.stem for item in (root / "issues").glob("*.md")}
                missing.extend("issue-missing:" + str(x.get("id")) for x in nodes if x.get("id") not in issue_names)
                for item in (root / "issues").glob("*.md"):
                    text = item.read_text(encoding="utf-8")
                    issue_id = item.stem
                    if issue_id not in {x.get("id") for x in nodes} or "issue_id: " + issue_id not in text or "published" not in text.lower() or "review" not in text.lower() or not text.strip(): missing.append("issue:" + item.name)
            if not (root / "dag-audit.json").is_file():
                missing.append("dag-audit")
            else:
                audit = json.loads((root / "dag-audit.json").read_text(encoding="utf-8"))
                if audit.get("status") != "reviewed" or audit.get("node_count") != len(nodes) or set(audit.get("node_ids", [])) != {item.get("id") for item in nodes}: missing.append("dag-audit.invalid")
            if not (root / "plan-confirmation.json").is_file():
                missing.append("plan-confirmation")
            else:
                confirmation = json.loads((root / "plan-confirmation.json").read_text(encoding="utf-8"))
                card = json.loads((root / "authorization-card.json").read_text(encoding="utf-8"))
                if confirmation.get("status") != "confirmed" or confirmation.get("plan_revision") != str(plan.get("version")) or confirmation.get("authorization_digest") != card.get("digest"):
                    missing.append("plan-confirmation.invalid")
                elif not _valid_plan_confirmation_binding(
                    paths, root, plan, card, confirmation
                ):
                    missing.append("plan-confirmation.invalid")
        except (OSError, ValueError, json.JSONDecodeError):
            missing.append("published_artifacts")
    return PlanningGate("execution_ready" if not missing else "planning_required", plan_id, missing)


def require_execution_ready(gate: PlanningGate) -> None:
    if gate.status != "execution_ready":
        raise PermissionError("planning_required: " + ", ".join(gate.missing))


def screen_session(paths, session_id: str, request: str, origin: str = "user_entry") -> SessionGate:
    if origin not in ("user_entry", "worker_dispatch"):
        raise ValueError("invalid session origin")
    if not session_id or not isinstance(request, str):
        raise ValueError("session id and request are required")
    status = "session_screened"
    gate = SessionGate(status, session_id, request, origin)
    try:
        directory = paths.vibe_dir
        directory.mkdir(parents=True, exist_ok=True)
        lock_handle = open(directory / ".session-gates.lock", "a+", encoding="utf-8")
        flock_exclusive(lock_handle.fileno())
        target = directory / "session-gates.json"
        data = {}
        if target.is_file():
            if target.is_symlink():
                raise PermissionError("session gate path is symlinked")
            data = json.loads(target.read_text(encoding="utf-8"))
        old = data.get(session_id)
        digest = hashlib.sha256(request.encode("utf-8")).hexdigest()
        if old:
            if old.get("request_digest") != digest or old.get("origin") != origin:
                flock_release(lock_handle.fileno()); lock_handle.close()
                raise PermissionError("session binding conflict")
            result = SessionGate(old.get("status", "session_screened"), session_id, request, origin)
            flock_release(lock_handle.fileno()); lock_handle.close()
            return result
        data[session_id] = {"status": status, "session_id": session_id, "origin": origin, "request_digest": digest, "evidence_ref": "session-gates.json#" + session_id}
        descriptor, temporary_name = tempfile.mkstemp(prefix=".session-gates.", dir=str(directory))
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, sort_keys=True); stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary_name, target)
        finally:
            if os.path.exists(temporary_name): os.unlink(temporary_name)
        flock_release(lock_handle.fileno()); lock_handle.close()
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise PermissionError("session gate persistence failed") from error
    return gate


def require_session_screened(gate: SessionGate) -> None:
    if gate.status != "session_screened":
        raise PermissionError("session screening required")


def validate_child_session_binding(parent_run_id: str, plan_revision: str, authorization_digest: str, node_id: str, role: str, worker_profile: WorkerProfile) -> None:
    if not all(isinstance(x, str) and x.strip() for x in (parent_run_id, plan_revision, authorization_digest, node_id)):
        raise ValueError("child session binding is incomplete")
    if role not in ("developer", "reviewer", "rework", "successor"):
        raise ValueError("invalid child role")
    if not isinstance(worker_profile, WorkerProfile) or not worker_profile.allowlist:
        raise ValueError("worker profile is required")
    if not worker_profile.writer or not worker_profile.worktree or not worker_profile.branch:
        raise ValueError("writer/worktree/branch binding is required")
    required = {"issue_complexity_ref", "complexity_band", "risk_tags", "availability_evidence"}
    if not required.issubset(worker_profile.selection_basis):
        raise ValueError("WorkerProfile selection_basis is incomplete")
    if any(Path(item).is_absolute() or ".." in Path(item).parts for item in worker_profile.allowlist):
        raise ValueError("allowlist must remain within project")
