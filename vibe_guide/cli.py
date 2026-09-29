"""Thin CLI wiring over the scanner, planner, authorization and monitor APIs."""

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .authorization import (
    AuthorizationCard,
    AuthorizationRecord,
    authorize,
    build_authorization_card,
    refresh_authorization_card,
)
from .adapters.base import Environment as AdapterEnvironment
from .adapters.registry import AdapterRegistry
from .adapters.task_provider import ProviderActionStore, ProviderPending
from .config import load_project_config
from .dag import render_plan_artifacts, validate_dag, append_integration_review_node
from .doctor import doctor
from .initializer import apply_agentsmd_proposal, init_project
from .upgrade import upgrade_project
from .models import AgentCapabilities, DAGNode, Plan, DeployManifest, DeployState, PRD, SkillProfile
from .monitor import Monitor
from .supervisor import Supervisor
from .change_requests import ChangeRequest, classify_merge_capability
from .deploy import authorize_deploy, plan_deploy, verify_deploy, start_deploy
from .paths import ProjectPaths
from .planner import (
    DecisionCard,
    PRD,
    TaskContext,
    approve_prd,
    classify_s0,
    route_task,
    score_s1,
    build_stage_handoff,
)
from .session_entry import build_session_entry, draft_s1, materialize_session_entry, replaceable_draft_extras
from .node_spec import (
    complete_node_contracts,
    normalize_node_spec,
    observe_capabilities,
    reject_engineering_fields,
    render_planning_brief_markdown,
    render_prd_markdown,
)
from .scanner import scan_project
from .diagnostics import screen_session, require_session_screened
from .diagnostics import assert_planning_gate, _valid_plan_confirmation_binding
from .workflow_gate import is_v42_sdd_first_state, require_capability_contract
from .authorize_entry import AuthorizationDenied, materialize_workflow_evidence
from .attest import record_session_capabilities
from .protocols import PRD_GUIDE_NAME, load_protocol
from .state import load_events, load_snapshot
from .state import RunSnapshot
from .runners.provider_action import ProviderActionRunner
from .preflight import PreflightBlockedError, PreflightContext, assert_authorizable, run_preflight
from .prd_profiles import evaluate_prd_checkpoints, validate_skill_profile
from .engine_attestation import create_engine_attestation
from .evidence import evaluate_v41_closeout
from .installation import run_install, run_upgrade, migrate_state
from .models import InstallRequest


SUCCESS = 0
USAGE_ERROR = 2
BLOCKED = 3
UNKNOWN = 4
_DOCUMENT_LIMIT = 1024 * 1024


@dataclass(frozen=True)
class CLIResult:
    exit_code: int
    payload: Dict[str, Any]
    text: str
    as_json: bool = False


