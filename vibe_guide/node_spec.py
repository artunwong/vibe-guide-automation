"""Single source of truth for the engineering fields vibe derives for a plan.

The host agent hands vibe a *product-level* spec: title, objective, the
product decisions the user confirmed, and each node's business contract.
Everything else the publish gate needs -- routing band, observed adapter
capabilities, project id, the five-part integration contract, node status,
adapter routing -- is derived here from the session entry, the capability
bridge and the plan itself.  The CLI, the agent-facing protocol and the tests
all reference this module so the convention is defined once.

Nothing here approves a product decision or invents capability evidence:
``decisions`` pass through untouched and ``capabilities`` come only from the
bridge file a desktop session recorded.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Dict, List, Optional

from .adapters.base import Environment
from .adapters.registry import AdapterRegistry
from .adapters.task_provider import ProviderActionStore, ProviderPending
from .models import IntegrationAcceptanceContract, node_branch, node_worktree
from .prd_profiles import render_planning_brief
from .scanner import resolve_rules_file

#: Fields the agent / product manager supplies.  Only business semantics.
PRODUCT_SPEC_FIELDS: Dict[str, Any] = {
    "title": "str",
    "objective": "str",
    "decisions": [
        {"question": "str", "options": ["str"], "impact": "str", "recommendation": "str",
         "status": "approved|unresolved", "selected": "str|null", "field": "str"}
    ],
    "nodes": [
        {"id": "str", "title": "str", "depends_on": ["node id"], "integration_after": ["node id"],
         "parallel_group": "str|null",
         "contract": {"input": "str", "output": "str", "error_behavior": "str", "acceptance_example": "str"},
         "files": ["project-relative path"]}
    ],
    "prd?": {"<section>": [{"value": "str", "source": "user_confirmed|system_inferred|needs_confirmation|unverified"}]},
    "goals?": [
        {"id": "str", "user_scenario": "str", "code_evidence": "str", "spec_ref": "str",
         "issue_ref": "str", "dag_nodes": ["node id"], "runtime_acceptance": "str"}
    ],
    "rationale?": {"framing|tradeoffs|flow|acceptance": "verified_fact: ..."},
    "remote_git_actions?": "allow|deny",
}

#: Fields vibe derives.  A product spec carrying any of them is rejected.
ENGINEERING_TOP_LEVEL_FIELDS = (
    "complexity_band", "route", "route_result", "capabilities", "project_id",
    "integration_contract", "spec_path", "plan_id",
)
ENGINEERING_NODE_FIELDS = ("status",)
# `worktree` and `branch` are derived per node so parallel writers cannot share
# a directory.  The fill below only supplies what is missing, so a product spec
# naming them would keep its own value and bypass that isolation entirely.
# `writer` and `reviewer` are the aliases the fill reads to set `worker` and
# `reviewer_worker`, so guarding only the destination leaves the alias as a way
# to name another machine's worker.
ENGINEERING_CONTRACT_FIELDS = (
    "adapter_id", "project_id", "worker", "reviewer_worker", "worker_profile",
    "worktree", "branch", "writer", "reviewer",
)
ENGINEERING_FIELDS = {
    "top_level": ENGINEERING_TOP_LEVEL_FIELDS,
    "node": ENGINEERING_NODE_FIELDS,
    "contract": ENGINEERING_CONTRACT_FIELDS,
}

#: Never inside an authorization card; mirrors the product rule in AGENTS.md §6.
ALWAYS_EXCLUDED_SCOPE = ["deploy", "release", "production_write", "credentials", "external_communication"]

_GUIDE_CAPABILITIES = {
    "agent_id": "pending", "shell": False, "subprocess": False, "worktree": False,
    "background": False, "session_resume": False, "level": "guide",
}


def reject_engineering_fields(spec: Any) -> None:
    """Fail closed when a product spec carries fields vibe derives."""
    if not isinstance(spec, dict):
        raise TypeError("product spec must be a JSON object")
    found: List[str] = [key for key in ENGINEERING_TOP_LEVEL_FIELDS if key in spec]
    for index, node in enumerate(spec.get("nodes") or []):
        if not isinstance(node, dict):
            continue
        label = str(node.get("id") or index)
        found.extend("nodes[{}].{}".format(label, key) for key in ENGINEERING_NODE_FIELDS if key in node)
        contract = node.get("contract")
        if isinstance(contract, dict):
            found.extend("nodes[{}].contract.{}".format(label, key) for key in ENGINEERING_CONTRACT_FIELDS if key in contract)
    if found:
        raise ValueError("以下字段由系统自动生成，请从产品 spec 中删除：" + ", ".join(found))


@dataclass(frozen=True)
class ObservedCapabilities:
    adapter_id: str
    capabilities: Dict[str, Any]
    project_id: Optional[str]
    detection: Any


def observe_capabilities(paths: Any) -> ObservedCapabilities:
    """Read the capability bridge a desktop session recorded and detect the adapter.

    Raises ``ProviderPending`` when the bridge file is missing or invalid; the
    caller decides whether that blocks (complex plans) or falls back (others).
    """
    store = ProviderActionStore(paths)
    try:
        observed = store.capabilities()
    except (FileNotFoundError, OSError, TypeError, ValueError) as error:
        raise ProviderPending("provider capability observation pending") from error
    adapter_id = observed["adapter_id"]
    facts = observed["facts"]
    environment = Environment(
        # The manifest's agent probe is `command`-kind and is read through
        # has_command(); hand the session's `<adapter>.agent` statement to
        # `commands` too, or its recorded evidence is always False.
        commands={name: value for name, value in facts.items() if name.endswith(".agent")},
        facts=facts,
        provenance={key: observed["provenance"] for key in facts},
        available_agents=(adapter_id,),
    )
    try:
        adapter = AdapterRegistry().get(adapter_id)
    except KeyError as error:
        # An unknown adapter name is invalid evidence, not a pending probe.
        raise ValueError("capabilities.json names an unknown adapter: {}".format(adapter_id)) from error
    detection = adapter.detect(environment)
    caps = detection.capabilities
    project_id = observed.get("project_id")
    if not isinstance(project_id, str) or not project_id.strip():
        project_id = None
    return ObservedCapabilities(
        adapter_id,
        {
            "agent_id": caps.agent_id, "shell": caps.shell, "subprocess": caps.subprocess,
            "worktree": caps.worktree, "background": caps.background,
            "session_resume": caps.session_resume, "level": caps.level,
        },
        project_id.strip() if project_id else None,
        detection,
    )


def complete_node_contracts(raw_nodes: List[Dict[str, Any]], adapter_id: str, project_id: Optional[str]) -> None:
    """Fill the engineering defaults every node contract needs (in place, missing only).

    Node-level ``files`` (the product spec shape) moves into the contract, which
    is where the authorization card binds file scope; ``DAGNode`` has no such
    field.
    """
    for item in raw_nodes:
        if not isinstance(item, dict):
            raise TypeError("node spec entries must be objects")
        item.setdefault("status", "planned")
        if item.get("parallel_group") == "":
            item["parallel_group"] = None
        contract = item.setdefault("contract", {})
        if not isinstance(contract, dict):
            raise TypeError("node contract must be an object")
        if "files" in item:
            contract.setdefault("files", list(item.pop("files") or []))
        contract.setdefault("adapter_id", adapter_id)
        if project_id:
            contract.setdefault("project_id", project_id)
        # One writer per node means one worktree and one branch per node.  A
        # product spec carries no engineering fields, so without a derived
        # default every node used to land in the project root on the trunk:
        # parallel developers would share a tree, and nothing cross-checks two
        # nodes for pointing at the same directory.
        #
        # The names come from models.node_worktree/node_branch, the same
        # derivation Monitor falls back to, so the two sides cannot drift; the
        # spec's literal values used to shadow those safe defaults.  These are
        # identity strings for the dispatch contract, not directories vibe
        # creates: provisioning the tree belongs to whoever runs the node.
        contract.setdefault("worktree", node_worktree(item.get("id")))
        contract.setdefault("branch", node_branch(item.get("id")))
        contract.setdefault("worker", contract.get("writer", "worker"))
        contract.setdefault("reviewer_worker", contract.get("reviewer", "reviewer"))
        contract.setdefault("worker_profile", {
            "worker": contract.get("writer", "worker"),
            "model": "default",
            "reasoning": "normal",
            "fallbacks": [],
            "selection_basis": {
                "issue_complexity_ref": item.get("id", "node"),
                "complexity_band": "standard",
                "risk_tags": contract.get("risk_tags", []),
                "availability_evidence": "configured",
            },
            "writer": contract.get("writer", "worker"),
            "worktree": contract["worktree"],
            "branch": contract["branch"],
            "allowlist": contract.get("files", []) or ["."],
        })


def derive_integration_contract(spec: Dict[str, Any], entry: Any, paths: Any) -> Dict[str, Any]:
    """Project the five-part integration contract from the plan itself."""
    node_ids = [str(node.get("id")) for node in spec.get("nodes") or [] if isinstance(node, dict) and node.get("id")]
    agents_ref = (
        resolve_rules_file(paths.root)
        or ".vibe/proposals/agentsmd/proposal.md"
    )
    contract = {
        "iteration_context": {
            "kind": "iteration",
            "plan_id": entry.plan_id,
            "request_digest": hashlib.sha256(entry.request.encode("utf-8")).hexdigest(),
        },
        "compatibility_scope": node_ids,
        "agentsmd_acceptance_refs": [agents_ref],
        "integration_acceptance_contract": {
            "p0_p2_cleared": "verified_fact: 整合 review 报告 P0/P1/P2 清零",
            "full_diff_reviewed": "verified_fact: 全量 diff 已由独立 reviewer 只读审阅",
            "prd_spec_matched": "verified_fact: 交付与 PRD/Spec 合同一致",
        },
        "unverified_or_excluded": list(ALWAYS_EXCLUDED_SCOPE),
    }
    IntegrationAcceptanceContract.from_dict(contract)
    return contract


def normalize_node_spec(spec: Any, entry: Any, paths: Any, route_governs_band: bool = True) -> Dict[str, Any]:
    """Return a publish-ready node spec: fills what is missing, never overwrites.

    ``entry`` is the ``SessionEntry`` the request routed through.  On the
    product path (``--from-prd``) its band is authoritative: a complex-routed
    request publishes as complex, and the spec can only raise the band, never
    lower it.  On the legacy ``--node-spec`` path (``route_governs_band=False``)
    the band stays spec-declared, as it always was: a hand-written spec without
    ``complexity_band`` or an integration contract is a legacy background plan
    even when S1 routes complex.  ``test_v310_cli`` pins that compatibility.
    """
    if not isinstance(spec, dict):
        raise TypeError("node spec must be a JSON object")
    out = json.loads(json.dumps(spec, ensure_ascii=False))
    route = entry.route
    spec_says_complex = (
        out.get("complexity_band") == "complex" or out.get("route") == "complex"
        or bool(out.get("integration_contract"))
    )
    if route_governs_band:
        band = "complex" if spec_says_complex or route.complexity_band == "complex" else route.complexity_band
        if band == "complex":
            out["complexity_band"] = "complex"
        else:
            out.setdefault("complexity_band", band)
        out.setdefault("route", route.route)
        out.setdefault("route_result", route.to_dict())
    else:
        band = "complex" if spec_says_complex else str(out.get("complexity_band", ""))
    out.setdefault("plan_id", entry.plan_id)

    observed: Optional[ObservedCapabilities] = None
    try:
        observed = observe_capabilities(paths)
    except ProviderPending:
        observed = None
    if "capabilities" not in out:
        if observed is not None:
            out["capabilities"] = dict(observed.capabilities)
        elif band == "complex":
            raise PermissionError("engine_attestation_unavailable: verified Monitor engine evidence is required")
        else:
            out["capabilities"] = dict(_GUIDE_CAPABILITIES)
    capabilities = out["capabilities"]
    adapter_id = str(capabilities.get("agent_id", "pending"))

    project_id = out.get("project_id")
    if (not isinstance(project_id, str) or not project_id.strip()) and observed is not None and observed.project_id:
        project_id = observed.project_id
        out["project_id"] = project_id
    if not isinstance(project_id, str) or not project_id.strip():
        project_id = None
        if capabilities.get("level") == "full" and observed is not None:
            raise PermissionError("project_id_unavailable: 可见任务路由需要项目 ID，请在登记会话能力时一并提供")

    raw_nodes = out.get("nodes")
    if not isinstance(raw_nodes, list):
        raise TypeError("node spec nodes must be a list")
    if not raw_nodes:
        # Say it here in plain terms; otherwise the derived integration
        # contract fails first on an empty compatibility scope.
        raise ValueError("node spec must contain at least one node")
    complete_node_contracts(raw_nodes, adapter_id, project_id)

    if band == "complex":
        if not out.get("integration_contract"):
            out["integration_contract"] = derive_integration_contract(out, entry, paths)
        out.setdefault("spec_path", ".vibe/plans/{}/prd.md".format(entry.plan_id))
    out.setdefault("remote_git_actions", "deny")
    pending = unresolved_confirmations(out)
    if pending:
        # Feed the existing PRD checkpoint gate: an unclosed needs_confirmation
        # item is a product question the user has not answered, and publish
        # must stop at blocked_design with that question, not proceed.
        rationale = out.get("rationale")
        if not isinstance(rationale, dict):
            rationale = {} if rationale in (None, "") else {"framing": rationale}
        # Assign, do not setdefault: a spec that already carries a falsy
        # product_question would otherwise block the injection, and the
        # checkpoint gate would see no open question and publish the plan with
        # the item still unconfirmed.  A real pending item outranks whatever
        # the spec put there.
        rationale["product_question"] = {
            "question": pending[0],
            "options": [],
            "impact": "该项未确认前不能进入规划；共 {} 项待确认".format(len(pending)),
        }
        out["rationale"] = rationale
    return out


NEEDS_CONFIRMATION_SOURCE = "needs_confirmation"
PRD_SOURCE_MARKERS = ("user_confirmed", "system_inferred", NEEDS_CONFIRMATION_SOURCE, "unverified")


def _source_marker(item: Any) -> str:
    """The normalized source marker of one PRD item.

    Comparison is whitespace- and case-insensitive: a stray space would
    otherwise read as a confirmed item and let an open question through.
    """
    if not isinstance(item, dict):
        return ""
    return str(item.get("source", "")).strip().lower()


def unresolved_confirmations(spec: Dict[str, Any]) -> List[str]:
    """PRD items the product manager has not confirmed yet, in document order.

    A prd of the wrong shape raises instead of reading as empty; treating it
    as "nothing pending" would turn this gate into a fail-open door for any
    agent that wrote the section incorrectly.
    """
    prd = spec.get("prd")
    if prd is None:
        return []
    if not isinstance(prd, dict):
        raise ValueError("prd 必须是按小节组织的对象；收到 {}".format(type(prd).__name__))
    pending: List[str] = []
    for section, items in prd.items():
        if isinstance(items, (dict, str)):
            entries: List[Any] = [items]
        elif isinstance(items, list):
            entries = list(items)
        else:
            raise ValueError("prd.{} 必须是条目列表或单个条目；收到 {}".format(section, type(items).__name__))
        for item in entries:
            if not isinstance(item, dict):
                raise ValueError(
                    "prd.{} 的每个条目必须是带 value 与 source 的对象；收到 {}".format(section, type(item).__name__)
                )
            marker = _source_marker(item)
            if marker not in PRD_SOURCE_MARKERS:
                raise ValueError(
                    "prd.{} 的条目 source 必须是 {} 之一；收到 {!r}".format(
                        section, "/".join(PRD_SOURCE_MARKERS), item.get("source")
                    )
                )
            if marker == NEEDS_CONFIRMATION_SOURCE:
                pending.append(str(item.get("value", "")).strip() or "（未填写内容的待确认项）")
    return pending


def _source_line(item: Any) -> str:
    if isinstance(item, dict):
        return "- {}（来源：{}）".format(item.get("value", ""), item.get("source", "unverified"))
    return "- {}（来源：unverified）".format(item)


def render_prd_markdown(spec: Dict[str, Any], decisions: List[Any], evidence_priority: List[str]) -> str:
    """Render prd.md; the ``状态：approved`` / ``审核：reviewed`` lines are gate inputs."""
    title = str(spec.get("title", "")).strip()
    objective = str(spec.get("objective", "")).strip()
    lines = ["# {}".format(title), "", "状态：approved", "审核：reviewed", "", "目标：{}".format(objective), ""]
    prd = spec.get("prd")
    if isinstance(prd, dict):
        for section, items in prd.items():
            lines.append("## {}".format(section))
            lines.append("")
            for item in items if isinstance(items, list) else [items]:
                lines.append(_source_line(item))
            lines.append("")
    lines.append("## 已批准产品决策")
    lines.append("")
    for item in decisions:
        lines.append("- {} → {}".format(item.question, item.selected))
    lines.append("")
    lines.append("证据优先级：{}".format(" > ".join(evidence_priority)))
    return "\n".join(lines) + "\n"


def render_planning_brief_markdown(plan_id: str, spec: Dict[str, Any], plan: Any = None) -> str:
    """Render planning-brief.md from the product spec's PRD sections and goals."""
    prd = spec.get("prd") if isinstance(spec.get("prd"), dict) else {}

    def _values(section: str) -> List[Any]:
        items = prd.get(section, [])
        return list(items) if isinstance(items, list) else [items]

    draft = {
        "objective": {"value": str(spec.get("objective", "")).strip(), "source": "user_confirmed"},
        "user_scenarios": _values("user_scenarios"),
        "code_evidence": _values("code_evidence"),
        "non_goals": _values("non_goals"),
    }
    goals = spec.get("goals") or []
    if goals and plan is not None:
        # build_planning_brief validates the seven traceability fields and
        # requires the five integration-contract sections; they live on the
        # normalized spec, so hand them over rather than expecting Plan to
        # carry duplicates.  Its compatibility_scope is a mapping, the
        # contract's is a list of node ids.
        from .planner import build_planning_brief
        contract = spec.get("integration_contract") or {}
        build_planning_brief(
            plan, goals,
            iteration_context=contract.get("iteration_context") or {"kind": "iteration", "plan_id": plan_id},
            compatibility_scope={"node_ids": list(contract.get("compatibility_scope") or [])},
            agentsmd_acceptance_refs=list(contract.get("agentsmd_acceptance_refs") or []),
            integration_acceptance_contract=contract.get("integration_acceptance_contract") or {},
            unverified_or_excluded=list(contract.get("unverified_or_excluded") or []),
        )
    rows = [
        {
            "goal": goal.get("id"), "scenario": goal.get("user_scenario"),
            "current_evidence": goal.get("code_evidence"), "spec": goal.get("spec_ref"),
            "issue": goal.get("issue_ref"), "dag_node": goal.get("dag_nodes"),
            "runtime_acceptance": goal.get("runtime_acceptance"),
        }
        for goal in goals if isinstance(goal, dict)
    ]
    return render_planning_brief(plan_id, draft, rows)
