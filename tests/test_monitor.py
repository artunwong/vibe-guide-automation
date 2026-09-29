from copy import deepcopy
import hashlib
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide.authorization import authorize, build_authorization_card
from vibe_guide.contracts import RunEvent, RunHandle
from vibe_guide.models import AgentCapabilities, DAGNode, Plan, node_branch
from vibe_guide.monitor import Monitor, VISIBLE_SDD_PROTOCOL_REF
from vibe_guide.adapters.task_provider import ProviderPending
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners.fake import FakeRunner
from vibe_guide.state import (
    acquire_writer_lease,
    append_event,
    load_events,
    load_snapshot,
    release_writer_lease,
    save_snapshot,
)
from vibe_guide.task_registry import TaskBinding, load_task_binding, save_task_binding
from vibe_guide.capability_contract import build_contract, save_contract


class StartResponseLostRunner(FakeRunner):
    def start(self, contract, worktree):
        super().start(contract, worktree)
        raise ConnectionError("start response lost")


class ProviderRetryRunner(FakeRunner):
    def __init__(self):
        super().__init__()
        self.binding_attempts = 0

    def task_binding(self, contract, worktree, run_id, status):
        self.binding_attempts += 1
        if self.binding_attempts == 1:
            raise ProviderPending("provider capability bridge is pending")
        return TaskBinding(
            provider="fake",
            mode="background",
            issue_id=contract["node_id"],
            role=contract["role"],
            task_id=contract["task_id"],
            worktree=str(worktree),
            branch=contract.get("branch", "branch-" + contract["node_id"]),
            run_id=run_id,
            status=status,
            generation=contract["generation"],
        )


class PollResponseLostRunner(FakeRunner):
    def poll(self, handle: RunHandle):
        raise ConnectionError("poll response lost")


class ReviewerBindingFailureRunner(FakeRunner):
    def task_binding(self, contract, worktree, run_id, status):
        if contract["role"] == "reviewer":
            raise ValueError("reviewer binding unavailable during recovery")
        return TaskBinding(
            provider="fake",
            mode="background",
            issue_id=contract["node_id"],
            role=contract["role"],
            task_id=contract["task_id"],
            worktree=str(worktree),
            branch=contract.get("branch", "branch-" + contract["node_id"]),
            run_id=run_id,
            status=status,
            generation=contract["generation"],
        )


class DuplicateHandleRunner(FakeRunner):
    def start(self, contract, worktree):
        super().start(contract, worktree)
        return RunHandle("shared-handle")


class UnclaimedEventRunner(FakeRunner):
    def poll(self, handle: RunHandle):
        return [RunEvent("delivered", {"evidence": "unclaimed"})]


class SecretStartResponseLostRunner(FakeRunner):
    def start(self, contract, worktree):
        super().start(contract, worktree)
        raise ConnectionError("START_EXCEPTION_SECRET_SENTINEL")


class SecretPollResponseLostRunner(FakeRunner):
    def poll(self, handle: RunHandle):
        raise ConnectionError("POLL_EXCEPTION_SECRET_SENTINEL")


class ObservedContinuationRunner(FakeRunner):
    def __init__(self, task_id=None, cursor=None):
        super().__init__()
        self.observed_task_id = task_id
        self.observed_cursor = cursor

    def task_binding(self, contract, worktree, run_id, status):
        return TaskBinding(
            provider="runner",
            mode="background",
            issue_id=contract["node_id"],
            role=contract["role"],
            task_id=self.observed_task_id or contract["task_id"],
            worktree=str(worktree),
            run_id=run_id,
            status=status,
            generation=contract["generation"],
            cursor=self.observed_cursor,
        )


