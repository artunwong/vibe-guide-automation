"""visible-sdd-worker protocol contract: in-session SDD with reviewer independence.

ISSUE-05 pins the worker-session protocol shipped with the package:
(1) the fixed in-session flow -- dev subagent implements, review subagent
    (independent context, read-only, non-author) reviews, P0-P2 rework returns
    to dev, re-review, all under one session identity;
(2) the review subagent prompt must explicitly ban git write operations, with
    cross-branch reads only via ``git show <ref>:<path>`` / ``git diff``;
(3) evidence chain: each implement/review/rework round's conclusion is written
    into the session delivery for the supervisor to close into events.jsonl;
(4) a review subagent editing business code is a violation: node -> blocked.

The contract elements must not weaken: a missing element keeps the tests red.
Missing or illegal protocol names reuse load_protocol's existing error paths
(FileNotFoundError / ValueError); no new branches are added for this protocol.
"""
import re
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from vibe_guide.monitor import VISIBLE_SDD_PROTOCOL_REF

PROTOCOL_NAME = "visible-sdd-worker"
EVIDENCE_REF = "session-delivery#review-round-1"


def _load() -> str:
    from vibe_guide.protocols import load_protocol
    return load_protocol(PROTOCOL_NAME)


class VisibleSddProtocolShippingTests(unittest.TestCase):
    def test_protocol_file_is_shipped_as_package_data(self):
        from vibe_guide import protocols
        path = Path(protocols.__file__).parent / (PROTOCOL_NAME + ".md")
        self.assertTrue(path.is_file(), "protocol file must ship with the package")

    def test_protocol_loads_by_simple_name(self):
        text = _load()
        self.assertIsInstance(text, str)
        self.assertTrue(text.strip())


class VisibleSddDualRoleOrderTests(unittest.TestCase):
    """Element 1: fixed in-session dual-role flow under one session identity."""

    def setUp(self):
        self.text = _load()

    def test_names_dev_then_review_subagent_order(self):
        """Order is pinned inside the fixed-flow code block, not raw text.

        Bare substring positions drift ("preview" contains "review");
        the fenced flow block is the authoritative statement of order.
        """
        match = re.search(r"```text\n(.*?)\n```", self.text, re.S)
        self.assertIsNotNone(match, "protocol must contain the fixed-flow code block")
        flow = match.group(1)
        dev_pos = flow.find("dev")
        review_pos = flow.find("review")
        self.assertNotEqual(dev_pos, -1, "flow block must name the dev subagent")
        self.assertNotEqual(review_pos, -1, "flow block must name the review subagent")
        self.assertLess(dev_pos, review_pos, "dev implements before review reviews")

    def test_review_subagent_is_independent_read_only_non_author(self):
        for token in ("独立上下文", "只读", "非作者"):
            self.assertIn(token, self.text, token)

    def test_p0_p2_rework_returns_to_dev_then_re_review(self):
        text = self.text
        self.assertIn("P0", text)
        self.assertIn("P2", text)
        rework_pos = text.find("返工")
        re_review_pos = text.find("复审")
        self.assertNotEqual(rework_pos, -1, "rework (返工) must be part of the flow")
        self.assertNotEqual(re_review_pos, -1, "re-review (复审) must follow rework")
        self.assertLess(rework_pos, re_review_pos, "复审 follows 返工")

    def test_rework_target_role_is_dev_subagent(self):
        """Rework must go back to the dev subagent, never to any role.

        Mutant check: replacing "由 dev 子代理修复" with "由任意角色修复"
        must turn this red -- a reviewer (or anyone else) performing the
        rework itself is exactly the independence violation element 4 bans.
        """
        self.assertRegex(
            self.text,
            r"由\s*dev\s*子代理修复",
            "rework must be pinned to the dev subagent role",
        )

    def test_flow_keeps_single_session_identity(self):
        self.assertIn("同一会话", self.text)


