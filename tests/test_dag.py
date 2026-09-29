import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide.dag import DAGAuditResult, audit_dag, node_scoped_ready, ready_nodes, render_plan_artifacts, validate_dag
from vibe_guide.models import DAGNode, Plan
from vibe_guide.path_ownership import normalize_project_path


def node(node_id, depends=None, integration=None, group=None, status="planned", contract=None):
    default_contract = {
        "input": "request",
        "output": "result",
        "error_behavior": "return an error",
        "acceptance_example": "example passes",
    }
    return DAGNode(node_id, node_id, depends or [], integration or [], group, default_contract if contract is None else contract, status)


def audited_node(node_id, depends=None, integration=None, group=None, status="planned", contract=None, writer=None, allowlist=None):
    base = {
        "input": "request",
        "output": "result",
        "error_behavior": "return an error",
        "acceptance_examples": ["example passes"],
        "risk_tags": ["scheduling"],
        "writer": writer or "writer-" + node_id,
        "worktree": ".vibe/worktrees/" + node_id,
        "allowlist": allowlist or ["vibe_guide/" + node_id + ".py"],
    }
    if contract is not None:
        base = dict(contract)
    return DAGNode(
        node_id, node_id, depends or [], integration or [], group, base, status,
        writer=writer or "", allowlist=allowlist or [],
    )