def node(node_id, depends_on=None, worker=None):
    return DAGNode(
        node_id,
        node_id,
        depends_on or [],
        [],
        "g1",
        {
            "files": [node_id + ".py"],
            "worker": worker or "worker-" + node_id,
            "worktree": ".worktrees/" + node_id,
            "worker_profile": {"worker": "codex", "model": "test", "reasoning": "normal", "fallbacks": [], "selection_basis": {"issue_complexity_ref": node_id, "complexity_band": "standard", "risk_tags": [], "availability_evidence": "test"}, "writer": "writer", "worktree": ".worktrees/" + node_id, "branch": "branch-" + node_id, "allowlist": [node_id + ".py"]},
        },
        "ready",
    )


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = ProjectPaths(Path(self.temporary.name))
        (self.paths.vibe / "state.json").parent.mkdir(parents=True, exist_ok=True)
        (self.paths.vibe / "state.json").write_text('{"workflow_version": 2, "session_gate": "s0_required"}\n', encoding="utf-8")
        save_contract(
            self.paths,
            build_contract(self.paths.root, provider="fake", host_id="local"),
        )
        self.capabilities = AgentCapabilities("fake", True, True, True, True, True, "full")

    def tearDown(self):
        self.temporary.cleanup()

    def _reconciliation_fixture(self):
        current_node = node("n1")
        current_node.contract.update({"status_file": "status.txt", "handoff_file": "handoff.md"})
        monitor, record = self.authorized_monitor([current_node])
        runner = FakeRunner()
        snapshot = monitor.start(record, runner)
        developer = load_task_binding(self.paths, "n1", "developer", run_id=snapshot.run_id)
        reviewer = TaskBinding(
            provider="fake", mode="background", issue_id="n1", role="reviewer",
            task_id="reviewer:n1", worktree=".worktrees/n1", branch=node_branch("n1"),
            status_file="status.txt", handoff_file="handoff.md", run_id=snapshot.run_id,
            status="review", generation=1,
        )
        save_task_binding(self.paths, reviewer)
        worktree = self.paths.root / ".worktrees" / "n1"
        worktree.mkdir(parents=True, exist_ok=True)
        (worktree / "status.txt").write_text("done\n", encoding="utf-8")
        (worktree / "handoff.md").write_text("handoff\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=worktree, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=worktree, check=True)
        subprocess.run(["git", "add", "."], cwd=worktree, check=True)
        subprocess.run(["git", "commit", "-qm", "evidence"], cwd=worktree, check=True)
        subprocess.run(["git", "branch", "-M", node_branch("n1")], cwd=worktree, check=True)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=worktree, check=True, capture_output=True, text=True).stdout.strip()
        current = snapshot.nodes["n1"]
        current.update({"status": "blocked_unknown", "active_role": None, "active_task": None,
                        "reviewer_started": True, "reviewer_identity": reviewer.task_id,
                        "review_generation": 1, "quarantine": {"run_id": snapshot.run_id, "handle_id": None, "reason": "evidence"}})
        snapshot.handles.clear()
        save_snapshot(self.paths, snapshot)
        def digest(path):
            return hashlib.sha256(path.read_bytes()).hexdigest()
        package = {
            "schema_version": 1, "run_id": snapshot.run_id, "plan_id": snapshot.plan_id,
            "plan_revision": snapshot.plan_version, "authorization_digest": snapshot.authorization_digest,
            "node_contract_digest": snapshot.node_contract_digest, "nodes": [{
                "node_id": "n1",
                "developer": {"task_id": developer.task_id, "generation": developer.generation,
                               "worktree": developer.worktree, "branch": developer.branch,
                               "status": developer.status, "head": head, "status_file": {"path": "status.txt", "sha256": digest(worktree / "status.txt")},
                               "handoff_file": {"path": "handoff.md", "sha256": digest(worktree / "handoff.md")}},
                "reviewer": {"task_id": reviewer.task_id, "generation": reviewer.generation,
                              "worktree": reviewer.worktree, "branch": reviewer.branch,
                              "status": reviewer.status, "head": head, "clearance": {"p0": 0, "p1": 0, "p2": 0},
                              "status_file": {"path": "status.txt", "sha256": digest(worktree / "status.txt")},
                              "handoff_file": {"path": "handoff.md", "sha256": digest(worktree / "handoff.md")}}
            }]
        }
        return monitor, snapshot, package

    def test_reconcile_evidence_promotes_valid_same_run_pair(self):
        monitor, snapshot, package = self._reconciliation_fixture()
        reconciled = monitor.reconcile_evidence(snapshot.run_id, package)
        self.assertEqual(reconciled.nodes["n1"]["status"], "accepted", reconciled.nodes["n1"])
        events = load_events(self.paths, snapshot.run_id)
        names = [event["event"] for event in events]
        self.assertLess(names.index("delivered"), names.index("accepted"))

    def test_reconcile_evidence_rejects_mixed_package_without_mutation(self):
        monitor, snapshot, package = self._reconciliation_fixture()
        package["nodes"].append(dict(package["nodes"][0], node_id="unknown"))
        before = load_events(self.paths, snapshot.run_id)
        with self.assertRaises(ValueError):
            monitor.reconcile_evidence(snapshot.run_id, package)
        self.assertEqual(load_events(self.paths, snapshot.run_id), before)

    def test_reconcile_evidence_replay_is_idempotent(self):
        monitor, snapshot, package = self._reconciliation_fixture()
        first = monitor.reconcile_evidence(snapshot.run_id, package)
        events = load_events(self.paths, snapshot.run_id)
        second = monitor.reconcile_evidence(snapshot.run_id, package)
        self.assertEqual(second.to_dict(), first.to_dict())
        self.assertEqual(load_events(self.paths, snapshot.run_id), events)

    def test_reconcile_evidence_malformed_replay_is_rejected_without_mutation(self):
        monitor, snapshot, package = self._reconciliation_fixture()
        monitor.reconcile_evidence(snapshot.run_id, package)
        before = load_events(self.paths, snapshot.run_id)
        malformed = {key: value for key, value in package.items() if key != "schema_version"}
        with self.assertRaisesRegex(ValueError, "schema"):
            monitor.reconcile_evidence(snapshot.run_id, malformed)
        self.assertEqual(load_events(self.paths, snapshot.run_id), before)

    def test_reconcile_evidence_rejects_active_writer_without_mutation(self):
        monitor, snapshot, package = self._reconciliation_fixture()
        snapshot.nodes["n1"]["active_task"] = {"role": "developer", "task_id": "live", "generation": 2, "handle_id": "h"}
        snapshot.handles["n1"] = "h"
        save_snapshot(self.paths, snapshot)
        before = load_events(self.paths, snapshot.run_id)
        with self.assertRaisesRegex(ValueError, "active writer"):
            monitor.reconcile_evidence(snapshot.run_id, package)
        self.assertEqual(load_events(self.paths, snapshot.run_id), before)

    def test_reconcile_evidence_rejects_head_mismatch(self):
        monitor, snapshot, package = self._reconciliation_fixture()
        package["nodes"][0]["developer"]["head"] = "0" * 40
        before = load_events(self.paths, snapshot.run_id)
        with self.assertRaisesRegex(ValueError, "HEAD mismatch"):
            monitor.reconcile_evidence(snapshot.run_id, package)
        self.assertEqual(load_events(self.paths, snapshot.run_id), before)


    def authorized_monitor(self, nodes, active_pair_limit=None):
        plan = Plan("plan-1", 1, "docs/prd.md", [item.id for item in nodes], "draft")
        card = build_authorization_card(
            plan,
            nodes,
            self.capabilities,
            active_pair_limit=active_pair_limit,
        )
        return Monitor(self.paths, plan, nodes), authorize(card, "AUTHORIZE")

    def test_brief_gate_blocks_before_lease_or_provider_start(self):
        current = node("n1")
        current.contract["brief_required"] = True
        current.contract["implementation_brief"] = {
            "issue_id": "n1", "goal": "goal", "non_goals": [],
            "owned_paths": ["n1.py"], "read_paths": [],
            "call_chain": ["n1.py:missing"],
            "invariants": [{"id": "I1", "entrypoint": "n1.py:missing",
                            "positive_case": "ok", "negative_case": "bad",
                            "test_command": "python -m unittest"}],
            "base_sha": "0" * 40, "plan_revision": 1,
            "execution_epoch": 0, "evidence_ref": "brief.json",
        }
        monitor, record = self.authorized_monitor([current])
        runner = FakeRunner()

        snapshot = monitor.start(record, runner)

        self.assertEqual(snapshot.nodes["n1"]["status"], "brief_pending")
        self.assertEqual(runner.start_calls, [])
        self.assertIsNone(snapshot.nodes["n1"].get("active_task"))
        self.assertIsNone(snapshot.nodes["n1"].get("start_intent"))
        self.assertTrue(any("base_sha" in item for item in snapshot.nodes["n1"]["brief_evidence"]["missing"]))

    def test_starts_independent_nodes_together_and_waits_for_hard_dependency(self):
        nodes = [node("n1"), node("n2"), node("n3", ["n1"])]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner()

        snapshot = monitor.start(record, runner)

        self.assertEqual([call["node_id"] for call in runner.start_calls], ["n1", "n2"])
        self.assertEqual(snapshot.nodes["n1"]["status"], "running")
        self.assertEqual(snapshot.nodes["n2"]["status"], "running")
        self.assertEqual(snapshot.nodes["n3"]["status"], "planned")

    def test_stopped_developer_releases_capacity_without_archiving_pair(self):
        nodes = [node("n1"), node("n2")]
        monitor, record = self.authorized_monitor(nodes, active_pair_limit=1)
        runner = FakeRunner(
            events={
                ("n1", "developer"): [
                    ("stopped", {"reason": "provider stopped task"})
                ]
            }
        )

        snapshot = monitor.start(record, runner)
        self.assertEqual([call["node_id"] for call in runner.start_calls], ["n1"])

        stopped = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(stopped.nodes["n1"]["status"], "stopped")
        self.assertFalse(stopped.nodes["n1"]["pair_archived"])
        self.assertEqual(stopped.nodes["n2"]["status"], "running")
        self.assertEqual(
            [call["node_id"] for call in runner.start_calls], ["n1", "n2"]
        )

    def test_failed_developer_releases_capacity_without_archiving_pair(self):
        nodes = [node("n1"), node("n2")]
        monitor, record = self.authorized_monitor(nodes, active_pair_limit=1)
        runner = FakeRunner(
            events={
                ("n1", "developer"): [
                    ("failed", {"reason": "provider failed task"})
                ]
            }
        )

        snapshot = monitor.start(record, runner)
        self.assertEqual([call["node_id"] for call in runner.start_calls], ["n1"])

        failed = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(failed.nodes["n1"]["status"], "failed")
        self.assertFalse(failed.nodes["n1"]["pair_archived"])
        self.assertEqual(failed.nodes["n2"]["status"], "running")
        self.assertEqual(
            [call["node_id"] for call in runner.start_calls], ["n1", "n2"]
        )

    def test_blocked_unknown_with_active_task_and_handle_keeps_capacity_occupied(self):
        nodes = [node("n1"), node("n2")]
        monitor, record = self.authorized_monitor(nodes, active_pair_limit=1)
        runner = FakeRunner()
        snapshot = monitor.start(record, runner)
        current = snapshot.nodes["n1"]
        current["status"] = "blocked_unknown"
        current["retryable_action"] = None
        self.assertIsInstance(current.get("active_task"), dict)
        self.assertIn("n1", snapshot.handles)
        save_snapshot(self.paths, snapshot)

        blocked = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(blocked.nodes["n1"]["status"], "blocked_unknown")
        self.assertEqual(blocked.nodes["n2"]["status"], "planned")
        self.assertEqual(
            [call["node_id"] for call in runner.start_calls], ["n1"]
        )

    def test_historical_reviewer_flag_without_current_binding_starts_successor(self):
        monitor, record = self.authorized_monitor([node("n1")])
        runner = FakeRunner(
            events={
                ("n1", "developer"): [("complete", {"evidence": "delivery"})],
            }
        )
        snapshot = monitor.start(record, runner)
        current = snapshot.nodes["n1"]
        historical_reviewer = "reviewer:n1:historical"
        current["reviewer_started"] = True
        current["reviewer_identity"] = historical_reviewer
        save_snapshot(self.paths, snapshot)

        resumed = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(resumed.nodes["n1"]["status"], "review")
        reviewer_calls = [
            call for call in runner.start_calls if call.get("role") == "reviewer"
        ]
        self.assertEqual(len(reviewer_calls), 1)
        reviewer_call = reviewer_calls[0]
        self.assertFalse(reviewer_call["continuation"])
        self.assertTrue(reviewer_call["successor"])
        self.assertNotEqual(reviewer_call["task_id"], historical_reviewer)
        self.assertEqual(
            reviewer_call["predecessor_task_id"], historical_reviewer
        )
        reviewer_binding = load_task_binding(
            self.paths, "n1", "reviewer", run_id=snapshot.run_id
        )
        self.assertEqual(reviewer_binding.successor_of, historical_reviewer)

    def test_resume_recovers_reviewer_started_without_current_binding(self):
        monitor, record = self.authorized_monitor([node("n1")])
        failed_runner = ReviewerBindingFailureRunner(
            events={
                ("n1", "developer"): [("complete", {"evidence": "delivery"})],
            }
        )
        snapshot = monitor.start(record, failed_runner)
        blocked = monitor.tick(snapshot.run_id, failed_runner)

        current = blocked.nodes["n1"]
        self.assertEqual(current["status"], "blocked_unknown")
        self.assertEqual(current["developer_generation"], 1)
        self.assertEqual(
            load_task_binding(
                self.paths, "n1", "developer", run_id=blocked.run_id
            ).status,
            "delivered",
        )
        self.assertTrue(current["reviewer_started"])
        self.assertIsNone(current["active_task"])
        self.assertIsNone(current["retryable_action"])
        self.assertEqual(blocked.handles, {})
        with self.assertRaises(FileNotFoundError):
            load_task_binding(self.paths, "n1", "reviewer", run_id=blocked.run_id)

        recovery_runner = FakeRunner()
        resumed = monitor.resume(blocked.run_id, recovery_runner)

        self.assertEqual(resumed.nodes["n1"]["status"], "review")
        reviewer_calls = [
            call for call in recovery_runner.start_calls if call.get("role") == "reviewer"
        ]
        self.assertEqual(len(reviewer_calls), 1)
        reviewer_call = reviewer_calls[0]
        self.assertFalse(reviewer_call["continuation"])
        self.assertTrue(reviewer_call["successor"])
        self.assertEqual(
            reviewer_call["predecessor_task_id"], current["reviewer_identity"]
        )

    def test_reviewer_binding_failure_does_not_restart_delivered_developer(self):
        monitor, record = self.authorized_monitor([node("n1")])
        runner = ReviewerBindingFailureRunner(
            events={
                ("n1", "developer"): [("complete", {"evidence": "delivery"})],
            }
        )
        snapshot = monitor.start(record, runner)

        blocked = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(blocked.nodes["n1"]["status"], "blocked_unknown")
        self.assertEqual(
            [call["role"] for call in runner.start_calls], ["developer"]
        )

    def test_v2_run_and_child_binding_lock_the_same_capability_contract_digest(self):
        monitor, record = self.authorized_monitor([node("n1")])
        runner = FakeRunner()

        snapshot = monitor.start(record, runner)

        contract = json.loads(
            (self.paths.vibe / "session-contract.json").read_text(encoding="utf-8")
        )
        self.assertEqual(snapshot.capability_contract_digest, contract["contract_digest"])
        self.assertEqual(
            runner.start_calls[0]["capability_contract_digest"],
            contract["contract_digest"],
        )
        self.assertEqual(
            runner.start_calls[0]["child_binding"]["capability_contract_digest"],
            contract["contract_digest"],
        )

    def test_provider_pending_is_retried_without_capability_unavailable_status(self):
        monitor, record = self.authorized_monitor([node("n1")])
        runner = ProviderRetryRunner()
        snapshot = monitor.start(record, runner)

        self.assertEqual(snapshot.nodes["n1"]["status"], "running")
        self.assertEqual(snapshot.status, "running")
        self.assertEqual(
            snapshot.nodes["n1"]["retryable_action"]["phase"], "develop"
        )
        self.assertFalse(
            any(event["event"] == "blocked_unknown" for event in load_events(self.paths, snapshot.run_id))
        )
        snapshot = monitor.resume(snapshot.run_id, runner)
        self.assertEqual(runner.binding_attempts, 2)
        self.assertEqual(snapshot.nodes["n1"]["status"], "running")

    def test_fresh_capability_contract_requires_explicit_reauthorization(self):
        monitor, record = self.authorized_monitor([node("n1")])
        runner = FakeRunner()
        snapshot = monitor.start(record, runner)
        previous_digest = snapshot.capability_contract_digest
        fresh = build_contract(
            self.paths.root,
            provider="fake",
            host_id="local",
            facts={
                "runtime.exec": {
                    "status": "verified_available",
                    "scope": "task",
                    "route": "functions.exec",
                    "evidence_ref": "live:test-refresh",
                }
            },
        )
        save_contract(self.paths, fresh)

        with self.assertRaisesRegex(PermissionError, "run binding mismatch"):
            monitor.resume(snapshot.run_id, FakeRunner())

        with patch.object(monitor, "_schedule_ready", return_value=None):
            rebound = monitor.reauthorize(
                snapshot.run_id,
                record,
                FakeRunner(),
                "capability_contract_changed",
            )

        self.assertNotEqual(previous_digest, fresh.contract_digest)
        self.assertEqual(rebound.capability_contract_digest, fresh.contract_digest)
        transition = [
            event
            for event in load_events(self.paths, snapshot.run_id)
            if event["event"] == "authorization_reauthorized"
        ][-1]
        self.assertEqual(
            transition["data"]["previous_capability_contract_digest"],
            previous_digest,
        )
        self.assertEqual(
            transition["data"]["capability_contract_digest"],
            fresh.contract_digest,
        )

    def test_quarantined_node_without_request_binding_or_handle_never_resumes_old_task(self):
        monitor, record = self.authorized_monitor([node("n1")])
        initial_runner = FakeRunner()
        snapshot = monitor.start(record, initial_runner)
        current = snapshot.nodes["n1"]
        current["status"] = "blocked_unknown"
        current["active_role"] = None
        current["active_task"] = None
        current["quarantine"] = {
            "run_id": snapshot.run_id,
            "handle_id": None,
            "reason": "old task has no durable binding",
        }
        current["retryable_action"] = {
            "role": "developer",
            "phase": "rework",
            "continuation": False,
        }
        snapshot.handles.clear()
        tasks_path = Path(self.temporary.name) / ".vibe/runs" / snapshot.run_id / "tasks.json"
        tasks_path.unlink()
        from vibe_guide import monitor as monitor_module

        with patch.object(monitor_module, "save_snapshot", wraps=monitor_module.save_snapshot):
            monitor_module.save_snapshot(self.paths, snapshot)

        recovery_runner = FakeRunner()
        resumed = monitor.resume(snapshot.run_id, recovery_runner)

        self.assertEqual(recovery_runner.start_calls, [])
        self.assertEqual(resumed.nodes["n1"]["status"], "blocked_unknown")
        self.assertIsNotNone(resumed.nodes["n1"].get("quarantine"))

    def test_reauthorization_creates_visible_successor_only_after_absence_is_proven(self):
        monitor, record = self.authorized_monitor([node("n1")])
        initial_runner = FakeRunner()
        snapshot = monitor.start(record, initial_runner)
        old_identity = snapshot.nodes["n1"]["developer_identity"]
        current = snapshot.nodes["n1"]
        current["status"] = "blocked_unknown"
        current["active_role"] = None
        current["active_task"] = None
        current["quarantine"] = {
            "run_id": snapshot.run_id,
            "handle_id": None,
            "reason": "old task has no durable binding",
        }
        current["retryable_action"] = {
            "role": "developer",
            "phase": "rework",
            "continuation": True,
        }
        snapshot.handles.clear()
        tasks_path = Path(self.temporary.name) / ".vibe/runs" / snapshot.run_id / "tasks.json"
        tasks_path.unlink()
        from vibe_guide import monitor as monitor_module

        monitor_module.save_snapshot(self.paths, snapshot)
        fresh = build_contract(
            self.paths.root,
            provider="fake",
            host_id="local",
            facts={
                "runtime.exec": {
                    "status": "verified_available",
                    "scope": "task",
                    "route": "functions.exec",
                    "evidence_ref": "live:test-successor",
                }
            },
        )
        save_contract(self.paths, fresh)

        recovery_runner = FakeRunner()
        rebound = monitor.reauthorize(
            snapshot.run_id,
            record,
            recovery_runner,
            "capability_contract_changed",
        )

        self.assertEqual(len(recovery_runner.start_calls), 1)
        call = recovery_runner.start_calls[0]
        self.assertNotEqual(call["task_id"], old_identity)
        self.assertTrue(call["successor"])
        self.assertEqual(call["capability_contract_digest"], fresh.contract_digest)
        self.assertEqual(call["child_binding"]["worktree"], ".worktrees/n1")
        self.assertEqual(call["branch"], node_branch("n1"))
        self.assertEqual(
            call["child_binding"]["capability_contract_digest"],
            fresh.contract_digest,
        )
        self.assertEqual(rebound.nodes["n1"]["status"], "rework")

    def test_reauthorization_continues_delivered_developer_without_successor(self):
        monitor, record = self.authorized_monitor([node("n1")])
        initial_runner = FakeRunner()
        snapshot = monitor.start(record, initial_runner)
        current = snapshot.nodes["n1"]
        current.update(
            {
                "status": "delivered",
                "active_role": None,
                "active_task": None,
                "reviewer_started": True,
                "pair_archived": True,
            }
        )
        developer = load_task_binding(
            self.paths, "n1", "developer", run_id=snapshot.run_id
        )
        developer.status = "delivered"
        save_task_binding(self.paths, developer)
        reviewer = TaskBinding(
            provider="fake",
            mode="background",
            issue_id="n1",
            role="reviewer",
            task_id="reviewer-n1",
            worktree=".worktrees/n1",
            branch="branch-n1",
            run_id=snapshot.run_id,
            status="stopped",
            generation=1,
        )
        save_task_binding(self.paths, reviewer)
        snapshot.handles.clear()
        save_snapshot(self.paths, snapshot)

        changed = node("n1")
        changed.contract["acceptance_example"] = "changed after delivery"
        changed.contract["files"] = ["n1.py", "n1-extra.py"]
        changed.contract["worker_profile"]["allowlist"] = [
            "n1.py",
            "n1-extra.py",
        ]
        changed_plan = Plan("plan-1", 1, "docs/prd.md", ["n1"], "draft")
        changed_record = authorize(
            build_authorization_card(changed_plan, [changed], self.capabilities),
            "AUTHORIZE",
        )
        recovery_runner = FakeRunner()

        def existing_binding(contract, worktree, run_id, status):
            binding = load_task_binding(
                self.paths, contract["node_id"], contract["role"], run_id=run_id
            )
            binding.status = status
            binding.generation = contract["generation"]
            return binding

        recovery_runner.task_binding = existing_binding
        rebound = Monitor(self.paths, changed_plan, [changed]).reauthorize(
            snapshot.run_id,
            changed_record,
            recovery_runner,
            "executable_contract_changed",
        )

        developer_calls = [
            call for call in recovery_runner.start_calls if call.get("role") == "developer"
        ]
        self.assertEqual(len(developer_calls), 1)
        self.assertTrue(developer_calls[0]["continuation"])
        self.assertFalse(developer_calls[0]["successor"])
        self.assertEqual(developer_calls[0]["task_id"], developer.task_id)
        self.assertEqual(rebound.nodes["n1"]["status"], "rework")

    def test_resume_normalizes_legacy_successor_marker_for_delivered_developer(self):
        monitor, record = self.authorized_monitor([node("n1")])
        initial_runner = FakeRunner()
        snapshot = monitor.start(record, initial_runner)
        current = snapshot.nodes["n1"]
        current.update(
            {
                "status": "blocked_unknown",
                "active_role": None,
                "active_task": None,
                "pair_archived": True,
                "retryable_action": {
                    "role": "developer",
                    "phase": "rework",
                    "continuation": True,
                    "pending_schedule": True,
                    "successor_candidate": True,
                },
            }
        )
        snapshot.handles.clear()
        developer = load_task_binding(
            self.paths, "n1", "developer", run_id=snapshot.run_id
        )
        developer.status = "delivered"
        save_task_binding(self.paths, developer)
        save_snapshot(self.paths, snapshot)

        recovery_runner = FakeRunner()
        resumed = monitor.resume(snapshot.run_id, recovery_runner)

        self.assertEqual(len(recovery_runner.start_calls), 1)
        self.assertTrue(recovery_runner.start_calls[0]["continuation"])
        self.assertFalse(recovery_runner.start_calls[0]["successor"])
        self.assertEqual(recovery_runner.start_calls[0]["task_id"], developer.task_id)
        self.assertEqual(resumed.nodes["n1"]["status"], "rework")

    def test_quarantine_does_not_clear_legacy_marker_for_delivered_developer(self):
        monitor, record = self.authorized_monitor([node("n1")])
        initial_runner = FakeRunner()
        snapshot = monitor.start(record, initial_runner)
        current = snapshot.nodes["n1"]
        current.update(
            {
                "status": "blocked_unknown",
                "active_role": None,
                "active_task": None,
                "pair_archived": True,
                "quarantine": {
                    "run_id": snapshot.run_id,
                    "handle_id": None,
                    "reason": "stale quarantine from reauthorization",
                },
                "retryable_action": {
                    "role": "developer",
                    "phase": "rework",
                    "continuation": True,
                    "pending_schedule": True,
                    "successor_candidate": False,
                },
            }
        )
        snapshot.handles.clear()
        developer = load_task_binding(
            self.paths, "n1", "developer", run_id=snapshot.run_id
        )
        developer.status = "delivered"
        save_task_binding(self.paths, developer)
        save_snapshot(self.paths, snapshot)

        recovery_runner = FakeRunner()
        resumed = monitor.resume(snapshot.run_id, recovery_runner)

        self.assertEqual(len(recovery_runner.start_calls), 1)
        self.assertTrue(recovery_runner.start_calls[0]["continuation"])
        self.assertFalse(recovery_runner.start_calls[0]["successor"])
        self.assertEqual(recovery_runner.start_calls[0]["task_id"], developer.task_id)
        self.assertEqual(resumed.nodes["n1"]["status"], "rework")
        self.assertIsNone(resumed.nodes["n1"].get("quarantine"))

    def test_quarantine_without_retry_marker_reconstructs_delivered_continuation(self):
        monitor, record = self.authorized_monitor([node("n1")])
        initial_runner = FakeRunner()
        snapshot = monitor.start(record, initial_runner)
        current = snapshot.nodes["n1"]
        current.update(
            {
                "status": "blocked_unknown",
                "active_role": None,
                "active_task": None,
                "developer_generation": 2,
                "pair_archived": False,
                "quarantine": {
                    "run_id": snapshot.run_id,
                    "handle_id": None,
                    "reason": "retry marker was lost after reauthorization",
                },
                "retryable_action": None,
            }
        )
        snapshot.handles.clear()
        developer = load_task_binding(
            self.paths, "n1", "developer", run_id=snapshot.run_id
        )
        developer.status = "delivered"
        save_task_binding(self.paths, developer)
        save_snapshot(self.paths, snapshot)

        recovery_runner = FakeRunner()
        resumed = monitor.resume(snapshot.run_id, recovery_runner)

        self.assertEqual(len(recovery_runner.start_calls), 1)
        self.assertTrue(recovery_runner.start_calls[0]["continuation"])
        self.assertFalse(recovery_runner.start_calls[0]["successor"])
        self.assertEqual(recovery_runner.start_calls[0]["task_id"], developer.task_id)
        self.assertEqual(resumed.nodes["n1"]["status"], "rework")
        self.assertIsNone(resumed.nodes["n1"].get("quarantine"))

    def test_reauthorization_keeps_ambiguous_old_binding_blocked_without_successor(self):
        monitor, record = self.authorized_monitor([node("n1")])
        initial_runner = FakeRunner()
        snapshot = monitor.start(record, initial_runner)
        current = snapshot.nodes["n1"]
        current["status"] = "blocked_unknown"
        current["active_role"] = None
        current["active_task"] = None
        current["quarantine"] = {
            "run_id": snapshot.run_id,
            "handle_id": None,
            "reason": "old task status is ambiguous",
        }
        current["retryable_action"] = {
            "role": "developer",
            "phase": "rework",
            "continuation": True,
        }
        snapshot.handles.clear()
        binding = load_task_binding(
            self.paths, "n1", "developer", run_id=snapshot.run_id
        )
        binding.status = "running"
        save_task_binding(self.paths, binding)
        from vibe_guide import monitor as monitor_module

        monitor_module.save_snapshot(self.paths, snapshot)
        fresh = build_contract(
            self.paths.root,
            provider="fake",
            host_id="local",
            facts={
                "runtime.exec": {
                    "status": "verified_available",
                    "scope": "task",
                    "route": "functions.exec",
                    "evidence_ref": "live:test-ambiguous",
                }
            },
        )
        save_contract(self.paths, fresh)

        recovery_runner = FakeRunner()
        rebound = monitor.reauthorize(
            snapshot.run_id,
            record,
            recovery_runner,
            "capability_contract_changed",
        )

        self.assertEqual(recovery_runner.start_calls, [])
        self.assertEqual(rebound.nodes["n1"]["status"], "blocked_unknown")
        self.assertEqual(
            rebound.nodes["n1"]["retryable_action"]["continuation"], True
        )

    def test_replayed_old_task_reconciled_forged_predecessor_never_starts_successor(self):
        monitor, record = self.authorized_monitor([node("n1")])
        snapshot = monitor.start(record, FakeRunner())
        current = snapshot.nodes["n1"]
        current.update(
            {
                "status": "blocked_unknown",
                "active_role": None,
                "active_task": None,
                "quarantine": {"run_id": snapshot.run_id, "handle_id": None, "reason": "replay"},
                "retryable_action": {
                    "role": "developer",
                    "phase": "rework",
                    "continuation": True,
                    "pending_schedule": True,
                    "successor_candidate": True,
                },
            }
        )
        snapshot.handles.clear()
        (Path(self.temporary.name) / ".vibe/runs" / snapshot.run_id / "tasks.json").unlink()
        save_snapshot(self.paths, snapshot)
        append_event(
            self.paths,
            RunEvent(
                "old_task_reconciled",
                {
                    "run_id": snapshot.run_id,
                    "node_id": "n1",
                    "role": "developer",
                    "proof": "absent",
                    "predecessor_task_id": "evil-predecessor",
                },
            ),
            {
                "role": "system",
                "task_id": None,
                "handle_id": None,
                "generation": 0,
                "authorization_digest": snapshot.authorization_digest,
                "node_contract_digest": snapshot.node_contract_digest,
            },
        )

        recovery = FakeRunner()
        resumed = monitor.resume(snapshot.run_id, recovery)

        self.assertEqual(recovery.start_calls, [])
        self.assertEqual(resumed.nodes["n1"]["status"], "blocked_unknown")
        self.assertIsNone(resumed.nodes["n1"].get("retryable_action"))

    def test_reauthorization_replay_is_idempotent_for_transition_and_reconciliation_events(self):
        monitor, record = self.authorized_monitor([node("n1")])
        snapshot = monitor.start(record, FakeRunner())
        current = snapshot.nodes["n1"]
        current.update(
            {
                "status": "blocked_unknown",
                "active_role": None,
                "active_task": None,
                "quarantine": {"run_id": snapshot.run_id, "handle_id": None, "reason": "replay"},
                "retryable_action": {
                    "role": "developer",
                    "phase": "rework",
                    "continuation": True,
                    "pending_schedule": True,
                    "successor_candidate": True,
                },
            }
        )
        snapshot.handles.clear()
        (Path(self.temporary.name) / ".vibe/runs" / snapshot.run_id / "tasks.json").unlink()
        save_snapshot(self.paths, snapshot)
        fresh = build_contract(
            self.paths.root,
            provider="fake",
            host_id="local",
            facts={
                "runtime.exec": {
                    "status": "verified_available",
                    "scope": "task",
                    "route": "functions.exec",
                    "evidence_ref": "live:idempotent-refresh",
                }
            },
        )
        save_contract(self.paths, fresh)
        with patch.object(monitor, "_schedule_ready", side_effect=RuntimeError("interrupted")):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                monitor.reauthorize(snapshot.run_id, record, FakeRunner(), "capability_contract_changed")

        resumed = monitor.reauthorize(snapshot.run_id, record, FakeRunner(), "capability_contract_changed")
        events = load_events(self.paths, snapshot.run_id)
        self.assertEqual(sum(event["event"] == "authorization_reauthorized" for event in events), 1)
        self.assertEqual(sum(event["event"] == "old_task_reconciled" for event in events), 1)
        self.assertEqual(resumed.nodes["n1"]["status"], "rework")

    def test_active_pair_capacity_archives_accepted_pair_and_starts_next(self):
        nodes = [node("n1"), node("n2")]
        monitor, record = self.authorized_monitor(nodes, active_pair_limit=1)
        runner = FakeRunner(
            events={
                ("n1", "developer"): [("complete", {"evidence": "delivery"})],
                ("n1", "reviewer"): [
                    ("accepted", {"evidence": "P0-P2 clear"})
                ],
            }
        )

        snapshot = monitor.start(record, runner)
        self.assertEqual([call["node_id"] for call in runner.start_calls], ["n1"])
        snapshot = monitor.tick(snapshot.run_id, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)

        self.assertTrue(snapshot.nodes["n1"]["pair_archived"])
        self.assertEqual(snapshot.nodes["n1"]["status"], "accepted")
        self.assertEqual(snapshot.nodes["n2"]["status"], "running")
        self.assertEqual(
            load_task_binding(
                self.paths, "n1", "developer", snapshot.run_id
            ).status,
            "archived",
        )
        self.assertEqual(
            load_task_binding(
                self.paths, "n1", "reviewer", snapshot.run_id
            ).status,
            "archived",
        )

    def test_node_provider_self_claims_do_not_become_visible_task_evidence(self):
        claimed = node("n1")
        claimed.contract.update(
            {
                "provider": "codex",
                "mode": "visible",
                "hostId": "fake-host",
                "developer_task_id": "fabricated-thread",
            }
        )
        monitor, record = self.authorized_monitor([claimed])

        snapshot = monitor.start(record, FakeRunner())
        binding = load_task_binding(
            self.paths, "n1", "developer", snapshot.run_id
        )

        self.assertEqual(binding.provider, "runner")
        self.assertEqual(binding.mode, "background")
        self.assertFalse(binding.visible)
        self.assertNotEqual(binding.task_id, "fabricated-thread")

    def test_provider_complete_is_delivery_and_does_not_unlock_dependent_node(self):
        nodes = [node("n1"), node("n2", ["n1"])]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner(
            events={("n1", "developer"): [("complete", {"evidence": "delivery"})]}
        )

        snapshot = monitor.start(record, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(snapshot.nodes["n1"]["status"], "review")
        self.assertEqual(snapshot.nodes["n2"]["status"], "planned")
        self.assertEqual(
            [(call["node_id"], call["role"]) for call in runner.start_calls],
            [("n1", "developer"), ("n1", "reviewer")],
        )

    def test_missing_authorization_never_starts_runner(self):
        nodes = [node("n1")]
        plan = Plan("plan-1", 1, "docs/prd.md", ["n1"], "draft")
        runner = FakeRunner()

        with self.assertRaises(PermissionError):
            Monitor(self.paths, plan, nodes).start(None, runner)
        self.assertEqual(runner.start_calls, [])

    def test_review_finding_reworks_with_same_worker_and_preserves_evidence(self):
        nodes = [node("n1", worker="worker-original")]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner(
            events={
                ("n1", "developer"): [
                    ("delivered", {"evidence": "delivery-1"}),
                    ("delivered", {"evidence": "delivery-2"}),
                ],
                ("n1", "reviewer"): [
                    ("review_finding", {"finding": "fix test", "in_contract": True}),
                    ("accepted", {"evidence": "review-2"}),
                ]
            }
        )
        snapshot = monitor.start(record, runner)

        snapshot = monitor.tick(snapshot.run_id, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(runner.start_calls[-1]["worker"], "worker-original")
        self.assertEqual(runner.start_calls[-1]["phase"], "rework")
        self.assertEqual(snapshot.nodes["n1"]["status"], "rework")
        self.assertEqual(
            snapshot.nodes["n1"]["evidence"],
            ["[REDACTED_PROVIDER_TEXT]", "[REDACTED_PROVIDER_TEXT]"],
        )

        snapshot = monitor.tick(snapshot.run_id, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(snapshot.nodes["n1"]["status"], "accepted")
        self.assertEqual(
            snapshot.nodes["n1"]["evidence"],
            [
                "[REDACTED_PROVIDER_TEXT]",
                "[REDACTED_PROVIDER_TEXT]",
                "[REDACTED_PROVIDER_TEXT]",
                "[REDACTED_PROVIDER_TEXT]",
            ],
        )

    def test_rework_lookup_reads_same_developer_binding_once_per_tick(self):
        nodes = [node("n1", worker="worker-original")]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner(
            events={
                ("n1", "developer"): [("delivered", {"evidence": "delivery"})],
                ("n1", "reviewer"): [
                    ("review_finding", {"finding": "fix test", "in_contract": True}),
                ],
            }
        )
        snapshot = monitor.start(record, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)

        calls = []
        original_load = load_task_binding

        def counted_load(paths, issue_id, role, run_id=None):
            calls.append((issue_id, role, run_id))
            return original_load(paths, issue_id, role, run_id=run_id)

        with patch("vibe_guide.monitor.load_task_binding", side_effect=counted_load):
            snapshot = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(snapshot.nodes["n1"]["status"], "rework")
        self.assertEqual(
            calls.count(("n1", "developer", snapshot.run_id)),
            1,
        )

    def test_p1_finding_without_contract_marker_reworks_original_developer(self):
        for marker in ("missing", False):
            with self.subTest(in_contract=marker):
                nodes = [node("n1", worker="worker-original")]
                monitor, record = self.authorized_monitor(nodes)
                finding = {"finding": "P1 review finding", "severity": "P1"}
                if marker != "missing":
                    finding["in_contract"] = marker
                runner = FakeRunner(
                    events={
                        ("n1", "developer"): [
                            ("delivered", {"evidence": "delivery"})
                        ],
                        ("n1", "reviewer"): [("review_finding", finding)],
                    }
                )

                snapshot = monitor.start(record, runner)
                original_developer = runner.start_calls[0]
                snapshot = monitor.tick(snapshot.run_id, runner)
                snapshot = monitor.tick(snapshot.run_id, runner)

                self.assertEqual(snapshot.nodes["n1"]["status"], "rework")
                self.assertEqual(
                    [(call["role"], call["phase"]) for call in runner.start_calls],
                    [("developer", "develop"), ("reviewer", "review"), ("developer", "rework")],
                )
                rework = runner.start_calls[-1]
                for field in ("task_id", "worker", "worktree", "branch"):
                    self.assertEqual(rework[field], original_developer[field])
                self.assertTrue(rework["continuation"])
                self.assertFalse(rework["successor"])
                release_writer_lease(
                    self.paths, "n1", ".worktrees/n1", snapshot.run_id
                )

    def test_blocked_design_stops_old_task_and_releases_lease_for_new_authorization(self):
        original = node("n1")
        monitor, record = self.authorized_monitor([original])
        runner = FakeRunner(
            events={
                ("n1", "developer"): [
                    ("complete", {"evidence": "delivery"})
                ],
                ("n1", "reviewer"): [
                    (
                        "review_finding",
                        {
                            "finding": "genuine product choice",
                            "in_contract": False,
                        },
                    )
                ],
            }
        )
        snapshot = monitor.start(record, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)
        old_reviewer_handle = snapshot.handles["n1"]
        snapshot = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(snapshot.nodes["n1"]["status"], "blocked_design")
        self.assertIn(old_reviewer_handle, runner.stop_calls)
        self.assertTrue(snapshot.nodes["n1"]["old_task_reconciled"])

        corrected = node("n1")
        corrected.contract["acceptance_example"] = "approved corrected outcome"
        new_monitor, new_record = self.authorized_monitor([corrected])
        restarted = new_monitor.start(new_record, FakeRunner())
        self.assertEqual(restarted.nodes["n1"]["status"], "running")

    def test_unique_in_scope_consistency_is_corrected_but_real_choice_blocks(self):
        target = node("n1")
        target.contract["naming"] = "approved-name"
        decisions = [
            {
                "id": "decision-naming",
                "question": "canonical name",
                "field": "naming",
                "revision": 1,
                "selected": "approved-name",
                "status": "approved",
            }
        ]
        plan = Plan(
            "plan-1",
            1,
            "docs/prd.md",
            ["n1"],
            "draft",
            decisions=decisions,
        )
        card = build_authorization_card(plan, [target], self.capabilities)
        monitor = Monitor(self.paths, plan, [target])
        runner = FakeRunner(
            events={
                ("n1", "developer"): [
                    ("complete", {"evidence": "delivery"})
                ]
            }
        )
        snapshot = monitor.start(authorize(card, "AUTHORIZE"), runner)
        snapshot = monitor.tick(snapshot.run_id, runner)
        binding = runner.start_calls[-1]["consistency_binding"]
        runner.events[("n1", "reviewer")] = [
            (
                "review_finding",
                {
                    "finding": "stale lower contract name",
                    "in_contract": False,
                    "consistency": {
                        "field": "naming",
                        "action": "rework",
                        "files": ["n1.py"],
                        "candidates": [
                            {
                                "source": "approved_prd",
                                "value": "approved-name",
                                "binding": binding,
                                "decision": {
                                    "id": "decision-naming",
                                    "field": "naming",
                                    "revision": 1,
                                    "status": "approved",
                                    "selected": "approved-name",
                                },
                            },
                            {
                                "source": "implementation",
                                "value": "stale-name",
                            },
                        ],
                    },
                },
            )
        ]
        snapshot = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(snapshot.nodes["n1"]["status"], "rework")
        self.assertEqual(
            snapshot.nodes["n1"]["contract_overrides"],
            {"naming": "approved-name"},
        )
        self.assertEqual(
            snapshot.nodes["n1"]["corrections"][0]["source"],
            "approved_prd",
        )
        self.assertEqual(
            snapshot.nodes["n1"]["corrections"][0]["consistency_binding"],
            binding,
        )

        ambiguous = node("n2")
        ambiguous_plan = Plan("plan-2", 1, "docs/prd.md", ["n2"], "draft")
        ambiguous_card = build_authorization_card(
            ambiguous_plan, [ambiguous], self.capabilities
        )
        ambiguous_monitor = Monitor(self.paths, ambiguous_plan, [ambiguous])
        ambiguous_runner = FakeRunner(
            events={
                ("n2", "developer"): [
                    ("complete", {"evidence": "delivery"})
                ]
            }
        )
        blocked = ambiguous_monitor.start(
            authorize(ambiguous_card, "AUTHORIZE"), ambiguous_runner
        )
        blocked = ambiguous_monitor.tick(blocked.run_id, ambiguous_runner)
        ambiguous_binding = ambiguous_runner.start_calls[-1]["consistency_binding"]
        ambiguous_runner.events[("n2", "reviewer")] = [
            (
                "review_finding",
                {
                    "finding": "two approved outcomes",
                    "in_contract": False,
                    "consistency": {
                        "field": "naming",
                        "action": "rework",
                        "files": ["n2.py"],
                        "candidates": [
                            {
                                "source": "approved_prd",
                                "value": "a",
                                "binding": ambiguous_binding,
                                "decision": {
                                    "id": "decision-a",
                                    "field": "naming",
                                    "revision": 1,
                                    "status": "approved",
                                    "selected": "a",
                                },
                            },
                            {
                                "source": "approved_prd",
                                "value": "b",
                                "binding": ambiguous_binding,
                                "decision": {
                                    "id": "decision-b",
                                    "field": "naming",
                                    "revision": 1,
                                    "status": "approved",
                                    "selected": "b",
                                },
                            },
                        ],
                    },
                },
            )
        ]
        blocked = ambiguous_monitor.tick(blocked.run_id, ambiguous_runner)
        self.assertEqual(blocked.nodes["n2"]["status"], "blocked_design")

    def test_unbound_current_user_text_cannot_override_approved_decision(self):
        target = node("n1")
        target.contract["naming"] = "approved-name"
        plan = Plan(
            "plan-1",
            1,
            "docs/prd.md",
            ["n1"],
            "draft",
            decisions=[
                {
                    "id": "decision-naming",
                    "question": "canonical name",
                    "field": "naming",
                    "revision": 1,
                    "selected": "approved-name",
                    "status": "approved",
                }
            ],
        )
        card = build_authorization_card(plan, [target], self.capabilities)
        monitor = Monitor(self.paths, plan, [target])
        runner = FakeRunner(
            events={
                ("n1", "developer"): [("complete", {"evidence": "delivery"})],
                ("n1", "reviewer"): [
                    (
                        "review_finding",
                        {
                            "finding": "unbound text claims a user override",
                            "in_contract": False,
                            "consistency": {
                                "field": "naming",
                                "action": "rework",
                                "files": ["n1.py"],
                                "candidates": [
                                    {
                                        "source": "current_user",
                                        "value": "unapproved-name",
                                    },
                                    {
                                        "source": "approved_prd",
                                        "value": "approved-name",
                                        "binding": {},
                                        "decision": {
                                            "id": "decision-naming",
                                            "field": "naming",
                                            "revision": 1,
                                            "status": "approved",
                                            "selected": "approved-name",
                                        },
                                    },
                                ],
                            },
                        },
                    )
                ],
            }
        )

        snapshot = monitor.start(authorize(card, "AUTHORIZE"), runner)
        snapshot = monitor.tick(snapshot.run_id, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(snapshot.nodes["n1"]["status"], "blocked_design")
        self.assertNotIn("naming", snapshot.nodes["n1"]["contract_overrides"])

    def test_consistency_correction_event_recovers_after_interrupted_snapshot_save(self):
        target = node("n1")
        target.contract["naming"] = "approved-name"
        plan = Plan(
            "plan-1",
            1,
            "docs/prd.md",
            ["n1"],
            "draft",
            decisions=[
                {
                    "id": "decision-naming",
                    "question": "canonical name",
                    "field": "naming",
                    "revision": 1,
                    "selected": "approved-name",
                    "status": "approved",
                }
            ],
        )
        card = build_authorization_card(plan, [target], self.capabilities)
        monitor = Monitor(self.paths, plan, [target])
        runner = FakeRunner(
            events={
                ("n1", "developer"): [("complete", {"evidence": "delivery"})]
            }
        )
        snapshot = monitor.start(authorize(card, "AUTHORIZE"), runner)
        snapshot = monitor.tick(snapshot.run_id, runner)
        binding = runner.start_calls[-1]["consistency_binding"]
        runner.events[("n1", "reviewer")] = [
            (
                "review_finding",
                {
                    "finding": "stale lower contract name",
                    "in_contract": False,
                    "consistency": {
                        "field": "naming",
                        "action": "rework",
                        "files": ["n1.py"],
                        "candidates": [
                            {
                                "source": "approved_prd",
                                "value": "approved-name",
                                "binding": binding,
                                "decision": {
                                    "id": "decision-naming",
                                    "field": "naming",
                                    "revision": 1,
                                    "status": "approved",
                                    "selected": "approved-name",
                                },
                            },
                            {
                                "source": "implementation",
                                "value": "stale-name",
                            },
                        ],
                    },
                },
            )
        ]

        with patch.object(
            monitor,
            "_start_task",
            side_effect=RuntimeError("interrupted after correction event"),
        ):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                monitor.tick(snapshot.run_id, runner)

        recovery_runner = FakeRunner()
        recovered = monitor.resume(snapshot.run_id, recovery_runner)
        correction = recovered.nodes["n1"]["corrections"][0]
        self.assertEqual(recovered.nodes["n1"]["contract_overrides"], {"naming": "approved-name"})
        self.assertEqual(correction["consistency_binding"], binding)
        self.assertEqual(recovered.nodes["n1"]["status"], "rework")
        self.assertEqual(recovery_runner.start_calls[-1]["phase"], "rework")

        persisted = [
            record for record in load_events(self.paths, snapshot.run_id)
            if record["event"] == "consistency_corrected"
        ][0]["data"]
        self.assertEqual(
            set(persisted),
            {
                "run_id",
                "node_id",
                "field",
                "value",
                "source",
                "action",
                "files",
                "consistency_binding",
                "decision",
            },
        )

    def test_reauthorization_event_recovers_before_snapshot_and_is_not_duplicated(self):
        original = node("n1")
        original_plan = Plan("plan-1", 1, "docs/prd.md", ["n1"], "draft")
        original_record = authorize(
            build_authorization_card(
                original_plan, [original], self.capabilities
            ),
            "AUTHORIZE",
        )
        original_monitor = Monitor(self.paths, original_plan, [original])
        runner = FakeRunner()
        snapshot = original_monitor.start(original_record, runner)

        corrected = node("n1")
        corrected.contract["acceptance_example"] = "corrected implementation outcome"
        corrected_record = authorize(
            build_authorization_card(
                original_plan, [corrected], self.capabilities
            ),
            "AUTHORIZE",
        )
        corrected_monitor = Monitor(self.paths, original_plan, [corrected])
        with patch.object(
            corrected_monitor,
            "_schedule_ready",
            side_effect=RuntimeError("interrupted after reauthorization event"),
        ):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                corrected_monitor.reauthorize(
                    snapshot.run_id,
                    corrected_record,
                    runner,
                    "executable_contract_changed",
                )

        recovery_runner = FakeRunner()
        recovered = corrected_monitor.reauthorize(
            snapshot.run_id,
            corrected_record,
            recovery_runner,
            "executable_contract_changed",
        )

        self.assertEqual(recovered.authorization_digest, corrected_record.digest)
        self.assertEqual(recovered.nodes["n1"]["status"], "rework")
        self.assertEqual(recovery_runner.start_calls[-1]["phase"], "rework")
        self.assertEqual(
            len(
                [
                    event for event in load_events(self.paths, snapshot.run_id)
                    if event["event"] == "authorization_reauthorized"
                ]
            ),
            1,
        )

    def test_reauthorization_retains_independent_accepted_epoch(self):
        nodes = [node("n1"), node("n2")]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner(
            events={
                ("n1", "developer"): [("complete", {"evidence": "delivery-n1"})],
                ("n1", "reviewer"): [("accepted", {"evidence": "review-n1"})],
                ("n2", "developer"): [("complete", {"evidence": "delivery-n2"})],
                ("n2", "reviewer"): [("accepted", {"evidence": "review-n2"})],
            }
        )
        snapshot = monitor.start(record, runner)
        for _ in range(3):
            snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(snapshot.nodes["n1"]["status"], "accepted")
        self.assertEqual(snapshot.nodes["n2"]["status"], "accepted")
        old_n2_acceptance = dict(snapshot.nodes["n2"]["acceptance"])

        changed = node("n1")
        changed.contract["acceptance_example"] = "changed n1 contract"
        changed_nodes = [changed, node("n2")]
        changed_plan = Plan(
            "plan-1", 1, "docs/prd.md", [item.id for item in changed_nodes], "draft"
        )
        changed_record = authorize(
            build_authorization_card(changed_plan, changed_nodes, self.capabilities),
            "AUTHORIZE",
        )
        changed_monitor = Monitor(self.paths, changed_plan, changed_nodes)

        reauthorized = changed_monitor.reauthorize(
            snapshot.run_id,
            changed_record,
            FakeRunner(),
            "executable_contract_changed",
        )

        self.assertEqual(reauthorized.nodes["n2"]["status"], "accepted")
        self.assertEqual(
            reauthorized.nodes["n2"]["acceptance"]["contract_digest"],
            old_n2_acceptance["contract_digest"],
        )
        self.assertEqual(
            reauthorized.nodes["n2"]["acceptance"]["authorization_epoch"],
            changed_record.digest,
        )
        self.assertEqual(reauthorized.nodes["n1"]["status"], "rework")

    def _assert_reauthorization_rejects_observed_continuation_mismatch(
        self, task_id=None, cursor=None
    ):
        original = node("n1")
        monitor, record = self.authorized_monitor([original])
        initial = FakeRunner(
            events={
                ("n1", "developer"): [("complete", {"evidence": "delivery"})],
                ("n1", "reviewer"): [("accepted", {"evidence": "review"})],
            }
        )
        snapshot = monitor.start(record, initial)
        snapshot = monitor.tick(snapshot.run_id, initial)
        snapshot = monitor.tick(snapshot.run_id, initial)
        binding = load_task_binding(
            self.paths, "n1", "developer", run_id=snapshot.run_id
        )
        binding.cursor = "original-cursor"
        save_task_binding(self.paths, binding)

        changed = node("n1")
        changed.contract["acceptance_example"] = "changed contract"
        changed_plan = Plan("plan-1", 1, "docs/prd.md", ["n1"], "draft")
        changed_record = authorize(
            build_authorization_card(changed_plan, [changed], self.capabilities),
            "AUTHORIZE",
        )
        observed = ObservedContinuationRunner(task_id=task_id, cursor=cursor)
        reauthorized = Monitor(self.paths, changed_plan, [changed]).reauthorize(
            snapshot.run_id,
            changed_record,
            observed,
            "executable_contract_changed",
        )

        self.assertEqual(reauthorized.nodes["n1"]["status"], "blocked_unknown")
        self.assertEqual(observed.start_calls, [])

    def test_reauthorization_rejects_observed_continuation_identity_mismatch(self):
        self._assert_reauthorization_rejects_observed_continuation_mismatch(
            task_id="wrong-task-id",
            cursor="original-cursor",
        )

    def test_reauthorization_rejects_observed_continuation_cursor_mismatch(self):
        self._assert_reauthorization_rejects_observed_continuation_mismatch(
            cursor="wrong-cursor"
        )

    def test_reauthorization_invalidates_accepted_downstream_suffix_and_recovers(self):
        upstream = node("n1")
        downstream = node("n2", ["n1"])
        independent = node("n3")
        nodes = [upstream, downstream, independent]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner(
            events={
                ("n1", "developer"): [("complete", {"evidence": "delivery-n1"})],
                ("n1", "reviewer"): [("accepted", {"evidence": "review-n1"})],
                ("n2", "developer"): [("complete", {"evidence": "delivery-n2"})],
                ("n2", "reviewer"): [("accepted", {"evidence": "review-n2"})],
                ("n3", "developer"): [("complete", {"evidence": "delivery-n3"})],
                ("n3", "reviewer"): [("accepted", {"evidence": "review-n3"})],
            }
        )
        snapshot = monitor.start(record, runner)
        for _ in range(6):
            snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertTrue(
            all(item["status"] == "accepted" for item in snapshot.nodes.values())
        )
        old_identities = {
            node_id: (
                snapshot.nodes[node_id]["developer_identity"],
                snapshot.nodes[node_id]["reviewer_identity"],
            )
            for node_id in ("n2", "n3")
        }
        old_cursors = {}
        for node_id in ("n2", "n3"):
            for role in ("developer", "reviewer"):
                binding = load_task_binding(
                    self.paths, node_id, role, run_id=snapshot.run_id
                )
                binding.cursor = "cursor-{}-{}".format(node_id, role)
                save_task_binding(self.paths, binding)
                old_cursors["{}:{}".format(node_id, role)] = binding.cursor

        changed = node("n1")
        changed.contract["acceptance_example"] = "changed upstream contract"
        changed_nodes = [changed, node("n2", ["n1"]), node("n3")]
        changed_plan = Plan(
            "plan-1", 1, "docs/prd.md", [item.id for item in changed_nodes], "draft"
        )
        changed_record = authorize(
            build_authorization_card(changed_plan, changed_nodes, self.capabilities),
            "AUTHORIZE",
        )
        changed_monitor = Monitor(self.paths, changed_plan, changed_nodes)

        with patch.object(
            changed_monitor,
            "_schedule_ready",
            side_effect=RuntimeError("interrupted after reauthorization event"),
        ):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                changed_monitor.reauthorize(
                    snapshot.run_id,
                    changed_record,
                    FakeRunner(),
                    "executable_contract_changed",
                )

        recovered = changed_monitor.reauthorize(
            snapshot.run_id,
            changed_record,
            FakeRunner(),
            "executable_contract_changed",
        )

        self.assertEqual(recovered.nodes["n2"]["status"], "blocked_unknown")
        self.assertIsNone(recovered.nodes["n2"]["acceptance"])
        self.assertTrue(
            recovered.nodes["n2"]["retryable_action"]["pending_schedule"]
        )
        self.assertEqual(recovered.nodes["n3"]["status"], "accepted")
        self.assertEqual(
            (
                recovered.nodes["n2"]["developer_identity"],
                recovered.nodes["n2"]["reviewer_identity"],
            ),
            old_identities["n2"],
        )
        self.assertEqual(
            (
                recovered.nodes["n3"]["developer_identity"],
                recovered.nodes["n3"]["reviewer_identity"],
            ),
            old_identities["n3"],
        )
        for node_id in ("n2", "n3"):
            for role in ("developer", "reviewer"):
                self.assertEqual(
                    load_task_binding(
                        self.paths, node_id, role, run_id=snapshot.run_id
                    ).cursor,
                    old_cursors["{}:{}".format(node_id, role)],
                )
        self.assertEqual(
            len(
                [
                    event
                    for event in load_events(self.paths, snapshot.run_id)
                    if event["event"] == "authorization_reauthorized"
                ]
            ),
            1,
        )

    def test_reauthorization_waits_for_hard_dependency_before_downstream_rework(self):
        nodes = [node("n1"), node("n2", ["n1"]), node("n3")]
        monitor, record = self.authorized_monitor(nodes)
        initial = FakeRunner(
            events={
                (node_id, "developer"): [("complete", {"evidence": node_id})]
                for node_id in ("n1", "n2", "n3")
            }
        )
        initial.events.update(
            {
                (node_id, "reviewer"): [("accepted", {"evidence": node_id})]
                for node_id in ("n1", "n2", "n3")
            }
        )
        snapshot = monitor.start(record, initial)
        for _ in range(6):
            snapshot = monitor.tick(snapshot.run_id, initial)
        self.assertTrue(
            all(current["status"] == "accepted" for current in snapshot.nodes.values())
        )

        changed = node("n1")
        changed.contract["acceptance_example"] = "changed upstream"
        changed_nodes = [changed, node("n2", ["n1"]), node("n3")]
        changed_plan = Plan(
            "plan-1", 1, "docs/prd.md", [item.id for item in changed_nodes], "draft"
        )
        changed_record = authorize(
            build_authorization_card(changed_plan, changed_nodes, self.capabilities),
            "AUTHORIZE",
        )
        changed_monitor = Monitor(self.paths, changed_plan, changed_nodes)
        recovery = FakeRunner(
            events={
                ("n1", "developer"): [("complete", {"evidence": "new-n1"})],
                ("n1", "reviewer"): [("accepted", {"evidence": "new-n1"})],
            }
        )

        snapshot = changed_monitor.reauthorize(
            snapshot.run_id,
            changed_record,
            recovery,
            "executable_contract_changed",
        )

        self.assertEqual([call["node_id"] for call in recovery.start_calls], ["n1"])
        transition_sequence = next(
            event["sequence"]
            for event in load_events(self.paths, snapshot.run_id)
            if event["event"] == "authorization_reauthorized"
        )
        n2_start_events = [
            event
            for event in load_events(self.paths, snapshot.run_id)
            if event["event"] == "start_intent"
            and event["data"].get("node_id") == "n2"
            and event["sequence"] > transition_sequence
        ]
        self.assertEqual(n2_start_events, [])
        snapshot = changed_monitor.tick(snapshot.run_id, recovery)
        self.assertEqual([call["node_id"] for call in recovery.start_calls], ["n1", "n1"])
        snapshot = changed_monitor.tick(snapshot.run_id, recovery)
        self.assertEqual(
            [call["node_id"] for call in recovery.start_calls],
            ["n1", "n1", "n2"],
        )

    def test_reauthorization_active_downstream_waits_for_reaccepted_dependency_and_capacity(
        self,
    ):
        nodes = [node("n1"), node("n2", ["n1"])]
        monitor, record = self.authorized_monitor(nodes, active_pair_limit=1)
        initial = FakeRunner(
            events={
                ("n1", "developer"): [("complete", {"evidence": "old-n1"})],
                ("n1", "reviewer"): [("accepted", {"evidence": "old-n1"})],
            }
        )
        snapshot = monitor.start(record, initial)
        snapshot = monitor.tick(snapshot.run_id, initial)
        snapshot = monitor.tick(snapshot.run_id, initial)
        self.assertEqual(snapshot.nodes["n1"]["status"], "accepted")
        self.assertEqual(snapshot.nodes["n2"]["status"], "running")

        original_handle = snapshot.handles["n2"]
        original_identity = snapshot.nodes["n2"]["developer_identity"]
        downstream_binding = load_task_binding(
            self.paths, "n2", "developer", run_id=snapshot.run_id
        )
        downstream_binding.cursor = "active-downstream-cursor"
        save_task_binding(self.paths, downstream_binding)

        changed_upstream = node("n1")
        changed_upstream.contract["acceptance_example"] = "changed upstream"
        changed_nodes = [changed_upstream, node("n2", ["n1"])]
        changed_plan = Plan("plan-1", 1, "docs/prd.md", ["n1", "n2"], "draft")
        changed_record = authorize(
            build_authorization_card(
                changed_plan,
                changed_nodes,
                self.capabilities,
                active_pair_limit=1,
            ),
            "AUTHORIZE",
        )
        recovery = FakeRunner(
            events={
                ("n1", "developer"): [("complete", {"evidence": "new-n1"})],
                ("n1", "reviewer"): [("accepted", {"evidence": "new-n1"})],
            }
        )
        changed_monitor = Monitor(self.paths, changed_plan, changed_nodes)

        snapshot = changed_monitor.reauthorize(
            snapshot.run_id,
            changed_record,
            recovery,
            "executable_contract_changed",
        )

        self.assertIn(original_handle, recovery.stop_calls)
        self.assertEqual([call["node_id"] for call in recovery.start_calls], ["n1"])
        self.assertEqual(snapshot.nodes["n2"]["status"], "blocked_unknown")
        self.assertTrue(snapshot.nodes["n2"]["retryable_action"]["pending_schedule"])
        transition_sequence = next(
            event["sequence"]
            for event in load_events(self.paths, snapshot.run_id)
            if event["event"] == "authorization_reauthorized"
        )

        snapshot = changed_monitor.tick(snapshot.run_id, recovery)
        before_acceptance = [
            event
            for event in load_events(self.paths, snapshot.run_id)
            if event["event"] == "start_intent"
            and event["data"].get("node_id") == "n2"
            and event["sequence"] > transition_sequence
        ]
        self.assertEqual(before_acceptance, [])
        self.assertEqual([call["node_id"] for call in recovery.start_calls], ["n1", "n1"])

        snapshot = changed_monitor.tick(snapshot.run_id, recovery)
        self.assertEqual(snapshot.nodes["n1"]["status"], "accepted")
        self.assertEqual(
            [call["node_id"] for call in recovery.start_calls],
            ["n1", "n1", "n2"],
        )
        self.assertEqual(recovery.start_calls[-1]["task_id"], original_identity)
        continued_binding = load_task_binding(
            self.paths, "n2", "developer", run_id=snapshot.run_id
        )
        self.assertEqual(continued_binding.task_id, original_identity)
        self.assertEqual(continued_binding.cursor, "active-downstream-cursor")

    def test_reauthorization_retry_obeys_capacity_but_not_integration_after(self):
        independent = [node("n1"), node("n2")]
        monitor, record = self.authorized_monitor(independent, active_pair_limit=1)
        initial = FakeRunner(
            events={
                (node_id, "developer"): [("complete", {"evidence": node_id})]
                for node_id in ("n1", "n2")
            }
        )
        initial.events.update(
            {
                (node_id, "reviewer"): [("accepted", {"evidence": node_id})]
                for node_id in ("n1", "n2")
            }
        )
        snapshot = monitor.start(record, initial)
        for _ in range(5):
            snapshot = monitor.tick(snapshot.run_id, initial)
        self.assertEqual(snapshot.status, "complete")

        changed_nodes = [node("n1"), node("n2")]
        for item in changed_nodes:
            item.contract["acceptance_example"] = "changed-" + item.id
        changed_plan = Plan(
            "plan-1", 1, "docs/prd.md", [item.id for item in changed_nodes], "draft"
        )
        changed_record = authorize(
            build_authorization_card(
                changed_plan, changed_nodes, self.capabilities, active_pair_limit=1
            ),
            "AUTHORIZE",
        )
        recovery = FakeRunner(
            events={
                ("n1", "developer"): [("complete", {"evidence": "new-n1"})],
                ("n1", "reviewer"): [("accepted", {"evidence": "new-n1"})],
            }
        )
        changed_monitor = Monitor(self.paths, changed_plan, changed_nodes)
        snapshot = changed_monitor.reauthorize(
            snapshot.run_id,
            changed_record,
            recovery,
            "executable_contract_changed",
        )
        self.assertEqual([call["node_id"] for call in recovery.start_calls], ["n1"])
        transition_sequence = next(
            event["sequence"]
            for event in load_events(self.paths, snapshot.run_id)
            if event["event"] == "authorization_reauthorized"
        )
        self.assertFalse(
            any(
                event["event"] == "start_intent"
                and event["data"].get("node_id") == "n2"
                and event["sequence"] > transition_sequence
                for event in load_events(self.paths, snapshot.run_id)
            )
        )
        snapshot = changed_monitor.tick(snapshot.run_id, recovery)
        self.assertEqual([call["node_id"] for call in recovery.start_calls], ["n1", "n1"])
        changed_monitor.tick(snapshot.run_id, recovery)
        self.assertEqual(
            [call["node_id"] for call in recovery.start_calls],
            ["n1", "n1", "n2"],
        )

        integration_upstream = node("i1")
        integration_downstream = node("i2")
        integration_downstream.integration_after = ["i1"]
        integration_monitor, integration_record = self.authorized_monitor(
            [integration_upstream, integration_downstream], active_pair_limit=2
        )
        integration_runner = FakeRunner(
            events={
                (node_id, "developer"): [("complete", {"evidence": node_id})]
                for node_id in ("i1", "i2")
            }
        )
        integration_runner.events.update(
            {
                (node_id, "reviewer"): [("accepted", {"evidence": node_id})]
                for node_id in ("i1", "i2")
            }
        )
        integration_snapshot = integration_monitor.start(
            integration_record, integration_runner
        )
        self.assertEqual(
            [call["node_id"] for call in integration_runner.start_calls],
            ["i1", "i2"],
        )
        for _ in range(3):
            integration_snapshot = integration_monitor.tick(
                integration_snapshot.run_id, integration_runner
            )
        self.assertEqual(integration_snapshot.status, "complete")

        changed_i1 = node("i1")
        changed_i1.contract["acceptance_example"] = "changed integration upstream"
        changed_i2 = node("i2")
        changed_i2.integration_after = ["i1"]
        changed_integration_nodes = [changed_i1, changed_i2]
        changed_integration_plan = Plan(
            "plan-1", 1, "docs/prd.md", ["i1", "i2"], "draft"
        )
        changed_integration_record = authorize(
            build_authorization_card(
                changed_integration_plan,
                changed_integration_nodes,
                self.capabilities,
                active_pair_limit=2,
            ),
            "AUTHORIZE",
        )
        integration_recovery = FakeRunner()
        Monitor(
            self.paths, changed_integration_plan, changed_integration_nodes
        ).reauthorize(
            integration_snapshot.run_id,
            changed_integration_record,
            integration_recovery,
            "executable_contract_changed",
        )
        self.assertEqual(
            [call["node_id"] for call in integration_recovery.start_calls],
            ["i1", "i2"],
        )

    def test_applied_reauthorization_rejects_forged_retained_downstream(self):
        nodes = [node("n1"), node("n2", ["n1"]), node("n3")]
        monitor, record = self.authorized_monitor(nodes)
        initial = FakeRunner(
            events={
                (node_id, "developer"): [("complete", {"evidence": node_id})]
                for node_id in ("n1", "n2", "n3")
            }
        )
        initial.events.update(
            {
                (node_id, "reviewer"): [("accepted", {"evidence": node_id})]
                for node_id in ("n1", "n2", "n3")
            }
        )
        snapshot = monitor.start(record, initial)
        for _ in range(6):
            snapshot = monitor.tick(snapshot.run_id, initial)

        changed = node("n1")
        changed.contract["acceptance_example"] = "changed upstream"
        changed_nodes = [changed, node("n2", ["n1"]), node("n3")]
        changed_plan = Plan(
            "plan-1", 1, "docs/prd.md", [item.id for item in changed_nodes], "draft"
        )
        changed_record = authorize(
            build_authorization_card(changed_plan, changed_nodes, self.capabilities),
            "AUTHORIZE",
        )
        changed_monitor = Monitor(self.paths, changed_plan, changed_nodes)
        with patch.object(changed_monitor, "_schedule_ready", return_value=None):
            applied = changed_monitor.reauthorize(
                snapshot.run_id,
                changed_record,
                FakeRunner(),
                "executable_contract_changed",
            )

        run_directory = Path(self.temporary.name) / ".vibe/runs" / applied.run_id
        event_path = run_directory / "events.jsonl"
        records = [json.loads(line) for line in event_path.read_text().splitlines()]
        transition_index = next(
            index
            for index, event in enumerate(records)
            if event["event"] == "authorization_reauthorized"
        )
        transition = records[transition_index]["data"]
        transition["affected_nodes"] = ["n1"]
        transition["retained_acceptances"]["n2"] = transition[
            "invalidated_acceptances"
        ].pop("n2")
        forged_n2_contract = json.loads(
            transition["authorized_node_contracts"]["n2"]
        )
        forged_n2_contract["depends_on"] = []
        transition["authorized_node_contracts"]["n2"] = json.dumps(
            forged_n2_contract,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        previous_digest = (
            records[transition_index - 1]["event_digest"]
            if transition_index
            else None
        )
        for event in records[transition_index:]:
            event["previous_event_digest"] = previous_digest
            payload = dict(event)
            payload.pop("event_digest", None)
            event["event_digest"] = hashlib.sha256(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            previous_digest = event["event_digest"]
        event_path.write_text(
            "".join(
                json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
                for event in records
            ),
            encoding="utf-8",
        )

        forged = applied.to_dict()
        forged_n2 = forged["nodes"]["n2"]
        forged_n2["status"] = "accepted"
        forged_n2["acceptance"] = {
            "contract_digest": transition["node_contract_digests"]["n2"],
            "authorization_epoch": transition["authorization_digest"],
        }
        forged_n2["pair_archived"] = True
        forged_n2["retryable_action"] = None
        encoded = json.dumps(
            forged, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        for state_name in ("state.json", "state.previous.json"):
            (run_directory / state_name).write_text(encoded, encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "no valid snapshot"):
            load_snapshot(self.paths, applied.run_id)

    def test_unknown_is_blocked_unknown_not_completed(self):
        nodes = [node("n1")]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner(events={"n1": [("unknown", {"reason": "poll timeout"})]})
        snapshot = monitor.start(record, runner)

        snapshot = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(snapshot.nodes["n1"]["status"], "blocked_unknown")
        self.assertEqual(snapshot.status, "blocked_unknown")

    def test_rework_reuses_developer_and_reviewer_task_identities(self):
        nodes = [node("n1", worker="worker-original")]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner(
            events={
                ("n1", "developer"): [
                    ("delivered", {"evidence": "delivery-1"}),
                    ("delivered", {"evidence": "delivery-2"}),
                ],
                ("n1", "reviewer"): [
                    ("review_finding", {"finding": "fix test", "in_contract": True}),
                    ("accepted", {"evidence": "review-2"}),
                ],
            }
        )
        snapshot = monitor.start(record, runner)
        for _ in range(4):
            snapshot = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(snapshot.nodes["n1"]["status"], "accepted")
        self.assertEqual(
            [call["phase"] for call in runner.start_calls],
            ["develop", "review", "rework", "review"],
        )
        self.assertEqual(runner.start_calls[0]["task_id"], runner.start_calls[2]["task_id"])
        self.assertEqual(runner.start_calls[1]["task_id"], runner.start_calls[3]["task_id"])
        self.assertTrue(runner.start_calls[3]["continuation"])

    def test_second_monitor_cannot_take_an_existing_writer_lease(self):
        nodes = [node("n1")]
        monitor, record = self.authorized_monitor(nodes)
        first_runner = FakeRunner()
        monitor.start(record, first_runner)

        second_runner = FakeRunner()
        second_snapshot = Monitor(self.paths, monitor.plan, nodes).start(record, second_runner)

        self.assertEqual(second_snapshot.nodes["n1"]["status"], "blocked_unknown")
        self.assertEqual(second_runner.start_calls, [])

    def test_accepted_node_unlocks_dependent_node_and_resume_uses_snapshot(self):
        nodes = [node("n1"), node("n2", ["n1"])]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner(
            events={
                ("n1", "developer"): [("delivered", {"evidence": "delivery"})],
                ("n1", "reviewer"): [("accepted", {"evidence": "ok"})],
            }
        )
        snapshot = monitor.start(record, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)

        resumed = monitor.resume(snapshot.run_id, runner)

        self.assertEqual(resumed.nodes["n1"]["status"], "accepted")
        self.assertEqual(resumed.nodes["n2"]["status"], "running")
        self.assertEqual(
            [(call["node_id"], call["role"]) for call in runner.start_calls],
            [("n1", "developer"), ("n1", "reviewer"), ("n2", "developer")],
        )

    def test_live_contract_mutation_invalidates_authorization_before_start(self):
        base = node("n1")
        base.contract.update(
            {
                "branch": "codex/safe",
                "provider": "codex",
                "mode": "visible",
                "hostId": "local",
                "developer_task_id": "thread-safe",
            }
        )
        plan = Plan("plan-bound", 1, "docs/prd.md", ["n1"], "draft")
        record = authorize(
            build_authorization_card(plan, [base], self.capabilities), "AUTHORIZE"
        )
        mutations = {
            "files": ["outside.py"],
            "worker": "worker-evil",
            "worktree": "../outside",
            "branch": "codex/evil",
            "provider": "other-provider",
            "developer_task_id": "thread-other",
            "action": "deploy",
        }

        for field, value in mutations.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as root:
                changed = deepcopy(base)
                changed.contract[field] = value
                runner = FakeRunner()
                with self.assertRaises(PermissionError):
                    Monitor(ProjectPaths(Path(root)), plan, [changed]).start(record, runner)
                self.assertEqual(runner.start_calls, [])

    def test_contract_mutation_is_rechecked_before_tick(self):
        nodes = [node("n1")]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner()
        snapshot = monitor.start(record, runner)
        monitor.nodes["n1"].contract["files"] = ["outside.py"]

        with self.assertRaises(PermissionError):
            monitor.tick(snapshot.run_id, runner)

    def test_forged_snapshot_without_authorization_or_event_lineage_is_rejected(self):
        nodes = [node("n1")]
        monitor, _record = self.authorized_monitor(nodes)
        run_dir = Path(self.temporary.name) / ".vibe/runs/run-forged"
        run_dir.mkdir(parents=True)
        (run_dir / "state.json").write_text(
            json.dumps(
                {
                    "run_id": "run-forged",
                    "plan_id": "plan-1",
                    "plan_version": 1,
                    "status": "running",
                    "nodes": {
                        "n1": {
                            "status": "planned",
                            "worker": "worker-n1",
                            "worktree": ".worktrees/n1",
                            "evidence": [],
                        }
                    },
                    "handles": {},
                    "tasks": {},
                }
            ),
            encoding="utf-8",
        )
        runner = FakeRunner()

        with self.assertRaises(ValueError):
            monitor.resume("run-forged", runner)
        self.assertEqual(runner.start_calls, [])

    def test_developer_cannot_self_accept(self):
        nodes = [node("n1")]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner(events={"n1": [("accepted", {"evidence": "self"})]})
        snapshot = monitor.start(record, runner)

        snapshot = monitor.tick(snapshot.run_id, runner)

        self.assertNotEqual(snapshot.nodes["n1"]["status"], "accepted")
        self.assertNotEqual(snapshot.status, "complete")
        self.assertFalse(
            acquire_writer_lease(
                self.paths, "n1", ".worktrees/n1", "run-second"
            )
        )

    def test_delivered_node_still_requires_independent_review(self):
        delivered = node("n1")
        delivered.status = "delivered"
        monitor, record = self.authorized_monitor([delivered])
        runner = FakeRunner()

        snapshot = monitor.start(record, runner)

        self.assertEqual(snapshot.nodes["n1"]["status"], "review")
        self.assertEqual([call["role"] for call in runner.start_calls], ["reviewer"])
        self.assertNotEqual(snapshot.status, "complete")

    def test_acceptance_rejects_conflicting_reviewer_provenance(self):
        nodes = [node("n1")]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner(
            events={
                ("n1", "developer"): [("delivered", {"evidence": "delivery"})],
                ("n1", "reviewer"): [
                    ("accepted", {"evidence": "review", "role": "developer", "generation": 999})
                ],
            }
        )
        snapshot = monitor.start(record, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)

        snapshot = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(snapshot.nodes["n1"]["status"], "blocked_unknown")
        self.assertNotEqual(snapshot.status, "complete")

    def test_start_response_loss_quarantines_writer_lease(self):
        nodes = [node("n1")]
        monitor, record = self.authorized_monitor(nodes)

        snapshot = monitor.start(record, StartResponseLostRunner())

        self.assertEqual(snapshot.nodes["n1"]["status"], "blocked_unknown")
        self.assertFalse(
            acquire_writer_lease(self.paths, "n1", ".worktrees/n1", "run-second")
        )

    def test_task_binding_status_tracks_confirmed_runtime_state(self):
        nodes = [node("n1")]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner(
            events={
                ("n1", "developer"): [("delivered", {"evidence": "delivery"})],
                ("n1", "reviewer"): [("accepted", {"evidence": "review"})],
            }
        )

        snapshot = monitor.start(record, runner)
        self.assertEqual(
            load_task_binding(
                self.paths, "n1", "developer", run_id=snapshot.run_id
            ).status,
            "running",
        )

        snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(
            load_task_binding(
                self.paths, "n1", "developer", run_id=snapshot.run_id
            ).status,
            "delivered",
        )
        self.assertEqual(
            load_task_binding(
                self.paths, "n1", "reviewer", run_id=snapshot.run_id
            ).status,
            "review",
        )

        snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(
            load_task_binding(
                self.paths, "n1", "reviewer", run_id=snapshot.run_id
            ).status,
            "archived",
        )
        self.assertEqual(
            load_task_binding(
                self.paths, "n1", "developer", run_id=snapshot.run_id
            ).status,
            "archived",
        )

    def test_recovery_rejects_forged_terminal_before_releasing_lease(self):
        nodes = [node("n1")]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner()
        snapshot = monitor.start(record, runner)
        append_event(
            self.paths,
            RunEvent(
                "terminal_failed",
                {"run_id": snapshot.run_id, "node_id": "n1", "reason": "forged"},
            ),
            {
                "authorization_digest": snapshot.authorization_digest,
                "node_contract_digest": snapshot.node_contract_digest,
            },
        )

        with self.assertRaises(ValueError):
            monitor.resume(snapshot.run_id, runner)
        self.assertFalse(
            acquire_writer_lease(self.paths, "n1", ".worktrees/n1", "run-second")
        )

    def test_acceptance_uses_reviewer_binding_from_current_run(self):
        nodes = [node("n1")]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner(
            events={
                ("n1", "developer"): [("delivered", {"evidence": "delivery"})],
                ("n1", "reviewer"): [("accepted", {"evidence": "review"})],
            }
        )
        snapshot = monitor.start(record, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)
        current_registry = (
            Path(self.temporary.name)
            / ".vibe"
            / "runs"
            / snapshot.run_id
            / "tasks.json"
        )
        foreign_registry = (
            Path(self.temporary.name)
            / ".vibe"
            / "runs"
            / "zzzz-foreign-run"
            / "tasks.json"
        )
        foreign_registry.parent.mkdir(parents=True)
        foreign_registry.write_bytes(current_registry.read_bytes())
        current_registry.unlink()

        snapshot = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(snapshot.nodes["n1"]["status"], "blocked_unknown")

    def test_duplicate_active_handle_quarantines_second_node(self):
        nodes = [node("n1"), node("n2")]
        monitor, record = self.authorized_monitor(nodes)

        snapshot = monitor.start(record, DuplicateHandleRunner())

        self.assertEqual(snapshot.nodes["n1"]["status"], "running")
        self.assertEqual(snapshot.nodes["n2"]["status"], "blocked_unknown")
        self.assertEqual(list(snapshot.handles.values()), ["shared-handle"])
        self.assertFalse(
            acquire_writer_lease(
                self.paths, "n2", ".worktrees/n2", "run-second"
            )
        )

    def test_state_transition_requires_complete_provider_claims(self):
        nodes = [node("n1")]
        monitor, record = self.authorized_monitor(nodes)
        snapshot = monitor.start(record, UnclaimedEventRunner())

        snapshot = monitor.tick(snapshot.run_id, UnclaimedEventRunner())

        self.assertEqual(snapshot.nodes["n1"]["status"], "blocked_unknown")
        self.assertFalse(snapshot.nodes["n1"]["reviewer_started"])
        self.assertFalse(
            acquire_writer_lease(
                self.paths, "n1", ".worktrees/n1", "run-second"
            )
        )

    def test_provider_payloads_and_exceptions_never_reach_persisted_artifacts(self):
        cases = (
            (
                "EVENT_SECRET_SENTINEL",
                FakeRunner(
                    events={
                        ("n1", "developer"): [
                            (
                                "delivered",
                                {
                                    "evidence": "EVENT_SECRET_SENTINEL",
                                    "nested": {"Api-Key": "EVENT_SECRET_SENTINEL"},
                                },
                            )
                        ]
                    }
                ),
                True,
            ),
            (
                "START_EXCEPTION_SECRET_SENTINEL",
                SecretStartResponseLostRunner(),
                False,
            ),
            (
                "POLL_EXCEPTION_SECRET_SENTINEL",
                SecretPollResponseLostRunner(),
                True,
            ),
        )
        for sentinel, runner, tick in cases:
            with self.subTest(sentinel=sentinel), tempfile.TemporaryDirectory() as root:
                paths = ProjectPaths(Path(root))
                nodes = [node("n1")]
                plan = Plan("plan-secret", 1, "docs/prd.md", ["n1"], "draft")
                record = authorize(
                    build_authorization_card(plan, nodes, self.capabilities), "AUTHORIZE"
                )
                monitor = Monitor(paths, plan, nodes)
                snapshot = monitor.start(record, runner)
                if tick:
                    snapshot = monitor.tick(snapshot.run_id, runner)

                durable = b"".join(
                    path.read_bytes()
                    for path in sorted((Path(root) / ".vibe").rglob("*"))
                    if path.is_file()
                )
                self.assertNotIn(sentinel.encode("utf-8"), durable)

    def test_provider_confirmed_stop_is_persisted_as_failed_terminal_run(self):
        nodes = [node("n1")]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner(
            events={
                ("n1", "developer"): [
                    ("stopped", {"reason": "provider stopped task"})
                ]
            }
        )
        snapshot = monitor.start(record, runner)

        stopped = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(stopped.nodes["n1"]["status"], "stopped")
        self.assertEqual(stopped.status, "failed")
        self.assertNotIn("n1", stopped.handles)
        self.assertTrue(stopped.nodes["n1"]["reason"])
        resumed = monitor.resume(stopped.run_id, runner)
        ticked = monitor.tick(stopped.run_id, runner)
        self.assertEqual(resumed.status, "failed")
        self.assertEqual(ticked.status, "failed")
        self.assertEqual(len(runner.start_calls), 1)

    def test_poll_loss_and_repeated_unknown_keep_writer_quarantined(self):
        nodes = [node("n1")]
        monitor, record = self.authorized_monitor(nodes)
        runner = PollResponseLostRunner()
        snapshot = monitor.start(record, runner)

        snapshot = monitor.tick(snapshot.run_id, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(snapshot.nodes["n1"]["status"], "blocked_unknown")
        self.assertFalse(
            acquire_writer_lease(self.paths, "n1", ".worktrees/n1", "run-second")
        )

    def test_start_intent_is_recoverable_without_duplicate_after_interruption(self):
        nodes = [node("n1")]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner()
        from vibe_guide import monitor as monitor_module

        real_save = monitor_module.save_snapshot

        def interrupt_after_external_start(paths, snapshot):
            if runner.start_calls:
                raise RuntimeError("process interrupted after external start")
            return real_save(paths, snapshot)

        with patch("vibe_guide.monitor.save_snapshot", side_effect=interrupt_after_external_start):
            with self.assertRaises(RuntimeError):
                monitor.start(record, runner)
        self.assertEqual(len(runner.start_calls), 1)

        recovery_runner = FakeRunner()
        recovered = monitor.resume(
            next((Path(self.temporary.name) / ".vibe/runs").iterdir()).name,
            recovery_runner,
        )

        self.assertEqual(recovery_runner.start_calls, [])
        self.assertEqual(recovered.nodes["n1"]["status"], "running")
        self.assertIn("n1", recovered.handles)
        self.assertFalse(
            acquire_writer_lease(
                self.paths, "n1", ".worktrees/n1", "run-second"
            )
        )

    def test_tampered_event_lineage_blocks_tick(self):
        nodes = [node("n1")]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner()
        snapshot = monitor.start(record, runner)
        event_path = Path(self.temporary.name) / ".vibe/runs" / snapshot.run_id / "events.jsonl"
        records = [json.loads(line) for line in event_path.read_text().splitlines()]
        records[0]["data"]["authorization_digest"] = "0" * 64
        event_path.write_text(
            "".join(json.dumps(item) + "\n" for item in records), encoding="utf-8"
        )

        with self.assertRaises(ValueError):
            monitor.tick(snapshot.run_id, runner)

    def test_recovery_rejects_stale_reviewer_generation_without_releasing_lease(self):
        nodes = [node("n1")]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner(
            events={("n1", "developer"): [("delivered", {"evidence": "delivery"})]}
        )
        snapshot = monitor.start(record, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)
        active = snapshot.nodes["n1"]["active_task"]
        append_event(
            self.paths,
            RunEvent(
                "accepted",
                {"run_id": snapshot.run_id, "node_id": "n1", "evidence": "stale"},
            ),
            {
                "role": "reviewer",
                "task_id": active["task_id"],
                "handle_id": active["handle_id"],
                "generation": active["generation"] - 1,
                "authorization_digest": snapshot.authorization_digest,
                "node_contract_digest": snapshot.node_contract_digest,
            },
        )

        with self.assertRaises(ValueError):
            monitor.resume(snapshot.run_id, runner)
        self.assertFalse(
            acquire_writer_lease(self.paths, "n1", ".worktrees/n1", "run-second")
        )


class VisibleSddRunner(FakeRunner):
    """Provider bridge fixture: one visible worker session per node.

    The binding observed by the bridge carries the topology the monitor
    requested, mirroring a platform that honored the dispatch ruling.
    """

    def __init__(self, events=None, binding_topology="visible-sdd", binding_mode="visible"):
        super().__init__(events)
        self.binding_topology = binding_topology
        self.binding_mode = binding_mode
        self.started_at = []

    def start(self, contract, worktree):
        self.started_at.append(time.monotonic())
        return super().start(contract, worktree)

    def task_binding(self, contract, worktree, run_id, status):
        return TaskBinding(
            provider="fake-visible",
            mode=self.binding_mode,
            topology=self.binding_topology,
            host="fake-host",
            issue_id=contract["node_id"],
            role=contract["role"],
            task_id=contract["task_id"],
            worktree=str(worktree),
            branch=contract.get("branch", "branch-" + contract["node_id"]),
            run_id=run_id,
            status=status,
            generation=contract["generation"],
            limitations=list(contract.get("dispatch_limitations", [])),
        )


V46_DISPATCH_FIXTURE = (
    Path(__file__).parent / "fixtures" / "v46-dispatch-two-node.json"
)


def load_v46_dispatch_fixture():
    payload = json.loads(V46_DISPATCH_FIXTURE.read_text(encoding="utf-8"))
    nodes = [
        DAGNode(
            item["id"],
            item["id"],
            list(item["depends_on"]),
            [],
            item["parallel_group"],
            dict(item["contract"]),
            "ready",
        )
        for item in payload["nodes"]
    ]
    return payload, nodes


class VisibleDispatchTests(unittest.TestCase):
    """V4.6 ISSUE-04: topology-aware parallel dispatch of visible sessions."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = ProjectPaths(Path(self.temporary.name))
        (self.paths.vibe / "state.json").parent.mkdir(parents=True, exist_ok=True)
        (self.paths.vibe / "state.json").write_text('{"workflow_version": 2, "session_gate": "s0_required"}\n', encoding="utf-8")
        save_contract(
            self.paths,
            build_contract(self.paths.root, provider="fake", host_id="local"),
        )
        self.capabilities = AgentCapabilities("fake", True, True, True, True, True, "full")

    def tearDown(self):
        self.temporary.cleanup()

    def authorized_monitor(self, nodes, active_pair_limit=None, topology_rulings=None):
        plan = Plan("plan-1", 1, "docs/prd.md", [item.id for item in nodes], "draft")
        with patch(
            "vibe_guide.dag._write_scope_conflict_errors",
            return_value={},
        ):
            card = build_authorization_card(
                plan,
                nodes,
                self.capabilities,
                active_pair_limit=active_pair_limit,
            )
        return (
            Monitor(self.paths, plan, nodes, topology_rulings=topology_rulings),
            authorize(card, "AUTHORIZE"),
        )

    def test_visible_sdd_dispatch_creates_parallel_visible_sessions_in_one_tick(self):
        payload, nodes = load_v46_dispatch_fixture()
        self.assertTrue(all(not item["depends_on"] for item in payload["nodes"]))
        monitor, record = self.authorized_monitor(
            nodes, topology_rulings=payload["topology_rulings"]
        )
        runner = VisibleSddRunner()

        before = time.monotonic()
        snapshot = monitor.start(record, runner)
        after = time.monotonic()

        self.assertEqual(
            [call["node_id"] for call in runner.start_calls], ["sdd-a", "sdd-b"]
        )
        # Both sessions were created inside the same scheduling tick: their
        # creation timestamps fall into one overlapping window.
        self.assertEqual(len(runner.started_at), 2)
        self.assertTrue(
            all(before <= stamp <= after for stamp in runner.started_at)
        )
        for call in runner.start_calls:
            node_id = call["node_id"]
            self.assertEqual(call["role"], "developer")
            self.assertEqual(call["topology"], "visible-sdd")
            self.assertEqual(call["sdd_protocol"], VISIBLE_SDD_PROTOCOL_REF)
            self.assertEqual(call["files"], [node_id + ".py"])
            self.assertTrue(call["worktree"].endswith(".worktrees/" + node_id))
            self.assertEqual(call["branch"], "branch-" + node_id)
        for node_id in ("sdd-a", "sdd-b"):
            binding = load_task_binding(
                self.paths, node_id, "developer", run_id=snapshot.run_id
            )
            self.assertEqual(binding.mode, "visible")
            self.assertEqual(binding.topology, "visible-sdd")
            self.assertEqual(snapshot.nodes[node_id]["status"], "running")

    def test_session_limit_prefers_tighter_project_config_and_releases_on_archive(self):
        payload, nodes = load_v46_dispatch_fixture()
        (self.paths.vibe / "config.json").write_text(
            json.dumps({"max_active_worker_sessions": 1}), encoding="utf-8"
        )
        monitor, record = self.authorized_monitor(
            nodes, topology_rulings=payload["topology_rulings"]
        )
        runner = VisibleSddRunner(
            events={
                ("sdd-a", "developer"): [
                    (
                        "complete",
                        {
                            "evidence": "delivery",
                            "in_session_review": {
                                "protocol": VISIBLE_SDD_PROTOCOL_REF,
                                "evidence_ref": "session-delivery#review-round-1",
                                "clearance": {"p0": 0, "p1": 0, "p2": 0},
                            },
                        },
                    )
                ]
            }
        )

        snapshot = monitor.start(record, runner)
        # Card default would allow both nodes; the tighter project config
        # limits the tick to one active worker session.
        self.assertEqual([call["node_id"] for call in runner.start_calls], ["sdd-a"])
        self.assertEqual(snapshot.nodes["sdd-b"]["status"], "planned")

        snapshot = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(snapshot.nodes["sdd-a"]["status"], "accepted")
        self.assertTrue(snapshot.nodes["sdd-a"]["pair_archived"])
        self.assertEqual(
            load_task_binding(
                self.paths, "sdd-a", "developer", run_id=snapshot.run_id
            ).status,
            "archived",
        )
        # The archived session released its slot in the same tick, so the
        # next ready node was dispatched without waiting for another pass.
        self.assertEqual(
            [call["node_id"] for call in runner.start_calls], ["sdd-a", "sdd-b"]
        )
        self.assertEqual(snapshot.nodes["sdd-b"]["status"], "running")

    def test_session_limit_prefers_tighter_authorization_card(self):
        payload, nodes = load_v46_dispatch_fixture()
        (self.paths.vibe / "config.json").write_text(
            json.dumps({"max_active_worker_sessions": 5}), encoding="utf-8"
        )
        monitor, record = self.authorized_monitor(
            nodes, active_pair_limit=1, topology_rulings=payload["topology_rulings"]
        )
        runner = VisibleSddRunner()

        snapshot = monitor.start(record, runner)

        self.assertEqual([call["node_id"] for call in runner.start_calls], ["sdd-a"])
        self.assertEqual(snapshot.nodes["sdd-b"]["status"], "planned")

    def test_supervisor_identity_is_structurally_rejected_as_node_writer(self):
        payload, nodes = load_v46_dispatch_fixture()
        rogue = nodes[0]
        rogue.contract["worker"] = "vibeguide_monitor"
        rogue.contract["worker_profile"]["worker"] = "vibeguide_monitor"
        rogue.contract["worker_profile"]["writer"] = "vibeguide_monitor"
        monitor, record = self.authorized_monitor(
            nodes, topology_rulings=payload["topology_rulings"]
        )
        runner = VisibleSddRunner()

        snapshot = monitor.start(record, runner)

        self.assertEqual(len(runner.start_calls), 1)
        self.assertEqual(runner.start_calls[0]["node_id"], "sdd-b")
        self.assertEqual(snapshot.nodes["sdd-a"]["status"], "blocked_unknown")
        self.assertIn("supervisor", snapshot.nodes["sdd-a"]["reason"])

        # Structural, not advisory: a later tick never retries the dispatch.
        snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(len(runner.start_calls), 1)
        self.assertEqual(snapshot.nodes["sdd-a"]["status"], "blocked_unknown")

    def test_visible_sdd_delivery_without_in_session_review_is_rejected_for_re_report(self):
        """ISSUE-83: a missing review payload is a format error, not a brick.

        The delivery used to be recorded and the handle popped before the
        in-session review evidence was checked, bricking the run with no
        re-report path.  Validate-first rejects it as a recoverable
        ``acceptance_rejected`` audit event: no lifecycle transition, live
        handle, same session may correct and re-report.
        """
        from vibe_guide.state import load_events

        payload, nodes = load_v46_dispatch_fixture()
        monitor, record = self.authorized_monitor(
            nodes, topology_rulings=payload["topology_rulings"]
        )
        runner = VisibleSddRunner(
            events={("sdd-a", "developer"): [("complete", {"evidence": "delivery"})]}
        )
        snapshot = monitor.start(record, runner)

        snapshot = monitor.tick(snapshot.run_id, runner)

        current = snapshot.nodes["sdd-a"]
        self.assertNotEqual(current["status"], "blocked_unknown", current.get("reason"))
        self.assertNotEqual(current["status"], "accepted")
        self.assertIn("sdd-a", snapshot.handles)
        self.assertIsInstance(current.get("active_task"), dict)
        rejections = [
            record_ for record_ in load_events(self.paths, snapshot.run_id)
            if record_["event"] == "acceptance_rejected"
            and record_["data"].get("node_id") == "sdd-a"
        ]
        self.assertEqual(len(rejections), 1)
        self.assertTrue(rejections[0]["data"].get("recoverable"))
        binding = load_task_binding(
            self.paths, "sdd-a", "developer", run_id=snapshot.run_id
        )
        self.assertNotEqual(binding.status, "archived")
        # No reviewer task is ever started for visible-sdd.
        self.assertTrue(all(call["role"] == "developer" for call in runner.start_calls))

    def test_binding_topology_mismatch_fails_closed(self):
        payload, nodes = load_v46_dispatch_fixture()
        monitor, record = self.authorized_monitor(
            nodes, topology_rulings=payload["topology_rulings"]
        )
        runner = VisibleSddRunner(binding_topology="dual-visible")

        snapshot = monitor.start(record, runner)

        self.assertEqual(snapshot.nodes["sdd-a"]["status"], "blocked_unknown")
        self.assertEqual(snapshot.nodes["sdd-b"]["status"], "blocked_unknown")
        self.assertEqual(runner.start_calls, [])

    def test_visible_sdd_delivered_node_is_blocked_not_stranded(self):
        """A delivered visible-sdd node at schedule time means lost evidence.

        The in-session review is consumed at delivery; a delivered state that
        reaches the scheduler can only come from an interrupted recovery.
        The node must fail closed (blocked_unknown, slot still accounted),
        never strand in delivered and never dispatch a reviewer task.
        """
        payload, nodes = load_v46_dispatch_fixture()
        monitor, record = self.authorized_monitor(
            nodes, topology_rulings=payload["topology_rulings"]
        )
        runner = VisibleSddRunner()
        snapshot = monitor.start(record, runner)
        current = snapshot.nodes["sdd-a"]
        current["status"] = "delivered"
        current["active_role"] = None
        current["active_task"] = None
        snapshot.handles.pop("sdd-a", None)
        save_snapshot(self.paths, snapshot)

        snapshot = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(snapshot.nodes["sdd-a"]["status"], "blocked_unknown")
        self.assertIn("in-session review", snapshot.nodes["sdd-a"]["reason"])
        self.assertFalse(snapshot.nodes["sdd-a"]["pair_archived"])
        self.assertTrue(all(call["role"] == "developer" for call in runner.start_calls))

    def test_visible_sdd_acceptance_recovers_from_event_replay(self):
        """Crash after the accepted event landed but before the snapshot did.

        Restoring the pre-tick snapshot forces the monitor to replay the
        durable delivered+accepted events; the visible-sdd variant must
        rebuild the accepted, archived node without a reviewer binding.
        """
        payload, nodes = load_v46_dispatch_fixture()
        monitor, record = self.authorized_monitor(
            nodes, topology_rulings=payload["topology_rulings"]
        )
        runner = VisibleSddRunner(
            events={
                ("sdd-a", "developer"): [
                    (
                        "complete",
                        {
                            "evidence": "delivery",
                            "in_session_review": {
                                "protocol": VISIBLE_SDD_PROTOCOL_REF,
                                "evidence_ref": "session-delivery#review-round-1",
                                "clearance": {"p0": 0, "p1": 0, "p2": 0},
                            },
                        },
                    )
                ]
            }
        )
        snapshot = monitor.start(record, runner)
        preserved = deepcopy(snapshot)

        snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(snapshot.nodes["sdd-a"]["status"], "accepted")

        # Lose every snapshot write the tick performed.
        save_snapshot(self.paths, preserved)

        recovered = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(recovered.nodes["sdd-a"]["status"], "accepted")
        self.assertTrue(recovered.nodes["sdd-a"]["pair_archived"])
        self.assertEqual(
            recovered.nodes["sdd-a"]["review_clearance"], {"p0": 0, "p1": 0, "p2": 0}
        )
        self.assertEqual(
            load_task_binding(
                self.paths, "sdd-a", "developer", run_id=snapshot.run_id
            ).status,
            "archived",
        )

    def test_visible_sdd_delivered_replay_without_evidence_is_blocked_unknown(self):
        """Only the delivered event survived: evidence is unverifiable.

        Replay applies the delivery, and the scheduler must then fail closed
        to blocked_unknown instead of stranding the node in delivered.
        """
        payload, nodes = load_v46_dispatch_fixture()
        monitor, record = self.authorized_monitor(
            nodes, topology_rulings=payload["topology_rulings"]
        )
        runner = VisibleSddRunner()
        snapshot = monitor.start(record, runner)
        active = snapshot.nodes["sdd-a"]["active_task"]
        append_event(
            self.paths,
            RunEvent(
                "delivered",
                {
                    "run_id": snapshot.run_id,
                    "node_id": "sdd-a",
                    "evidence": "delivery",
                },
            ),
            {
                "role": "developer",
                "task_id": active["task_id"],
                "handle_id": active["handle_id"],
                "generation": active["generation"],
                "authorization_digest": snapshot.authorization_digest,
                "node_contract_digest": snapshot.node_contract_digest,
            },
        )

        replayed = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(replayed.nodes["sdd-a"]["status"], "blocked_unknown")
        self.assertIn("in-session review", replayed.nodes["sdd-a"]["reason"])
        self.assertFalse(replayed.nodes["sdd-a"]["pair_archived"])
        self.assertTrue(all(call["role"] == "developer" for call in runner.start_calls))

    def test_background_topology_dispatch_carries_disclosure(self):
        """A contract-stamped background ruling degrades with disclosure."""
        background_node = node("bg")
        background_node.contract["dispatch_topology"] = "background"
        monitor, record = self.authorized_monitor([background_node])
        runner = FakeRunner()

        snapshot = monitor.start(record, runner)

        self.assertEqual(runner.start_calls[0]["topology"], "background")
        limitations = runner.start_calls[0]["dispatch_limitations"]
        self.assertTrue(any("background" in item for item in limitations))
        binding = load_task_binding(
            self.paths, "bg", "developer", run_id=snapshot.run_id
        )
        self.assertEqual(binding.topology, "background")
        self.assertEqual(binding.mode, "background")
        self.assertEqual(binding.limitations, limitations)
        self.assertEqual(snapshot.nodes["bg"]["status"], "running")
    def test_parallel_group_audit_refused_nodes_are_never_dispatched(self):
        """ISSUE-04 + ISSUE-06 gate: conflicting group members get no lease.

        Two nodes share parallel_group g1 with intersecting write scopes;
        the parallel-group audit refuses both, so Monitor.start must not
        dispatch either, while an unrelated node in its own group starts.
        """
        from vibe_guide import dag as dag_module

        if not callable(getattr(dag_module, "_parallel_group_errors", None)):
            self.skipTest("parallel-group audit gate lands with ISSUE-06 (PR #62)")
        conflicted_a = node("ga")
        conflicted_b = node("gb")
        for item in (conflicted_a, conflicted_b):
            item.parallel_group = "g1"
            item.allowlist = ["shared.py"]
            item.contract["allowlist"] = ["shared.py"]
            item.contract["worker_profile"]["allowlist"] = ["shared.py"]
        independent = node("gc")
        independent.parallel_group = "g2"
        monitor, record = self.authorized_monitor(
            [conflicted_a, conflicted_b, independent],
            topology_rulings={"codex": "dual-visible"},
        )
        runner = VisibleSddRunner(binding_topology="dual-visible", binding_mode="background")

        snapshot = monitor.start(record, runner)

        self.assertEqual([call["node_id"] for call in runner.start_calls], ["gc"])
        self.assertEqual(snapshot.nodes["ga"]["status"], "planned")
        self.assertEqual(snapshot.nodes["gb"]["status"], "planned")
        self.assertEqual(snapshot.nodes["gc"]["status"], "running")
        # Never dispatched means no writer lease was ever acquired: another
        # run can take both leases immediately.
        self.assertTrue(
            acquire_writer_lease(self.paths, "ga", ".worktrees/ga", "run-other")
        )
        self.assertTrue(
            acquire_writer_lease(self.paths, "gb", ".worktrees/gb", "run-other")
        )


class PathOwnershipGateTests(unittest.TestCase):
    """V4.7 ISSUE-01: validate_path_ownership guards every dispatch round.

    The gate runs over every candidate writer of the round regardless of
    parallel_group; any owned_paths overlap or unverifiable ownership refuses
    the whole round, marks the candidates blocked_unknown and writes the
    run-scoped dag-audit document.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = ProjectPaths(Path(self.temporary.name))
        (self.paths.vibe / "state.json").parent.mkdir(parents=True, exist_ok=True)
        (self.paths.vibe / "state.json").write_text('{"workflow_version": 2, "session_gate": "s0_required"}\n', encoding="utf-8")
        save_contract(
            self.paths,
            build_contract(self.paths.root, provider="fake", host_id="local"),
        )
        self.capabilities = AgentCapabilities("fake", True, True, True, True, True, "full")

    def tearDown(self):
        self.temporary.cleanup()

    def authorized_monitor(self, nodes):
        plan = Plan("plan-1", 1, "docs/prd.md", [item.id for item in nodes], "draft")
        with patch(
            "vibe_guide.dag._write_scope_conflict_errors",
            return_value={},
        ):
            card = build_authorization_card(plan, nodes, self.capabilities)
        return Monitor(self.paths, plan, nodes), authorize(card, "AUTHORIZE")

    @staticmethod
    def owned_node(node_id, owned, group="g1"):
        item = node(node_id)
        item.parallel_group = group
        item.owned_paths = list(owned)
        return item

    def run_audit(self, snapshot):
        path = self.paths.root / ".vibe" / "runs" / snapshot.run_id / "dag-audit.json"
        self.assertTrue(path.is_file(), "run-scoped dag-audit.json was not written")
        data = json.loads(path.read_text(encoding="utf-8"))
        # dag-audit.json is now a list of conflict records (append-on-conflict);
        # return the most recent record so per-round assertions stay unchanged.
        if isinstance(data, list):
            self.assertGreater(len(data), 0, "dag-audit.json list is empty")
            return data[-1]
        return data

    def assert_round_refused(self, snapshot, runner, node_ids):
        self.assertEqual(runner.start_calls, [])
        for node_id in node_ids:
            self.assertEqual(snapshot.nodes[node_id]["status"], "blocked_unknown", node_id)
            self.assertIn("path ownership", snapshot.nodes[node_id]["reason"])
            # Refused before any lease: another run can take the writer slot.
            self.assertTrue(
                acquire_writer_lease(self.paths, node_id, ".worktrees/" + node_id, "run-other")
            )
        events = [e for e in load_events(self.paths, snapshot.run_id) if e["event"] == "path_ownership_conflict"]
        self.assertEqual(len(events), 1)
        self.assertEqual(sorted(events[0]["data"]["node_ids"]), sorted(node_ids))
        audit = self.run_audit(snapshot)
        self.assertEqual(audit["status"], "blocked_unknown")
        self.assertEqual(sorted(audit["node_ids"]), sorted(node_ids))
        self.assertEqual(audit["node_count"], len(node_ids))
        self.assertFalse(audit["path_ownership"]["valid"])
        return audit

    def test_intersecting_owned_paths_refuse_the_round_and_write_audit(self):
        nodes = [self.owned_node("a", ["shared.py"]), self.owned_node("b", ["shared.py"])]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner()

        snapshot = monitor.start(record, runner)

        audit = self.assert_round_refused(snapshot, runner, ["a", "b"])
        self.assertEqual(
            audit["path_ownership"]["conflicts"],
            [{"path": "shared.py", "nodes": ["a", "b"]}],
        )
        self.assertIn("shared.py", snapshot.nodes["a"]["reason"])
        self.assertIn("shared.py", snapshot.nodes["b"]["reason"])
        # Structural, not advisory: a later tick never dispatches them.
        snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(runner.start_calls, [])
        self.assertEqual(snapshot.nodes["a"]["status"], "blocked_unknown")
        self.assertEqual(snapshot.nodes["b"]["status"], "blocked_unknown")

    def test_disjoint_owned_paths_dispatch_normally(self):
        nodes = [self.owned_node("a", ["src/a.py"]), self.owned_node("b", ["src/b.py"])]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner()

        snapshot = monitor.start(record, runner)

        self.assertEqual([call["node_id"] for call in runner.start_calls], ["a", "b"])
        self.assertEqual(snapshot.nodes["a"]["status"], "running")
        self.assertEqual(snapshot.nodes["b"]["status"], "running")
        self.assertFalse((self.paths.root / ".vibe" / "runs" / snapshot.run_id / "dag-audit.json").exists())
        self.assertEqual(
            [e for e in load_events(self.paths, snapshot.run_id) if e["event"] == "path_ownership_conflict"],
            [],
        )

    def test_one_conflicting_pair_refuses_all_three_candidates(self):
        nodes = [
            self.owned_node("a", ["shared.py"]),
            self.owned_node("b", ["shared.py"]),
            self.owned_node("c", ["src/c.py"], group="g2"),
        ]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner()

        snapshot = monitor.start(record, runner)

        audit = self.assert_round_refused(snapshot, runner, ["a", "b", "c"])
        self.assertEqual(audit["path_ownership"]["conflicts"], [{"path": "shared.py", "nodes": ["a", "b"]}])
        self.assertEqual(snapshot.nodes["c"]["status"], "blocked_unknown")

    def test_empty_owned_paths_do_not_block(self):
        nodes = [self.owned_node("a", ["src/a.py"]), self.owned_node("b", [])]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner()

        snapshot = monitor.start(record, runner)

        self.assertEqual([call["node_id"] for call in runner.start_calls], ["a", "b"])
        self.assertEqual(snapshot.nodes["b"]["status"], "running")

    def test_cross_group_intersection_is_refused(self):
        nodes = [
            self.owned_node("a", ["shared.py"], group="g1"),
            self.owned_node("b", ["shared.py"], group="g2"),
        ]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner()

        snapshot = monitor.start(record, runner)

        audit = self.assert_round_refused(snapshot, runner, ["a", "b"])
        self.assertEqual(audit["path_ownership"]["conflicts"], [{"path": "shared.py", "nodes": ["a", "b"]}])

    def test_ungrouped_intersection_is_refused(self):
        nodes = [
            self.owned_node("a", ["shared.py"], group=None),
            self.owned_node("b", ["shared.py"], group=None),
        ]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner()

        snapshot = monitor.start(record, runner)

        audit = self.assert_round_refused(snapshot, runner, ["a", "b"])
        self.assertEqual(audit["path_ownership"]["conflicts"], [{"path": "shared.py", "nodes": ["a", "b"]}])

    def test_missing_ownership_metadata_fails_closed(self):
        # DAGNode itself refuses non-list ownership fields, so the
        # missing_nodes branch is driven through the validator's own result
        # shape: the monitor must treat it exactly like a conflict.
        from vibe_guide import monitor as monitor_module
        from vibe_guide.path_ownership import PathOwnershipResult

        nodes = [self.owned_node("m", ["src/m.py"], group="g1"), self.owned_node("n", ["src/n.py"], group="g2")]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner()

        with patch.object(
            monitor_module,
            "validate_path_ownership",
            return_value=PathOwnershipResult(False, [], ["m"]),
        ):
            snapshot = monitor.start(record, runner)

        audit = self.assert_round_refused(snapshot, runner, ["m", "n"])
        self.assertEqual(audit["path_ownership"]["conflicts"], [])
        self.assertEqual(audit["path_ownership"]["missing_nodes"], ["m"])

    def test_invalid_owned_path_fails_closed(self):
        nodes = [
            self.owned_node("a", ["../outside.py"], group="g1"),
            self.owned_node("b", ["src/b.py"], group="g2"),
        ]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner()

        snapshot = monitor.start(record, runner)

        self.assert_round_refused(snapshot, runner, ["a", "b"])

    def test_accepted_predecessor_sharing_owned_paths_does_not_block_successor(self):
        """A hard dependency is the legitimate way to serialize one file.

        Distinct groups keep the V4.6 parallel-group audit out of the way so
        only the ownership gate decides.
        """
        nodes = [self.owned_node("a", ["shared.py"], group="g1"), self.owned_node("b", ["shared.py"], group="g2")]
        nodes[1].depends_on = ["a"]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner(
            events={
                ("a", "developer"): [("delivered", {"evidence": "delivery"})],
                ("a", "reviewer"): [("accepted", {"evidence": "ok"})],
            }
        )

        snapshot = monitor.start(record, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(snapshot.nodes["a"]["status"], "accepted")
        self.assertEqual(snapshot.nodes["b"]["status"], "running")
        self.assertEqual(
            [(call["node_id"], call["role"]) for call in runner.start_calls],
            [("a", "developer"), ("a", "reviewer"), ("b", "developer")],
        )
        self.assertFalse((self.paths.root / ".vibe" / "runs" / snapshot.run_id / "dag-audit.json").exists())

    def test_in_flight_writer_blocks_a_new_candidate_without_retro_blocking_itself(self):
        nodes = [
            self.owned_node("a", ["shared.py"], group="g1"),
            self.owned_node("b", ["shared.py"], group="g2"),
            self.owned_node("c", ["src/c.py"], group="g3"),
        ]
        nodes[1].depends_on = ["c"]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner(
            events={
                ("c", "developer"): [("delivered", {"evidence": "delivery"})],
                ("c", "reviewer"): [("accepted", {"evidence": "ok"})],
            }
        )

        snapshot = monitor.start(record, runner)
        self.assertEqual([call["node_id"] for call in runner.start_calls], ["a", "c"])
        snapshot = monitor.tick(snapshot.run_id, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(snapshot.nodes["c"]["status"], "accepted")
        self.assertEqual(snapshot.nodes["a"]["status"], "running")
        self.assertEqual(snapshot.nodes["b"]["status"], "blocked_unknown")
        self.assertIn("shared.py", snapshot.nodes["b"]["reason"])
        self.assertNotIn("b", [call["node_id"] for call in runner.start_calls])
        audit = self.run_audit(snapshot)
        self.assertEqual(sorted(audit["node_ids"]), ["a", "b"])
        self.assertEqual(audit["path_ownership"]["blocked_nodes"], ["b"])
        self.assertEqual(audit["path_ownership"]["conflicts"], [{"path": "shared.py", "nodes": ["b", "a"]}])


if __name__ == "__main__":
    unittest.main()