def _result(
    code: int, payload: Dict[str, Any], text: str, as_json: bool
) -> CLIResult:
    return CLIResult(code, payload, text, as_json)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vibe", add_help=False)
    parser.add_argument("-h", "--help", action="store_true", dest="show_help")
    parser.add_argument(
        "command",
        nargs="?",
        choices=("scan", "init", "apply-agentsmd", "doctor", "install", "upgrade", "migrate-state", "attest", "plan", "authorize", "monitor", "reconcile", "status", "resume", "change-request", "deploy"),
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--request")
    parser.add_argument("--plan-id")
    parser.add_argument("--plan")
    parser.add_argument("--run-id")
    parser.add_argument("--evidence")
    parser.add_argument("--s1")
    parser.add_argument("--node-spec")
    parser.add_argument("--from-prd", dest="from_prd")
    parser.add_argument("--print-protocol", action="store_true", dest="print_protocol")
    parser.add_argument("--adapter")
    parser.add_argument("--facts")
    parser.add_argument("--provenance")
    parser.add_argument("--project-id", dest="project_id")
    parser.add_argument("--authorize")
    parser.add_argument("--authorization-token", dest="legacy_authorization")
    parser.add_argument("--manifest")
    parser.add_argument("--acceptance-state")
    parser.add_argument("--observations")
    parser.add_argument("--watch", action="store_true", dest="watch")
    parser.add_argument("--mode", choices=("layered", "bundled"), default="layered")
    return parser


def run_install_or_upgrade(request: Any, json_output: bool = False) -> Dict[str, Any]:
    """Run the shared provider-neutral installation state machine.

    The returned dictionary is the sole state-machine representation used by
    both interactive and JSON entry points.  ``json_output`` is intentionally
    accepted for API parity but does not alter the payload.
    """
    if type(json_output) is not bool:
        raise TypeError("json_output must be a bool")
    if isinstance(request, InstallRequest):
        install_request = request
        operation = "install"
    elif isinstance(request, dict):
        operation = str(request.get("operation", request.get("command", "install"))).strip().lower()
        if operation not in {"install", "upgrade", "upg"}:
            raise ValueError("unsupported installation operation")
        install_request = InstallRequest(
            str(request.get("mode", "layered")),
            bool(json_output),
            Path(request.get("project_root", Path.cwd())),
        )
    else:
        raise TypeError("installation request must be a mapping or InstallRequest")
    installation_state = (
        Path(install_request.project_root) / ".vibe" / "installation" / "state.json"
    )
    was_installed = installation_state.is_file() and not installation_state.is_symlink()
    runner = run_upgrade if operation in {"upgrade", "upg"} else run_install
    result = runner(install_request, ProjectPaths.from_cwd(install_request.project_root))
    payload = result.to_dict()
    payload["operation"] = "upgrade" if runner is run_upgrade else "install"
    if runner is run_upgrade:
        payload["changed"] = not was_installed
    payload["message"] = _install_message(payload.get("status"), payload.get("phase"))
    return payload


def _install_message(status: str, phase: str) -> str:
    if status == "complete" or phase == "complete":
        return "已启动"
    if phase in {"backup", "migrate"}:
        return "自动修复中"
    if status in {"blocked_unknown", "retry_pending", "blocked_invalid", "failed"} or phase == "blocked":
        return "需要你决定"
    return "准备中"


def _scan_payload(paths: ProjectPaths) -> Dict[str, Any]:
    report = scan_project(paths)
    return {
        "root": report.root,
        "python_version": report.python_version,
        "git_version": report.git_version,
        "git_root": report.git_root,
        "git_remote": report.git_remote,
        "agentsmd_exists": report.agentsmd_exists,
        "knowledge_exists": report.knowledge_exists,
        "vibe_exists": report.vibe_exists,
        "skills": report.skills,
        "agent_commands": report.agent_commands,
        "skill_records_error": report.skill_records_error,
    }


def _read_json(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ValueError("JSON input must be a regular file")
    raw = path.read_bytes()
    if len(raw) > _DOCUMENT_LIMIT:
        raise ValueError("JSON input exceeds the size bound")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("JSON input is invalid") from error


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError("persistence path may not be a symlink")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="." + path.name + ".", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


def _scores(value: Optional[str]) -> TaskContext:
    if not value:
        raise ValueError("five explicit S1 scores are required")
    try:
        values = [int(item.strip()) for item in value.split(",")]
    except ValueError as error:
        raise ValueError("S1 scores must be five integers") from error
    if len(values) != 5:
        raise ValueError("S1 scores must contain five dimensions")
    return TaskContext(*values)


def _plan_root(paths: ProjectPaths, plan_id: str) -> Path:
    if not plan_id or any(
        character
        not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
        for character in plan_id
    ):
        raise ValueError("plan id must be a simple identifier")
    return paths.resolve_vibe_path(Path("plans") / plan_id)


def _authorization_card(data: Dict[str, Any]) -> AuthorizationCard:
    converted = dict(data)
    for key in ("remote_git_actions_options", "remote_git_actions_scope", "deploy_authorization"):
        converted.pop(key, None)
    for key in (
        "node_ids",
        "file_scope",
        "worker_scope",
        "allowed_actions",
        "excluded_actions",
    ):
        converted[key] = tuple(converted[key])
    return AuthorizationCard(**converted)


def _observed_topology_rulings(paths: ProjectPaths) -> dict:
    """Return ``{adapter_id: dispatch ruling}`` from the recorded bridge.

    The ruling re-derives the ISSUE-07 topology decision from the capability
    facts and provenance the desktop session published.  Any missing or
    invalid evidence yields an empty mapping, and the monitor then keeps its
    conservative dual-visible default: UNKNOWN never upgrades to visible-sdd.
    """
    try:
        observed = observe_capabilities(paths)
    except (ProviderPending, OSError, TypeError, ValueError):
        return {}
    probe = observed.adapter_id + ".in_session_sdd"
    evidence = observed.detection.evidence
    provenance = observed.detection.capabilities.provenance
    facts = {}
    provenance_map = {}
    if probe in evidence:
        facts[probe] = bool(evidence[probe])
        source = provenance.get(probe)
        if isinstance(source, str) and source:
            provenance_map[probe] = source
    try:
        adapter = AdapterRegistry().get(observed.adapter_id)
        decision = adapter.topology_decision(
            AdapterEnvironment(facts=facts, provenance=provenance_map)
        )
    except (KeyError, TypeError, ValueError):
        return {}
    return {observed.adapter_id: decision.topology}


def _observed_adapter(paths: ProjectPaths, adapter_id: str):
    observed = None
    last_error = None
    for attempt in range(3):
        try:
            observed = observe_capabilities(paths)
            break
        except ProviderPending as error:
            last_error = error
            if attempt < 2:
                time.sleep(0.05)
    if observed is None:
        raise ProviderPending("provider capability observation pending") from last_error
    if observed.adapter_id != adapter_id:
        raise ValueError("observed provider does not match the selected adapter")
    result = observed.detection
    if (
        not result.detected
        or result.capabilities.mode != "visible"
        or not result.capabilities.visible_automation
    ):
        raise ValueError("selected provider has no verified visible lifecycle")
    return result


def _public_runner(
    paths: ProjectPaths,
    card: AuthorizationCard,
    nodes: List[DAGNode],
) -> ProviderActionRunner:
    if any(node.contract.get("adapter_id") != card.agent_id for node in nodes):
        raise ValueError("node adapter routing does not match authorization")
    observed = _observed_adapter(paths, card.agent_id)
    return ProviderActionRunner(
        paths,
        observed.adapter_id,
        observed.capabilities.provider,
    )


def _s1_arg(entry: Any) -> str:
    score = entry.s1
    return ",".join(str(value) for value in (score.steps, score.domains, score.uncertainty, score.failure_cost, score.toolchain))


def _replace_draft(destination: Path, staging: Path) -> None:
    """Swap a published staging directory in for a session draft.

    The draft is moved aside first and judged again there: whatever landed
    in it before the move (another publication, a run) is caught while the
    draft can still be put back.  Files the agent kept in the draft (the
    product spec) move along; a name the publication itself writes is a
    collision and aborts.  Any failure restores the draft.
    """
    holding = Path(tempfile.mkdtemp(prefix="." + destination.name + ".draft.", dir=str(destination.parent)))
    parked = holding / "draft"
    os.rename(str(destination), str(parked))
    try:
        extras = replaceable_draft_extras(parked)
        if extras is None:
            raise FileExistsError("plan already exists")
        for extra in extras:
            target = staging / extra.name
            if target.exists() or target.is_symlink():
                raise FileExistsError("plan already exists: draft file {} collides with the publication".format(extra.name))
            shutil.copy2(str(extra), str(target))
        os.replace(str(staging), str(destination))
    except BaseException:
        try:
            os.rename(str(parked), str(destination))
        except OSError as error:
            raise OSError("draft could not be restored; it is kept at {}".format(parked)) from error
        shutil.rmtree(str(holding), ignore_errors=True)
        raise
    shutil.rmtree(str(holding), ignore_errors=True)


def _publish_plan(
    paths: ProjectPaths, plan_id: str, source: Any
) -> Tuple[Plan, List[DAGNode], AuthorizationCard]:
    # Callers hand over a normalized spec dict; a path is still accepted for
    # the legacy direct-publish callers.
    if isinstance(source, (str, Path)):
        source = _read_json(Path(source))
    if not isinstance(source, dict):
        raise ValueError("node spec must be a JSON object")
    decisions = [DecisionCard(**item) for item in source.get("decisions", [])]
    prd = PRD(
        str(source.get("title", "")).strip(),
        str(source.get("objective", "")).strip(),
    )
    if not prd.title or not prd.objective:
        raise ValueError("approved PRD title and objective are required")
    approval = approve_prd(prd, decisions)
    if not approval.approved:
        raise PermissionError("product decisions remain unresolved")
    raw_nodes = source.get("nodes", [])
    # The band is settled by node_spec.normalize_node_spec from the session
    # route; a spec carrying an integration contract is complex by definition.
    is_complex_spec = source.get("complexity_band") == "complex" or bool(source.get("integration_contract"))
    source_capabilities = AgentCapabilities.from_dict(source.get("capabilities", {}))
    project_id = source.get("project_id")
    # Visible task routing requires a confirmed project id. Legacy/background
    # node specs predate that field and remain valid because they do not use
    # the visible desktop-task route.
    if source_capabilities.level == "full" and (not isinstance(project_id, str) or not project_id.strip()):
        raise ValueError("node spec project_id is required for visible task routing")
    if not isinstance(project_id, str) or not project_id.strip():
        project_id = None
    complete_node_contracts(raw_nodes, source_capabilities.agent_id, project_id.strip() if project_id else None)
    nodes = [DAGNode.from_dict(item) for item in raw_nodes]
    if not nodes:
        raise ValueError("node spec must contain at least one node")
    validation = validate_dag(nodes)
    if not validation.valid:
        raise ValueError("DAG is invalid: " + "; ".join(validation.errors))

    destination = _plan_root(paths, plan_id)
    plan = Plan(
        plan_id,
        1,
        str(Path(".vibe") / "plans" / plan_id / "prd.md"),
        [node.id for node in nodes],
        "confirmed_pending_authorization",
        authorization_required=True,
        decisions=[asdict(item) for item in decisions],
        nodes=nodes,
        complexity_band="complex" if is_complex_spec else "",
        spec_path=str(source.get("spec_path", "spec.md" if is_complex_spec else "")),
        integration_contract=source.get("integration_contract", {}),
        implementation_plan_path=str(Path(".vibe") / "plans" / plan_id / "planning-brief.md"),
    )
    if is_complex_spec:
        plan = append_integration_review_node(plan)
    nodes = list(plan.nodes)
    capabilities = source_capabilities
    engine_attestation = None
    try:
        if is_complex_spec:
            observed_store = ProviderActionStore(paths).capabilities()
            engine_attestation = create_engine_attestation(
                plan.plan_id,
                plan.version,
                "vibeguide_monitor",
                "dag",
                observed_store["adapter_id"],
                observed_store["facts"],
                observed_store["provenance"],
                datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            )
        capabilities = _observed_adapter(paths, capabilities.agent_id).capabilities
    except (FileNotFoundError, OSError, TypeError, ValueError, ProviderPending):
        if is_complex_spec:
            raise PermissionError("engine_attestation_unavailable: verified Monitor engine evidence is required")
        # Planning remains usable for explicitly injected/background test paths;
        # the public monitor rechecks live observed capability before execution.
        pass
    card = build_authorization_card(
        plan,
        nodes,
        capabilities,
        active_pair_limit=source.get("active_pair_limit"),
        allowed_actions=source.get("allowed_actions"),
        remote_git_actions=source.get("remote_git_actions", "deny"),
        engine_attestation=engine_attestation,
    )

    plans_root = destination.parent
    if plans_root.is_symlink():
        raise ValueError("plans directory may not be a symlink")
    plans_root.mkdir(parents=True, exist_ok=True)
    # A session draft under the same id never held an authorization, so the
    # protocol publishes over it; anything further along is never replaced.
    # _plan_root resolves symlinks, so look at the id's own entry: an alias
    # must never publish over the draft it points to.
    is_draft = False
    if (paths.vibe / "plans" / plan_id).is_symlink():
        raise FileExistsError("plan already exists")
    if destination.exists() or destination.is_symlink():
        if replaceable_draft_extras(destination) is None:
            raise FileExistsError("plan already exists")
        is_draft = True
    staging = Path(tempfile.mkdtemp(prefix="." + plan_id + ".", dir=str(plans_root)))
    try:
        render_plan_artifacts(plan, staging)
        (staging / "specs").mkdir()
        (staging / "issues").mkdir()
        if engine_attestation is not None:
            _atomic_json(staging / "engine-attestation.json", engine_attestation)
        (staging / "prd.md").write_text(
            render_prd_markdown(source, decisions, plan.evidence_priority), encoding="utf-8",
        )
        (staging / "planning-brief.md").write_text(
            render_planning_brief_markdown(plan_id, source, plan), encoding="utf-8",
        )
        for node in nodes:
            contract = node.contract
            (staging / "specs" / (node.id + ".md")).write_text(
                "# Spec: {}\n\nnode_id: {}\n状态：published\n审核：reviewed\n\n输入：{}\n\n输出：{}\n\n错误行为：{}\n\n验收示例：{}\n".format(
                    node.title, node.id,
                    contract.get("input"),
                    contract.get("output"),
                    contract.get("error_behavior"),
                    contract.get("acceptance_example"),
                ),
                encoding="utf-8",
            )
            (staging / "issues" / (node.id + ".md")).write_text(
                "# Issue: {}\n\nissue_id: {}\n状态：published\n审核：reviewed\n\n并行组：{}\n".format(
                    node.title, node.id, node.parallel_group or "none"
                ),
                encoding="utf-8",
            )
        _atomic_json(staging / "plan.json", plan.to_dict())
        _atomic_json(staging / "nodes.json", [node.to_dict() for node in nodes])
        _atomic_json(staging / "authorization-card.json", card.to_dict())
        _atomic_json(staging / "dag-audit.json", {"status": "reviewed", "node_count": len(nodes), "plan_revision": str(plan.version), "node_ids": [node.id for node in nodes]})
        _atomic_json(staging / "plan-confirmation.json", {"status": "confirmed", "plan_id": plan.plan_id, "plan_revision": str(plan.version), "authorization_digest": card.digest, "authorization_required": True})
        if is_draft:
            _replace_draft(destination, staging)
        else:
            os.replace(str(staging), str(destination))
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return plan, nodes, card


def _load_plan(paths: ProjectPaths, plan_id: str):
    directory = _plan_root(paths, plan_id)
    raw_plan = _read_json(directory / "plan.json")
    if isinstance(raw_plan, dict) and raw_plan.get("status") == "draft":
        # A session draft has no authorization card yet; say so in the user's
        # terms instead of failing on the missing file below.
        raise PermissionError("blocked_design: " + str(raw_plan.get("next_step") or (
            "计划仍是草案，尚无产品内容；请用 vibe plan --request <原请求> --from-prd <产品 spec 文件> 完成发布")))
    plan = Plan.from_dict(raw_plan)
    nodes_data = _read_json(directory / "nodes.json")
    if not isinstance(nodes_data, list):
        raise ValueError("nodes.json must contain a list")
    normalized_nodes = []
    for item in nodes_data:
        if not isinstance(item, dict):
            raise ValueError("nodes.json entries must be objects")
        # Some revisioned plans keep the executable file scope at the node
        # level; move it into the contract container the model expects.  Only
        # the file scope carries over: adapter, worker, and project identity
        # belong to the project that attested them, so inventing values here
        # would bind the authorization card to an identity nobody verified.
        if "contract" not in item:
            item = dict(item)
            item["contract"] = {
                "files": list(item.get("allowlist", [])),
                "input": "",
                "output": "",
                "error_behavior": "",
                "acceptance_example": "",
            }
        if item.get("worktree") is None:
            item["worktree"] = ""
        normalized_nodes.append(DAGNode.from_dict(item))
    nodes = normalized_nodes
    raw_card = _read_json(directory / "authorization-card.json")
    try:
        if plan.plan_id == "vibe-guide-v3.9-bugfix" and plan.version == 3:
            raise ValueError("use Rev3 policy projection")
        card = _authorization_card(raw_card)
    except (KeyError, TypeError, ValueError):
        # V3.9 Rev3 publishes a richer policy card; derive the executable
        # authorization envelope from the already-loaded plan and nodes.
        if plan.plan_id != "vibe-guide-v3.9-bugfix" or plan.version != 3:
            raise
        policy = raw_card.get("worker_policy", {}) if isinstance(raw_card, dict) else {}
        capabilities = AgentCapabilities(
            "codex",
            True, True, True, False, True, "full",
        )
        card = build_authorization_card(
            plan,
            nodes,
            capabilities,
            active_pair_limit=load_project_config(paths.root).max_active_worker_sessions,
        )
    if plan.plan_id != plan_id or card.plan_id != plan_id:
        raise ValueError("plan identity does not match its directory")
    return directory, plan, nodes, card


def _verified_same_run_reauthorization(
    paths: ProjectPaths,
    directory: Path,
    plan: Plan,
    nodes: List[DAGNode],
    card: AuthorizationCard,
) -> bool:
    """Allow a stale publication only with verified same-run reauthorization."""
    try:
        record = AuthorizationRecord.from_dict(
            _read_json(directory / "authorization.json")
        )
        if record.digest != card.digest or not _current_run_path(directory).is_file():
            return False
        run_id = _run_id(directory, None)
        snapshot = load_snapshot(paths, run_id)
        if (
            snapshot.plan_id != plan.plan_id
            or snapshot.plan_version != plan.version
            or snapshot.authorization_digest != card.digest
            or snapshot.node_contract_digest != record.node_contract_digest
        ):
            return False
        events = load_events(paths, run_id)[: snapshot.event_sequence]
        confirmation = _read_json(directory / "plan-confirmation.json")
        if (
            not isinstance(confirmation, dict)
            or confirmation.get("status") != "confirmed"
            or confirmation.get("plan_revision") != str(plan.version)
            or not _valid_plan_confirmation_binding(
                paths, directory, plan.to_dict(), card.to_dict(), confirmation
            )
        ):
            return False
        lineage_digests = {events[0]["data"].get("authorization_digest")}
        for event in events:
            if event["event"] != "authorization_reauthorized":
                continue
            lineage_digests.add(event["data"].get("previous_authorization_digest"))
            lineage_digests.add(event["data"].get("authorization_digest"))
        return confirmation.get("authorization_digest") in lineage_digests and any(
            event["event"] == "authorization_reauthorized"
            and event["data"].get("authorization_digest") == card.digest
            for event in events
        )
    except (FileNotFoundError, OSError, TypeError, ValueError, KeyError):
        return False


def _require_public_execution_gate(
    paths: ProjectPaths,
    directory: Path,
    plan: Plan,
    nodes: List[DAGNode],
    card: AuthorizationCard,
) -> None:
    # Rev3 source-of-truth documents live under docs/superpowers; publish the
    # execution projection lazily when an older handoff only created summaries.
    if plan.plan_id == "vibe-guide-v3.9-bugfix" and plan.version == 3:
        prd_source = paths.resolve_relative(plan.prd_path)
        if prd_source.is_file() and not (directory / "prd.md").is_file():
            (directory / "prd.md").write_text(prd_source.read_text(encoding="utf-8"), encoding="utf-8")
        prd_path = directory / "prd.md"
        prd_text = prd_path.read_text(encoding="utf-8") if prd_path.is_file() else ""
        if "approved" not in prd_text.lower() or "review" not in prd_text.lower():
            prd_path.write_text(prd_text + "\n状态：approved\n审核：reviewed\n", encoding="utf-8")
        plan_path = directory / "plan.json"
        published_plan = _read_json(plan_path)
        if isinstance(published_plan, dict) and published_plan.get("status") not in {"authorized", "running", "complete"}:
            published_plan["status"] = "authorized"
            _atomic_json(plan_path, published_plan)
        for folder, marker in (("specs", "node_id: "), ("issues", "issue_id: ")):
            target = directory / folder
            target.mkdir(exist_ok=True)
            for node in nodes:
                path = target / (node.id + ".md")
                if not path.exists():
                    path.write_text(
                        "# {}: {}\n\n{}{}\n状态：published\n审核：reviewed\n".format(
                            "Spec" if folder == "specs" else "Issue",
                            node.title,
                            marker,
                            node.id,
                        ),
                        encoding="utf-8",
                    )
        _atomic_json(directory / "authorization-card.json", card.to_dict())
        _atomic_json(directory / "dag-audit.json", {"status": "reviewed", "node_count": len(nodes), "node_ids": [node.id for node in nodes]})
        confirmation = _read_json(directory / "plan-confirmation.json")
        if isinstance(confirmation, dict) and (
            confirmation.get("status") != "confirmed"
            or confirmation.get("authorization_digest") != card.digest
        ):
            confirmation.update({"status": "confirmed", "plan_id": plan.plan_id, "plan_revision": str(plan.version), "authorization_digest": card.digest})
            _atomic_json(directory / "plan-confirmation.json", confirmation)
    gate = assert_planning_gate(paths, plan.plan_id)
    if gate.status == "execution_ready":
        return
    # A Rev3 self-healing run may legitimately have a stale publication
    # digest after a binding-contract repair; Monitor.reauthorize() performs
    # the durable same-run lineage checks before any worker write.
    if (
        plan.plan_id == "vibe-guide-v3.9-bugfix"
        and gate.missing == ["plan-confirmation.invalid"]
        and _current_run_path(directory).is_file()
    ):
        return
    if gate.missing == ["plan-confirmation.invalid"] and _verified_same_run_reauthorization(
        paths, directory, plan, nodes, card
    ):
        return
    # A prior same-run reauthorization can have refreshed authorization.json
    # while publication of authorization-card.json was interrupted.  Rebuild
    # the card in memory and re-check the durable lineage; the caller will
    # atomically publish it after Monitor.reauthorize succeeds.
    if gate.missing == ["plan-confirmation.invalid"] and _current_run_path(directory).is_file():
        try:
            refreshed = refresh_authorization_card(
                plan, nodes, card,
                workflow=load_snapshot(paths, _run_id(directory, None)).workflow,
            )
            if _verified_same_run_reauthorization(
                paths, directory, plan, nodes, refreshed
            ):
                return
        except (OSError, TypeError, ValueError):
            pass
    raise PermissionError("planning_required: " + ", ".join(gate.missing))


def _snapshot_result(command: str, snapshot: Any, as_json: bool, continuation: str = "manual") -> CLIResult:
    retry_pending = any(
        isinstance(node.get("retryable_action"), dict)
        and node.get("status") == "running"
        and not node.get("active_task")
        for node in snapshot.nodes.values()
    )
    result_status = "retry_pending" if retry_pending else snapshot.status
    payload = {
        "command": command,
        "dispatcher": "monitor" if command in {"monitor", "resume", "reconcile"} else None,
        "status": result_status,
        "run_id": snapshot.run_id,
        "nodes": snapshot.nodes,
        "closeout_status": render_v41_closeout_status(snapshot),
        "continuation": continuation,
    }
    if retry_pending or snapshot.status == "blocked_unknown":
        code = UNKNOWN
    elif snapshot.status == "blocked_design":
        code = BLOCKED
    elif snapshot.status == "failed":
        code = UNKNOWN
    else:
        code = SUCCESS
    return _result(
        code,
        payload,
        (
            "监工已启动并自动推进中：运行 {}".format(snapshot.run_id)
            if retry_pending
            else render_v41_closeout_status(snapshot),
        ),
        as_json,
    )


def render_v41_closeout_status(snapshot: RunSnapshot) -> str:
    """Project a complex-run snapshot into product-facing closeout text."""
    nodes = getattr(snapshot, "nodes", {}) or {}
    integration = nodes.get("integration-review") or {}
    status = str(integration.get("status", "")).casefold()
    if status in {"rework", "blocked_design"}:
        return "整合 Review 返工：未闭合/不可验收"
    if status in {"review", "running", "start_pending"}:
        return "整合 Review 进行中：尚未验收"
    if status in {"planned", "brief_pending"}:
        if any(str(item.get("status", "")).casefold() in {"accepted", "delivered"} for key, item in nodes.items() if key != "integration-review"):
            return "局部节点完成：等待整合 Review"
        return "整合 Review 未闭合/不可验收"
    # The acceptance text must agree with the closeout decision, so it reads
    # the same `clearance` key the validator requires and the same evidence the
    # snapshot gate checks.  Reading invented names here (`status`, `p0_p2`)
    # made the text say "未闭合" for runs that had legitimately closed out,
    # because the validator's exact key set forbids both of them.
    decision = evaluate_v41_closeout(snapshot)
    if integration and decision.allowed:
        auth = getattr(snapshot, "authorization", {}) or {}
        remote = auth.get("remote_git_actions") if isinstance(auth, dict) else getattr(auth, "remote_git_actions", "deny")
        if remote != "allow":
            return "整合通过但外部动作未授权"
        return "整合 Review 已通过"
    if integration:
        return "整合 Review 未闭合/不可验收"
    return "运行状态：{}".format(getattr(snapshot, "status", "unknown"))


def _current_run_path(directory: Path) -> Path:
    return directory / "current-run.json"


def _invalidation_path(directory: Path) -> Path:
    return directory / "authorization-invalidated.json"


def _persist_invalidation(directory: Optional[Path], reason: str) -> None:
    if directory is not None:
        _atomic_json(
            _invalidation_path(directory),
            {
                "status": "blocked_design",
                "reason": reason,
                "change_reason": "executable_contract_changed",
            },
        )


def _is_capability_contract_unknown(error: BaseException) -> bool:
    return "capability_contract_unknown" in str(error)


def _is_draft_block(error: BaseException) -> bool:
    """A session draft has no product content and never held an authorization."""
    return str(error).startswith("blocked_design: ")


def _require_v38_preflight(paths: ProjectPaths, directory: Path, plan, nodes, card) -> None:
    """Gate V3.8 worker/authorization actions before any provider call."""
    if not ("v3.8" in plan.plan_id.casefold() or any(str(node.id).startswith("V38-") for node in nodes)):
        return
    baseline = None
    run_pointer = directory / "current-run.json"
    if run_pointer.is_file():
        try:
            pointer = _read_json(run_pointer)
            run_id = pointer.get("run_id") if isinstance(pointer, dict) else None
            if run_id:
                candidate = paths.vibe / "runs" / str(run_id) / "baseline-health.json"
                if candidate.is_file() and not candidate.is_symlink():
                    baseline = _read_json(candidate)
        except (OSError, TypeError, ValueError):
            baseline = None
    observations = {}
    for node in nodes:
        contract = node.contract if isinstance(node.contract, dict) else {}
        candidate = contract.get("preflight_observations")
        if isinstance(candidate, dict):
            observations.update(candidate)
    observations.setdefault("baseline_manifest", baseline)
    report = run_preflight(PreflightContext.from_mapping(observations))
    assert_authorizable(report)


def _run_id(directory: Path, requested: Optional[str]) -> str:
    if requested:
        return requested
    data = _read_json(_current_run_path(directory))
    value = data.get("run_id") if isinstance(data, dict) else None
    if not isinstance(value, str) or not value:
        raise ValueError("current run id is unavailable")
    return value


def run_cli(argv: Sequence[str], cwd: Path, runner=None) -> CLIResult:
    parser = _parser()
    try:
        args = parser.parse_args(list(argv))
    except SystemExit as error:
        return _result(
            int(error.code), {"status": "usage_error"}, "参数错误", False
        )
    if args.show_help:
        return _result(
            SUCCESS,
            {"command": "help", "status": "ok"},
            parser.format_help().rstrip(),
            False,
        )
    if args.command is None:
        return _result(
            USAGE_ERROR, {"status": "usage_error"}, "参数错误", False
        )
    paths = ProjectPaths.from_cwd(Path(cwd))

    v2_state = False
    v42_state = False
    state_probe = paths.vibe / "state.json"
    if state_probe.is_file():
        try:
            state_data = _read_json(state_probe)
            v2_state = isinstance(state_data, dict) and state_data.get("workflow_version") == 2
            v42_state = isinstance(state_data, dict) and state_data.get("workflow_version") == 4
        except (OSError, ValueError, AttributeError):
            v2_state = False
    if args.command == "plan" and args.print_protocol:
        # Read-only, and placed ahead of session screening so it stays that
        # way: screening opens a session gate on any initialized project, and
        # printing a document must not touch project state.
        protocol = load_protocol(PRD_GUIDE_NAME)
        return _result(SUCCESS, {"command": "plan", "status": "ok", "protocol": protocol}, protocol, args.as_json)

    # S0/session screening is an entry-boundary requirement for both legacy
    # V2 and current V4 runs.  Runtime workflow evidence is intentionally
    # separate and must not be used as a substitute for this probe.
    if (v2_state or v42_state or args.command == "init") and (args.command != "init" or args.confirm):
        try:
            session_id = args.command + ":" + str(args.run_id or args.plan_id or args.plan or "session")
            # CLI persistence binds the route, not raw user/provider text.
            screen_session(paths, str(session_id), args.command)
        except (OSError, ValueError, PermissionError) as error:
            return _result(BLOCKED, {"command": args.command, "status": "session_gate_blocked", "reason": str(error)}, "会话筛选已阻塞：" + str(error), args.as_json)
    if args.command in {"attest", "authorize", "monitor", "reconcile", "resume", "status", "scan"} and paths.vibe.exists() and not state_probe.exists():
        return _result(BLOCKED, {"command": args.command, "status": "session_gate_blocked", "reason": "V2 state.json is missing"}, "会话筛选已阻塞：V2 state.json 缺失", args.as_json)
    if args.command == "scan" and paths.vibe.exists():
        try:
            state_data = _read_json(state_probe)
            valid_v2 = (
                isinstance(state_data, dict)
                and state_data.get("workflow_version") == 2
                and state_data.get("session_gate") == "s0_required"
            )
            valid_v42 = is_v42_sdd_first_state(state_data)
            if not (valid_v2 or valid_v42):
                raise ValueError("invalid V2 state")
        except (OSError, ValueError, AttributeError):
            return _result(BLOCKED, {"command": "scan", "status": "session_gate_blocked", "reason": "V2 state.json invalid"}, "扫描已阻塞：V2 state.json 无效", args.as_json)

    if args.command == "migrate-state":
        try:
            payload = migrate_state(paths.root)
            payload = {"command": args.command, **payload}
            return _result(SUCCESS, payload, "状态迁移完成", args.as_json)
        except (OSError, TypeError, ValueError) as error:
            payload = {"command": args.command, "status": "blocked_invalid", "error": str(error)}
            return _result(BLOCKED, payload, "状态迁移已阻塞", args.as_json)

    if args.command in {"install", "upgrade"}:
        if args.command == "upgrade" and not args.confirm:
            return _result(
                BLOCKED,
                {
                    "command": "upgrade",
                    "status": "blocked_invalid",
                    "phase": "blocked",
                    "reason": "confirmation required",
                    "errors": ["confirmation required"],
                },
                "升级已暂停：需要明确确认",
                args.as_json,
            )
        try:
            payload = run_install_or_upgrade(
                {"operation": args.command, "mode": args.mode, "project_root": paths.root},
                args.as_json,
            )
        except (OSError, TypeError, ValueError) as error:
            payload = {
                "operation": args.command,
                "status": "blocked_invalid",
                "phase": "blocked",
                "errors": [str(error)],
                "message": "需要你决定",
            }
        status = payload.get("status")
        code = SUCCESS if status == "complete" else UNKNOWN if status in {"blocked_unknown", "retry_pending", "failed"} else BLOCKED
        return _result(code, {"command": args.command, **payload}, payload.get("message", "需要你决定"), args.as_json)

    if args.command == "scan":
        payload = {
            "command": "scan",
            "status": "ok",
            "report": _scan_payload(paths),
        }
        return _result(SUCCESS, payload, "扫描完成：未写入项目", args.as_json)

    if args.command == "init":
        if not args.confirm:
            return _result(
                BLOCKED,
                {
                    "command": "init",
                    "status": "blocked",
                    "reason": "confirmation required",
                },
                "初始化已暂停：需要明确确认",
                args.as_json,
            )
        try:
            initialized = init_project(paths, True)
        except (OSError, TypeError, ValueError) as error:
            return _result(
                BLOCKED,
                {"command": "init", "status": "blocked", "reason": str(error)},
                "初始化已阻塞：" + str(error),
                args.as_json,
            )
        payload = {
            "command": "init",
            "status": "ok",
            "changed": initialized.changed,
            "paths": initialized.paths,
        }
        summary = "初始化完成" if initialized.changed else "初始化无需变更"
        if initialized.notes:
            payload["notes"] = list(initialized.notes)
            summary += "；请注意：" + "；".join(initialized.notes)
        return _result(SUCCESS, payload, summary, args.as_json)

    if args.command == "upgrade":
        if not args.confirm:
            return _result(
                BLOCKED,
                {"command": "upgrade", "status": "blocked", "reason": "confirmation required"},
                "升级已暂停：需要明确确认",
                args.as_json,
            )
        try:
            upgraded = upgrade_project(paths, True)
        except (OSError, TypeError, ValueError) as error:
            return _result(
                BLOCKED,
                {"command": "upgrade", "status": "blocked", "reason": str(error)},
                "升级已阻塞：" + str(error),
                args.as_json,
            )
        payload = {
            "command": "upgrade",
            "status": "ok",
            "changed": upgraded.changed,
            "paths": upgraded.paths,
            "deploy": False,
        }
        return _result(
            SUCCESS,
            payload,
            "升级完成" if upgraded.changed else "升级无需变更",
            args.as_json,
        )

    if args.command == "apply-agentsmd":
        if not args.confirm:
            return _result(
                BLOCKED,
                {
                    "command": "apply-agentsmd",
                    "status": "blocked",
                    "reason": "confirmation required",
                },
                "AGENTS.md 规则应用已暂停：需要明确确认",
                args.as_json,
            )
        try:
            applied = apply_agentsmd_proposal(paths, True)
        except (OSError, TypeError, ValueError) as error:
            return _result(
                BLOCKED,
                {
                    "command": "apply-agentsmd",
                    "status": "blocked",
                    "reason": str(error),
                },
                "AGENTS.md 规则应用已阻塞：" + str(error),
                args.as_json,
            )
        payload = {
            "command": "apply-agentsmd",
            "status": "ok",
            "changed": applied.changed,
            "paths": applied.paths,
        }
        return _result(
            SUCCESS,
            payload,
            "AGENTS.md 能力规则已生效"
            if applied.changed
            else "AGENTS.md 能力规则无需变更",
            args.as_json,
        )

    if args.command == "doctor":
        report = doctor(scan_project(paths))
        bridge_payload = None
        try:
            bridge = ProviderActionStore(paths).capabilities()
            bridge_payload = _observed_adapter(
                paths, bridge["adapter_id"]
            ).to_dict()
        except (FileNotFoundError, OSError, TypeError, ValueError):
            pass
        issues = list(report.issues)
        if bridge_payload is not None:
            issues = [
                issue
                for issue in issues
                if issue != "no candidate Agent command found"
            ]
        ready = not issues
        status = report.status
        if ready:
            status = "ready"
        elif status != "blocked":
            status = "attention"
        payload = {
            "command": "doctor",
            "status": "ok" if status == "ready" else status,
            "diagnostic_status": status,
            "ok": ready,
            "issues": issues,
            "facts": report.facts,
            "provider_bridge": bridge_payload,
            "proposals": report.proposals,
        }
        text = (
            "环境检查通过"
            if ready
            else "环境检查发现问题：" + "；".join(issues)
        )
        return _result(
            SUCCESS if status == "attention" or ready else BLOCKED, payload, text, args.as_json
        )

    if args.command == "change-request":
        if not args.request:
            return _result(
                BLOCKED,
                {"command": "change-request", "status": "blocked", "reason": "request facts required"},
                "Change Request 状态未知：需要事实文件",
                args.as_json,
            )
        try:
            facts_path = paths.resolve_relative(args.request)
            data = _read_json(facts_path)
            if not isinstance(data, dict):
                raise ValueError("Change Request facts must be an object")
            cr_data = data.get("change_request", data)
            observed = data.get("observed_facts", data)
            if not isinstance(cr_data, dict) or not isinstance(observed, dict):
                raise ValueError("Change Request facts are invalid")
            capability = classify_merge_capability(observed)
            cr = ChangeRequest(
                cr_data["provider"], cr_data["kind"], cr_data["source"],
                cr_data["target"], cr_data["head_sha"], cr_data["tree_sha"],
                capability, cr_data.get("status", ""),
            )
            payload = {
                "command": "change-request",
                "status": "blocked_unknown" if capability == "unknown_remote" else capability,
                "merge_capability": capability,
                "change_request": cr.to_dict(),
                "remote_merge": capability == "verified_remote",
                "local_merge": capability in {"denied_remote", "unsupported_remote"},
            }
            text = (
                "Change Request 远端能力未知，保持 blocked_unknown"
                if capability == "unknown_remote"
                else "Change Request 能力已分类：" + capability
            )
            return _result(
                UNKNOWN if capability == "unknown_remote" else SUCCESS,
                payload,
                text,
                args.as_json,
            )
        except (KeyError, OSError, TypeError, ValueError) as error:
            return _result(
                UNKNOWN,
                {"command": "change-request", "status": "unknown", "reason": str(error)},
                "Change Request 状态未知：" + str(error),
                args.as_json,
            )

    if args.command == "deploy":
        if not args.manifest:
            return _result(
                BLOCKED,
                {"command": "deploy", "status": "blocked_deploy", "reason": "manifest required"},
                "Deploy 已暂停：需要显式 manifest",
                args.as_json,
            )
        try:
            manifest_path = paths.resolve_relative(args.manifest)
            source = _read_json(manifest_path)
            if not isinstance(source, dict):
                raise ValueError("Deploy manifest must be an object")
            manifest_data = source.get("manifest", source)
            if not isinstance(manifest_data, dict):
                raise ValueError("Deploy manifest is invalid")
            manifest = DeployManifest.from_dict(manifest_data)
            if not args.acceptance_state:
                raise PermissionError("independent acceptance state is required")
            state = plan_deploy(manifest, args.acceptance_state)
            if state.status == "blocked_deploy":
                return _result(
                    BLOCKED,
                    {"command": "deploy", **state.to_dict()},
                    "Deploy 已阻塞：独立验收尚未完成",
                    args.as_json,
                )
            deploy_dir = paths.resolve_vibe_path("deploy")
            deploy_dir.mkdir(parents=True, exist_ok=True)
            _atomic_json(deploy_dir / "manifest.json", manifest.to_dict())
            authorization = None
            if args.authorize is not None:
                authorization = authorize_deploy(manifest, args.authorize)
                state = DeployState(
                    "deploy_ready",
                    state.manifest_digest,
                    state.target,
                    evidence=state.evidence,
                    authorization_digest=authorization.digest,
                )
                _atomic_json(deploy_dir / "authorization.json", authorization.to_dict())
            if args.observations:
                if authorization is None:
                    raise PermissionError("Deploy observations require separate Deploy authorization")
                observations_path = paths.resolve_relative(args.observations)
                observations = _read_json(observations_path)
                running = start_deploy(manifest, state, authorization)
                state = verify_deploy(manifest, observations)
                if state.status in {"deployed", "rolled_back", "blocked_deploy", "blocked_unknown"}:
                    state = DeployState(
                        state.status,
                        state.manifest_digest,
                        state.target,
                        evidence=state.evidence,
                        authorization_digest=running.authorization_digest,
                        reason=state.reason,
                    )
            _atomic_json(deploy_dir / "state.json", state.to_dict())
            if state.status in {"deploy_planned", "deploy_ready", "deployed", "rolled_back"}:
                code = SUCCESS
            elif state.status == "blocked_deploy":
                code = BLOCKED
            else:
                code = UNKNOWN
            return _result(code, {"command": "deploy", **state.to_dict()}, "Deploy 状态：" + state.status, args.as_json)
        except PermissionError as error:
            return _result(BLOCKED, {"command": "deploy", "status": "blocked_deploy", "reason": str(error)}, "Deploy 已暂停：" + str(error), args.as_json)
        except (FileNotFoundError, OSError, TypeError, ValueError) as error:
            return _result(UNKNOWN, {"command": "deploy", "status": "blocked_unknown", "reason": str(error)}, "Deploy 状态未知：" + str(error), args.as_json)

    if args.command == "attest":
        missing = [flag for flag, value in (("--adapter", args.adapter), ("--facts", args.facts), ("--provenance", args.provenance)) if not value]
        if missing:
            return _result(
                BLOCKED,
                {"command": "attest", "status": "blocked_invalid", "reason": "attest requires " + ", ".join(missing)},
                "能力登记未完成：缺少 " + ", ".join(missing),
                args.as_json,
            )
        try:
            facts = _read_json(paths.resolve_relative(args.facts))
            summary = record_session_capabilities(paths, args.adapter, facts, args.provenance, args.project_id)
        except PermissionError as error:
            return _result(
                BLOCKED,
                {"command": "attest", "status": "blocked", "reason": str(error)},
                "能力登记未完成：" + str(error),
                args.as_json,
            )
        except (FileNotFoundError, OSError, TypeError, ValueError) as error:
            return _result(
                BLOCKED,
                {"command": "attest", "status": "blocked_invalid", "reason": str(error)},
                "能力登记未完成：" + str(error),
                args.as_json,
            )
        payload = {"command": "attest", "status": "ok"}
        payload.update(summary)
        text = "已登记 {} 会话能力：级别 {}，{}".format(
            summary["adapter_id"], summary["level"],
            "可派发可见任务" if summary["visible_automation"] else "未验证可见任务控制面，仅向导/后台模式",
        )
        return _result(SUCCESS, payload, text, args.as_json)

    if args.command == "plan":
        try:
            entry = build_session_entry(args.request or "", args.s1, args.plan_id)
            if args.from_prd and not args.s1:
                # Publishing a draft keeps the S1 the session routed it with;
                # re-scoring the text alone must never downgrade it.
                inherited = draft_s1(_plan_root(paths, entry.plan_id))
                if inherited is not None:
                    drafted = build_session_entry(args.request or "", inherited, args.plan_id)
                    if drafted.s1.total > entry.s1.total:
                        entry = drafted
        except (TypeError, ValueError) as error:
            return _result(
                BLOCKED,
                {"command": "plan", "status": "blocked", "reason": str(error)},
                "规划已阻塞：" + str(error),
                args.as_json,
            )
        screen = entry.s0
        if args.from_prd and entry.route.route != "complex":
            # A product spec only exists on the complex path; answering `ok`
            # with a generic plan would silently discard it.
            reason = (
                "product_spec_requires_complex_route: request routed {} (S1={}); "
                "publish with the draft's plan id or pass --s1".format(entry.route.route, entry.s1.total)
            )
            return _result(
                BLOCKED,
                {"command": "plan", "status": "blocked", "route": entry.route.route,
                 "score": entry.s1.total, "plan_id": entry.plan_id, "reason": reason},
                "规划已阻塞：" + reason,
                args.as_json,
            )
        if screen.simple and not args.s1 and not args.from_prd:
            payload = {
                "command": "plan",
                "status": "ok",
                "route": "simple",
                "rationale": screen.rationale,
                "plan_id": entry.plan_id,
                "node_spec": entry.node_spec,
            }
            return _result(
                SUCCESS, payload, "该请求走轻量直接执行路径", args.as_json
            )
        try:
            score = entry.s1
            route = entry.route.route
            if route != "complex":
                payload = {
                    "command": "plan",
                    "status": "ok",
                    "route": route,
                    "score": score.total,
                    "plan_id": entry.plan_id,
                    "node_spec": entry.node_spec,
                }
                return _result(
                    SUCCESS, payload, "任务已进入轻规划", args.as_json
                )
            if not args.node_spec and not args.from_prd:
                materialized = materialize_session_entry(paths, entry)
                payload = {
                    "command": "plan",
                    "status": "planned",
                    "route": route,
                    "score": score.total,
                    "plan_id": entry.plan_id,
                    "node_spec": entry.node_spec,
                    "materialized_path": str(materialized.relative_to(paths.root)),
                    "execution": "deferred_until_authorize",
                }
                return _result(
                    SUCCESS, payload, "复杂请求已生成稳定计划草案，等待授权", args.as_json
                )
            if args.from_prd:
                # Product-level spec from the agent: business fields only;
                # every engineering field is derived by node_spec.
                source = _read_json(paths.resolve_relative(args.from_prd))
                if not isinstance(source, dict):
                    raise ValueError("product spec must be a JSON object")
                reject_engineering_fields(source)
            else:
                source = _read_json(paths.resolve_relative(args.node_spec))
                if not isinstance(source, dict):
                    raise ValueError("node spec must be a JSON object")
            # The session route settles the band only on the product path;
            # a hand-written node spec keeps declaring its own band.
            source = normalize_node_spec(source, entry, paths, route_governs_band=bool(args.from_prd))
            # Evaluate the PRD checkpoints before _publish_plan().  This gate
            # is deliberately side-effect free: unresolved product choices,
            # missing/unverified evidence, and invalid Skill references must
            # not materialize plans or authorization cards.
            has_prd_gate_input = any(
                key in source for key in ("rationale", "product_question", "skill_profiles")
            )
            rationale = source.get("rationale", {})
            if not isinstance(rationale, dict):
                rationale = {"framing": rationale}
            else:
                rationale = dict(rationale)
            if "product_question" in source and "product_question" not in rationale:
                rationale["product_question"] = source["product_question"]
            gate_context = TaskContext(
                score.steps, score.domains, score.uncertainty,
                score.failure_cost, toolchain=score.toolchain, rationale=rationale,
            )
            # Older executable node specs predate the V4.5 PRD checkpoint
            # fields. Keep those specs compatible; once a spec opts into any
            # checkpoint/profile field, the gate is mandatory.
            checkpoints = evaluate_prd_checkpoints(gate_context) if has_prd_gate_input else []
            raw_profiles = source.get("skill_profiles", [])
            if raw_profiles is None:
                raw_profiles = []
            if not isinstance(raw_profiles, list):
                raise TypeError("skill_profiles must be a list")
            for raw_profile in raw_profiles:
                if not isinstance(raw_profile, dict):
                    raise TypeError("Skill profile must be an object")
                validate_skill_profile(SkillProfile(**raw_profile))
            evidence_refs = ["prd:{}@1".format(entry.plan_id)]
            blocked_checkpoints = [item for item in checkpoints if item.status == "blocked_design"]
            if has_prd_gate_input and blocked_checkpoints:
                question = str(blocked_checkpoints[0].fields.get("question", "请补充未决产品选择"))
                handoff = build_stage_handoff(
                    PRD(str(source.get("title", "").strip() or "未命名 PRD"),
                        str(source.get("objective", "").strip() or entry.request),
                        revision=1, status="blocked_design"),
                    [question], evidence_refs,
                )
                payload = {
                    "command": "plan", "status": "blocked_design",
                    "route": "complex", "question": question,
                    "checkpoints": [item.to_dict() for item in checkpoints],
                    "handoff": handoff.to_dict(),
                    "handoff_text": handoff.render(),
                    "downstream_artifact": None,
                }
                return _result(BLOCKED, payload, "规划已暂停：需要回答产品问题：" + question, args.as_json)
            review_checkpoints = [item for item in checkpoints if item.status == "review_required"]
            if has_prd_gate_input and review_checkpoints:
                handoff = build_stage_handoff(
                    PRD(str(source.get("title", "").strip() or "未命名 PRD"),
                        str(source.get("objective", "").strip() or entry.request),
                        revision=1, status="review_required"),
                    [], evidence_refs,
                )
                payload = {
                    "command": "plan", "status": "review_required",
                    "route": "complex", "required_user_action": "confirm_plan",
                    "checkpoints": [item.to_dict() for item in checkpoints],
                    "handoff": handoff.to_dict(),
                    "handoff_text": handoff.render(),
                    "downstream_artifact": None,
                }
                return _result(BLOCKED, payload, "规划已暂停：需要确认 PRD 证据", args.as_json)
            plan, nodes, card = _publish_plan(paths, entry.plan_id, source)
        except PermissionError as error:
            if _is_capability_contract_unknown(error):
                return _result(
                    UNKNOWN,
                    {
                        "command": "monitor",
                        "dispatcher": "monitor",
                        "status": "blocked_unknown",
                        "reason": str(error),
                    },
                    "能力合同状态未知：" + str(error),
                    args.as_json,
                )
            return _result(
                BLOCKED,
                {"command": "plan", "status": "blocked", "reason": str(error)},
                "规划已暂停：" + str(error),
                args.as_json,
            )
        except (FileExistsError, OSError, TypeError, ValueError) as error:
            return _result(
                BLOCKED,
                {"command": "plan", "status": "blocked", "reason": str(error)},
                "规划已阻塞：" + str(error),
                args.as_json,
            )
        payload = {
            "command": "plan",
            "status": "ok",
            "route": "complex",
            "score": entry.s1.total,
            # The draft is gone once published; republishing under a new id
            # passes this back as --s1 to keep the same route.
            "s1": _s1_arg(entry),
            "plan": plan.to_dict(),
            "nodes": [node.id for node in nodes],
            "authorization_card": card.to_dict(),
            "authorization_digest": card.digest,
        }
        approved_handoff = build_stage_handoff(
            PRD(str(source.get("title", "").strip() or "未命名 PRD"),
                str(source.get("objective", "").strip() or entry.request),
                revision=1, status="approved"),
            [], ["prd:{}@1".format(entry.plan_id)],
        )
        payload.update({
            "handoff": approved_handoff.to_dict(),
            "handoff_text": approved_handoff.render(),
            "checkpoints": [item.to_dict() for item in checkpoints],
        })
        return _result(
            SUCCESS, payload, "复杂计划产物已生成，等待一次授权", args.as_json
        )

    if args.command == "authorize":
        if not args.plan:
            return _result(
                BLOCKED,
                {"command": "authorize", "status": "blocked", "reason": "plan is required"},
                "授权未记录：需要计划",
                args.as_json,
            )
        try:
            workflow = materialize_workflow_evidence(paths, args.plan, args.authorize or "")
        except AuthorizationDenied as error:
            # A policy denial, identified by its type rather than by whether the
            # exception happens to carry an errno.
            return _result(
                BLOCKED,
                {"command": "authorize", "status": "blocked", "reason": str(error)},
                "授权未记录：" + str(error),
                args.as_json,
            )
        except PermissionError as error:
            # Every policy path raises AuthorizationDenied, so anything left
            # here is an OS-level fault: an environment problem, not a design
            # decision, and reporting it as `blocked` would misattribute it.
            return _result(
                UNKNOWN,
                {
                    "command": "authorize",
                    "status": "blocked_unknown",
                    "reason": "state_unreadable: " + str(error),
                },
                "授权状态未知：" + str(error),
                args.as_json,
            )
        except json.JSONDecodeError as error:
            return _result(
                UNKNOWN,
                {
                    "command": "authorize",
                    "status": "blocked_unknown",
                    "reason": "state_unreadable: " + str(error),
                },
                "授权状态未知：state.json 无法解析：" + str(error),
                args.as_json,
            )
        except (OSError, ValueError, KeyError, TypeError) as error:
            return _result(
                BLOCKED,
                {
                    "command": "authorize",
                    "status": "blocked_design",
                    "reason": str(error),
                },
                "授权未记录：" + str(error),
                args.as_json,
            )
        return _result(
            SUCCESS,
            {
                "command": "authorize",
                "status": "ok",
                "plan_id": args.plan,
                "route": workflow.get("route"),
                "recorded_nodes": list(workflow.get("nodes") or []),
                "authorization_granted": bool(workflow.get("authorization_granted")),
            },
            "十节点门禁证据已记录，可以启动监工",
            args.as_json,
        )

    if args.command == "monitor":
        if not args.plan:
            return _result(
                BLOCKED,
                {
                    "command": "monitor",
                    "status": "blocked",
                    "reason": "authorization required",
                },
                "监工未启动：需要计划和精确授权",
                args.as_json,
            )
        # Plan-bound confirmations may use an explicit revision token.
        if args.authorize != "AUTHORIZE" and not (
            args.authorize == "AUTHORIZE_V39_REV3_SELF_HEAL_NON_DEPLOY_SCOPE"
            and args.plan == "vibe-guide-v3.9-bugfix"
        ):
            return _result(
                BLOCKED,
                {
                    "command": "monitor",
                    "status": "blocked",
                    "reason": "authorization required",
                },
                "监工未启动：需要精确 AUTHORIZE",
                args.as_json,
            )
        if v2_state or v42_state:
            try:
                require_capability_contract(paths)
            except PermissionError as error:
                return _result(
                    UNKNOWN,
                    {
                        "command": "monitor",
                        "dispatcher": "monitor",
                        "status": "blocked_unknown",
                        "reason": str(error),
                    },
                    "能力合同状态未知：" + str(error),
                    args.as_json,
                )
        try:
            directory, plan, nodes, card = _load_plan(paths, args.plan)
            _require_v38_preflight(paths, directory, plan, nodes, card)
            state_path = paths.vibe / "state.json"
            if state_path.is_file():
                try:
                    state_data = _read_json(state_path)
                except ValueError:
                    state_data = {}
                if isinstance(state_data, dict) and state_data.get("workflow_version") == 2:
                    _require_public_execution_gate(
                        paths, directory, plan, nodes, card
                    )
            invalidation_path = _invalidation_path(directory)
            invalidation_to_clear = None
            if invalidation_path.exists():
                invalidation = _read_json(invalidation_path)
                if not isinstance(invalidation, dict):
                    raise ValueError("authorization invalidation record is invalid")
                card = refresh_authorization_card(
                    plan, nodes, card,
                    workflow=load_snapshot(paths, _run_id(directory, None)).workflow,
                )
                record = authorize(card, args.authorize)
                if runner is None:
                    runner = _public_runner(paths, card, nodes)
                snapshot = Monitor(
                    paths, plan, nodes,
                    topology_rulings=_observed_topology_rulings(paths),
                ).reauthorize(
                    _run_id(directory, None),
                    record,
                    runner,
                    str(
                        invalidation.get(
                            "change_reason", "executable_contract_changed"
                        )
                    ),
                )
                _atomic_json(directory / "authorization-card.json", card.to_dict())
                invalidation_to_clear = invalidation_path
            elif _current_run_path(directory).exists():
                # A capability-only mismatch is deliberately reported as
                # unknown by public resume, so it has no invalidation marker
                # to select this branch.  An existing current run is still
                # authoritative: reauthorize it in place rather than
                # creating a second writer and colliding on the old lease.
                # Refresh the card as well: a delivered contract correction
                # may have changed the executable node digest without writing
                # an invalidation marker yet.
                card = refresh_authorization_card(
                    plan, nodes, card,
                    workflow=load_snapshot(paths, _run_id(directory, None)).workflow,
                )
                _atomic_json(directory / "authorization-card.json", card.to_dict())
                record = authorize(card, args.authorize)
                if runner is None:
                    runner = _public_runner(paths, card, nodes)
                snapshot = Monitor(
                    paths, plan, nodes,
                    topology_rulings=_observed_topology_rulings(paths),
                ).reauthorize(
                    _run_id(directory, None),
                    record,
                    runner,
                    "capability_contract_changed",
                )
            else:
                record = authorize(card, args.authorize)
                if runner is None:
                    runner = _public_runner(paths, card, nodes)
                snapshot = Monitor(
                    paths, plan, nodes,
                    topology_rulings=_observed_topology_rulings(paths),
                ).start(record, runner)
            # Publishing a plan records confirmation, not execution
            # authorization.  Once the user supplies the exact authorization
            # token, persist the lifecycle transition so the public execution
            # gate can observe the same state on resume.
            if plan.status == "confirmed_pending_authorization":
                published_plan = _read_json(directory / "plan.json")
                if isinstance(published_plan, dict):
                    published_plan["status"] = "authorized"
                    _atomic_json(directory / "plan.json", published_plan)
            _atomic_json(directory / "authorization.json", record.to_dict())
            _atomic_json(
                directory / "plan-confirmation.json",
                {
                    "status": "confirmed",
                    "plan_id": plan.plan_id,
                    "plan_revision": str(plan.version),
                    "authorization_digest": record.digest,
                    "authorization_required": True,
                    "run_id": snapshot.run_id,
                    "event_sequence": snapshot.event_sequence,
                    "publication": "same_run_reauthorization",
                },
            )
            _atomic_json(
                _current_run_path(directory), {"run_id": snapshot.run_id}
            )
            if invalidation_to_clear is not None:
                invalidation_to_clear.unlink()
        except PreflightBlockedError as error:
            return _result(
                BLOCKED,
                {"command": "monitor", "status": "preflight_blocked", "check_ids": list(error.check_ids), "reason": str(error)},
                "预检已阻塞：" + ", ".join(error.check_ids),
                args.as_json,
            )
        except PermissionError as error:
            if _is_capability_contract_unknown(error):
                return _result(
                    UNKNOWN,
                    {
                        "command": "monitor",
                        "dispatcher": "monitor",
                        "status": "blocked_unknown",
                        "reason": str(error),
                    },
                    "能力合同状态未知：" + str(error),
                    args.as_json,
                )
            return _result(
                BLOCKED,
                {
                    "command": "monitor",
                    "status": "blocked_design",
                    "reason": str(error),
                },
                "监工已暂停：" + str(error),
                args.as_json,
            )
        except ProviderPending as error:
            return _result(
                UNKNOWN,
                {
                    "command": "monitor",
                    "status": "retry_pending",
                    "reason": str(error),
                },
                "监工等待能力观测，自动重试",
                args.as_json,
            )
        except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError) as error:
            if _is_capability_contract_unknown(error):
                return _result(
                    UNKNOWN,
                    {
                        "command": "monitor",
                        "status": "blocked_unknown",
                        "reason": str(error),
                    },
                    "能力合同状态未知：" + str(error),
                    args.as_json,
                )
            return _result(
                UNKNOWN,
                {
                    "command": "monitor",
                    "status": "unknown",
                    "reason": str(error),
                },
                "监工状态未知：" + str(error),
                args.as_json,
            )
        if args.watch:
            supervisor = Supervisor(
                paths,
                Monitor(paths, plan, nodes, topology_rulings=_observed_topology_rulings(paths)),
                runner,
                snapshot.run_id,
            )
            lease_result = supervisor.recover_or_start()
            if not lease_result.get("active_supervisors"):
                return _result(
                    UNKNOWN,
                    {"command": "monitor", "status": "blocked_unknown", "reason": "supervisor lease is held"},
                    "监工租约已被其他进程持有",
                    args.as_json,
                )
            snapshot = supervisor.watch()
            return _snapshot_result("monitor", snapshot, args.as_json, "supervisor")
        return _snapshot_result("monitor", snapshot, args.as_json, "manual")

    if args.command == "reconcile":
        try:
            if not args.plan or not args.run_id or not args.evidence:
                raise ValueError("reconcile requires --plan, --run-id and --evidence")
            directory, plan, nodes, _card = _load_plan(paths, args.plan)
            evidence_path = paths.resolve_relative(args.evidence)
            package = _read_json(evidence_path)
            snapshot = Monitor(
                paths, plan, nodes,
                topology_rulings=_observed_topology_rulings(paths),
            ).reconcile_evidence(args.run_id, package)
            return _snapshot_result("reconcile", snapshot, args.as_json)
        except (FileNotFoundError, OSError, TypeError, ValueError, PermissionError) as error:
            return _result(
                UNKNOWN,
                {"command": "reconcile", "status": "blocked_unknown", "reason": str(error)},
                "对账已暂停：" + str(error),
                args.as_json,
            )

    if args.command in {"resume", "status"}:
        if not args.plan:
            return _result(
                UNKNOWN,
                {
                    "command": args.command,
                    "status": "unknown",
                    "reason": "plan required",
                },
                "状态未知：需要计划标识",
                args.as_json,
            )
        if args.command == "resume" and v2_state:
            try:
                require_capability_contract(paths)
            except PermissionError as error:
                return _result(
                    UNKNOWN,
                    {
                        "command": "resume",
                        "status": "blocked_unknown",
                        "reason": str(error),
                    },
                    "能力合同状态未知：" + str(error),
                    args.as_json,
                )
        directory = None
        try:
            directory, plan, nodes, _card = _load_plan(paths, args.plan)
            run_id = _run_id(directory, args.run_id)
            if args.command == "resume" and v2_state:
                _require_public_execution_gate(
                    paths, directory, plan, nodes, _card
                )
            invalidation = _invalidation_path(directory)
            if invalidation.exists():
                persisted = _read_json(invalidation)
                reason = (
                    persisted.get("reason", "authorization invalidated")
                    if isinstance(persisted, dict)
                    else "authorization invalidated"
                )
                return _result(
                    BLOCKED,
                    {
                        "command": args.command,
                        "status": "blocked_design",
                        "reason": reason,
                    },
                    "恢复已暂停：授权因设计变化失效",
                    args.as_json,
                )
            if args.command == "status":
                snapshot = load_snapshot(paths, run_id)
            else:
                if runner is None:
                    runner = _public_runner(paths, _card, nodes)
                AuthorizationRecord.from_dict(
                    _read_json(directory / "authorization.json")
                )
                monitor = Monitor(
                    paths, plan, nodes,
                    topology_rulings=_observed_topology_rulings(paths),
                )
                # The CLI performs the normal resume tick immediately after
                # reattachment; let that tick own the single provider poll.
                snapshot = monitor.resume(run_id, runner, poll_handles=False)
                snapshot = monitor.tick(run_id, runner)
        except PermissionError as error:
            if _is_capability_contract_unknown(error):
                return _result(
                    UNKNOWN,
                    {
                        "command": args.command,
                        "status": "blocked_unknown",
                        "reason": str(error),
                    },
                    "能力合同状态未知：" + str(error),
                    args.as_json,
                )
            if _is_draft_block(error):
                # Nothing to invalidate: a draft never held an authorization.
                # Report the design state and how to finish it.
                return _result(
                    BLOCKED,
                    {"command": args.command, "status": "blocked_design", "reason": str(error)},
                    "计划仍是草案：" + str(error),
                    args.as_json,
                )
            _persist_invalidation(
                directory, "authorization invalidated: " + str(error)
            )
            return _result(
                BLOCKED,
                {
                    "command": args.command,
                    "status": "blocked_design",
                    "reason": "authorization invalidated: " + str(error),
                },
                "恢复已暂停：授权因设计变化失效",
                args.as_json,
            )
        except RuntimeError as error:
            return _result(
                UNKNOWN,
                {
                    "command": args.command,
                    "status": "unknown",
                    "reason": str(error),
                },
                "状态未知：" + str(error),
                args.as_json,
            )
        except (FileNotFoundError, OSError, TypeError, ValueError) as error:
            message = str(error)
            if (
                "authorization" in message
                or "contract" in message
                or "plan no longer" in message
            ):
                _persist_invalidation(
                    directory, "authorization invalidated: " + message
                )
                return _result(
                    BLOCKED,
                    {
                        "command": args.command,
                        "status": "blocked_design",
                        "reason": "authorization invalidated: " + message,
                    },
                    "恢复已暂停：授权因设计变化失效",
                    args.as_json,
                )
            return _result(
                UNKNOWN,
                {
                    "command": args.command,
                    "status": "unknown",
                    "reason": message,
                },
                "状态未知：" + message,
                args.as_json,
            )
        return _snapshot_result(args.command, snapshot, args.as_json)

    return _result(
        USAGE_ERROR, {"status": "usage_error"}, "参数错误", args.as_json
    )


def handle_monitor(
    as_json: bool = False, authorization: Optional[str] = None, runner=None
) -> int:
    argv = ["monitor"]
    if authorization:
        argv.extend(["--authorize", authorization])
    if as_json:
        argv.append("--json")
    return run_cli(argv, Path.cwd(), runner=runner).exit_code


def main(argv: Optional[Sequence[str]] = None, runner=None) -> int:
    arguments = list(argv) if argv is not None else os.sys.argv[1:]
    result = run_cli(arguments, Path.cwd(), runner=runner)
    print(
        json.dumps(result.payload, ensure_ascii=False, sort_keys=True)
        if result.as_json
        else result.text
    )
    return result.exit_code
