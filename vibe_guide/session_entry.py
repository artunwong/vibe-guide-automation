"""Pure V4.5 session-entry projection.

The entry point owns only S0/S1 routing and deterministic planning defaults.
It deliberately has no provider, writer, or monitor side effects; execution is
handled by the later authorization boundary.
"""

from dataclasses import asdict, dataclass
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from .planner import RouteResult, S0Result, S1Score, TaskContext, classify_s0, parse_s1_context, route_task, score_s1


def _normalize_request(request: str) -> str:
    if not isinstance(request, str):
        raise TypeError("request must be a string")
    return " ".join(request.strip().lower().split())


def stable_plan_id(request: str) -> str:
    """Return a path-safe id stable for equivalent request whitespace/case."""
    normalized = _normalize_request(request)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return "session-" + (digest or "0" * 16)


def _bounded(value: int) -> int:
    return max(0, min(5, int(value)))


def default_s1_context(request: str) -> TaskContext:
    """Derive a conservative, deterministic S1 context from request text.

    This is a routing fallback only. It does not claim provider capability or
    infer executable permissions.
    """
    normalized = _normalize_request(request)
    if not normalized:
        return TaskContext(0, 0, 0, 0, 0, rationale={"source": "stable_default"})
    markers = [
        "并", "然后", "以及", "同时", "设计", "实现", "开发", "重构",
        "迁移", "集成", "测试", "部署", "系统", "多个", "workflow", "pipeline",
    ]
    actions = set(re.findall(r"[a-z]+", normalized))
    action_words = {"add", "build", "create", "deploy", "design", "fix", "implement", "integrate", "migrate", "refactor", "rename", "test", "update", "write"}
    marker_count = sum(normalized.count(marker) for marker in markers)
    english_count = len(actions & action_words)
    steps = _bounded(max(1, marker_count + english_count))
    domains = _bounded(1 + int(any(word in normalized for word in ("系统", "数据", "接口", "database", "api"))))
    uncertainty = _bounded(1 + int(any(word in normalized for word in ("设计", "未知", "探索", "integrat", "deploy"))))
    failure_cost = _bounded(1 + int(any(word in normalized for word in ("支付", "生产", "部署", "迁移", "安全", "credential"))))
    toolchain = _bounded(1 + int(any(word in normalized for word in ("测试", "部署", "workflow", "pipeline", "集成"))))
    # A request that explicitly spans design/build/verification or deployment
    # is complex even when no user S1 override is supplied. Keep this as a
    # deterministic routing signal; it does not authorize execution.
    complex_signal = marker_count >= 4 or english_count >= 3 or any(
        word in normalized for word in ("部署", "迁移", "支付", "pipeline", "workflow")
    )
    if complex_signal:
        steps = max(steps, 5)
        domains = max(domains, 3)
        uncertainty = max(uncertainty, 3)
        failure_cost = max(failure_cost, 3)
        toolchain = max(toolchain, 3)
    return TaskContext(
        steps, domains, uncertainty, failure_cost, toolchain,
        rationale={"source": "stable_default", "request_digest": hashlib.sha256(normalized.encode("utf-8")).hexdigest()},
    )


def _default_node_spec(request: str, plan_id: str, route: RouteResult) -> Dict[str, Any]:
    digest = hashlib.sha256(_normalize_request(request).encode("utf-8")).hexdigest()[:12]
    node_id = "session-node-" + (digest or "0" * 12)
    title = "会话请求：" + request.strip()[:80]
    return {
        "title": title,
        "objective": request.strip(),
        "plan_id": plan_id,
        "route": route.route,
        "complexity_band": route.complexity_band,
        "route_result": route.to_dict(),
        "capabilities": {
            "agent_id": "pending",
            "shell": False,
            "subprocess": False,
            "worktree": False,
            "background": False,
            "session_resume": False,
            "level": "guide",
        },
        "nodes": [{
            "id": node_id,
            "title": title,
            "depends_on": [],
            "integration_after": [],
            "parallel_group": None,
            "status": "planned",
            "contract": {
                "input": request.strip(),
                "output": "按授权后的执行合同交付",
                "error_behavior": "保留 blocked_unknown 并提供可恢复状态",
                "acceptance_example": "入口输出稳定的 plan_id 和 node id",
            },
        }],
    }


@dataclass(frozen=True)
class SessionEntry:
    request: str
    s0: S0Result
    s1: S1Score
    route: RouteResult
    plan_id: str
    node_spec: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "request": self.request,
            "s0": asdict(self.s0),
            "s1": asdict(self.s1),
            "route": self.route.to_dict(),
            "plan_id": self.plan_id,
            "node_spec": json.loads(json.dumps(self.node_spec, ensure_ascii=False, sort_keys=True)),
        }


DRAFT_NEXT_STEP = (
    "计划仍是草案，尚无产品内容；请用 vibe plan --request <原请求> --from-prd <产品 spec 文件> 完成发布"
)


# What materialize_session_entry writes; publishing may replace exactly these.
DRAFT_ARTIFACTS = ("plan.json", "nodes.json", "node-spec.json")
# Names that mean a directory has left the draft stage, whatever plan.json says.
_NON_DRAFT_PREFIXES = ("authorization", "current-run", "plan-confirmation", "engine-attestation")