class VisibleSddGitWriteBanTests(unittest.TestCase):
    """Element 2: review subagent prompt must ban git writes explicitly."""

    def setUp(self):
        self.text = _load()

    def test_git_write_ban_list_is_explicit(self):
        """checkout/switch/reset/clean/stash must each appear on a ban line.

        Presence alone is not enough: naming ``git checkout`` in an
        "allowed reads" sentence must turn this test red, so every banned
        op needs at least one line that also carries a ban marker.
        """
        ban_markers = ("禁", "不得")
        for op in ("checkout", "switch", "reset", "clean", "stash"):
            lines = [line for line in self.text.splitlines() if op in line]
            self.assertTrue(lines, op + " must be named in the protocol")
            self.assertTrue(
                any(any(marker in line for marker in ban_markers) for line in lines),
                op + " must appear on a ban line, got: " + repr(lines),
            )

    def test_extended_git_write_ban_list_is_named(self):
        """Beyond the five mandatory ops, the protocol bans all git writes."""
        for op in ("git add", "git commit", "git push", "git merge",
                   "git rebase", "git cherry-pick"):
            self.assertIn(op, self.text, op)

    def test_cross_branch_reads_limited_to_show_and_diff(self):
        self.assertIn("git show", self.text)
        self.assertIn("git diff", self.text)
        for line in self.text.splitlines():
            if "git show" in line and "<ref>:" in line:
                break
        else:
            self.fail("protocol must show the git show <ref>:<path> read pattern")


class VisibleSddEvidenceChainTests(unittest.TestCase):
    """Element 3: evidence write-back into session delivery and events.jsonl."""

    def setUp(self):
        self.text = _load()

    def test_each_round_writes_conclusion_into_session_delivery(self):
        for token in ("证据", "交付"):
            self.assertIn(token, self.text, token)

    def test_evidence_granularity_rounds_distinguishable_old_kept_new_appended(self):
        """Granularity, not just presence: rounds distinguishable, history kept."""
        for token in ("轮次", "保留", "追加"):
            self.assertIn(token, self.text, token)

    def test_supervisor_closes_evidence_into_events_jsonl(self):
        self.assertIn("events.jsonl", self.text)
        self.assertIn("监工", self.text)


class VisibleSddReviewerViolationTests(unittest.TestCase):
    """Element 4: reviewer editing business code = violation, node -> blocked."""

    def setUp(self):
        self.text = _load()

    def test_reviewer_editing_business_code_is_violation(self):
        self.assertIn("代改", self.text)
        self.assertIn("违规", self.text)

    def test_violation_blocks_node_and_is_recorded(self):
        self.assertIn("blocked", self.text)
        self.assertIn("记录", self.text)


