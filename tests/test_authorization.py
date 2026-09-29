from dataclasses import replace
from copy import deepcopy
import unittest

from vibe_guide.authorization import (
    BACKGROUND_MODE_DISCLOSURES,
    authorize,
    build_authorization_card,
    is_authorization_valid,
    refresh_authorization_card,
    validate_runtime_contract,
)
from vibe_guide.models import AgentCapabilities, DAGNode, Plan


def node(node_id, files, worker="worker-1"):
    return DAGNode(
        node_id,
        node_id,
        [],
        [],
        "parallel",
        {"files": files, "worker": worker, "worktree": ".worktrees/" + node_id},
        "ready",
    )


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.plan = Plan("plan-1", 3, "docs/prd.md", ["n1", "n2"], "draft")
        self.nodes = [node("n1", ["b.py", "a.py"]), node("n2", ["c.py"], "worker-2")]
        self.capabilities = AgentCapabilities(
            "codex", True, True, True, True, True, "full"
        )

    def test_card_lists_actions_scope_and_explicitly_excludes_deploy(self):
        card = build_authorization_card(self.plan, self.nodes, self.capabilities)

        # Default switch is deny: the remote Git group (commit/push/PR/MR/merge)
        # is not granted, per the confirmed V4.5 session-entry design.
        self.assertEqual(
            card.allowed_actions,
            ("accept", "develop", "review", "rework", "test"),
        )
        self.assertEqual(card.remote_git_actions, "deny")
        self.assertEqual(card.excluded_actions, ("create_mr", "deploy", "merge", "push"))
        self.assertEqual(card.node_ids, ("n1", "n2"))
        self.assertEqual(card.file_scope, ("a.py", "b.py", "c.py"))
        self.assertEqual(card.worker_scope, ("worker-1", "worker-2"))
        self.assertEqual(card.plan_version, 3)
        self.assertNotIn("token", card.to_dict())

    def test_authorization_binds_canonical_plan_and_invalidates_on_change(self):
        first = build_authorization_card(self.plan, self.nodes, self.capabilities)
        reordered = build_authorization_card(
            Plan("plan-1", 3, "docs/prd.md", ["n2", "n1"], "draft"),
            list(reversed(self.nodes)),
            self.capabilities,
        )
        self.assertEqual(first.digest, reordered.digest)

        record = authorize(first, "AUTHORIZE")
        self.assertTrue(is_authorization_valid(record, self.plan))
        self.assertFalse(
            is_authorization_valid(
                record, Plan("plan-1", 4, "docs/prd.md", ["n1", "n2"], "draft")
            )
        )
        self.assertFalse(
            is_authorization_valid(
                record, Plan("plan-1", 3, "docs/prd.md", ["n1"], "draft")
            )
        )

    def test_confirmation_must_be_explicit(self):
        card = build_authorization_card(self.plan, self.nodes, self.capabilities)
        with self.assertRaises(ValueError):
            authorize(card, "yes")

    def test_refresh_preserves_explicit_local_merge_authorization(self):
        plan = Plan("plan-local", 1, "docs/prd.md", ["n1"], "draft")
        node_value = node("n1", ["safe.py"])
        card = build_authorization_card(
            plan,
            [node_value],
            self.capabilities,
            allowed_actions=("develop", "test", "review", "merge_local"),
        )
        refreshed = refresh_authorization_card(plan, [node_value], card)

        self.assertIn("merge_local", refreshed.allowed_actions)
        self.assertEqual(
            authorize(refreshed, "AUTHORIZE").allowed_actions,
            ("develop", "test", "review", "merge_local"),
        )

    def test_authorization_rejects_tampered_scope_or_actions(self):
        card = build_authorization_card(self.plan, self.nodes, self.capabilities)
        record = authorize(card, "AUTHORIZE")

        self.assertFalse(
            is_authorization_valid(
                replace(record, allowed_actions=record.allowed_actions + ("push",)),
                self.plan,
            )
        )
        self.assertFalse(
            is_authorization_valid(
                replace(record, file_scope=record.file_scope + ("outside.py",)),
                self.plan,
            )
        )

    def test_authorization_rejects_overlapping_parallel_write_scope(self):
        nodes = [node("n1", ["README.md"], "worker-1"), node("n2", ["README.md"], "worker-2")]
        plan = Plan("plan-overlap", 1, "docs/prd.md", ["n1", "n2"], "draft")
        with self.assertRaisesRegex(ValueError, "write scope"):
            build_authorization_card(plan, nodes, self.capabilities)

    def test_full_executable_contract_is_bound_and_excluded_actions_are_rejected(self):
        base = node("n1", ["safe.py"])
        base.contract.update(
            {
                "branch": "codex/safe",
                "provider": "codex",
                "mode": "visible",
                "hostId": "local",
                "developer_task_id": "thread-safe",
            }
        )
        plan = Plan("plan-contract", 1, "docs/prd.md", ["n1"], "draft")
        baseline = build_authorization_card(plan, [base], self.capabilities)

        mutations = {
            "files": ["outside.py"],
            "worker": "worker-evil",
            "worktree": "../outside",
            "branch": "codex/evil",
            "provider": "other-provider",
            "developer_task_id": "thread-other",
        }
        for key, value in mutations.items():
            with self.subTest(field=key):
                changed = deepcopy(base)
                changed.contract[key] = value
                card = build_authorization_card(plan, [changed], self.capabilities)
                self.assertNotEqual(card.digest, baseline.digest)

        excluded = deepcopy(base)
        excluded.contract["action"] = "deploy"
        with self.assertRaises(ValueError):
            build_authorization_card(plan, [excluded], self.capabilities)

    def test_authorization_rejects_raw_secret_fields(self):
        secret_node = node("n1", ["safe.py"])
        secret_node.contract["token"] = "raw-secret-sentinel"
        plan = Plan("plan-secret", 1, "docs/prd.md", ["n1"], "draft")

        with self.assertRaises(ValueError):
            build_authorization_card(plan, [secret_node], self.capabilities)

    def test_action_values_are_canonicalized_and_all_deploy_shapes_are_rejected(self):
        plan = Plan("plan-actions", 1, "docs/prd.md", ["n1"], "draft")
        canonical = node("n1", ["safe.py"])
        canonical.contract["action"] = "develop"
        decorated = deepcopy(canonical)
        decorated.contract["action"] = "  DeVeLoP  "

        self.assertEqual(
            build_authorization_card(plan, [canonical], self.capabilities).digest,
            build_authorization_card(plan, [decorated], self.capabilities).digest,
        )

        variants = (
            {"action": "DEPLOY"},
            {"actions": ["test", " Deploy "]},
            {"provider": {"requested_actions": ["commit", "dEpLoY"]}},
            {"provider": {"nested": {"allowed-actions": " DEPLOY "}}},
        )
        for contract_update in variants:
            with self.subTest(contract=contract_update):
                candidate = node("n1", ["safe.py"])
                candidate.contract.update(contract_update)
                with self.assertRaises(ValueError):
                    build_authorization_card(plan, [candidate], self.capabilities)

    def test_runtime_actions_are_closed_and_files_are_normalized_lists(self):
        for action in (
            "production-deploy",
            "install-skill",
            "grant-system-permission",
            "external-write",
        ):
            with self.subTest(action=action), self.assertRaises(ValueError):
                validate_runtime_contract(
                    {"action": action, "files": ["src/app.py"]}
                )

        for files in (
            "src/app.py",
            ["../outside.py"],
            ["/absolute.py"],
            ["src/app.py", "src/app.py"],
        ):
            with self.subTest(files=files), self.assertRaises(ValueError):
                validate_runtime_contract({"action": "test", "files": files})

        for actions in ([], [["test"]], ["test", 1]):
            with self.subTest(actions=actions), self.assertRaises(ValueError):
                validate_runtime_contract(
                    {"actions": actions, "files": ["src/app.py"]}
                )

        normalized = validate_runtime_contract(
            {"actions": ["TEST", "develop"], "files": ["src/./app.py"]},
            authorized_actions=("develop", "test"),
            authorized_files=("src/app.py",),
        )
        self.assertEqual(normalized["actions"], ["test", "develop"])
        self.assertEqual(normalized["files"], ["src/app.py"])

    def test_authorization_binds_active_pair_limit_and_normalized_file_scope(self):
        normalized_node = node("n1", ["src/./app.py"])
        normalized_node.contract["provider_scope"] = {
            "files": ["tests/./test_app.py"]
        }
        plan = Plan("plan-capacity", 1, "docs/prd.md", ["n1"], "draft")

        card = build_authorization_card(
            plan, [normalized_node], self.capabilities, active_pair_limit=1
        )

        self.assertEqual(
            card.file_scope, ("src/app.py", "tests/test_app.py")
        )
        self.assertEqual(card.active_pair_limit, 1)
        self.assertEqual(authorize(card, "AUTHORIZE").active_pair_limit, 1)