def _read_draft_plan(directory: Path) -> Optional[Dict[str, Any]]:
    plan_file = directory / "plan.json"
    if directory.is_symlink() or not directory.is_dir() or plan_file.is_symlink() or not plan_file.is_file():
        return None
    try:
        plan = json.loads(plan_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(plan, dict) or plan.get("status") != "draft":
        return None
    return plan


def replaceable_draft_extras(directory: Path) -> Optional[list]:
    """Files to carry over when a publish replaces a never-authorized draft.

    Returns ``None`` unless ``directory`` is still exactly a session draft:
    ``plan.json`` says ``draft``, every entry is a regular file, and nothing
    in it belongs to publication, authorization, or a run.  Anything else the
    agent left there (the product spec, per the protocol) is returned so the
    caller keeps it.
    """
    if _read_draft_plan(directory) is None:
        return None
    extras = []
    for child in sorted(directory.iterdir()):
        if child.is_symlink() or not child.is_file():
            return None
        if child.name in DRAFT_ARTIFACTS:
            continue
        if child.name.startswith(_NON_DRAFT_PREFIXES):
            return None
        extras.append(child)
    return extras


def draft_s1(directory: Path) -> Optional[str]:
    """The session's own S1 recorded by a draft, as an explicit ``--s1`` value."""
    plan = _read_draft_plan(directory)
    route = plan.get("route_result") if plan else None
    dimensions = route.get("dimensions") if isinstance(route, dict) else None
    if not isinstance(dimensions, dict):
        return None
    names = ("steps", "domains", "uncertainty", "failure_cost", "toolchain")
    values = [dimensions.get(name) for name in names]
    if not all(isinstance(value, int) and not isinstance(value, bool) for value in values):
        return None
    return ",".join(str(value) for value in values)


def materialize_session_entry(paths: Any, entry: SessionEntry) -> Path:
    """Persist only deterministic planning inputs for a new session.

    The files are intentionally incomplete for execution: no authorization
    card, provider task, writer, lease, or monitor state is created here.
    Repeated calls replace equivalent projections atomically and return the
    same directory.
    """
    if not hasattr(paths, "resolve_vibe_path"):
        raise TypeError("paths must provide resolve_vibe_path")
    raw_vibe = Path(paths.root) / ".vibe"
    if raw_vibe.is_symlink():
        raise ValueError(".vibe directory may not be symlinked")
    raw_plans = raw_vibe / "plans"
    if raw_plans.is_symlink():
        raise ValueError("planning directory may not be symlinked")
    raw_root = raw_plans / entry.plan_id
    # Inspect the un-resolved path first; resolve_vibe_path intentionally
    # canonicalizes symlinks, which would otherwise hide a redirection.
    if raw_root.is_symlink() or raw_root.parent.is_symlink():
        raise ValueError("planning directory may not be symlinked")
    root = paths.resolve_vibe_path(Path("plans") / entry.plan_id)
    root.parent.mkdir(parents=True, exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    plan = {
        "plan_id": entry.plan_id,
        "version": 1,
        "status": "draft",
        "prd_path": str(Path(".vibe") / "plans" / entry.plan_id / "prd.md"),
        "node_ids": [item["id"] for item in entry.node_spec["nodes"]],
        "complexity_band": entry.route.complexity_band,
        "route_result": entry.route.to_dict(),
        # A draft has no product content yet; tell the agent how to finish it
        # instead of letting monitor trip over the missing authorization card.
        "next_step": DRAFT_NEXT_STEP,
    }
    artifacts = dict(zip(DRAFT_ARTIFACTS, (plan, entry.node_spec["nodes"], entry.node_spec)))
    existing_plan = root / "plan.json"
    if existing_plan.is_symlink():
        raise ValueError("planning artifact path may not be a symlink")
    if existing_plan.is_file():
        try:
            existing = json.loads(existing_plan.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("existing plan artifact is invalid") from error
        if not isinstance(existing, dict) or existing.get("status") != "draft":
            raise ValueError("existing plan is no longer a draft")
    for name, payload in artifacts.items():
        target = root / name
        if target.is_symlink():
            raise ValueError("planning artifact path may not be a symlink")
        descriptor, temporary_name = tempfile.mkstemp(prefix="." + name + ".", dir=str(root))
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(str(temporary), str(target))
        finally:
            if temporary.exists():
                temporary.unlink()
    return root


def build_session_entry(request: str, s1: Any = None, plan_id: Optional[str] = None) -> SessionEntry:
    normalized = _normalize_request(request)
    if not normalized:
        raise ValueError("request is required")
    s0 = classify_s0(normalized)
    context = parse_s1_context(s1)
    if context is None and s1 is not None and str(s1).strip():
        # An explicit override the user typed must not silently fall back to
        # the heuristic; they would believe it took effect.
        raise ValueError("--s1 must be five integers between 0 and 5, separated by commas")
    if context is None:
        context = default_s1_context(normalized)
    score = score_s1(context)
    route = route_task(context)
    resolved_plan_id = str(plan_id).strip() if isinstance(plan_id, str) and plan_id.strip() else stable_plan_id(normalized)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", resolved_plan_id):
        raise ValueError("plan id must be a simple identifier")
    return SessionEntry(normalized, s0, score, route, resolved_plan_id, _default_node_spec(normalized, resolved_plan_id, route))