class DAGTests(unittest.TestCase):
    def test_authoritative_worker_profile_identity_unlocks_nodes_after_v2_0(self):
        def authoritative(node_id, depends=None, status="planned"):
            contract = {
                "input": "request",
                "output": "result",
                "error_behavior": "return blocked_dag",
                "acceptance_example": "ready set is observable",
                "risk_tags": ["scheduling"],
                "worker_profile": {
                    "writer": "codex-app-visible-developer",
                    "allowlist": [
                        "vibe_guide/{}.py".format(node_id.lower()),
                        "tests/test_{}.py".format(node_id.lower()),
                    ],
                },
            }
            return DAGNode(
                node_id, node_id, depends or [], [], "specialized", contract, status,
                worktree=".vibe/worktrees/" + node_id,
            )

        nodes = [
            authoritative("V2-0", status="delivered"),
            authoritative("V2-1", ["V2-0"]),
            authoritative("V2-2", ["V2-0"]),
            authoritative("V2-3", ["V2-0"]),
            authoritative("V2-4", ["V2-0"]),
            authoritative("V2-5", ["V2-0"]),
            authoritative("V2-6", ["V2-0"]),
            authoritative("V2-8", ["V2-0"]),
            authoritative("V2-7", ["V2-1", "V2-2", "V2-3", "V2-4", "V2-5", "V2-6", "V2-8"]),
        ]
        result = audit_dag(Plan("v2", 5, "prd.md", [node.id for node in nodes], "authorized", nodes=nodes))
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.ready_nodes, ["V2-1", "V2-2", "V2-3", "V2-4", "V2-5", "V2-6", "V2-8"])
        self.assertEqual(result.blocked_nodes, ["V2-7"])

    def test_authoritative_worker_profile_missing_or_inconsistent_identity_blocks(self):
        base = {
            "input": "request",
            "output": "result",
            "error_behavior": "return blocked_dag",
            "acceptance_example": "ready set is observable",
            "risk_tags": ["scheduling"],
        }
        missing = dict(base, worker_profile={"writer": "writer"})
        inconsistent = dict(base, writer="top-writer", worker_profile={"writer": "nested-writer", "allowlist": ["same.py"]})
        nodes = [
            DAGNode("missing", "missing", [], [], None, missing, "planned", worktree=".vibe/worktrees/missing"),
            DAGNode("inconsistent", "inconsistent", [], [], None, inconsistent, "planned", worktree=".vibe/worktrees/inconsistent"),
        ]
        result = audit_dag(Plan("p1", 1, "prd.md", [node.id for node in nodes], "authorized", nodes=nodes))
        self.assertEqual(result.status, "blocked_dag")
        self.assertTrue(any("allowlist" in reason for reason in result.reasons["missing"]))
        self.assertTrue(any("writer mismatch" in reason for reason in result.reasons["inconsistent"]))
    def test_integration_after_does_not_block_readiness(self):
        n = node("n1", integration=["later"])
        self.assertEqual(ready_nodes([n]), [n])

    def test_incomplete_hard_dependency_blocks(self):
        dep = node("dep", status="running")
        child = node("child", depends=["dep"])
        self.assertEqual(ready_nodes([dep, child]), [])

    def test_only_accepted_hard_dependency_unlocks_dependent_node(self):
        child = node("child", depends=["dep"])

        self.assertEqual(ready_nodes([node("dep", status="delivered"), child]), [])
        self.assertEqual(
            ready_nodes([node("dep", status="accepted"), child]),
            [child],
        )

    def test_duplicate_ids_and_cycles_fail_validation(self):
        duplicate = validate_dag([node("n1"), node("n1")])
        self.assertFalse(duplicate.valid)
        cycle = validate_dag([node("a", depends=["b"]), node("b", depends=["a"])])
        self.assertFalse(cycle.valid)

    def test_independent_parallel_group_nodes_are_ready_together(self):
        a, b = node("a", group="g"), node("b", group="g")
        self.assertEqual(ready_nodes([a, b]), [a, b])

    def test_empty_contract_is_not_ready(self):
        self.assertEqual(ready_nodes([node("n", contract={})]), [])

    def test_incomplete_placeholder_contract_is_not_ready(self):
        self.assertEqual(ready_nodes([node("n", contract={"input": "request"})]), [])

    def test_invalid_contract_does_not_suppress_independent_ready_node(self):
        bad = node("bad", contract={})
        good = node("good")

        self.assertEqual(ready_nodes([bad, good]), [good])

    def test_whitespace_and_empty_nested_contract_fields_are_not_ready(self):
        contracts = (
            {
                "input": "   ",
                "output": "result",
                "error_behavior": "return an error",
                "acceptance_example": "example passes",
            },
            {
                "inputs": {"request": {"fields": ["account_id", "   "]}},
                "outputs": ["result"],
                "errors": ["return an error"],
                "acceptance_examples": ["example passes"],
            },
            {
                "inputs": {"request": {}},
                "outputs": ["result"],
                "errors": ["return an error"],
                "acceptance_examples": ["example passes"],
            },
        )

        for contract in contracts:
            with self.subTest(contract=contract):
                self.assertEqual(ready_nodes([node("n", contract=contract)]), [])

    def test_design_change_blocks_node(self):
        blocked_contract = {
            "input": "request",
            "output": "result",
            "error_behavior": "return an error",
            "acceptance_example": "example passes",
            "design_change": True,
        }
        self.assertEqual(ready_nodes([node("n", contract=blocked_contract)]), [])

    def test_render_plan_artifacts(self):
        fixture = Path(__file__).parent / "fixtures" / "plans" / "basic-plan.json"
        plan = Plan.from_dict(json.loads(fixture.read_text(encoding="utf-8")))
        with tempfile.TemporaryDirectory() as d:
            artifacts = render_plan_artifacts(plan, Path(d))
            self.assertTrue(artifacts.dag_path.exists())
            self.assertTrue(artifacts.plan_path.exists())

    def test_render_plan_artifacts_rejects_collision_without_overwrite(self):
        plan = Plan("p1", 1, "prd.md", ["n1"], "draft")
        with tempfile.TemporaryDirectory() as d:
            output_dir = Path(d)
            dag_path = output_dir / "dag.yaml"
            dag_path.write_text("prior evidence\n", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                render_plan_artifacts(plan, output_dir)

            self.assertEqual(dag_path.read_text(encoding="utf-8"), "prior evidence\n")
            self.assertFalse((output_dir / "plan.md").exists())

    def test_audit_returns_blocked_dag_for_missing_contract(self):
        plan = Plan("p1", 1, "prd.md", ["n1"], "authorized", nodes=[audited_node("n1", contract={"output": "result"})])
        result = audit_dag(plan)
        self.assertIsInstance(result, DAGAuditResult)
        self.assertEqual(result.status, "blocked_dag")
        self.assertEqual(result.ready_nodes, [])
        self.assertIn("n1", result.blocked_nodes)
        self.assertTrue(result.reasons["n1"])

    def test_audit_keeps_integration_after_non_blocking_and_reports_parallel_ready_set(self):
        nodes = [
            audited_node("V2-0", status="accepted"),
            audited_node("V2-1", depends=["V2-0"], integration=["V2-9"], group="specialized"),
            audited_node("V2-2", depends=["V2-0"], group="specialized"),
            audited_node("V2-3", depends=["V2-0"], group="specialized"),
            audited_node("V2-4", depends=["V2-0"], group="specialized"),
            audited_node("V2-5", depends=["V2-0"], group="specialized"),
            audited_node("V2-6", depends=["V2-0"], group="specialized"),
            audited_node("V2-8", depends=["V2-0"], group="specialized"),
            audited_node("V2-7", depends=["V2-1", "V2-2", "V2-3", "V2-4", "V2-5", "V2-6", "V2-8"], group="integration"),
        ]
        result = audit_dag(Plan("v2", 1, "prd.md", [n.id for n in nodes], "authorized", nodes=nodes))
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.ready_nodes, ["V2-1", "V2-2", "V2-3", "V2-4", "V2-5", "V2-6", "V2-8"])
        self.assertEqual(result.blocked_nodes, ["V2-7"])
        self.assertIn("hard dependencies", " ".join(result.reasons["V2-7"]))

    def test_audit_blocks_hard_cycle_and_missing_dependency(self):
        nodes = [audited_node("a", depends=["b"]), audited_node("b", depends=["a"]), audited_node("c", depends=["missing"])]
        result = audit_dag(Plan("p1", 1, "prd.md", ["a", "b", "c"], "authorized", nodes=nodes))
        self.assertEqual(result.status, "blocked_dag")
        self.assertEqual(set(result.blocked_nodes), {"a", "b", "c"})
        self.assertTrue(any("cycle" in reason for reason in result.reasons["a"]))
        self.assertTrue(any("unknown hard dependenc" in reason for reason in result.reasons["c"]))

    def test_audit_blocks_duplicate_writer_and_allowlist_mismatch(self):
        nodes = [
            audited_node("a", writer="same-writer", allowlist=["same.py"], contract={"worktree": ".vibe/worktrees/shared"}),
            audited_node("b", writer="same-writer", allowlist=["same.py"], contract={"worktree": ".vibe/worktrees/shared"}),
            audited_node("c", contract={"allowlist": ["c.py"], "writer": "writer-c"}, allowlist=["other.py"]),
        ]
        result = audit_dag(Plan("p1", 1, "prd.md", ["a", "b", "c"], "authorized", nodes=nodes))
        self.assertEqual(result.status, "blocked_dag")
        self.assertTrue(any("duplicate writer" in reason for reason in result.reasons["a"]))
        self.assertTrue(any("allowlist" in reason for reason in result.reasons["c"]))

    def test_audit_blocks_overlapping_write_scope_without_hard_dependency(self):
        nodes = [
            audited_node("a", group="first", allowlist=["vibe_guide/cli.py"]),
            audited_node("b", group="second", allowlist=["vibe_guide/cli.py"]),
        ]
        result = audit_dag(Plan("p1", 1, "prd.md", ["a", "b"], "authorized", nodes=nodes))
        self.assertEqual(result.status, "blocked_dag")
        self.assertTrue(any("overlapping write scope" in reason and "vibe_guide/cli.py" in reason for reason in result.reasons["a"]))
        self.assertTrue(any("depends_on" in reason or "split" in reason for reason in result.reasons["b"]))

    def test_audit_allows_overlapping_write_scope_when_hard_dependency_serializes_nodes(self):
        nodes = [
            audited_node("a", group="first", status="delivered", allowlist=["README.md"]),
            audited_node("b", depends=["a"], group="second", allowlist=["README.md"]),
        ]
        result = audit_dag(Plan("p1", 1, "prd.md", ["a", "b"], "authorized", nodes=nodes))
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.ready_nodes, ["b"])

    def test_audit_reports_conflicting_nodes_paths_and_split_direction(self):
        nodes = [
            audited_node("a", group="first", allowlist=["README.md", "docs/a.md"]),
            audited_node("b", group="second", allowlist=["README.md", "docs/b.md"]),
        ]
        result = audit_dag(Plan("p1", 1, "prd.md", ["a", "b"], "authorized", nodes=nodes))
        reason = " ".join(result.reasons["a"])
        self.assertIn("a and b", reason)
        self.assertIn("README.md", reason)
        self.assertTrue("depends_on" in reason and "split" in reason)

    def test_render_includes_audit_contract_and_identity_evidence(self):
        n1 = audited_node("n1", status="accepted")
        n2 = audited_node("n2", depends=["n1"])
        plan = Plan("p1", 1, "prd.md", ["n1", "n2"], "authorized", nodes=[n1, n2])
        with tempfile.TemporaryDirectory() as d:
            artifacts = render_plan_artifacts(plan, Path(d))
            data = json.loads(artifacts.dag_path.read_text(encoding="utf-8"))
            self.assertEqual(data["audit"]["status"], "ready")
            rendered = {item["id"]: item for item in data["nodes"]}
            self.assertEqual(rendered["n2"]["writer"], "writer-n2")
            self.assertEqual(rendered["n2"]["allowlist"], ["vibe_guide/n2.py"])
            self.assertEqual(rendered["n2"]["depends_on"], ["n1"])

    def test_render_plan_artifacts_rolls_back_partial_publish(self):
        plan = Plan("p1", 1, "prd.md", ["n1"], "draft")
        real_link = os.link
        calls = 0

        def fail_second_link(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated second publish failure")
            return real_link(source, destination)

        with tempfile.TemporaryDirectory() as d:
            output_dir = Path(d)
            with patch("os.link", side_effect=fail_second_link):
                with self.assertRaisesRegex(OSError, "second publish failure"):
                    render_plan_artifacts(plan, output_dir)

            self.assertFalse((output_dir / "dag.yaml").exists())
            self.assertFalse((output_dir / "plan.md").exists())
class ParallelGroupAuditTests(unittest.TestCase):
    """V4.6 ISSUE-06: parallel_group dispatch-time dependency audit."""

    def _group_node(self, node_id, group="g", allowlist=None, owned=None,
                    contract_overrides=None, status="planned"):
        contract = {
            "input": "request",
            "output": "result",
            "error_behavior": "return blocked_dag",
            "acceptance_examples": ["example passes"],
            "risk_tags": ["scheduling"],
            "writer": "writer-" + node_id,
            "worktree": ".vibe/worktrees/" + node_id,
            "allowlist": list(allowlist if allowlist is not None else ["vibe_guide/{}.py".format(node_id)]),
        }
        contract.update(contract_overrides or {})
        return DAGNode(
            node_id, node_id, [], [], group, contract, status,
            writer=contract.get("writer", ""),
            worktree=contract.get("worktree", ""),
            allowlist=list(allowlist if allowlist is not None else ["vibe_guide/{}.py".format(node_id)]),
            owned_paths=list(owned or []),
        )

    def _plan(self, nodes):
        return Plan("pg", 1, "prd.md", [n.id for n in nodes], "authorized", nodes=nodes)

    def test_overlapping_allowlists_are_refused_from_parallel_group(self):
        nodes = [
            self._group_node("a", allowlist=["vibe_guide/shared.py", "vibe_guide/a.py"]),
            self._group_node("b", allowlist=["vibe_guide/shared.py"]),
        ]
        result = audit_dag(self._plan(nodes))
        self.assertEqual(result.status, "blocked_dag")
        self.assertEqual(result.ready_nodes, [])
        self.assertEqual(result.parallel_groups, {})
        self.assertEqual(set(result.blocked_nodes), {"a", "b"})
        for node_id in ("a", "b"):
            self.assertTrue(
                any("parallel_group" in reason and "overlapping write scope" in reason
                    and "vibe_guide/shared.py" in reason for reason in result.reasons[node_id]),
                result.reasons[node_id],
            )
            self.assertTrue(
                any("integration_after" in reason for reason in result.reasons[node_id]),
                result.reasons[node_id],
            )

    def test_overlapping_owned_paths_are_refused_from_parallel_group(self):
        nodes = [
            self._group_node("a", owned=["docs/owned/shared.md"]),
            self._group_node("b", owned=["docs/owned/shared.md"]),
        ]
        result = audit_dag(self._plan(nodes))
        self.assertEqual(result.status, "blocked_dag")
        self.assertEqual(result.ready_nodes, [])
        for node_id in ("a", "b"):
            self.assertTrue(
                any("overlapping write scope" in reason and "docs/owned/shared.md" in reason
                    for reason in result.reasons[node_id]),
                result.reasons[node_id],
            )

    def test_directory_prefix_overlap_is_refused_from_parallel_group(self):
        nodes = [
            self._group_node("a", allowlist=["tests/fixtures"]),
            self._group_node("b", allowlist=["tests/fixtures/v46.json"]),
        ]
        result = audit_dag(self._plan(nodes))
        self.assertEqual(result.status, "blocked_dag")
        self.assertTrue(
            any("overlapping write scope" in reason for reason in result.reasons["a"]),
            result.reasons["a"],
        )

    def test_artifact_reference_is_refused_from_parallel_group(self):
        producer = self._group_node(
            "producer",
            contract_overrides={"output": "init 物化 docs/specs/shared-protocol.md"},
        )
        consumer = self._group_node(
            "consumer",
            contract_overrides={"input": "块内容指向 docs/specs/shared-protocol.md 协议文件"},
        )
        result = audit_dag(self._plan([producer, consumer]))
        self.assertEqual(result.status, "blocked_dag")
        self.assertEqual(result.ready_nodes, [])
        self.assertEqual(result.parallel_groups, {})
        for node_id in ("producer", "consumer"):
            self.assertTrue(
                any("produced path docs/specs/shared-protocol.md" in reason
                    and "integration_after" in reason for reason in result.reasons[node_id]),
                result.reasons[node_id],
            )

    def test_disjoint_nodes_remain_in_parallel_group(self):
        nodes = [self._group_node("a"), self._group_node("b")]
        result = audit_dag(self._plan(nodes))
        self.assertEqual(result.status, "ready")
        self.assertEqual(set(result.ready_nodes), {"a", "b"})
        self.assertEqual(result.parallel_groups, {"g": ["a", "b"]})

    def test_unverifiable_write_scope_is_conservatively_refused(self):
        good = self._group_node("good")
        broken = DAGNode(
            "broken", "broken", [], [], "g",
            {
                "input": "request",
                "output": "result",
                "error_behavior": "return blocked_dag",
                "acceptance_examples": ["example passes"],
                "risk_tags": ["scheduling"],
                "writer": "writer-broken",
                "worktree": ".vibe/worktrees/broken",
                "allowlist": "not-a-list",
            },
            "planned",
            writer="writer-broken",
            worktree=".vibe/worktrees/broken",
        )
        result = audit_dag(self._plan([good, broken]))
        self.assertEqual(result.status, "blocked_dag")
        self.assertNotIn("broken", result.ready_nodes)
        # Dispatching the healthy sibling alongside a member whose write scope
        # cannot be verified is refused too: an unknown scope may overlap.
        self.assertNotIn("good", result.ready_nodes)
        self.assertEqual(result.parallel_groups, {})
        self.assertTrue(
            any("parallel_group" in reason and "missing or invalid" in reason
                for reason in result.reasons["broken"]),
            result.reasons["broken"],
        )
        self.assertTrue(
            any("unverifiable member" in reason for reason in result.reasons["good"]),
            result.reasons["good"],
        )

    def test_running_group_peer_blocks_conflicting_dispatch(self):
        running = self._group_node("a", allowlist=["vibe_guide/shared.py"], status="running")
        candidate = self._group_node("b", allowlist=["vibe_guide/shared.py"])
        result = audit_dag(self._plan([running, candidate]))
        self.assertEqual(result.status, "blocked_dag")
        self.assertEqual(result.ready_nodes, [])
        self.assertEqual(result.parallel_groups, {})
        # The in-flight writer is not retro-blocked; only the new dispatch is refused.
        self.assertFalse(result.reasons["a"])
        self.assertNotIn("a", result.blocked_nodes)
        self.assertTrue(
            any("overlapping write scope" in reason for reason in result.reasons["b"]),
            result.reasons["b"],
        )

    def test_rework_group_peer_blocks_conflicting_dispatch(self):
        rework = self._group_node("a", allowlist=["vibe_guide/shared.py"], status="rework")
        candidate = self._group_node("b", allowlist=["vibe_guide/shared.py"])
        result = audit_dag(self._plan([rework, candidate]))
        self.assertNotIn("b", result.ready_nodes)
        self.assertTrue(
            any("overlapping write scope" in reason for reason in result.reasons["b"]),
            result.reasons["b"],
        )

    def test_delivered_or_accepted_group_members_are_exempt(self):
        for past in ("delivered", "accepted"):
            done = self._group_node("a", allowlist=["vibe_guide/shared.py"], status=past)
            candidate = self._group_node("b", allowlist=["vibe_guide/shared.py"])
            result = audit_dag(self._plan([done, candidate]))
            self.assertEqual(result.status, "ready", past)
            self.assertEqual(result.ready_nodes, ["b"], past)
            self.assertEqual(result.parallel_groups, {"g": ["b"]}, past)

    def test_missing_allowlist_is_conservatively_refused(self):
        good = self._group_node("good")
        bare = DAGNode(
            "bare", "bare", [], [], "g",
            {
                "input": "request",
                "output": "result",
                "error_behavior": "return blocked_dag",
                "acceptance_examples": ["example passes"],
                "risk_tags": ["scheduling"],
                "writer": "writer-bare",
                "worktree": ".vibe/worktrees/bare",
            },
            "planned",
            writer="writer-bare",
            worktree=".vibe/worktrees/bare",
        )
        result = audit_dag(self._plan([good, bare]))
        self.assertNotIn("bare", result.ready_nodes)
        self.assertTrue(
            any("parallel_group" in reason and "missing or invalid" in reason
                for reason in result.reasons["bare"]),
            result.reasons["bare"],
        )

    def test_version_tokens_in_prose_do_not_count_as_produced_paths(self):
        producer = self._group_node("a", contract_overrides={"output": "发布 v1.2 构建产物"})
        consumer = self._group_node("b", contract_overrides={"input": "升级到 v1.2 后验证"})
        result = audit_dag(self._plan([producer, consumer]))
        self.assertEqual(result.status, "ready")
        self.assertEqual(set(result.ready_nodes), {"a", "b"})
        self.assertEqual(result.parallel_groups, {"g": ["a", "b"]})

    def test_path_reference_requires_whole_token_match(self):
        producer = self._group_node("a", contract_overrides={"output": "产出 docs/spec.md 定稿"})
        consumer = self._group_node("b", contract_overrides={"input": "参照 docs/spec.md.bak 备份"})
        result = audit_dag(self._plan([producer, consumer]))
        self.assertEqual(result.status, "ready")
        self.assertEqual(set(result.ready_nodes), {"a", "b"})

    def test_directory_scope_covers_references_to_files_inside_it(self):
        producer = self._group_node("a", allowlist=["docs/specs"])
        consumer = self._group_node(
            "b",
            allowlist=["vibe_guide/b.py"],
            contract_overrides={"input": "参照 docs/specs/v2.md 的格式"},
        )
        result = audit_dag(self._plan([producer, consumer]))
        self.assertEqual(result.status, "blocked_dag")
        self.assertEqual(result.ready_nodes, [])
        self.assertTrue(
            any("produced path docs/specs" in reason for reason in result.reasons["b"]),
            result.reasons["b"],
        )

    def test_node_scoped_ready_hard_gates_conflicting_parallel_group(self):
        nodes = [
            self._group_node("a", allowlist=["vibe_guide/shared.py"]),
            self._group_node("b", allowlist=["vibe_guide/shared.py"]),
        ]
        # Runtime dispatch projection applies the same gate as audit_dag:
        # a conflicting group is not dispatch-eligible even though no code
        # reads the rendered dag.yaml audit back.
        self.assertEqual(node_scoped_ready(nodes), [])

    def test_node_scoped_ready_keeps_conflict_free_group_dispatchable(self):
        nodes = [self._group_node("a"), self._group_node("b")]
        self.assertEqual(node_scoped_ready(nodes), ["a", "b"])

    def test_node_scoped_ready_conservatively_refuses_unverifiable_group(self):
        good = self._group_node("good")
        bare = DAGNode(
            "bare", "bare", [], [], "g",
            {
                "input": "request",
                "output": "result",
                "error_behavior": "return blocked_dag",
                "acceptance_examples": ["example passes"],
                "risk_tags": ["scheduling"],
                "writer": "writer-bare",
                "worktree": ".vibe/worktrees/bare",
            },
            "planned",
            writer="writer-bare",
            worktree=".vibe/worktrees/bare",
        )
        self.assertEqual(node_scoped_ready([good, bare]), [])
        # Ungrouped nodes are unaffected by the gate.
        solo = self._group_node("solo", group=None)
        self.assertEqual(node_scoped_ready([solo]), ["solo"])

    def test_node_scoped_ready_blocks_dispatch_alongside_running_peer(self):
        running = self._group_node("a", allowlist=["vibe_guide/shared.py"], status="running")
        candidate = self._group_node("b", allowlist=["vibe_guide/shared.py"], status="ready")
        self.assertEqual(node_scoped_ready([running, candidate]), [])

    def test_vibe_entry_regression_fixture_is_refused_from_parallel_group(self):
        fixture_path = Path(__file__).parent / "fixtures" / "v46-parallel-audit-vibe-entry.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        nodes = [
            DAGNode(
                item["id"], item["title"],
                item.get("depends_on", []), item.get("integration_after", []),
                item.get("parallel_group"), item["contract"], item.get("status", "planned"),
                writer=item.get("writer", ""),
                worktree=item.get("worktree", ""),
                allowlist=item.get("allowlist", []),
                owned_paths=item.get("owned_paths", []),
            )
            for item in fixture["nodes"]
        ]
        info = fixture["plan"]
        plan = Plan(info["plan_id"], info["version"], info["prd_path"],
                    [node.id for node in nodes], info["status"], nodes=nodes)
        result = audit_dag(plan)
        self.assertEqual(result.status, "blocked_dag")
        self.assertEqual(result.ready_nodes, [])
        self.assertEqual(result.parallel_groups, {})
        for node_id in ("ISSUE-01", "ISSUE-02"):
            self.assertTrue(
                any("produced path .vibe/proposals/skills/vibe-entry/SKILL.md" in reason
                    for reason in result.reasons[node_id]),
                result.reasons[node_id],
            )

    # --- V4.7 ISSUE-03: root/prefix overlap and CJK glued path references ---

    def test_normalize_project_path_rejects_project_root_dot(self):
        with self.assertRaises(ValueError):
            normalize_project_path(".")

    def test_parent_directory_owned_path_overlaps_child_file(self):
        nodes = [
            self._group_node("a", owned=["src"]),
            self._group_node("b", owned=["src/utils.py"]),
        ]
        result = audit_dag(self._plan(nodes))
        self.assertEqual(result.status, "blocked_dag")
        self.assertEqual(result.ready_nodes, [])
        for node_id in ("a", "b"):
            self.assertTrue(
                any("overlapping write scope (src)" in reason for reason in result.reasons[node_id]),
                result.reasons[node_id],
            )

    def test_shared_string_prefix_is_not_directory_overlap(self):
        nodes = [
            self._group_node("a", owned=["src"]),
            self._group_node("b", owned=["srcfoo/x.py"]),
        ]
        result = audit_dag(self._plan(nodes))
        self.assertEqual(result.status, "ready")
        self.assertEqual(set(result.ready_nodes), {"a", "b"})
        self.assertEqual(result.parallel_groups, {"g": ["a", "b"]})

    def test_cjk_glued_artifact_reference_is_detected(self):
        producer = self._group_node(
            "producer",
            contract_overrides={"output": "产出：reports/测试输出.json供下游校验"},
        )
        consumer = self._group_node(
            "consumer",
            contract_overrides={"input": "读取reports/测试输出.json后做断言"},
        )
        result = audit_dag(self._plan([producer, consumer]))
        self.assertEqual(result.status, "blocked_dag")
        self.assertEqual(result.ready_nodes, [])
        for node_id in ("producer", "consumer"):
            self.assertTrue(
                any("produced path reports/测试输出.json" in reason for reason in result.reasons[node_id]),
                result.reasons[node_id],
            )

    def test_ascii_path_glued_inside_cjk_prose_is_detected(self):
        producer = self._group_node(
            "producer",
            contract_overrides={"output": "产出：docs/spec.md定稿"},
        )
        consumer = self._group_node(
            "consumer",
            contract_overrides={"input": "参照docs/spec.md格式"},
        )
        result = audit_dag(self._plan([producer, consumer]))
        self.assertEqual(result.status, "blocked_dag")
        for node_id in ("producer", "consumer"):
            self.assertTrue(
                any("produced path docs/spec.md" in reason for reason in result.reasons[node_id]),
                result.reasons[node_id],
            )

    def test_cjk_prose_without_paths_neither_matches_nor_raises(self):
        producer = self._group_node(
            "producer",
            contract_overrides={"output": "完成中文说明文档的撰写。版本v1.2已发布，见第3.14节与附录/章节说明"},
        )
        consumer = self._group_node(
            "consumer",
            contract_overrides={"input": "阅读中文说明文档，参考版本v1.2、第3.14节及正文/背景后验证"},
        )
        result = audit_dag(self._plan([producer, consumer]))
        self.assertEqual(result.status, "ready")
        self.assertEqual(set(result.ready_nodes), {"producer", "consumer"})
        self.assertEqual(result.parallel_groups, {"g": ["producer", "consumer"]})

    def test_multi_extension_output_path_is_not_truncated_to_shorter_path(self):
        producer = self._group_node(
            "producer",
            contract_overrides={"output": "归档 docs/spec.md.bak 备份"},
        )
        consumer = self._group_node(
            "consumer",
            contract_overrides={"input": "参照docs/spec.md格式"},
        )
        result = audit_dag(self._plan([producer, consumer]))
        self.assertEqual(result.status, "ready")
        self.assertEqual(set(result.ready_nodes), {"producer", "consumer"})

    def test_underscore_or_dash_suffixed_output_path_is_not_truncated(self):
        for suffix in ("_v2", "-v2"):
            with self.subTest(suffix=suffix):
                producer = self._group_node(
                    "producer",
                    contract_overrides={"output": "产出 docs/spec.md{} 草稿".format(suffix)},
                )
                consumer = self._group_node(
                    "consumer",
                    contract_overrides={"input": "参照docs/spec.md格式"},
                )
                result = audit_dag(self._plan([producer, consumer]))
                self.assertEqual(result.status, "ready", result.reasons)
                self.assertEqual(set(result.ready_nodes), {"producer", "consumer"})

    def test_cjk_quoted_output_path_is_detected(self):
        for quoted in ("《reports/测试输出.json》", "“reports/测试输出.json”", "「reports/测试输出.json」"):
            with self.subTest(quoted=quoted):
                producer = self._group_node(
                    "producer",
                    contract_overrides={"output": "产出{}供下游校验".format(quoted)},
                )
                consumer = self._group_node(
                    "consumer",
                    contract_overrides={"input": "读取reports/测试输出.json后做断言"},
                )
                result = audit_dag(self._plan([producer, consumer]))
                self.assertEqual(result.status, "blocked_dag")
                for node_id in ("producer", "consumer"):
                    self.assertTrue(
                        any("produced path reports/测试输出.json" in reason
                            for reason in result.reasons[node_id]),
                        result.reasons[node_id],
                    )


if __name__ == "__main__":
    unittest.main()
