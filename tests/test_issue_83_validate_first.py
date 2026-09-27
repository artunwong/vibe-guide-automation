"""Issue #83: a rejected acceptance payload must never brick the run.

The fail-closed traps were an ordering defect, not a schema defect: the
monitor recorded the event, flipped the binding, popped the handle and only
*then* validated the payload -- so a reviewer writing `p3` or a worker
missing `in_session_review` destroyed every recovery handle before the
schema said no.  The contract these tests pin:

1. **Validate first.**  Schema validation and lineage checks run before any
   event is recorded, any binding is flipped, or any handle is popped.  A
   format error leaves the lifecycle byte-identical.
2. **Rejection is recoverable.**  A malformed claim/delivery is recorded as
   a pure-audit `acceptance_rejected` event; the handle and active task stay
   alive, so the same session can report a corrected payload and close the
   node.  Replay of that audit event is a no-op, not manual reconciliation.
3. **Fail-closed stays fail-closed.**  Contract-digest drift (tamper) still
   bricks, and substantive rejections (open P0-P2 in an acceptance) are
   unchanged -- only *format* errors gained the re-report channel.
4. **The producer is told the schema.**  The visible-sdd worker protocol
   names the delivery payload fields, and the integration reviewer's node
   contract names the claim keys; a whitelist extension (p3/p4 observations)
   keeps typo'd P0s fail-closed.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from vibe_guide.paths import ProjectPaths
from vibe_guide.protocols import load_protocol

EVIDENCE_REF = "issue-83#review-round-1"
VISIBLE_SDD_PROTOCOL_REF = "vibe_guide/protocols/visible-sdd-worker.md"


def _good_review():
    return {
        "protocol": VISIBLE_SDD_PROTOCOL_REF,
        "evidence_ref": EVIDENCE_REF,
        "clearance": {"p0": 0, "p1": 0, "p2": 0},
    }


class VisibleSddValidateFirstTests(unittest.TestCase):
    """Live delivery path: format errors reject, re-report succeeds."""

    def setUp(self):
        from vibe_guide.capability_contract import build_contract, save_contract
        from vibe_guide.models import AgentCapabilities

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.paths = ProjectPaths(Path(self.temporary.name))
        (self.paths.vibe / "state.json").parent.mkdir(parents=True, exist_ok=True)
        (self.paths.vibe / "state.json").write_text(
            '{"workflow_version": 2, "session_gate": "s0_required"}\n', encoding="utf-8"
        )
        save_contract(
            self.paths, build_contract(self.paths.root, provider="fake", host_id="local")
        )
        self.capabilities = AgentCapabilities("fake", True, True, True, True, True, "full")

    def _monitor(self):
        from tests.test_monitor import load_v46_dispatch_fixture
        from vibe_guide.authorization import authorize, build_authorization_card
        from vibe_guide.models import Plan
        from vibe_guide.monitor import Monitor

        payload, nodes = load_v46_dispatch_fixture()
        plan = Plan("plan-1", 1, "docs/prd.md", [item.id for item in nodes], "draft")
        card = build_authorization_card(plan, nodes, self.capabilities)
        monitor = Monitor(
            self.paths, plan, nodes, topology_rulings=payload["topology_rulings"]
        )
        return monitor, authorize(card, "AUTHORIZE")

    @staticmethod
    def _runner(queue):
        from tests.test_monitor import VisibleSddRunner

        return VisibleSddRunner(events={("sdd-a", "developer"): list(queue)})

    def _events(self, run_id, name=None):
        from vibe_guide.state import load_events

        records = load_events(self.paths, run_id)
        if name is None:
            return records
        return [r for r in records if r["event"] == name]

    def _node_events(self, run_id, name):
        return [r for r in self._events(run_id, name) if r["data"].get("node_id") == "sdd-a"]

    def _binding_status(self, run_id):
        from vibe_guide.task_registry import load_task_binding

        return load_task_binding(self.paths, "sdd-a", "developer", run_id=run_id).status

    def test_malformed_review_payload_rejects_without_bricking_then_re_report_succeeds(self):
        monitor, record = self._monitor()
        runner = self._runner([
            # First delivery: worker forgot the whole in_session_review field.
            ("complete", {"evidence": "delivery"}),
            # Re-report on the same session: corrected payload.
            ("complete", {"evidence": "delivery", "in_session_review": _good_review()}),
        ])
        snapshot = monitor.start(record, runner)

        snapshot = monitor.tick(snapshot.run_id, runner)

        current = snapshot.nodes["sdd-a"]
        # Zero lifecycle pollution: nothing was accepted, delivered or bricked,
        # and the session handle is still alive for the re-report.
        self.assertNotEqual(current["status"], "blocked_unknown", current.get("reason"))
        self.assertNotEqual(current["status"], "accepted")
        self.assertIn("sdd-a", snapshot.handles)
        self.assertIsInstance(current.get("active_task"), dict)
        self.assertEqual(self._node_events(snapshot.run_id, "accepted"), [])
        rejections = self._node_events(snapshot.run_id, "acceptance_rejected")
        self.assertEqual(len(rejections), 1, self._events(snapshot.run_id))
        self.assertTrue(rejections[0]["data"].get("recoverable"))
        # A recoverable format error is automatic recovery, not a user
        # decision: the node reason must not carry decision markers even
        # when the raw validator text mentions names like "out_of_scope".
        from vibe_guide.state import map_user_status

        self.assertNotEqual(map_user_status(current), "需要你决定")

        snapshot = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(snapshot.nodes["sdd-a"]["status"], "accepted")
        # The corrected re-report clears the rejection text.
        self.assertIsNone(snapshot.nodes["sdd-a"].get("reason"))
        self.assertEqual(len(self._node_events(snapshot.run_id, "accepted")), 1)
        # The audit trail keeps the rejection next to the later acceptance.
        self.assertEqual(len(self._node_events(snapshot.run_id, "acceptance_rejected")), 1)

    def test_missing_clearance_is_a_format_rejection_not_a_brick(self):
        monitor, record = self._monitor()
        runner = self._runner([
            ("complete", {
                "evidence": "delivery",
                "in_session_review": {"protocol": VISIBLE_SDD_PROTOCOL_REF,
                                      "evidence_ref": EVIDENCE_REF},
            }),
        ])
        snapshot = monitor.start(record, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)

        current = snapshot.nodes["sdd-a"]
        self.assertNotEqual(current["status"], "blocked_unknown")
        self.assertIn("sdd-a", snapshot.handles)
        self.assertEqual(len(self._node_events(snapshot.run_id, "acceptance_rejected")), 1)

    def test_foreign_protocol_pointer_is_a_format_rejection_not_a_brick(self):
        monitor, record = self._monitor()
        runner = self._runner([
            ("complete", {
                "evidence": "delivery",
                "in_session_review": {
                    "protocol": "vibe_guide/protocols/dual-visible-worker.md",
                    "evidence_ref": EVIDENCE_REF,
                    "clearance": {"p0": 0, "p1": 0, "p2": 0},
                },
            }),
        ])
        snapshot = monitor.start(record, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)

        current = snapshot.nodes["sdd-a"]
        self.assertNotEqual(current["status"], "blocked_unknown")
        self.assertIn("sdd-a", snapshot.handles)
        rejections = self._node_events(snapshot.run_id, "acceptance_rejected")
        self.assertEqual(len(rejections), 1)
        # Reasons are redacted on disk like every other provider-text field;
        # the distinguishable signal is the live handle, not the text.

    def test_digest_drift_still_fails_closed_and_still_mutates_nothing(self):
        """Tamper is not a format error: fail closed, but validate-first."""
        import hashlib
        from vibe_guide.state import save_snapshot

        monitor, record = self._monitor()
        runner = self._runner([
            ("complete", {"evidence": "delivery", "in_session_review": _good_review()}),
        ])
        snapshot = monitor.start(record, runner)
        # Smuggle the snapshot digest (editing the contract itself trips the
        # authorization gate first); the recomputed live digest exposes it.
        snapshot.nodes["sdd-a"]["contract_digest"] = hashlib.sha256(b"smuggled contract").hexdigest()
        save_snapshot(self.paths, snapshot)

        snapshot = monitor.tick(snapshot.run_id, runner)

        current = snapshot.nodes["sdd-a"]
        self.assertEqual(current["status"], "blocked_unknown")
        self.assertIn("digest", current["reason"])
        # The brick must not come with a half-applied delivery: no delivered
        # event, no binding flip, because the check ran before the mutation.
        self.assertEqual(self._node_events(snapshot.run_id, "accepted"), [])
        self.assertNotEqual(self._binding_status(snapshot.run_id), "accepted")

    def test_rejection_event_replay_is_a_no_op(self):
        """A crash between the rejection audit event and the snapshot save
        replays that event; pure audit must not become manual reconciliation."""
        monitor, record = self._monitor()
        runner = self._runner([("complete", {"evidence": "delivery"})])
        snapshot = monitor.start(record, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(len(self._node_events(snapshot.run_id, "acceptance_rejected")), 1)

        status_before = snapshot.nodes["sdd-a"]["status"]
        reason_before = snapshot.nodes["sdd-a"].get("reason")
        # Rewind to the rejection event itself: tick() appends further
        # topology observations after it, so rewinding by one would only
        # replay those and never exercise the no-op branch.
        rejection_sequence = self._node_events(
            snapshot.run_id, "acceptance_rejected"
        )[0]["sequence"]
        snapshot.event_sequence = rejection_sequence - 1
        monitor._reconcile_unapplied_events(snapshot)

        self.assertEqual(snapshot.nodes["sdd-a"]["status"], status_before)
        self.assertEqual(snapshot.nodes["sdd-a"].get("reason"), reason_before)
        self.assertNotEqual(snapshot.nodes["sdd-a"]["status"], "blocked_unknown")
        # Replaying the audit event must not duplicate it either.
        self.assertEqual(
            len(self._node_events(snapshot.run_id, "acceptance_rejected")), 1
        )

    def test_rejection_reason_never_trips_the_user_decision_mapping(self):
        """The raw validator text names claim keys like ``out_of_scope`` --
        a user-decision marker.  The node must carry the fixed marker-free
        text; the raw reason lives only in the redacted audit event."""
        from vibe_guide.evidence import build_integration_review_evidence
        from vibe_guide.state import map_user_status

        monitor, record = self._monitor()
        runner = self._runner([("complete", {"evidence": "delivery"})])
        snapshot = monitor.start(record, runner)
        active = snapshot.nodes["sdd-a"]["active_task"]
        self.assertIsInstance(active, dict)

        # The real raw reason the integration claim validator produces for a
        # free-form acceptance; it contains a user-decision marker ("scope"),
        # so the pre-fix code did surface "需要你决定" for it.
        with self.assertRaises(ValueError) as raised:
            build_integration_review_evidence(snapshot, "P0-P2 cleared", [], [])
        raw_reason = str(raised.exception)
        self.assertIn("out_of_scope", raw_reason)
        self.assertEqual(
            map_user_status({"status": "running", "reason": raw_reason}),
            "需要你决定",
        )

        monitor._reject_acceptance_format(snapshot, "sdd-a", active, raw_reason)

        node = snapshot.nodes["sdd-a"]
        self.assertNotIn("out_of_scope", node["reason"])
        self.assertNotEqual(map_user_status(node), "需要你决定")
        rejection = self._node_events(snapshot.run_id, "acceptance_rejected")[-1]
        self.assertTrue(rejection["data"].get("recoverable"))


class FindingSeverityWhitelistTests(unittest.TestCase):
    """p3/p4 observations register without loosening the fail-closed net."""

    def build(self, claim):
        from tests.test_integration_review_closeout_entry import _snapshot, CLEARED_CLAIM
        from vibe_guide.evidence import build_integration_review_evidence

        merged = dict(CLEARED_CLAIM)
        merged.update(claim)
        return build_integration_review_evidence(_snapshot(), merged, ["AGENTS.md"], [])

    def test_p3_and_p4_observations_do_not_count_into_the_clearance(self):
        package = self.build({"findings": [
            {"severity": "p3", "status": "open", "detail": "建议：补一条注释"},
            {"severity": "P4", "status": "open", "detail": "建议：命名"},
            {"severity": "p2", "status": "resolved", "detail": "已修"},
        ]})
        self.assertEqual(package["clearance"], {"p0": 0, "p1": 0, "p2": 0})
        self.assertEqual(len(package["findings"]), 3)

    def test_an_open_p0_next_to_p3_observations_still_blocks(self):
        package = self.build({"findings": [
            {"severity": "p0", "status": "open", "detail": "资金路径未验证"},
            {"severity": "p3", "status": "open", "detail": "建议"},
        ]})
        self.assertEqual(package["clearance"]["p0"], 1)

    def test_typo_severities_still_fail_closed(self):
        """The whitelist extends by name, not by shape: a mistyped P0 must
        still be rejected rather than silently dropped from the clearance."""
        for severity in ("critical", "po", "p00", "p5", "high", ""):
            with self.assertRaises(ValueError, msg=severity):
                self.build({"findings": [{"severity": severity, "status": "open", "detail": "x"}]})


class ProducerToldTheSchemaTests(unittest.TestCase):
    """Fix suggestion 4: the schema lives in the dispatch material, not just
    in the validator a rejected session can no longer ask."""

    def test_visible_sdd_protocol_names_the_delivery_payload_fields(self):
        text = load_protocol("visible-sdd-worker")
        for literal in (
            "in_session_review",
            "evidence_ref",
            "clearance",
            "delivery_evidence",
            "completion_marker",
            VISIBLE_SDD_PROTOCOL_REF,
        ):
            self.assertIn(literal, text)

    def test_visible_sdd_protocol_says_rejection_is_re_reportable(self):
        text = load_protocol("visible-sdd-worker")
        self.assertIn("acceptance_rejected", text)
        self.assertIn("重报", text)

    def test_prd_guide_documents_the_observation_severities(self):
        text = load_protocol("prd-guide")
        section = text.split("#### 整合审查节点的 accepted", 1)[1]
        self.assertIn("p3", section)
        self.assertIn("p4", section)

    def test_prd_guide_no_longer_says_format_errors_brick_the_node(self):
        text = load_protocol("prd-guide")
        section = text.split("#### 整合审查节点的 accepted", 1)[1]
        self.assertIn("acceptance_rejected", section)

    def test_integration_node_contract_names_the_claim_keys(self):
        from tests.test_integration_review_dispatch import (
            _business_node, _integration_contract,
        )
        from vibe_guide.dag import append_integration_review_node
        from vibe_guide.models import INTEGRATION_REVIEW_NODE_ID, Plan

        plan = Plan(
            "plan-1", 1, "prd.md", ["date-range-filter", "export-button"], "draft",
            nodes=[
                _business_node("date-range-filter", ["src/components/DateRange.tsx"]),
                _business_node("export-button", ["src/pages/view.tsx"]),
            ],
            spec_path="spec.md",
            complexity_band="complex",
            integration_contract=_integration_contract(["date-range-filter", "export-button"]),
        )
        plan = append_integration_review_node(plan)
        node = next(n for n in plan.nodes if n.id == INTEGRATION_REVIEW_NODE_ID)
        output = str(node.contract.get("output", ""))
        for key in ("findings", "iteration_compatibility", "test_runtime_delivery", "out_of_scope"):
            self.assertIn(key, output)


if __name__ == "__main__":
    unittest.main()