class VisibleSddAcceptanceStructureTests(unittest.TestCase):
    """V4.7 ISSUE-02: the acceptance evidence structure is fail-closed.

    ``contract_digest`` reuses the node-level digest the monitor stamps at
    dispatch (``executable_contract_digest([node])``, full SHA-256 hex) --
    supervisor ruling 2026-09-24: no second digest algorithm.  Construction
    rejects a missing, blank, placeholder or wrong-length digest, so no
    writer can persist a partial acceptance.
    """

    DIGEST = "0123456789abcdef" * 4
    PROTOCOL_REF = "vibe_guide/protocols/visible-sdd-worker.md"
    EVIDENCE_REF = EVIDENCE_REF

    def _acceptance(self, **overrides):
        from vibe_guide.binding_lifecycle import VisibleSddAcceptance

        values = dict(
            contract_digest=self.DIGEST,
            protocol_ref=self.PROTOCOL_REF,
            evidence_ref=self.EVIDENCE_REF,
        )
        values.update(overrides)
        return VisibleSddAcceptance(**values)

    def test_complete_acceptance_constructs(self):
        acceptance = self._acceptance()
        self.assertEqual(acceptance.contract_digest, self.DIGEST)
        self.assertEqual(acceptance.protocol_ref, self.PROTOCOL_REF)
        self.assertEqual(acceptance.evidence_ref, self.EVIDENCE_REF)

    def test_missing_or_placeholder_contract_digest_fails_construction(self):
        for bad in (None, "", "   ", "placeholder", "0" * 32, "0" * 63, "0" * 65, "g" * 64, self.DIGEST.upper()):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self._acceptance(contract_digest=bad)

    def test_missing_protocol_ref_or_evidence_ref_fails_construction(self):
        for bad in (None, "", "  ", "a\x00b"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self._acceptance(protocol_ref=bad)
            with self.assertRaises(ValueError, msg=repr(bad)):
                self._acceptance(evidence_ref=bad)

    def test_round_trip_through_dict_keeps_every_field(self):
        from vibe_guide.binding_lifecycle import VisibleSddAcceptance

        acceptance = self._acceptance()
        payload = acceptance.to_dict()
        self.assertEqual(
            payload,
            {
                "contract_digest": self.DIGEST,
                "protocol_ref": self.PROTOCOL_REF,
                "evidence_ref": self.EVIDENCE_REF,
            },
        )
        self.assertEqual(VisibleSddAcceptance.from_dict(payload), acceptance)

    def test_from_dict_rejects_missing_digest_or_foreign_shape(self):
        from vibe_guide.binding_lifecycle import VisibleSddAcceptance

        complete = self._acceptance().to_dict()
        without_digest = dict(complete)
        del without_digest["contract_digest"]
        extra_key = dict(complete, clearance={"p0": 0})
        for bad in (None, "", [], {}, without_digest, extra_key, dict(complete, contract_digest="")):
            with self.assertRaises(ValueError, msg=repr(bad)):
                VisibleSddAcceptance.from_dict(bad)


class VisibleSddAcceptanceMonitorTests(unittest.TestCase):
    """AC-02 on both visible-sdd acceptance paths: live delivery and replay.

    The accepted event carries the dispatch-time node ``contract_digest``;
    every path that turns it into an accepted node recomputes
    ``executable_contract_digest([node])`` from the live contract and
    compares.  A tampered or unreadable contract, or a foreign protocol
    pointer, never yields an accepted node.
    """

    def setUp(self):
        from vibe_guide.capability_contract import build_contract, save_contract
        from vibe_guide.models import AgentCapabilities
        from vibe_guide.paths import ProjectPaths

        self.temporary = tempfile.TemporaryDirectory()
        self.paths = ProjectPaths(Path(self.temporary.name))
        (self.paths.vibe / "state.json").parent.mkdir(parents=True, exist_ok=True)
        (self.paths.vibe / "state.json").write_text(
            '{"workflow_version": 2, "session_gate": "s0_required"}\n', encoding="utf-8"
        )
        save_contract(
            self.paths, build_contract(self.paths.root, provider="fake", host_id="local")
        )
        self.capabilities = AgentCapabilities("fake", True, True, True, True, True, "full")

    def tearDown(self):
        self.temporary.cleanup()

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
    def _runner(protocol=VISIBLE_SDD_PROTOCOL_REF):
        from tests.test_monitor import VisibleSddRunner

        return VisibleSddRunner(
            events={
                ("sdd-a", "developer"): [
                    (
                        "complete",
                        {
                            "evidence": "delivery",
                            "in_session_review": {
                                "protocol": protocol,
                                "evidence_ref": EVIDENCE_REF,
                                "clearance": {"p0": 0, "p1": 0, "p2": 0},
                            },
                        },
                    )
                ]
            }
        )

    def _accepted_events(self, run_id):
        from vibe_guide.state import load_events

        return [
            record
            for record in load_events(self.paths, run_id)
            if record["event"] == "accepted" and record["data"].get("node_id") == "sdd-a"
        ]

    def _binding_status(self, run_id):
        from vibe_guide.task_registry import load_task_binding

        return load_task_binding(self.paths, "sdd-a", "developer", run_id=run_id).status

    def test_accepted_event_reuses_dispatch_time_executable_contract_digest(self):
        """Ruling pin: the acceptance reuses the node digest, it does not mint one."""
        from vibe_guide.authorization import executable_contract_digest

        monitor, record = self._monitor()
        runner = self._runner()
        snapshot = monitor.start(record, runner)

        snapshot = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(snapshot.nodes["sdd-a"]["status"], "accepted")
        events = self._accepted_events(snapshot.run_id)
        self.assertEqual(len(events), 1)
        digest = events[0]["data"]["contract_digest"]
        self.assertEqual(digest, executable_contract_digest([monitor.nodes["sdd-a"]]))
        self.assertEqual(digest, snapshot.nodes["sdd-a"]["contract_digest"])
        self.assertEqual(len(digest), 64)

    def test_accepted_event_protocol_ref_matches_shipped_constant(self):
        """Element 3 of the spec: the accepted event names the shipped protocol.

        The pointer is asserted on the event the monitor emits (captured at
        ``_record``) because ``state._EVENT_DATA_KEYS`` does not yet persist
        ``protocol_ref``; the constant is also pinned to the packaged file so
        a moved protocol cannot leave acceptances pointing at the old path.
        """
        from vibe_guide import protocols
        from vibe_guide.authorization import executable_contract_digest

        shipped = Path(protocols.__file__).parent / Path(VISIBLE_SDD_PROTOCOL_REF).name
        self.assertTrue(shipped.is_file(), VISIBLE_SDD_PROTOCOL_REF)
        self.assertTrue(VISIBLE_SDD_PROTOCOL_REF.startswith("vibe_guide/protocols/"))

        monitor, record = self._monitor()
        runner = self._runner()
        with patch.object(monitor, "_record", wraps=monitor._record) as recorded:
            snapshot = monitor.start(record, runner)
            snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(snapshot.nodes["sdd-a"]["status"], "accepted")
        accepted = [call.args[2] for call in recorded.call_args_list if call.args[1] == "accepted"]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["protocol_ref"], VISIBLE_SDD_PROTOCOL_REF)
        self.assertEqual(accepted[0]["evidence_ref"], EVIDENCE_REF)
        self.assertEqual(
            accepted[0]["contract_digest"], executable_contract_digest([monitor.nodes["sdd-a"]])
        )

    def test_delivery_citing_foreign_protocol_is_rejected_for_re_report(self):
        """ISSUE-83: a foreign protocol pointer is a format error now.

        The acceptance still never happens -- the pointer is not the shipped
        protocol this acceptance certifies -- but the rejection is a
        recoverable ``acceptance_rejected`` audit event with the session
        handle kept alive, instead of a permanent brick with every recovery
        path destroyed first.
        """
        from vibe_guide.state import load_events

        monitor, record = self._monitor()
        runner = self._runner(protocol="vibe_guide/protocols/dual-visible-worker.md")
        snapshot = monitor.start(record, runner)

        snapshot = monitor.tick(snapshot.run_id, runner)

        current = snapshot.nodes["sdd-a"]
        self.assertNotEqual(current["status"], "blocked_unknown", current.get("reason"))
        self.assertNotEqual(current["status"], "accepted")
        self.assertIn("sdd-a", snapshot.handles)
        self.assertEqual(self._accepted_events(snapshot.run_id), [])
        rejections = [
            record_ for record_ in load_events(self.paths, snapshot.run_id)
            if record_["event"] == "acceptance_rejected"
            and record_["data"].get("node_id") == "sdd-a"
        ]
        self.assertEqual(len(rejections), 1)
        self.assertTrue(rejections[0]["data"].get("recoverable"))
        self.assertNotEqual(self._binding_status(snapshot.run_id), "archived")

    def test_replay_accepts_when_recomputed_digest_matches(self):
        from vibe_guide.state import save_snapshot

        monitor, record = self._monitor()
        runner = self._runner()
        snapshot = monitor.start(record, runner)
        preserved = deepcopy(snapshot)
        snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(snapshot.nodes["sdd-a"]["status"], "accepted")
        save_snapshot(self.paths, preserved)

        recovered = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(recovered.nodes["sdd-a"]["status"], "accepted")
        self.assertTrue(recovered.nodes["sdd-a"]["pair_archived"])
        self.assertEqual(self._binding_status(snapshot.run_id), "archived")

    def test_replay_rejects_accepted_event_when_contract_was_tampered(self):
        """Digest mismatch (64-hex, different content): the event is invalid.

        Editing the node contract itself trips the authorization gate first
        (PermissionError, node never accepted).  The case only the ISSUE-02
        recomputation catches is a smuggled acceptance: snapshot digest and
        accepted event agree with each other but not with the live contract.
        """
        import hashlib
        from vibe_guide.contracts import RunEvent
        from vibe_guide.state import append_event, load_snapshot, save_snapshot

        monitor, record = self._monitor()
        runner = self._runner()
        snapshot = monitor.start(record, runner)
        active = snapshot.nodes["sdd-a"]["active_task"]
        original_files = list(monitor.nodes["sdd-a"].contract["files"])

        monitor.nodes["sdd-a"].contract["files"] = original_files + ["smuggled.py"]
        with self.assertRaises(PermissionError):
            monitor.tick(snapshot.run_id, runner)
        monitor.nodes["sdd-a"].contract["files"] = original_files

        forged = hashlib.sha256(b"smuggled contract").hexdigest()
        snapshot.nodes["sdd-a"]["contract_digest"] = forged
        save_snapshot(self.paths, snapshot)
        append_event(
            self.paths,
            RunEvent(
                "accepted",
                {
                    "run_id": snapshot.run_id,
                    "node_id": "sdd-a",
                    "evidence": EVIDENCE_REF,
                    "contract_digest": forged,
                    "authorization_epoch": snapshot.authorization_digest,
                },
            ),
            {
                "role": "reviewer",
                "task_id": active["task_id"],
                "handle_id": None,
                "generation": active["generation"],
                "authorization_digest": snapshot.authorization_digest,
                "node_contract_digest": snapshot.node_contract_digest,
            },
        )

        with self.assertRaises(ValueError) as refused:
            monitor.tick(snapshot.run_id, runner)

        self.assertIn("stale", str(refused.exception))
        on_disk = load_snapshot(self.paths, snapshot.run_id)
        self.assertNotEqual(on_disk.nodes["sdd-a"]["status"], "accepted")
        self.assertFalse(on_disk.nodes["sdd-a"]["pair_archived"])

    def test_replay_rejects_forged_accepted_event_without_digest(self):
        from vibe_guide.contracts import RunEvent
        from vibe_guide.state import append_event, load_snapshot

        monitor, record = self._monitor()
        runner = self._runner()
        snapshot = monitor.start(record, runner)
        active = snapshot.nodes["sdd-a"]["active_task"]
        append_event(
            self.paths,
            RunEvent(
                "accepted",
                {
                    "run_id": snapshot.run_id,
                    "node_id": "sdd-a",
                    "evidence": EVIDENCE_REF,
                    "authorization_epoch": snapshot.authorization_digest,
                },
            ),
            {
                "role": "reviewer",
                "task_id": active["task_id"],
                "handle_id": None,
                "generation": active["generation"],
                "authorization_digest": snapshot.authorization_digest,
                "node_contract_digest": snapshot.node_contract_digest,
            },
        )

        with self.assertRaises(ValueError):
            monitor.tick(snapshot.run_id, runner)

        self.assertNotEqual(
            load_snapshot(self.paths, snapshot.run_id).nodes["sdd-a"]["status"], "accepted"
        )

    def test_live_acceptance_refuses_digest_that_does_not_match_live_contract(self):
        """Live path: a snapshot digest that no longer matches the contract.

        The delivered session review is otherwise valid; only the recomputed
        ``executable_contract_digest([node])`` exposes the mismatch, and the
        node must go blocked_unknown instead of accepting with that digest.
        """
        import hashlib
        from vibe_guide.state import save_snapshot

        monitor, record = self._monitor()
        runner = self._runner()
        snapshot = monitor.start(record, runner)
        snapshot.nodes["sdd-a"]["contract_digest"] = hashlib.sha256(b"smuggled contract").hexdigest()
        save_snapshot(self.paths, snapshot)

        snapshot = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(snapshot.nodes["sdd-a"]["status"], "blocked_unknown")
        self.assertIn("does not match the live node contract", snapshot.nodes["sdd-a"]["reason"])
        self.assertEqual(self._accepted_events(snapshot.run_id), [])
        self.assertNotEqual(self._binding_status(snapshot.run_id), "archived")

    def _refuse_unreadable_contract(self, unreadable):
        """Acceptance boundary with an unreadable contract: refused, blocked_unknown.

        Inside ``tick`` an emptied contract already trips the authorization
        gate (whole-DAG digest, PermissionError) and the ISSUE-01 ownership
        gate blocks it at dispatch, so the acceptance boundary is exercised
        directly: it is the last refusal before an accepted event is written.
        """
        from vibe_guide.contracts import RunEvent

        monitor, record = self._monitor()
        runner = self._runner()
        snapshot = monitor.start(record, runner)
        monitor.nodes["sdd-a"].contract = unreadable
        delivery = RunEvent(
            "complete",
            {
                "run_id": snapshot.run_id,
                "node_id": "sdd-a",
                "evidence": "delivery",
                "in_session_review": {
                    "protocol": VISIBLE_SDD_PROTOCOL_REF,
                    "evidence_ref": EVIDENCE_REF,
                    "clearance": {"p0": 0, "p1": 0, "p2": 0},
                },
            },
        )

        monitor._accept_visible_sdd_delivery(
            snapshot, "sdd-a", delivery, snapshot.nodes["sdd-a"]
        )

        self.assertEqual(snapshot.nodes["sdd-a"]["status"], "blocked_unknown")
        self.assertIn("unreadable", snapshot.nodes["sdd-a"]["reason"])
        self.assertIsNone(snapshot.nodes["sdd-a"]["acceptance"])
        self.assertEqual(self._accepted_events(snapshot.run_id), [])
        self.assertNotIn(self._binding_status(snapshot.run_id), {"accepted", "archived"})

    def test_delivery_with_empty_contract_is_refused_blocked_unknown(self):
        self._refuse_unreadable_contract({})

    def test_delivery_with_none_contract_is_refused_blocked_unknown(self):
        self._refuse_unreadable_contract(None)

    def test_unreadable_contract_never_yields_a_digest(self):
        monitor, _ = self._monitor()
        for unreadable in (None, {}, "", [], "not-a-dict"):
            monitor.nodes["sdd-a"].contract = unreadable
            with self.assertRaises(ValueError, msg=repr(unreadable)):
                monitor._live_node_contract_digest("sdd-a")


class VisibleSddErrorPathTests(unittest.TestCase):
    """Missing/illegal names reuse load_protocol's existing error paths."""

    def test_unknown_protocol_raises_file_not_found(self):
        from vibe_guide.protocols import load_protocol
        with self.assertRaises(FileNotFoundError):
            load_protocol("no-such-protocol")

    def test_illegal_protocol_name_raises_value_error(self):
        from vibe_guide.protocols import load_protocol
        for bad in ("Visible-SDD", "../etc/passwd", "a b", "", "x" * 300 + "!"):
            with self.assertRaises(ValueError, msg=bad):
                load_protocol(bad)


if __name__ == "__main__":
    unittest.main()