if __name__ == "__main__":
    unittest.main()


class AuthorizationWorkersSchemaTests(unittest.TestCase):
    """V4.6 ISSUE-02: structured per-node workers, main-session refusal and
    machine-checked background downgrade disclosure on authorization cards."""

    def setUp(self):
        self.plan = Plan("plan-workers", 1, "docs/prd.md", ["n1", "n2"], "draft")
        self.nodes = [node("n1", ["a.py"]), node("n2", ["b.py"], "worker-2")]
        self.capabilities = AgentCapabilities(
            "codex", True, True, True, True, True, "full"
        )

    @staticmethod
    def _workers_by_id(card_or_record):
        return {entry["node_id"]: entry for entry in card_or_record.workers}

    def test_workers_default_to_dual_visible_developer_entries(self):
        card = build_authorization_card(self.plan, self.nodes, self.capabilities)

        workers = self._workers_by_id(card)
        self.assertEqual(set(workers), {"n1", "n2"})
        entry = workers["n1"]
        self.assertEqual(
            set(entry),
            {"node_id", "topology", "mode", "role", "session_source", "limitations"},
        )
        self.assertEqual(entry["topology"], "dual-visible")
        self.assertEqual(entry["mode"], "visible")
        self.assertEqual(entry["role"], "developer")
        self.assertEqual(entry["session_source"], "worker-1")
        self.assertEqual(entry["limitations"], ())
        summary = card.topology_summary
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["visible"], 2)
        self.assertEqual(summary["background"], 0)
        self.assertEqual(summary["by_topology"]["dual-visible"], 2)

    def test_workers_and_topology_summary_are_bound_into_the_digest(self):
        base = build_authorization_card(self.plan, self.nodes, self.capabilities)
        changed = build_authorization_card(
            self.plan,
            self.nodes,
            self.capabilities,
            workers={"n1": {"topology": "visible-sdd"}},
        )

        self.assertNotEqual(base.digest, changed.digest)
        self.assertEqual(
            changed.topology_summary["by_topology"]["visible-sdd"], 1
        )

    def test_main_session_developer_variants_are_refused(self):
        variants = (
            "main session",
            "Main Session",
            "MAIN SESSION",
            "main-session",
            "main_session",
            "codex main session",
            "Codex Main Session",
            "  codex   main   session  ",
            "主会话",
            "main",
        )
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaises(ValueError):
                build_authorization_card(
                    self.plan,
                    self.nodes,
                    self.capabilities,
                    workers={"n1": {"session_source": variant}},
                )

    def test_main_session_developer_separator_and_compact_variants_are_refused(self):
        variants = (
            "MainSession",
            "mainsession",
            "main.session",
            "main/session",
            "main:session",
            "main thread",
            "main-thread",
            "mainthread",
            "MainThread",
            "claude main.thread",
        )
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaises(ValueError):
                build_authorization_card(
                    self.plan,
                    self.nodes,
                    self.capabilities,
                    workers={"n1": {"session_source": variant}},
                )

    def test_non_string_contract_worker_identity_is_refused(self):
        for bad_worker in (["main session"], {"session": "main"}, 7):
            with self.subTest(worker=bad_worker):
                evil = DAGNode(
                    "n9",
                    "n9",
                    [],
                    [],
                    None,
                    {"files": ["x.py"], "worker": bad_worker},
                    "ready",
                )
                plan = Plan("plan-bad-worker", 1, "docs/prd.md", ["n9"], "draft")
                with self.assertRaises(ValueError):
                    build_authorization_card(plan, [evil], self.capabilities)

    def test_main_session_identity_from_contract_worker_is_refused(self):
        evil = node("n9", ["x.py"], worker="Codex Main Session")
        plan = Plan("plan-main-session", 1, "docs/prd.md", ["n9"], "draft")

        with self.assertRaises(ValueError):
            build_authorization_card(plan, [evil], self.capabilities)

    def test_developer_rule_does_not_reject_main_session_reviewer(self):
        card = build_authorization_card(
            self.plan,
            self.nodes,
            self.capabilities,
            workers={"n1": {"role": "reviewer", "session_source": "codex main session"}},
        )

        self.assertEqual(self._workers_by_id(card)["n1"]["role"], "reviewer")

    def test_background_worker_without_limitations_is_refused(self):
        with self.assertRaises(ValueError):
            build_authorization_card(
                self.plan,
                self.nodes,
                self.capabilities,
                workers={"n1": {"mode": "background"}},
            )

    def test_background_worker_with_partial_disclosure_is_refused(self):
        partial = [
            "不可见：background 任务不在桌面 App 中可见",
            "不可直接进入：用户不能直接进入该任务会话",
        ]
        with self.assertRaises(ValueError):
            build_authorization_card(
                self.plan,
                self.nodes,
                self.capabilities,
                workers={"n1": {"mode": "background", "limitations": partial}},
            )

    def test_background_worker_with_full_disclosure_is_issued(self):
        card = build_authorization_card(
            self.plan,
            self.nodes,
            self.capabilities,
            workers={
                "n1": {
                    "mode": "background",
                    "limitations": list(BACKGROUND_MODE_DISCLOSURES),
                }
            },
        )

        entry = self._workers_by_id(card)["n1"]
        self.assertEqual(entry["mode"], "background")
        self.assertEqual(entry["topology"], "background")
        self.assertEqual(card.topology_summary["background"], 1)
        self.assertEqual(card.topology_summary["visible"], 1)
        record = authorize(card, "AUTHORIZE")
        self.assertEqual(self._workers_by_id(record)["n1"]["mode"], "background")
        self.assertEqual(record.topology_summary["by_topology"]["background"], 1)

    def test_background_topology_requires_background_mode(self):
        with self.assertRaises(ValueError):
            build_authorization_card(
                self.plan,
                self.nodes,
                self.capabilities,
                workers={
                    "n1": {
                        "topology": "background",
                        "mode": "visible",
                        "limitations": list(BACKGROUND_MODE_DISCLOSURES),
                    }
                },
            )

    def test_workers_entries_outside_the_dag_are_refused(self):
        with self.assertRaises(ValueError):
            build_authorization_card(
                self.plan,
                self.nodes,
                self.capabilities,
                workers={"n9": {"topology": "dual-visible"}},
            )

    def test_workers_entries_with_unknown_keys_are_refused(self):
        with self.assertRaises(ValueError):
            build_authorization_card(
                self.plan,
                self.nodes,
                self.capabilities,
                workers={"n1": {"topology": "dual-visible", "surprise": "x"}},
            )

    def test_active_pair_limit_snapshot_is_carried_from_the_caller(self):
        card = build_authorization_card(
            self.plan, self.nodes, self.capabilities, active_pair_limit=2
        )

        self.assertEqual(card.active_pair_limit, 2)
        self.assertEqual(authorize(card, "AUTHORIZE").active_pair_limit, 2)

    def test_refresh_preserves_workers_and_disclosures(self):
        card = build_authorization_card(
            self.plan,
            self.nodes,
            self.capabilities,
            workers={
                "n1": {
                    "mode": "background",
                    "limitations": list(BACKGROUND_MODE_DISCLOSURES),
                }
            },
        )
        refreshed = refresh_authorization_card(self.plan, self.nodes, card)

        entry = self._workers_by_id(refreshed)["n1"]
        self.assertEqual(entry["mode"], "background")
        self.assertEqual(
            entry["limitations"],
            tuple(BACKGROUND_MODE_DISCLOSURES),
        )
