"""Durable provider-neutral runner backed by desktop action requests/results."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any, Dict, Optional

from ..adapters.task_provider import (
    ProviderActionStore,
    ProviderPending,
    ProviderUnavailable,
    require_complex_monitor_dispatch,
)
from ..contracts import RunEvent, RunHandle, Runner
from ..capability_contract import load_contract
from ..models import (
    BindingIntent,
    BindingObservation,
    BindingVerification,
    WaitThreadsCursorObservation,
    WorkerProfile,
)
from ..model_router import provider_thinking_for
from ..paths import ProjectPaths
from ..task_registry import (
    DEFAULT_TOPOLOGY,
    _TOPOLOGIES,
    TaskBinding,
    binding_contract_enabled,
    load_task_binding,
    runtime_binding_gate,
    save_task_binding,
)
from ..workflow_gate import session_contract_prompt
from ..state import read_writer_lease
from ..providers import (
    CLAUDE_CODE_PROVIDER,
    CODEX_PROVIDER,
    WORKBUDDY_VISIBLE_PROVIDER,
)


#: Native desktop tools per visible provider, one definition for the five
#: mailbox operations.  The desktop session that services the mailbox reads
#: ``native_tool`` from each request and calls that tool.  Claude Code maps to
#: the desktop app's session tools: spawn a visible session, show it, read its
#: state, message it, and page its transcript (the page cursor is the wait
#: cursor).  Binding results for Claude Code use the provider-neutral
#: ``task_id`` / ``host`` fields; ``threadId`` / ``hostId`` remain Codex-only.
NATIVE_TOOL_MAP: Dict[str, Dict[str, str]] = {
    CODEX_PROVIDER: {
        "create": "codex_app__create_thread",
        "locate": "codex_app__navigate_to_codex_page",
        "visibility": "codex_app__wait_threads",
        "resume": "codex_app__send_message_to_thread",
        "wait": "codex_app__wait_threads",
    },
    CLAUDE_CODE_PROVIDER: {
        "create": "ccd_session__spawn_task",
        "locate": "ccd_window__open_session_in",
        "visibility": "ccd_session_mgmt__get_session",
        "resume": "ccd_session_mgmt__send_message",
        "wait": "ccd_session_mgmt__list_events",
    },
    WORKBUDDY_VISIBLE_PROVIDER: {
        "create": "workbuddy_job__create",
        "locate": "workbuddy_job__get",
        "visibility": "workbuddy_job__list",
        "resume": "workbuddy_job__reply",
        "wait": "workbuddy_job__wait",
    },
}


class ProviderActionRunner(Runner):
    """Expose a public CLI runner while the App owns native tool execution."""

    def __init__(
        self,
        paths: ProjectPaths,
        adapter_id: str,
        provider: str,
    ):
        self.paths = paths
        self.adapter_id = adapter_id
        self.provider = provider
        self.store = ProviderActionStore(paths)

    def _native_tool(self, operation: str) -> str:
        tools = NATIVE_TOOL_MAP.get(self.provider)
        if tools is None:
            # A placeholder name would sit in the mailbox forever; only
            # providers with a verified native control plane may dispatch.
            raise ProviderUnavailable(
                "no verified native control plane for provider {}".format(self.provider)
            )
        return tools[operation]

    def _consistency_instruction(self, contract: Dict[str, Any]) -> str:
        try:
            capability = session_contract_prompt(load_contract(self.paths))
        except (FileNotFoundError, OSError, TypeError, ValueError):
            capability = "Capability contract: {\"status\":\"unknown\"}"
        consistency = "一致性纠偏证据必须原样绑定：{}".format(
            json.dumps(
                contract.get("consistency_binding", {}),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return capability + "\n" + consistency

    def _validate_v39_create_binding(
        self, contract: Dict[str, Any], runtime_worktree: Path
    ) -> None:
        """Fail closed before a V3.9 provider create request is emitted."""
        required = ("project_id", "worktree", "managed_root", "branch", "base_sha")
        values = {name: contract.get(name) for name in required}
        project_id = values["project_id"]
        if not isinstance(project_id, str) or not project_id.strip() or "\x00" in project_id:
            raise ProviderUnavailable("provider binding project_id is blocked_unknown")
        for name in ("worktree", "managed_root"):
            value = values[name]
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                raise ProviderUnavailable("provider binding %s is blocked_unknown" % name)
            if not Path(value).is_absolute():
                raise ProviderUnavailable("provider binding %s is blocked_unknown" % name)

        branch = values["branch"]
        invalid_branch = (
            not isinstance(branch, str)
            or not branch.strip()
            or branch != branch.strip()
            or "\x00" in branch
            or any(char.isspace() for char in branch)
            or Path(branch).is_absolute()
            or branch.startswith("-")
            or branch.endswith("/")
            or branch.endswith(".")
            or branch.startswith(".")
            or branch.endswith(".lock")
            or branch in {".", "..", "@"}
            or "//" in branch
            or ".." in branch
            or "@{" in branch
            or any(char in branch for char in "~^:?*[\\")
            or any(part.startswith(".") or part.endswith(".lock") for part in branch.split("/"))
        )
        if invalid_branch:
            raise ProviderUnavailable("provider binding branch is blocked_unknown")

        base_sha = values["base_sha"]
        if not isinstance(base_sha, str) or len(base_sha) != 40 or any(
            char not in "0123456789abcdefABCDEF" for char in base_sha
        ):
            raise ProviderUnavailable("provider binding base_sha is blocked_unknown")
        if not isinstance(runtime_worktree, (str, Path)):
            raise ProviderUnavailable("provider binding path is blocked_unknown")
        try:
            contract_worktree = Path(values["worktree"]).resolve()
            managed_root = Path(values["managed_root"]).resolve()
            if contract_worktree == Path(contract_worktree.anchor) or managed_root == Path(managed_root.anchor):
                raise ValueError
            if contract_worktree.exists() and not contract_worktree.is_dir():
                raise ValueError
            if managed_root.exists() and not managed_root.is_dir():
                raise ValueError
            contract_worktree.relative_to(managed_root)
            runtime = Path(runtime_worktree)
            if not runtime.is_absolute() or runtime.resolve() != contract_worktree:
                raise ValueError
        except (OSError, RuntimeError, TypeError, ValueError):
            raise ProviderUnavailable("provider binding path is blocked_unknown")
        if "project" in contract and contract["project"] != project_id:
            raise ProviderUnavailable("provider binding alias is blocked_unknown")
        nested = contract.get("binding_contract")
        if nested is not None:
            if not isinstance(nested, dict):
                raise ProviderUnavailable("provider binding contract is blocked_unknown")
            for name in required:
                if name in nested and nested[name] != values[name]:
                    raise ProviderUnavailable("provider binding contract is blocked_unknown")

    def _action(
        self,
        contract: Dict[str, Any],
        run_id: str,
        operation: str,
        request: Dict[str, Any],
        sequence: int = 0,
    ) -> Dict[str, Any]:
        request = dict(request)
        for key in (
            "dispatcher", "complexity_band", "execution_engine", "engine_mode",
            "engine_evidence_ref", "authorization_digest", "plan_id", "plan_version",
        ):
            if key in contract:
                request[key] = contract[key]
        if contract.get("child_origin") == "worker_dispatch":
            request["origin"] = "worker_dispatch"
            request["child_binding"] = contract.get("child_binding")
        return self.store.request(
            operation=operation,
            provider=self.provider,
            run_id=run_id,
            issue_id=str(contract["node_id"]),
            role=str(contract["role"]),
            generation=int(contract["generation"]),
            native_tool=self._native_tool(operation),
            request=request,
            sequence=sequence,
        )

    @staticmethod
    def _routing_profile(contract: Dict[str, Any]) -> Optional[WorkerProfile]:
        """Validate the selected profile before any provider request."""
        raw = contract.get("worker_profile")
        child = contract.get("child_binding")
        child_raw = child.get("worker_profile") if isinstance(child, dict) else None
        if raw is None:
            raw = child_raw
        if raw is None:
            if contract.get("routing_required") is True or any(
                key in contract for key in ("issue_complexity", "model_probes", "models", "required_capabilities")
            ):
                raise ProviderUnavailable("worker model routing is missing")
            return None
        if isinstance(raw, WorkerProfile):
            profile = raw
        elif isinstance(raw, dict):
            try:
                profile = WorkerProfile.from_dict(raw)
            except (TypeError, ValueError) as error:
                raise ProviderUnavailable("worker model routing is invalid") from error
        else:
            raise ProviderUnavailable("worker model routing is invalid")
        if child_raw is not None and raw is not child_raw:
            try:
                child_profile = child_raw if isinstance(child_raw, WorkerProfile) else WorkerProfile.from_dict(child_raw)
            except (TypeError, ValueError) as error:
                raise ProviderUnavailable("child worker model routing is invalid") from error
            if child_profile != profile:
                raise ProviderUnavailable("worker model route conflicts with child binding")
        if isinstance(child, dict):
            for key, expected in (("model", profile.model), ("reasoning", profile.reasoning), ("route_digest", profile.route_digest)):
                if child.get(key) not in (None, "", expected):
                    raise ProviderUnavailable("worker model route conflicts with child binding")
        # A profile may only be used for the same Issue and child binding.
        basis_ref = profile.selection_basis.get("issue_complexity_ref")
        if basis_ref not in (None, contract.get("node_id")):
            raise ProviderUnavailable("worker model route Issue identity conflicts")
        return profile

    def _require_result(
        self,
        contract: Dict[str, Any],
        run_id: str,
        operation: str,
        request: Dict[str, Any],
    ) -> Dict[str, Any]:
        action = self._action(contract, run_id, operation, request)
        result = self.store.result(action["action_id"])
        if result is None:
            raise ProviderPending(
                "provider %s action is pending" % operation
            )
        return result

    def _validate_v39_create_binding(
        self, contract: Dict[str, Any], runtime_worktree: Path
    ) -> None:
        """Fail closed before a V3.9 provider create request is emitted.

        The create probe is still an external side effect, so every target
        constraint must come from the supervisor-owned contract.  In
        particular, the runtime ``worktree`` argument is only compared with
        the contract value and is never used to fill a missing value.
        """
        required = ("project_id", "worktree", "managed_root", "branch", "base_sha")
        values = {name: contract.get(name) for name in required}
        if (
            not isinstance(values["project_id"], str)
            or not values["project_id"].strip()
            or "\x00" in values["project_id"]
        ):
            raise ProviderUnavailable("provider binding project_id is blocked_unknown")

        for name in ("worktree", "managed_root"):
            value = values[name]
            if not isinstance(value, str) or not value.strip():
                raise ProviderUnavailable("provider binding %s is blocked_unknown" % name)
            path = Path(value)
            if not path.is_absolute():
                raise ProviderUnavailable("provider binding %s is blocked_unknown" % name)
            if "\x00" in value:
                raise ProviderUnavailable("provider binding %s is blocked_unknown" % name)

        branch = values["branch"]
        invalid_branch = (
            not isinstance(branch, str)
            or not branch.strip()
            or branch != branch.strip()
            or "\x00" in branch
            or any(char.isspace() for char in branch)
            or Path(branch).is_absolute()
            or branch.startswith("-")
            or branch.endswith("/")
            or branch.endswith(".")
            or branch.startswith(".")
            or branch.endswith(".lock")
            or branch in {".", "..", "@"}
            or "//" in branch
            or ".." in branch
            or "@{" in branch
            or any(char in branch for char in "~^:?*[\\")
            or any(part.startswith(".") or part.endswith(".lock") for part in branch.split("/"))
        )
        if invalid_branch:
            raise ProviderUnavailable("provider binding branch is blocked_unknown")

        base_sha = values["base_sha"]
        if not isinstance(base_sha, str) or len(base_sha) != 40 or any(
            char not in "0123456789abcdefABCDEF" for char in base_sha
        ):
            raise ProviderUnavailable("provider binding base_sha is blocked_unknown")
        if not isinstance(runtime_worktree, (str, Path)):
            raise ProviderUnavailable("provider binding path is blocked_unknown")

        try:
            contract_worktree = Path(values["worktree"]).resolve()
            managed_root = Path(values["managed_root"]).resolve()
            if contract_worktree == Path(contract_worktree.anchor):
                raise ValueError
            if managed_root == Path(managed_root.anchor):
                raise ValueError
            if contract_worktree.exists() and not contract_worktree.is_dir():
                raise ValueError
            if managed_root.exists() and not managed_root.is_dir():
                raise ValueError
            contract_worktree.relative_to(managed_root)
            runtime = Path(runtime_worktree)
            if not runtime.is_absolute() or runtime.resolve() != contract_worktree:
                raise ValueError
        except (OSError, RuntimeError, TypeError, ValueError):
            raise ProviderUnavailable("provider binding path is blocked_unknown")

        # The runtime argument is an identity check only.  It is never used
        # to repair or populate a missing contract worktree.
        # Accept legacy aliases only when they agree with the canonical
        # supervisor fields; never promote an alias or nested provider value.
        aliases = {"project": "project_id"}
        for alias, canonical in aliases.items():
            if alias in contract and contract[alias] != values[canonical]:
                raise ProviderUnavailable("provider binding alias is blocked_unknown")
        nested = contract.get("binding_contract")
        if nested is not None:
            if not isinstance(nested, dict):
                raise ProviderUnavailable("provider binding contract is blocked_unknown")
            for name in ("project_id", "worktree", "managed_root", "branch", "base_sha"):
                if name in nested and nested[name] != values[name]:
                    raise ProviderUnavailable("provider binding contract is blocked_unknown")

    def task_binding(
        self,
        contract: Dict[str, Any],
        worktree: Path,
        run_id: str,
        status: str,
    ) -> TaskBinding:
        node_id = str(contract["node_id"])
        role = str(contract["role"])
        generation = int(contract["generation"])
        try:
            existing = load_task_binding(
                self.paths, node_id, role, run_id=run_id
            )
        except FileNotFoundError:
            existing = None
        successor = bool(contract.get("successor"))
        v39 = binding_contract_enabled(contract)
        live_intent = contract.get("binding_intent")
        live_observation = contract.get("binding_observation")
        if v39:
            if not isinstance(live_intent, BindingIntent) or not isinstance(
                live_observation, BindingObservation
            ):
                # A task id is not known before provider create.  The only
                # permitted pre-create side effect is an explicitly-labelled
                # binding probe; it can never become a business write.
                if contract.get("binding_probe") is not True:
                    raise ProviderUnavailable("provider binding live evidence is missing")
            elif not runtime_binding_gate(contract).verified:
                raise ProviderUnavailable("provider binding preflight is blocked_unknown")
        if existing is not None and not successor:
            expected_allowlist = list(contract.get("files", []))
            if existing.allowlist != expected_allowlist:
                if (
                    not contract.get("continuation")
                    or not existing.allowlist
                    or not set(existing.allowlist).issubset(
                        set(expected_allowlist)
                    )
                ):
                    raise ValueError(
                        "provider continuation allowlist is not a narrow expansion"
                    )
                existing.allowlist = expected_allowlist
            existing.status = status
            existing.generation = generation
            if v39:
                existing.binding_intent = live_intent
                existing.binding_observation = live_observation
                verification = self.binding_gate(contract, existing)
                if not verification.verified:
                    raise ProviderUnavailable("provider continuation binding is blocked_unknown")
                existing.binding_state = verification.binding_state
                existing.business_write_allowed = verification.business_write_allowed
            return existing
        if existing is not None and existing.status not in {
            "stopped",
            "failed",
            "archived",
        }:
            raise ValueError("visible successor requires a stopped predecessor")
        if v39 and contract.get("binding_probe") is not True:
            raise ProviderUnavailable(
                "V3.9 provider create requires explicit binding_probe=true"
            )
        if v39:
            self._validate_v39_create_binding(contract, worktree)
        predecessor = contract.get("predecessor_task_id")
        if predecessor is None and existing is not None:
            predecessor = existing.task_id

        project_id = contract.get("project_id")
        if not isinstance(project_id, str) or not project_id:
            raise ValueError("visible provider contract requires project_id")
        prompt = "请执行 {} 任务，Issue {}。{}".format(
            role,
            node_id,
            self._consistency_instruction(contract),
        )
        create_request = {
            "prompt": prompt,
            "target": {
                "type": "project",
                "projectId": project_id,
                "environment": {"type": "local"},
            },
        }
        profile = self._routing_profile(contract)
        if profile is not None:
            thinking = provider_thinking_for(profile.reasoning)
            create_request.update(
                {
                    "model": profile.model,
                    "thinking": thinking,
                    "issue_id": node_id,
                    "route_digest": profile.route_digest,
                    "worker_profile": profile.to_dict(),
                }
            )
        topology = str(contract.get("topology") or DEFAULT_TOPOLOGY)
        if topology not in _TOPOLOGIES:
            raise ValueError("task binding topology is invalid")
        # Preflight the rulings this visible-only bridge can never satisfy
        # *before* the create side effect: a background ruling would
        # otherwise leak an armed, unregistered visible task on the desktop
        # (R3 P1-1), and a visible-sdd ruling without its protocol pointer
        # would arm a session that does not know the rules it must follow.
        if topology == "background":
            raise ValueError("background topology requires background mode")
        if topology == "visible-sdd":
            if role != "developer":
                raise ValueError("visible-sdd topology only binds a developer")
            sdd_protocol = contract.get("sdd_protocol")
            if not isinstance(sdd_protocol, str) or not sdd_protocol.strip():
                raise ValueError("visible-sdd dispatch requires an sdd_protocol pointer")
            # The desktop session servicing the mailbox must know it is
            # creating the single visible SDD worker session and which
            # protocol that session has to follow.
            create_request["topology"] = topology
            create_request["sdd_protocol"] = sdd_protocol
        if v39:
            # Keep the provider request bound to the same supervisor target
            # that will later be checked against live binding evidence.  These
            # are constraints, not evidence; the provider response can never
            # promote them to verified state by echoing them back.
            binding_contract = {
                "project_id": project_id,
                "worktree": contract.get("worktree"),
                "managed_root": contract.get("managed_root"),
                "branch": contract.get("branch"),
                "base_sha": contract.get("base_sha"),
            }
            create_request["target"]["binding_contract"] = binding_contract
            # Keep the original top-level shape for bridge consumers that
            # already inspect probe constraints there.
            create_request["binding"] = {
                key: binding_contract[key]
                for key in ("worktree", "managed_root", "branch", "base_sha")
            }
            create_request.update(
                {
                    "binding_probe": True,
                    "purpose": "binding_probe",
                    "business_write_allowed": False,
                }
            )
        if successor:
            create_request["successor"] = True
            create_request["predecessor_task_id"] = predecessor
        created = self._require_result(
            contract, run_id, "create", create_request
        )
        routed_profile = profile is not None and (
            contract.get("routing_required") is True
            or "selected_model" in profile.selection_basis
        )
        if routed_profile:
            actual_model = created.get("actual_model")
            actual_thinking = created.get("actual_thinking")
            if (
                not isinstance(actual_model, str)
                or not actual_model.strip()
                or actual_model.casefold() == "unknown"
                or actual_model != profile.model
            ):
                raise ProviderUnavailable("provider actual model is blocked_unknown")
            if (
                not isinstance(actual_thinking, str)
                or not actual_thinking.strip()
                or actual_thinking.casefold() == "unknown"
                or actual_thinking != create_request["thinking"]
            ):
                raise ProviderUnavailable("provider actual thinking is blocked_unknown")
        binding_data = created.get("binding")
        if not isinstance(binding_data, dict):
            raise ValueError("provider create result has no verified binding")
        task_id = binding_data.get("threadId") or binding_data.get("task_id")
        host = binding_data.get("hostId") or binding_data.get("host")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("provider create result has no task identity")
        if not isinstance(host, str) or not host:
            raise ValueError("provider create result has no host identity")
        if successor and predecessor and task_id == predecessor:
            raise ValueError("provider successor reused predecessor identity")

        # Once create returns a provider task id, no further provider action
        # may run until supervisor-owned protected evidence binds that id.
        # A probe result is not permission to locate, enter, or write.
        if v39:
            if not isinstance(live_intent, BindingIntent) or not isinstance(
                live_observation, BindingObservation
            ):
                raise ProviderUnavailable("provider probe returned no live binding evidence")
            if live_intent.task_id != task_id or live_intent.host_id != host:
                raise ProviderUnavailable("provider probe identity is not supervisor verified")
            lease = read_writer_lease(self.paths, node_id, live_intent.worktree)
            if lease is None or live_observation.lease != lease:
                raise ProviderUnavailable("provider probe lease is not supervisor verified")
            if not runtime_binding_gate(contract).verified:
                raise ProviderUnavailable("provider probe binding is blocked_unknown")

        located = self._require_result(
            contract,
            run_id,
            "locate",
            {"threadId": task_id},
        )
        if located.get("located") is not True:
            raise ValueError("provider task cannot be located")
        visible = self._require_result(
            contract,
            run_id,
            "visibility",
            {
                "targets": [{"threadId": task_id, "hostId": host}],
                "timeoutMs": 0,
            },
        )
        if visible.get("visible") is not True or visible.get("direct_enter") is not True:
            raise ValueError("provider task visibility is not verified")
        binding = TaskBinding(
            provider=self.provider,
            mode="visible",
            issue_id=node_id,
            role=role,
            # Propagate the supervisor-ruled dispatch topology.  The
            # TaskBinding constructor is fail-closed: a background ruling
            # cannot be satisfied by this visible-only bridge, and a
            # visible-sdd ruling only binds a developer session.
            topology=str(contract.get("topology") or DEFAULT_TOPOLOGY),
            limitations=list(contract.get("dispatch_limitations", [])),
            task_id=task_id,
            host=host,
            worktree=str(contract.get("worktree") or worktree),
            branch=str(contract.get("branch", "")),
            status_file=str(contract.get("status_file", "")),
            handoff_file=str(contract.get("handoff_file", "")),
            threadId=task_id if self.provider == CODEX_PROVIDER else None,
            hostId=host if self.provider == CODEX_PROVIDER else None,
            run_id=run_id,
            status=status,
            visible=True,
            generation=generation,
            allowlist=list(contract.get("files", [])),
            capability_contract_digest=contract.get("capability_contract_digest"),
            successor_of=predecessor if successor else None,
        )
        if profile is not None:
            binding.route_digest = profile.route_digest
            binding.model = profile.model
            binding.reasoning = profile.reasoning
        # Provider-returned host/project/lease/cursor and nested dictionaries
        # are self-report only.  Only supervisor-injected protected objects
        # from the contract can attest this final task binding.
        if v39:
            if not isinstance(live_intent, BindingIntent) or not isinstance(
                live_observation, BindingObservation
            ):
                raise ProviderUnavailable("provider task requires live binding evidence")
            if live_intent.task_id != task_id or live_intent.host_id != host:
                raise ProviderUnavailable("provider task identity disagrees with live evidence")
            for key, expected in (
                ("project_id", live_observation.project_id),
                ("managed_root", live_observation.managed_root),
                ("worktree", live_observation.worktree),
                ("branch", live_observation.branch),
                ("base_sha", live_observation.base_sha),
                ("head_sha", live_observation.head_sha),
                ("clean", live_observation.clean),
                ("cursor", live_observation.cursor),
            ):
                if key in binding_data and binding_data.get(key) != expected:
                    raise ProviderUnavailable("provider self-report conflicts with live evidence")
            binding.binding_intent = live_intent
            binding.binding_observation = live_observation
            verification = self.binding_gate(contract, binding)
            if not verification.verified:
                raise ProviderUnavailable("provider task binding is blocked_unknown")
            binding.binding_state = verification.binding_state
            binding.business_write_allowed = verification.business_write_allowed
        return binding

    @staticmethod
    def _handle_id(contract: Dict[str, Any], run_id: str) -> str:
        basis = "\0".join(
            (
                run_id,
                str(contract["node_id"]),
                str(contract["role"]),
                str(contract["generation"]),
            )
        )
        return "bridge-" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]

    def _handle_path(self, handle_id: str) -> Path:
        directory = self.store._directory("handles")
        return directory / (handle_id + ".json")

    def binding_gate(self, contract: Dict[str, Any], binding: TaskBinding):
        """Run the V3.9 binding gate at the provider boundary."""
        verification = runtime_binding_gate(contract, binding)
        if not binding_contract_enabled(contract) or not verification.verified:
            return verification
        intent = binding.binding_intent
        observation = binding.binding_observation
        lease = read_writer_lease(self.paths, str(contract["node_id"]), intent.worktree)
        if lease is None or observation.lease != lease:
            return BindingVerification("blocked_unknown", False, [], ["lease"])
        return verification

    # Named recovery probes are intentionally idempotent and read-only.  A
    # timeout or malformed response therefore remains ``blocked_unknown``;
    # callers may retry the same task without allocating a successor writer.
    def provider_binding_probe(self, contract: Dict[str, Any], binding: TaskBinding):
        return self.binding_gate(contract, binding)

    def binding_bootstrap(self, contract: Dict[str, Any], binding: TaskBinding, worktree: Path):
        """Safely repair detached/wrong-branch metadata without content reset.

        Only a clean worktree at the requested base may be attached to a new
        node branch.  Existing branch occupancy, path drift, dirty content or
        a wrong base remain blocked; no reset/clean/stash/checkout is used.
        """
        if not binding_contract_enabled(contract):
            return self.binding_gate(contract, binding)

        def blocked(reason: str):
            return BindingVerification("blocked_unknown", False, [reason], [])

        def read_git(root_path: Path, target_branch: str):
            def git(*args):
                return subprocess.run(
                    ["git", "-C", str(root_path), *args],
                    check=False,
                    capture_output=True,
                    text=True,
                )

            commands = {
                "head": ("rev-parse", "HEAD"),
                "status": ("status", "--porcelain", "--untracked-files=all"),
                "symbolic": ("symbolic-ref", "--short", "-q", "HEAD"),
                "ref": ("show-ref", "--verify", "--quiet", "refs/heads/" + target_branch),
                "worktrees": ("worktree", "list", "--porcelain"),
            }
            result = {name: git(*args) for name, args in commands.items()}
            if result["head"].returncode != 0 or result["status"].returncode != 0:
                return None
            if result["symbolic"].returncode not in (0, 1):
                return None
            if result["ref"].returncode not in (0, 1):
                return None
            if result["worktrees"].returncode != 0:
                return None
            return result

        def worktree_has_root(text: str, root_path: Path) -> bool:
            expected = "worktree " + str(root_path)
            return any(line.strip() == expected for line in text.splitlines())

        def worktree_owns_branch(text: str, root_path: Path, target_branch: str) -> bool:
            expected_root = "worktree " + str(root_path)
            expected_branch = "branch refs/heads/" + target_branch
            for block in text.strip().split("\n\n"):
                lines = {line.strip() for line in block.splitlines()}
                if expected_root in lines and expected_branch in lines:
                    return True
            return False
        root = Path(worktree)
        if isinstance(contract.get("binding_intent"), BindingIntent) and isinstance(
            contract.get("binding_observation"), BindingObservation
        ):
            binding.binding_intent = contract["binding_intent"]
            binding.binding_observation = contract["binding_observation"]
        live_intent = binding.binding_intent
        managed_root = contract.get("managed_root") or (
            live_intent.managed_root if isinstance(live_intent, BindingIntent) else None
        )
        branch = contract.get("branch") or binding.branch
        base_sha = contract.get("base_sha")
        if not isinstance(managed_root, str) or not isinstance(branch, str) or not isinstance(base_sha, str):
            return blocked("git_binding_contract")
        try:
            root = root.resolve(strict=True)
            managed = Path(managed_root).resolve(strict=True)
            root.relative_to(managed)
            if binding.worktree:
                bound_worktree = Path(binding.worktree)
                if not bound_worktree.is_absolute():
                    bound_worktree = self.paths.root / bound_worktree
                if bound_worktree.resolve() != root:
                    return blocked("worktree")
            node_id = str(contract.get("node_id") or binding.issue_id)
            run_id = str(contract.get("run_id") or binding.run_id or "")
            if not run_id or not binding.task_id or not binding.host:
                return blocked("task_identity")
            lease = read_writer_lease(self.paths, node_id, str(root))
            if lease is None:
                return blocked("lease")
            state = read_git(root, branch)
            if state is None:
                return blocked("git_observation")
            head = state["head"]
            status = state["status"]
            current = state["symbolic"]
            refs = state["ref"]
            occupied = state["worktrees"]
            if status.stdout:
                return blocked("dirty")
            if head.stdout.strip().lower() != base_sha.lower():
                return blocked("base_sha")
            branch_marker = "branch refs/heads/" + branch
            if not worktree_has_root(occupied.stdout, root):
                return blocked("worktree_root")
            if current.stdout.strip() == branch and not worktree_owns_branch(
                occupied.stdout, root, branch
            ):
                return blocked("worktree_branch")
            if branch_marker in occupied.stdout and current.stdout.strip() != branch:
                return blocked("branch_occupied")
            if refs.returncode == 0:
                # A branch already present may belong to another worktree;
                # never steal or rewrite it.
                if current.stdout.strip() == branch:
                    pass
                elif branch_marker in occupied.stdout:
                    return blocked("branch_occupied")
                else:
                    return blocked("branch_exists")
            elif current.stdout.strip() != branch:
                switched = subprocess.run(
                    ["git", "-C", str(root), "switch", "-c", branch, base_sha],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if switched.returncode != 0:
                    return blocked("git_switch")
                # Never reuse pre-switch stdout.  All binding fields are
                # re-read after the metadata mutation before re-gating.
                state = read_git(root, branch)
                if state is None or state["status"].stdout:
                    return blocked("post_read")
                if state["head"].stdout.strip().lower() != base_sha.lower():
                    return blocked("post_base_sha")
                if state["symbolic"].stdout.strip() != branch:
                    return blocked("post_branch")
                if not worktree_owns_branch(state["worktrees"].stdout, root, branch):
                    return blocked("post_worktree")
                head = state["head"]
            has_provenance = isinstance(binding.binding_intent, BindingIntent) and isinstance(
                binding.binding_observation, BindingObservation
            )
            if not has_provenance:
                project_id = contract.get("project_id")
                if not isinstance(project_id, str) or not project_id:
                    return blocked("project_id")
                wait_request = {
                    "purpose": "binding_probe",
                    "business_write_allowed": False,
                    "targets": [
                        {"threadId": binding.task_id, "hostId": binding.host}
                    ],
                    "timeoutMs": 0,
                }
                try:
                    action = self._action(
                        contract, run_id, "wait", wait_request, sequence=0
                    )
                    result = self.store.result(action["action_id"])
                except (OSError, TypeError, ValueError) as error:
                    return blocked("wait_result")
                if result is None:
                    raise ProviderPending("binding recovery wait probe is pending")
                if not isinstance(result, dict):
                    return blocked("wait_result")
                polls = result.get("polls")
                candidates = []
                if isinstance(polls, list):
                    candidates.extend(item for item in polls if isinstance(item, dict))
                poll = result.get("poll")
                if isinstance(poll, dict):
                    candidates.append(poll)
                matched = None
                for candidate in reversed(candidates):
                    thread = candidate.get("thread")
                    if not isinstance(thread, dict):
                        continue
                    if thread.get("id") != binding.task_id:
                        continue
                    if thread.get("hostId") != binding.host:
                        continue
                    cursor = candidate.get("cursor")
                    if isinstance(cursor, str) and cursor:
                        matched = (candidate, cursor)
                        break
                if matched is None:
                    return blocked("wait_result")
                _poll, cursor = matched
                try:
                    # The wait result is a separate observation boundary.  Do
                    # not construct evidence from the pre-wait Git stdout;
                    # re-read every Git field before binding the cursor.
                    state = read_git(root, branch)
                    if state is None or state["status"].stdout:
                        return blocked("post_read")
                    if state["head"].stdout.strip().lower() != base_sha.lower():
                        return blocked("post_base_sha")
                    if not worktree_has_root(state["worktrees"].stdout, root):
                        return blocked("post_worktree")
                    if state["symbolic"].stdout.strip() != branch:
                        return blocked("post_branch")
                    if not worktree_owns_branch(
                        state["worktrees"].stdout, root, branch
                    ):
                        return blocked("post_worktree")
                    head = state["head"]
                    cursor_observation = WaitThreadsCursorObservation.from_wait_threads(
                        binding.task_id, binding.host, cursor
                    )
                    fresh_lease = read_writer_lease(self.paths, node_id, str(root))
                    if fresh_lease is None or fresh_lease != lease:
                        return blocked("lease")
                    live_head = head.stdout.strip().lower()
                    contract_conflicts = []
                    expected_head = contract.get("head_sha")
                    if expected_head not in (None, "") and expected_head != live_head:
                        contract_conflicts.append("head_sha")
                    expected_clean = contract.get("clean")
                    if expected_clean is not None and expected_clean is not True:
                        contract_conflicts.append("clean")
                    expected_cursor = contract.get("cursor")
                    if expected_cursor not in (None, "") and expected_cursor != cursor:
                        contract_conflicts.append("cursor")
                    expected_lease = contract.get("lease")
                    if expected_lease not in (None, ""):
                        live_lease = fresh_lease.to_dict()
                        if hasattr(expected_lease, "to_dict"):
                            expected_lease = expected_lease.to_dict()
                        if expected_lease != live_lease:
                            contract_conflicts.append("lease")
                    if contract_conflicts:
                        return BindingVerification(
                            "blocked_unknown", False, [], contract_conflicts
                        )
                    intent = BindingIntent(
                        project_id=project_id,
                        task_id=binding.task_id,
                        host_id=binding.host,
                        node_id=node_id,
                        worktree=str(root),
                        managed_root=str(managed),
                        branch=branch,
                        base_sha=base_sha,
                        head_sha=live_head,
                        clean=True,
                        lease_id=fresh_lease.lease_id,
                        cursor=cursor,
                    )
                    observation = BindingObservation(
                        project_id=project_id,
                        task_id=binding.task_id,
                        host_id=binding.host,
                        node_id=node_id,
                        worktree=str(root),
                        managed_root=str(managed),
                        branch=branch,
                        base_sha=base_sha,
                        head_sha=live_head,
                        clean=True,
                        lease=fresh_lease,
                        cursor=cursor,
                        source="codex_app__wait_threads",
                        cursor_source="codex_app__wait_threads",
                        cursor_task_id=binding.task_id,
                        cursor_host_id=binding.host,
                        cursor_lineage=cursor_observation.lineage,
                        cursor_observation=cursor_observation,
                    )
                except (TypeError, ValueError):
                    return blocked("wait_result")
                binding.binding_intent = intent
                binding.binding_observation = observation
                binding.cursor = cursor
                contract = dict(contract)
                contract.update(
                    {
                        "binding_intent": intent,
                        "binding_observation": observation,
                        "managed_root": str(managed),
                        "worktree": str(root),
                        "branch": branch,
                        "base_sha": base_sha,
                        "head_sha": live_head,
                        "clean": True,
                        "lease": fresh_lease,
                        "cursor": cursor,
                    }
                )
            if isinstance(binding.binding_intent, BindingIntent) and isinstance(
                binding.binding_observation, BindingObservation
            ):
                # Refresh immutable Git metadata after a safe branch attach;
                # lease/cursor provenance is either the current protected
                # evidence above or the protected evidence supplied by the
                # already-verified contract.
                binding.binding_intent = replace(
                    binding.binding_intent, branch=branch, worktree=str(root), base_sha=base_sha
                )
                binding.binding_observation = replace(
                    binding.binding_observation,
                    branch=branch,
                    worktree=str(root),
                    base_sha=base_sha,
                    head_sha=head.stdout.strip().lower(),
                )
                contract = dict(contract)
                contract["binding_intent"] = binding.binding_intent
                contract["binding_observation"] = binding.binding_observation
        except (OSError, ValueError, subprocess.SubprocessError):
            return blocked("git_observation")
        verification = runtime_binding_gate(contract, binding)
        if not verification.verified:
            return verification
        return verification

    def start(self, contract: dict, worktree: Path) -> RunHandle:
        # Reject direct complex starts before task-binding reads or Provider I/O.
        require_complex_monitor_dispatch(contract, self.paths)
        run_id = str(contract.get("run_id", ""))
        if not run_id:
            raise ValueError("provider runner requires run_id")
        handle = RunHandle(self._handle_id(contract, run_id))
        binding = load_task_binding(
            self.paths,
            str(contract["node_id"]),
            str(contract["role"]),
            run_id=run_id,
        )
        expected_capability_digest = contract.get("capability_contract_digest")
        if expected_capability_digest and binding.capability_contract_digest != expected_capability_digest:
            raise ValueError("task binding capability contract digest is stale")
        if contract.get("successor") is True:
            predecessor = contract.get("predecessor_task_id")
            if predecessor and binding.task_id == predecessor:
                raise ValueError("successor task identity reused the predecessor")
        if binding_contract_enabled(contract):
            live_intent = contract.get("binding_intent")
            live_observation = contract.get("binding_observation")
            if isinstance(live_intent, BindingIntent) and isinstance(
                live_observation, BindingObservation
            ):
                binding.binding_intent = live_intent
                binding.binding_observation = live_observation
        profile = self._routing_profile(contract)
        if profile is not None:
            for field_name, expected in (
                ("route_digest", profile.route_digest),
                ("model", profile.model),
                ("reasoning", profile.reasoning),
            ):
                if getattr(binding, field_name, None) not in (None, "", expected):
                    raise ProviderUnavailable("task binding worker model route is stale")
        verification = self.binding_gate(contract, binding)
        if binding_contract_enabled(contract) and not verification.verified:
            raise ProviderUnavailable(
                "provider binding gate blocked_unknown: missing={} conflicts={}".format(
                    ",".join(verification.missing), ",".join(verification.conflicts)
                )
            )
        metadata = {
            "schema_version": 1,
            "handle_id": handle.run_id,
            "run_id": run_id,
            "node_id": str(contract["node_id"]),
            "role": str(contract["role"]),
            "task_id": binding.task_id,
            "host": binding.host,
            "generation": int(contract["generation"]),
            "pending_action": None,
            "pending_operation": None,
            "wait_sequence": 0,
            "cursor": binding.cursor,
            "terminal_confirmed": False,
            "successor": bool(contract.get("successor")),
            "predecessor_task_id": contract.get("predecessor_task_id"),
            "capability_contract_digest": expected_capability_digest,
            "dispatcher": contract.get("dispatcher"),
            "complexity_band": contract.get("complexity_band"),
            "execution_engine": contract.get("execution_engine"),
            "engine_mode": contract.get("engine_mode"),
            "engine_evidence_ref": contract.get("engine_evidence_ref"),
            "authorization_digest": contract.get("authorization_digest"),
            "plan_id": contract.get("plan_id"),
            "plan_version": contract.get("plan_version"),
        }
        if profile is not None:
            metadata.update(
                {
                    "model": profile.model,
                    "reasoning": profile.reasoning,
                    "thinking": provider_thinking_for(profile.reasoning),
                    "route_digest": profile.route_digest,
                }
            )
        if contract.get("continuation"):
            request = {
                "threadId": binding.task_id,
                "hostId": binding.host,
                "prompt": "请继续处理 Issue {}。{}".format(
                    contract["node_id"], self._consistency_instruction(contract)
                ),
            }
            action = self._action(contract, run_id, "resume", request)
            metadata["pending_action"] = action["action_id"]
            metadata["pending_operation"] = "resume"
        self.store._atomic(self._handle_path(handle.run_id), metadata)
        return handle

    def is_pending(self, handle: RunHandle) -> bool:
        metadata = self.store._read(self._handle_path(handle.run_id))
        action_id = metadata.get("pending_action")
        return bool(action_id and self.store.result(action_id) is None)

    def poll(self, handle: RunHandle):
        metadata = self.store._read(self._handle_path(handle.run_id))
        claims = {
            "node_id": metadata["node_id"],
            "role": metadata["role"],
            "task_id": metadata["task_id"],
            "handle_id": handle.run_id,
            "generation": metadata["generation"],
        }
        pending_action = metadata.get("pending_action")
        if pending_action:
            result = self.store.result(pending_action)
            if result is None:
                operation = str(metadata.get("pending_operation") or "action")
                return [
                    RunEvent(
                        "visibility_unknown",
                        {**claims, "reason": "provider %s is pending" % operation},
                    )
                ]
            operation = metadata.get("pending_operation")
            if operation == "wait":
                return self._wait_result(handle, metadata, claims, result)
            if operation != "resume" or result.get("resumed") is not True:
                return [
                    RunEvent(
                        "visibility_unknown",
                        {**claims, "reason": "provider resume is unverified"},
                    )
                ]
            metadata["pending_action"] = None
            metadata["pending_operation"] = None
            self.store._atomic(self._handle_path(handle.run_id), metadata)

        contract = {
            "node_id": metadata["node_id"],
            "role": metadata["role"],
            "generation": metadata["generation"],
            "dispatcher": metadata.get("dispatcher"),
            "complexity_band": metadata.get("complexity_band"),
            "execution_engine": metadata.get("execution_engine"),
            "engine_mode": metadata.get("engine_mode"),
            "engine_evidence_ref": metadata.get("engine_evidence_ref"),
            "authorization_digest": metadata.get("authorization_digest"),
            "plan_id": metadata.get("plan_id"),
            "plan_version": metadata.get("plan_version"),
        }
        wait_request = {
            "targets": [
                {
                    "threadId": metadata["task_id"],
                    "hostId": metadata["host"],
                }
            ],
            "timeoutMs": 0,
        }
        cursor = metadata.get("cursor")
        if isinstance(cursor, str) and cursor:
            wait_request["targets"][0]["afterCursor"] = cursor
        sequence = int(metadata.get("wait_sequence", 0)) + 1
        action = self._action(
            contract,
            metadata["run_id"],
            "wait",
            wait_request,
            sequence=sequence,
        )
        metadata["wait_sequence"] = sequence
        metadata["pending_action"] = action["action_id"]
        metadata["pending_operation"] = "wait"
        self.store._atomic(self._handle_path(handle.run_id), metadata)
        result = self.store.result(action["action_id"])
        if result is None:
            return [
                RunEvent(
                    "visibility_unknown",
                    {**claims, "reason": "provider wait is pending"},
                )
            ]
        return self._wait_result(handle, metadata, claims, result)

    def _wait_result(
        self,
        handle: RunHandle,
        metadata: Dict[str, Any],
        claims: Dict[str, Any],
        result: Dict[str, Any],
    ):
        status = result.get("status")
        cursor = result.get("cursor")
        if cursor is not None and (
            not isinstance(cursor, str)
            or not cursor
            or len(cursor) > 4096
            or "\x00" in cursor
        ):
            return [
                RunEvent(
                    "visibility_unknown",
                    {**claims, "reason": "provider cursor is invalid"},
                )
            ]
        if cursor is not None:
            binding = load_task_binding(
                self.paths,
                metadata["node_id"],
                metadata["role"],
                run_id=metadata["run_id"],
            )
            binding.cursor = cursor
            save_task_binding(self.paths, binding)
            metadata["cursor"] = cursor
        metadata["pending_action"] = None
        metadata["pending_operation"] = None
        self.store._atomic(self._handle_path(handle.run_id), metadata)
        if status in {"timeout", "unknown", "error"}:
            return [RunEvent("visibility_unknown", {**claims, "reason": str(status)})]
        event_name = result.get("event")
        if event_name not in {
            "complete",
            "delivered",
            "accepted",
            "review_finding",
            "failed",
            "stopped",
        }:
            return [
                RunEvent(
                    "visibility_unknown",
                    {**claims, "reason": "provider event is unsupported"},
                )
            ]
        if status not in {"complete", "completed", "failed", "stopped"}:
            return [
                RunEvent(
                    "visibility_unknown",
                    {**claims, "reason": "provider terminal state is unverified"},
                )
            ]
        metadata["terminal_confirmed"] = True
        self.store._atomic(self._handle_path(handle.run_id), metadata)
        data = dict(claims)
        # ``in_session_review`` carries the visible-sdd worker session's
        # review clearance (protocol/evidence_ref/p0-p2) that Monitor needs
        # to accept a single-session node (ISSUE-04).
        for key in ("evidence", "finding", "in_contract", "consistency",
                    "delivery_evidence", "completion_marker", "delivery_path",
                    "thread_status", "in_session_review"):
            if key in result:
                data[key] = result[key]
        return [RunEvent(str(event_name), data)]

    def stop(self, handle: RunHandle) -> None:
        metadata = self.store._read(self._handle_path(handle.run_id))
        if metadata.get("terminal_confirmed") is True:
            return
        pending_action = metadata.get("pending_action")
        if not isinstance(pending_action, str) or not pending_action:
            raise ProviderPending(
                "provider stop requires a pending terminal wait reconciliation"
            )
        if metadata.get("pending_operation") != "wait":
            raise ProviderPending(
                "provider stop cannot reconcile a non-terminal provider action"
            )
        result = self.store.result(pending_action)
        if result is None:
            raise ProviderPending("provider terminal wait is still pending")
        if result.get("status") not in {"stopped", "complete", "completed"} or result.get(
            "event"
        ) != "stopped":
            raise ProviderPending("provider terminal wait did not prove a stop")
        cursor = result.get("cursor")
        if cursor is not None and (
            not isinstance(cursor, str)
            or not cursor
            or len(cursor) > 4096
            or "\x00" in cursor
        ):
            raise ProviderPending("provider terminal wait cursor is invalid")
        claims = {
            "node_id": metadata["node_id"],
            "role": metadata["role"],
            "task_id": metadata["task_id"],
            "handle_id": handle.run_id,
            "generation": metadata["generation"],
        }
        events = self._wait_result(handle, metadata, claims, result)
        if len(events) != 1 or events[0].event != "stopped":
            raise ProviderPending("provider terminal wait did not prove a stop")
