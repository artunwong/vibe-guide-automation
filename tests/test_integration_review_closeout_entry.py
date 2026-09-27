"""The production entry point for run-level integration review evidence.

`evaluate_v41_closeout` requires `snapshot.integration_review_evidence`, but
until this module's fix nothing in production ever wrote it: a complex run
could accept every node, including `integration-review`, and still sit at
`running` forever with the reason "integration review evidence is missing".
The reviewer already reports through the §6.2 mailbox, so that acceptance is
the entry point -- vibe derives the run-bound fields and keeps only the
reviewer's judgement fields as input.

Every test here drives the public CLI and the real provider mailbox, or the
pure derivation used by it.  None hand-edits a `.vibe/` file.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from vibe_guide.cli import render_v41_closeout_status, run_cli
from vibe_guide.evidence import (
    INTEGRATION_REVIEW_CLAIM_KEYS,
    build_integration_review_evidence,
    evaluate_v41_closeout,
    validate_integration_review_evidence,
)
from vibe_guide import monitor
from vibe_guide.paths import ProjectPaths
from vibe_guide.protocols import load_protocol
from vibe_guide.adapters.task_provider import ProviderActionStore
from vibe_guide.state import durable_projection, load_events, load_snapshot

FIXTURE = Path(__file__).parent / "fixtures" / "pm-path" / "product-spec.json"
REQUEST = "设计并实现保单查看页的 PDF 导出，集成日期范围筛选、编写测试并部署"
FACTS = {name: True for name in (
    "claude-code.agent", "claude-code.shell", "claude-code.subprocess", "claude-code.worktree",
    "claude-code.visible_task.create", "claude-code.visible_task.enter",
    "claude-code.visible_task.resume", "claude-code.visible_task.wait",
    "claude-code.in_session_sdd",
)}
CLEARED_CLAIM = {
    "findings": [],
    "iteration_compatibility": {"status": "verified", "evidence": "迭代兼容性已逐条复核"},
    "test_runtime_delivery": {"status": "verified", "evidence": "测试与运行时交付已复核"},
    "out_of_scope": [],
}


def _snapshot(nodes=None, evidence=None):
    """A lineage-complete snapshot stand-in for the pure derivation tests."""
    return SimpleNamespace(
        run_id="run-1", plan_id="plan-1", plan_version=2, status="running",
        authorization_digest="a" * 64, node_contract_digest="b" * 64,
        prd_digest="c" * 64, spec_digest="d" * 64,
        nodes=nodes if nodes is not None else {
            "alpha": {"status": "accepted"},
            "beta": {"status": "accepted"},
            "integration-review": {"status": "accepted"},
        },
        integration_review_evidence=evidence or {},
        authorization={"remote_git_actions": "deny"},
    )


class IntegrationReviewPackageDerivationTests(unittest.TestCase):
    """What the reviewer may say, and what only the run may say."""

    def build(self, claim, snapshot=None):
        return build_integration_review_evidence(
            snapshot or _snapshot(), claim,
            agentsmd_acceptance_refs=["AGENTS.md"],
            unverified_or_excluded=["deploy", "release"],
        )

    def test_cleared_claim_derives_a_package_the_validator_accepts(self):
        snapshot = _snapshot()
        package = self.build(CLEARED_CLAIM, snapshot)
        validate_integration_review_evidence(snapshot, package)
        self.assertEqual(package["clearance"], {"p0": 0, "p1": 0, "p2": 0})

    def test_lineage_and_scope_come_from_the_run_not_from_the_reviewer(self):
        snapshot = _snapshot()
        package = self.build(CLEARED_CLAIM, snapshot)
        self.assertEqual(package["run_id"], "run-1")
        self.assertEqual(package["plan_revision"], 2)
        self.assertEqual(package["authorization_digest"], "a" * 64)
        self.assertEqual(package["aggregated_scope"]["nodes"], ["alpha", "beta"])
        self.assertEqual(package["agentsmd_acceptance_refs"], ["AGENTS.md"])
        self.assertEqual(package["unverified_or_excluded"], ["deploy", "release"])

    def test_claim_schema_is_exact_so_a_reviewer_cannot_forge_a_derived_field(self):
        for claim in (
            dict(CLEARED_CLAIM, run_id="other-run"),
            dict(CLEARED_CLAIM, clearance={"p0": 0, "p1": 0, "p2": 0}),
            {key: value for key, value in CLEARED_CLAIM.items() if key != "findings"},
            "P0-P2 cleared",
            None,
        ):
            with self.assertRaises(ValueError, msg=claim):
                self.build(claim)

    def test_unresolved_findings_are_counted_into_the_clearance(self):
        claim = dict(CLEARED_CLAIM, findings=[
            {"severity": "p1", "status": "open", "detail": "边界未覆盖"},
            {"severity": "p2", "status": "resolved", "detail": "已修"},
        ])
        self.assertEqual(self.build(claim)["clearance"], {"p0": 0, "p1": 1, "p2": 0})

    def test_only_a_resolved_finding_clears_so_a_reviewer_cannot_waive_its_own_p0(self):
        """`waived`/`accepted` do not clear, and `resolved` still does.

        Both spellings were registered as legal statuses while only `open` was
        counted, so `{"severity": "p0", "status": "waived"}` derived an all-zero
        clearance and closed the run out.  Rejecting them is a format convention,
        not a permission boundary: the second half of this test pins the part
        that stayed open, because the docstring on `_open_finding_counts` and the
        protocol both now say so and a future edit must not quietly re-promise
        an authorization gate the code does not implement.
        """
        for status in ("waived", "accepted", "open"):
            claim = dict(CLEARED_CLAIM, findings=[
                {"severity": "p0", "status": status, "detail": "资金结算路径没验证"},
            ])
            self.assertEqual(self.build(claim)["clearance"], {"p0": 1, "p1": 0, "p2": 0}, msg=status)
        restated = dict(CLEARED_CLAIM, findings=[
            {"severity": "p0", "status": "resolved", "detail": "资金结算路径没验证，我说修好了"},
        ])
        self.assertEqual(self.build(restated)["clearance"], {"p0": 0, "p1": 0, "p2": 0})

    def test_an_unregistered_finding_status_or_severity_fails_closed(self):
        """Typo severities still raise; p3/p4 left this set on purpose.

        The observation severities are whitelisted by name in ISSUE-83 and
        covered positively in `test_issue_83_validate_first`; what must never
        change is that an unrecognized severity (`critical`, `po`) is a
        schema error, not a silent drop from the clearance.
        """
        for finding in (
            {"severity": "p0", "status": "unresolved", "detail": "x"},
            {"severity": "p0", "status": "pending", "detail": "x"},
            {"severity": "p0", "detail": "x"},
            {"severity": "critical", "status": "open", "detail": "x"},
            {"severity": "p5", "status": "open", "detail": "x"},
            {"status": "open", "detail": "x"},
            "p0 open",
        ):
            with self.assertRaises(ValueError, msg=finding):
                self.build(dict(CLEARED_CLAIM, findings=[finding]))

    def test_a_verdict_must_carry_text_the_disk_can_keep(self):
        """A nested object under `evidence` cannot survive persistence.

        `state.py` redacts any value whose key is `evidence`, and for a dict it
        drops the keys that look sensitive -- so `{"secret_scan": "clean"}`
        persisted as `{}`, which the reload-time validator rejects.  The run
        announced `complete` once and then silently rolled back to the previous
        snapshot.  Reject the shape instead.
        """
        for verdict in (
            {"status": "verified", "evidence": {"secret_scan": "clean"}},
            {"status": "verified", "evidence": ["checked"]},
            {"status": "verified", "evidence": True},
            {"status": "verified", "evidence": ""},
            {"status": "verified"},
        ):
            with self.assertRaises(ValueError, msg=verdict):
                self.build(dict(CLEARED_CLAIM, iteration_compatibility=verdict))
            with self.assertRaises(ValueError, msg=verdict):
                self.build(dict(CLEARED_CLAIM, test_runtime_delivery=verdict))

    def test_the_derived_package_still_validates_after_persistence_redacts_it(self):
        """What is written must be what can be read back.

        The durable copy keeps the lineage and the clearance but replaces every
        provider text with a placeholder, so the package has to stay valid under
        that projection or the completed run becomes unloadable.
        """
        snapshot = _snapshot()
        package = self.build(dict(CLEARED_CLAIM, findings=[
            {"severity": "p2", "status": "resolved", "detail": "已修"},
        ]), snapshot)
        validate_integration_review_evidence(snapshot, durable_projection(package))

    def test_out_of_scope_changes_are_rejected(self):
        with self.assertRaises(ValueError):
            self.build(dict(CLEARED_CLAIM, out_of_scope=["docs/unrelated.md"]))

    def test_an_undeclared_acceptance_reference_fails_closed(self):
        with self.assertRaises(ValueError):
            build_integration_review_evidence(
                _snapshot(), CLEARED_CLAIM,
                agentsmd_acceptance_refs=[], unverified_or_excluded=["deploy"],
            )


class CloseoutTextTests(unittest.TestCase):
    """The product-facing text must read the keys the package actually carries.

    `render_v41_closeout_status` used to look for `status` and `p0_p2`, two
    keys the validator's exact key set forbids, so the text stayed "未闭合"
    even for a run that had legitimately closed out.  Pin the reader to the
    decision instead of to a key name.
    """

    def valid_package(self, snapshot):
        return build_integration_review_evidence(
            snapshot, CLEARED_CLAIM,
            agentsmd_acceptance_refs=["AGENTS.md"], unverified_or_excluded=["deploy"],
        )

    def test_text_says_passed_exactly_when_the_closeout_allows_it(self):
        snapshot = _snapshot()
        snapshot.integration_review_evidence = self.valid_package(snapshot)
        snapshot.status = "complete"
        self.assertTrue(evaluate_v41_closeout(snapshot).allowed)
        self.assertEqual(render_v41_closeout_status(snapshot), "整合通过但外部动作未授权")
        snapshot.authorization = {"remote_git_actions": "allow"}
        self.assertEqual(render_v41_closeout_status(snapshot), "整合 Review 已通过")

    def test_text_withholds_acceptance_when_the_closeout_does_not_allow_it(self):
        snapshot = _snapshot()
        package = self.valid_package(snapshot)
        package["findings"] = [{"severity": "p0", "status": "open", "detail": "资金路径"}]
        package["clearance"] = {"p0": 1, "p1": 0, "p2": 0}
        snapshot.integration_review_evidence = package
        snapshot.status = "complete"
        self.assertFalse(evaluate_v41_closeout(snapshot).allowed)
        text = render_v41_closeout_status(snapshot)
        self.assertIn("未闭合", text)
        self.assertIn("不可验收", text)


class ProtocolDocumentsTheClaimTests(unittest.TestCase):
    """The host agent can only send the right shape if the protocol says it.

    Anchored to the one subsection that owns the fact, so a key added to
    `INTEGRATION_REVIEW_CLAIM_KEYS` without a protocol update turns this red
    instead of silently letting reviewers report an unaccepted shape.
    """

    SECTION_HEADING = "#### 整合审查节点的 accepted"

    def section(self):
        text = load_protocol("prd-guide")
        self.assertIn(self.SECTION_HEADING, text)
        return text.split(self.SECTION_HEADING, 1)[1].split("\n#", 1)[0]

    def test_every_claim_key_the_code_requires_is_documented(self):
        section = self.section()
        for key in INTEGRATION_REVIEW_CLAIM_KEYS:
            self.assertIn(key, section)

    def test_the_derived_fields_are_documented_as_not_the_reviewers_to_send(self):
        section = self.section()
        self.assertIn("run_id", section)
        self.assertIn("clearance", section)

    def test_the_protocol_says_which_status_actually_clears_a_finding(self):
        """The protocol is what made a self-served waiver look legal.

        It listed `waived` among the statuses without saying that only
        `resolved` clears, so a host agent following it would waive its own P0.
        The rule is prose, so the assertion has to be on the prose that owns it.

        The first version of this rule then overshot in the other direction --
        "审查者不能给自己签豁免" promised an authorization gate the code does not
        implement, since rewriting the same P0 as `resolved` clears it.  So the
        limit is pinned too, on the clause only this row states.
        """
        section = self.section()
        self.assertIn("只有 `resolved` 算清零", section)
        self.assertIn("integration review acceptance still reports open P0-P2 findings", section)
        self.assertIn("格式约定，不是权限边界", section)

    def test_the_protocol_says_the_verdict_evidence_must_be_text(self):
        """Anchored on the error the code raises, not on prose.

        Asserting "必须是非空字符串" alone passed even with this rule deleted:
        the `cursor` rule further down §6.2 uses the same words, and the
        section window reaches it.

        The raised message is not unique to this row either -- the rejection
        checklist below quotes the same sentence, so inverting this rule while
        the checklist copy stands leaves a bare message assertion green.  So
        the rule is also pinned on the clause only this row states.
        """
        section = self.section()
        self.assertIn("needs a status and a non-empty evidence string", section)
        self.assertIn(
            "两个判断里的 `evidence` **必须是非空字符串**。给嵌套对象会被拒"
            "（`... needs a status and a non-empty evidence string`）",
            section,
        )

    def test_the_protocol_does_not_promise_a_reason_string_the_disk_erases(self):
        """The rejection cause is real but unreadable, so the doc must say so.

        Four different rejection causes were served through the real mailbox and
        all four read `[REDACTED_PROVIDER_TEXT]` in the node reason, in
        `vibe status --json` and in every event -- the reason is redacted on
        every persistence path.  What survives is the claim under the node's
        `evidence`, keys intact and values masked, so the shape is what the
        operator reads.  The marker below appears nowhere else in the protocol,
        and the assertions pin the conclusions, not the phrasing around them.
        A substring on `resolved` 和 `waived` 长得一样 alone was not enough:
        the conclusion beside it could be inverted back to the false claim this
        test exists to retract while that clause stayed verbatim, and the suite
        stayed green.  So the conclusion is asserted directly, its inversion is
        asserted absent, and the shape half gets its own anchor.
        """
        section = self.section()
        self.assertIn("[REDACTED_PROVIDER_TEXT]", section)
        self.assertIn("**形状读得出来**", section)
        self.assertIn("`resolved` 和 `waived` 长得一样", section)
        self.assertIn("`findings` 非空**不代表**有没清零的项", section)
        self.assertNotIn("`findings` 非空**就代表**", section)
        self.assertNotIn("这是可见的阻塞", section)

    def test_the_two_readable_shapes_are_stated_with_their_conditions(self):
        """Both shape rules hold only conditionally, and the unconditional
        phrasing of either one sends the operator to the wrong conclusion.

        `redact_provider_text` drops sensitive keys one at a time, so a nested
        object collapses to `{}` only when every key looks sensitive -- mixed
        keys survive as a non-empty object.  And the rejected claim is the last
        `evidence` entry only on rejection: `record_integration_review` appends
        the derived package after it once the run clears.  Asserted here
        because the loose wording of each passed every other test in this class.

        A bare substring on each condition was not enough either: the sentence
        around `只有每个键都像时` could be inverted to say a mixed object is the
        one that collapses, and the two cases in the `evidence`-entry rule could
        be swapped, both while the asserted literal stayed verbatim.  Eight
        in-memory mutations of this section survived the whole 64-test suite.
        So each condition is pinned together with the case it belongs to, and
        the inversion is asserted absent.
        """
        section = self.section()
        self.assertIn("只有每个键都像时才会只剩 `{}`，混着写还剩几个键", section)
        self.assertNotIn("混着写才会剩 `{}`", section)
        self.assertIn("被拒的时候它是最后一条；清零那次最后一条是派生出来的证据包，不是 claim", section)
        self.assertNotIn("清零的时候它是最后一条", section)

    def test_the_protocol_warns_the_top_level_status_can_be_rewritten(self):
        """Reading the top level instead of the node is the wrong instinct.

        The node is `blocked_unknown`, and so is the top level -- until another
        node retries, at which point the rendered status becomes `retry_pending`
        and names no node at all.  An operator told only "top level is
        blocked_unknown" concludes the run is fine.
        """
        section = self.section()
        self.assertIn("`blocked_unknown`", section)
        self.assertIn("`retry_pending`", section)
        self.assertIn("它不告诉你是哪个节点", section)

    def test_the_protocol_lists_every_closed_value_set_a_legal_shape_can_fail(self):
        """A legal shape can still be rejected on values, so the doc has to
        enumerate all of them or the operator runs out of places to look.

        Two rules (only `resolved` clears, `out_of_scope` empty) left three
        further rejections unlisted -- an unregistered `findings[].severity`,
        an unregistered `findings[].status`, and an unregistered verdict
        `status` -- each of which lands `blocked_unknown` behind a redacted
        reason while the shape on disk stays perfectly legal.  Anchored on the
        raised messages, which appear nowhere else in the protocol.

        The list is five rules now, not four.  An `evidence` of `""`, `"   "`,
        `"\n\t"` or an absent `evidence` key each raises the same
        non-empty-string message as a nested object, and all of those values
        redact to a byte-identical object on disk -- so it is exactly the
        rejection family this checklist exists to cover, and it was the one
        missing.  The count word is asserted too: the checklist promises
        exhaustiveness by number, so the number is part of the claim.
        """
        section = self.section()
        self.assertIn("查这五条", section)
        self.assertIn("integration finding schema is invalid", section)
        self.assertIn("integration finding status is invalid", section)
        self.assertIn("evidence is incomplete", section)
        self.assertIn("evidence is unknown or expired", section)
        # Bullets 1-3.  Each needs a literal the rules further up cannot supply:
        # the `resolved` wording at the top of the section satisfied a bare
        # substring on bullet 1, so bullet 1 is anchored on `accepted`/`waived`
        # being named as non-clearing; bullet 2 on the message the code raises;
        # bullet 3 on the empty-string case being called out by value.
        self.assertIn("`status` 写 `accepted` 或 `waived` 一样不算清零", section)
        self.assertIn("integration aggregated scope contains out-of-scope changes", section)
        self.assertIn("`evidence` 给空字符串、只有空格或换行、或者干脆不给这个键", section)


class MailboxClosesTheRunTests(unittest.TestCase):
    """The real product path: request → mailbox → complete."""

    maxDiff = None

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="v45-closeout-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.paths = ProjectPaths(self.root)

    def cli(self, *argv):
        return run_cli(list(argv) + ["--json"], self.root)

    def serve(self, reviewer_evidence, crash_once_on_acceptance=False, tamper_plan=None):
        """Publish, authorize and serve every mailbox request to a terminus."""
        self.assertEqual(self.cli("init", "--confirm").payload["status"], "ok")
        (self.root / "facts.json").write_text(json.dumps(FACTS), encoding="utf-8")
        self.cli("attest", "--adapter", "claude-code", "--facts", "facts.json",
                 "--provenance", "closeout e2e: native visible-task tools observed",
                 "--project-id", "closeoutprobe")
        shutil.copy(FIXTURE, self.root / "product-spec.json")
        self.cli("plan", "--request", REQUEST, "--plan-id", "closeout", "--from-prd", "product-spec.json")
        self.cli("authorize", "--plan", "closeout", "--authorize", "AUTHORIZE")
        result = self.cli("monitor", "--plan", "closeout", "--authorize", "AUTHORIZE")
        if tamper_plan is not None:
            # Whatever a dispatched agent could do to the plan file once the run
            # is under way, with no privilege this run lacks.  Editing it before
            # `monitor` starts is already refused at the card check, so the
            # window this closes is the one that stays open: after start.
            path = self.root / ".vibe" / "plans" / "closeout" / "plan.json"
            plan = json.loads(path.read_text(encoding="utf-8"))
            tamper_plan(plan)
            path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        run_id = result.payload["run_id"]
        store = ProviderActionStore(self.paths)
        waits = {}
        crashed = []
        real_save = monitor.save_snapshot

        def crashing_save(paths, snapshot):
            """Lose exactly the save that would have persisted the acceptance."""
            if not crashed and snapshot.nodes.get("integration-review", {}).get("status") == "accepted":
                crashed.append(True)
                monitor.save_snapshot = real_save
                raise OSError("crash after the event landed, before the snapshot did")
            return real_save(paths, snapshot)

        if crash_once_on_acceptance:
            monitor.save_snapshot = crashing_save
            self.addCleanup(setattr, monitor, "save_snapshot", real_save)
        for _ in range(80):
            for action in store.pending():
                store.complete(action["action_id"], self.reply(action, waits, reviewer_evidence))
            try:
                result = self.cli("resume", "--plan", "closeout")
            except OSError:
                continue
            if result.payload.get("status") == "complete":
                break
        if crash_once_on_acceptance:
            self.assertTrue(crashed, "the crash hook never fired")
        return result, load_snapshot(self.paths, run_id)

    @staticmethod
    def reply(action, waits, reviewer_evidence):
        operation = action["operation"]
        if operation == "create":
            return {"binding": {"task_id": "sess_" + action["action_id"][:8], "host": "probe"}}
        if operation == "locate":
            return {"located": True}
        if operation == "visibility":
            return {"visible": True, "direct_enter": True}
        if operation == "resume":
            return {"resumed": True}
        key = (action.get("issue_id"), action.get("role"), action.get("generation"))
        waits[key] = waits.get(key, 0) + 1
        cursor = "c_%s_%d" % (action["action_id"][:6], waits[key])
        if waits[key] == 1:
            return {"status": "timeout", "cursor": cursor}
        if action.get("role") == "reviewer":
            evidence = (
                reviewer_evidence(waits[key])
                if callable(reviewer_evidence)
                else reviewer_evidence
            )
            return {"status": "completed", "cursor": cursor, "event": "accepted",
                    "evidence": evidence}
        # ISSUE-04: regular nodes are ruled visible-sdd; the single worker
        # session's delivery must cite its in-session review clearance.
        return {"status": "completed", "cursor": cursor, "event": "complete",
                "delivery_evidence": {"completion_marker": "done",
                                      "delivery_path": "src/export.ts",
                                      "thread_status": "complete"},
                "in_session_review": {
                    "protocol": "vibe_guide/protocols/visible-sdd-worker.md",
                    "evidence_ref": "closeout-e2e#review-round-1",
                    "clearance": {"p0": 0, "p1": 0, "p2": 0}}}

    def test_a_structured_reviewer_acceptance_closes_the_run(self):
        result, snapshot = self.serve(CLEARED_CLAIM)
        self.assertEqual(result.payload["status"], "complete", result.payload)
        self.assertEqual(snapshot.status, "complete")
        package = {key: value for key, value in snapshot.integration_review_evidence.items()
                   if key != "history"}
        validate_integration_review_evidence(snapshot, package)
        self.assertEqual(package["clearance"], {"p0": 0, "p1": 0, "p2": 0})
        self.assertEqual(package["run_id"], snapshot.run_id)
        self.assertEqual(package["aggregated_scope"]["nodes"],
                         [node for node in snapshot.nodes if node != "integration-review"])
        self.assertTrue(evaluate_v41_closeout(snapshot).allowed)
        self.assertIn("整合", result.text[0] if isinstance(result.text, tuple) else result.text)

    def test_a_free_form_reviewer_acceptance_is_rejected_for_re_report_not_bricked(self):
        """A claim-shaped error is a format error, not a terminal doubt.

        ISSUE-83: this used to brick the run -- the accepted event was
        recorded, the binding flipped and the handle popped before the schema
        said no, leaving no recovery path.  Validate-first rejects it as pure
        audit: no lifecycle event, live handle, same session may re-report.
        """
        result, snapshot = self.serve("P0-P2 cleared")
        self.assertNotEqual(result.payload["status"], "complete", result.payload)
        node = snapshot.nodes["integration-review"]
        self.assertNotEqual(node["status"], "accepted")
        self.assertEqual(snapshot.integration_review_evidence, {})
        # The stable signals of a live re-report channel: the reviewer handle
        # and its active task were never consumed, and every malformed report
        # landed as a recoverable audit event rather than a lifecycle
        # transition.  (Node `status` itself is not a stable signal here --
        # the pre-existing "provider wait is pending" visibility flicker
        # rewrites it between polls and the next poll repairs it.)
        self.assertIn("integration-review", snapshot.handles)
        self.assertIsInstance(node.get("active_task"), dict)
        events = load_events(self.paths, snapshot.run_id)
        rejections = [
            record for record in events
            if record["event"] == "acceptance_rejected"
            and record["data"].get("node_id") == "integration-review"
        ]
        self.assertTrue(rejections, events)
        self.assertTrue(rejections[0]["data"].get("recoverable"))
        self.assertEqual(
            [r for r in events if r["event"] == "accepted"
             and r["data"].get("node_id") == "integration-review"],
            [],
        )

    def test_a_corrected_claim_on_the_same_session_closes_the_run(self):
        """The re-report channel end to end: malformed first, corrected after.

        The reviewer session's second completed wait carries a free-form
        string; the rejection keeps its handle alive, and the corrected claim
        on the same session closes the run out.
        """
        def evidence_by_wait(count):
            return "P0-P2 cleared" if count == 2 else CLEARED_CLAIM

        result, snapshot = self.serve(evidence_by_wait)
        self.assertEqual(result.payload["status"], "complete", result.payload)
        self.assertEqual(snapshot.status, "complete")
        package = {key: value for key, value in snapshot.integration_review_evidence.items()
                   if key != "history"}
        validate_integration_review_evidence(snapshot, package)
        self.assertEqual(package["clearance"], {"p0": 0, "p1": 0, "p2": 0})

    def test_an_acceptance_that_still_reports_findings_is_blocked(self):
        claim = dict(CLEARED_CLAIM, findings=[
            {"severity": "p1", "status": "open", "detail": "事务边界未验证"},
        ])
        result, snapshot = self.serve(claim)
        self.assertNotEqual(result.payload["status"], "complete", result.payload)
        self.assertEqual(snapshot.nodes["integration-review"]["status"], "blocked_unknown")
        self.assertEqual(snapshot.integration_review_evidence, {})

    def test_a_waived_finding_does_not_close_the_run(self):
        claim = dict(CLEARED_CLAIM, findings=[
            {"severity": "p0", "status": "waived", "detail": "资金结算路径没验证，本轮先放行"},
        ])
        result, snapshot = self.serve(claim)
        self.assertNotEqual(result.payload["status"], "complete", result.payload)
        self.assertEqual(snapshot.nodes["integration-review"]["status"], "blocked_unknown")
        self.assertEqual(snapshot.integration_review_evidence, {})

    def test_a_verdict_the_disk_cannot_keep_never_reports_complete(self):
        """The CLI must not announce a closeout the next read rolls back.

        With a nested object under `evidence` the first serve reported
        `complete`, then `load_snapshot` rejected the persisted package and fell
        back to the previous snapshot -- so the acceptance was lost and the run
        reverted to `blocked_unknown` for good.
        """
        claim = dict(CLEARED_CLAIM,
                     iteration_compatibility={"status": "verified", "evidence": {"secret_scan": "clean"}})
        result, snapshot = self.serve(claim)
        self.assertNotEqual(result.payload["status"], "complete", result.payload)
        node = snapshot.nodes["integration-review"]
        # ISSUE-83: a malformed verdict is rejected for re-report on the live
        # handle; it no longer bricks the run, and it never reported complete.
        self.assertNotEqual(node["status"], "accepted")
        self.assertIn("integration-review", snapshot.handles)
        self.assertEqual(snapshot.integration_review_evidence, {})

    def test_the_scope_the_reviewer_is_held_to_comes_from_the_plan(self):
        """The reviewer never sends these two lists, so the plan must supply them.

        Hardcoding either one in the monitor left every other assertion green,
        which is the whole claim of this change ("a reviewer cannot shrink the
        scope it is held to") going untested.
        """
        _, snapshot = self.serve(CLEARED_CLAIM)
        plan = json.loads((self.root / ".vibe" / "plans" / "closeout" / "plan.json").read_text(encoding="utf-8"))
        contract = plan["integration_contract"]
        package = snapshot.integration_review_evidence
        self.assertEqual(package["agentsmd_acceptance_refs"], contract["agentsmd_acceptance_refs"])
        self.assertEqual(package["unverified_or_excluded"], contract["unverified_or_excluded"])
        self.assertTrue(contract["unverified_or_excluded"], contract)

    def test_the_scope_cannot_be_shrunk_by_editing_the_plan_after_authorization(self):
        """Reading the live plan is only safe against the frozen digest.

        The plan is re-read on every resume and the agents this run dispatches
        can write to it, so without comparing the authorization card's
        `integration_contract_digest` a post-authorization edit rewrote the run's
        own audit package: the permanent exclusions the product owner was
        promised turned into whatever the editor wrote, and the run still
        reported `complete`.  The digest was recorded at authorization and read
        by nobody.
        """
        def shrink(plan):
            plan["integration_contract"]["unverified_or_excluded"] = ["nothing_at_all"]
            plan["integration_contract"]["agentsmd_acceptance_refs"] = ["forged.md"]

        result, snapshot = self.serve(CLEARED_CLAIM, tamper_plan=shrink)
        self.assertNotEqual(result.payload["status"], "complete", result.payload)
        self.assertEqual(snapshot.nodes["integration-review"]["status"], "blocked_unknown")
        self.assertEqual(snapshot.integration_review_evidence, {})

    def test_a_crash_before_the_acceptance_is_saved_recovers_by_re_reporting(self):
        """Recovery cannot rebuild the claim, so it asks for it again.

        The appended event keeps only redacted provider text, so replaying it
        could never reproduce the reviewer's judgement.  The replay door fails
        closed instead of inventing a package, which lets the monitor dispatch
        a second reviewer generation whose acceptance closes the run out for
        real.  A rebuilt-from-the-event package would leave the generation at 1.
        """
        result, snapshot = self.serve(CLEARED_CLAIM, crash_once_on_acceptance=True)
        integration = snapshot.nodes["integration-review"]
        self.assertGreater(integration["review_generation"], 1)
        self.assertEqual(result.payload["status"], "complete", result.payload)
        package = {key: value for key, value in snapshot.integration_review_evidence.items()
                   if key != "history"}
        validate_integration_review_evidence(snapshot, package)
        self.assertTrue(evaluate_v41_closeout(snapshot).allowed)


if __name__ == "__main__":
    unittest.main()
