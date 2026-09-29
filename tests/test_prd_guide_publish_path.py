"""prd-guide, followed to the letter, from draft to authorization (issue #97).

The protocol says: route the request (§1, no `--s1`), keep the draft's
`plan_id`, settle the remote Git switch with the product manager, then publish
with that same `plan_id` from the product spec (§5.3) and authorize.  The
earlier end-to-end test published under a fresh id with a text-complex request,
so none of the three places where the code disagreed with that text was ever
exercised:

1. the draft directory made publishing under its own id fail with
   `plan already exists`;
2. publishing without `--s1` re-scored the request text, and a draft routed
   complex by the session's own S1 fell to `simple`, dropped the product spec
   and still answered `status: ok`;
3. the remote Git switch is fixed at publish time, not at authorization.
"""
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vibe_guide.cli import run_cli
from vibe_guide.session_entry import stable_plan_id

FIXTURE = Path(__file__).parent / "fixtures" / "pm-path" / "product-spec.json"
# Complex from its text alone: the §1 path.
COMPLEX_REQUEST = "设计并实现保单查看页的 PDF 导出，集成日期范围筛选、编写测试并部署"
# Scores low from its text alone; only the session's own S1 makes it complex.
# This is the request that fell to `simple` while V4.10 was being published.
SHORT_REQUEST = "发布 V4.10 计划"
SESSION_S1 = "5,4,4,3,3"
FACTS = {name: True for name in (
    "claude-code.agent", "claude-code.shell", "claude-code.subprocess", "claude-code.worktree",
    "claude-code.visible_task.create", "claude-code.visible_task.enter",
    "claude-code.visible_task.resume", "claude-code.visible_task.wait",
    "claude-code.in_session_sdd",
)}


class _Project(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="issue97-"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def cli(self, *argv):
        return run_cli(list(argv) + ["--json"], self.root)

    def prepare_session(self):
        self.assertEqual(self.cli("init", "--confirm").payload["status"], "ok")
        (self.root / "facts.json").write_text(json.dumps(FACTS), encoding="utf-8")
        attested = self.cli(
            "attest", "--adapter", "claude-code", "--facts", "facts.json",
            "--provenance", "issue97: visible-task tools observed", "--project-id", "issue97",
        )
        self.assertEqual(attested.payload["status"], "ok", attested.payload)

    def write_spec(self, relative, remote_git_actions=None):
        spec = json.loads(FIXTURE.read_text(encoding="utf-8"))
        if remote_git_actions is not None:
            spec["remote_git_actions"] = remote_git_actions
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        return relative

    def plan_dir(self, plan_id):
        return self.root / ".vibe" / "plans" / plan_id

    def new_id_spec_location(self, plan_id):
        """Where §5.1 says to keep the spec when publishing under an id with no draft."""
        from vibe_guide.protocols import load_protocol
        text = load_protocol("prd-guide")
        start = text.index("### 5.1 ")
        section = text[start:text.index("\n### 5.2 ", start)]
        lines = [line for line in section.splitlines() if "没有草案的新 `plan_id`" in line]
        self.assertEqual(len(lines), 1, "§5.1 names no spec location for a new plan_id: {}".format(lines))
        locations = re.findall(r"`([^`]*product-spec\.json)`", lines[0])
        self.assertEqual(len(locations), 1, lines[0])
        location = locations[0].replace("<plan_id>", plan_id)
        self.assertNotRegex(location, "[<>]", "unresolved placeholder in §5.1's location")
        return location

    def card(self, plan_id):
        return json.loads((self.plan_dir(plan_id) / "authorization-card.json").read_text(encoding="utf-8"))


class ProtocolPathTests(_Project):
    def assert_complex_publish(self, published, plan_id, remote_git_actions):
        self.assertEqual(published.payload.get("status"), "ok", published.payload)
        self.assertEqual(published.payload.get("route"), "complex", published.payload)
        plan = json.loads((self.plan_dir(plan_id) / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(plan["complexity_band"], "complex")
        self.assertIn("integration-review", plan["node_ids"])
        self.assertEqual(self.card(plan_id)["remote_git_actions"], remote_git_actions)

    def test_draft_then_same_id_publish_without_s1_reaches_authorization(self):
        # §1: route the request as-is, no --s1; keep the draft's plan_id.
        routed = self.cli("plan", "--request", COMPLEX_REQUEST)
        self.assertEqual(routed.payload["status"], "planned", routed.payload)
        plan_id = routed.payload["plan_id"]
        self.prepare_session()
        # §5.1 suggests keeping the spec inside the draft's own directory, and
        # the remote Git choice is written into it before publishing (§5.3).
        spec = self.write_spec(".vibe/plans/{}/product-spec.json".format(plan_id), "allow")
        # §5.3: publish with the draft's plan_id, still without --s1.
        published = self.cli("plan", "--request", COMPLEX_REQUEST, "--plan-id", plan_id, "--from-prd", spec)
        self.assert_complex_publish(published, plan_id, "allow")
        self.assertTrue((self.root / spec).is_file(), "publishing replaced the draft and lost the spec")
        authorized = self.cli("authorize", "--plan", plan_id, "--authorize", "AUTHORIZE")
        self.assertEqual(authorized.payload.get("status"), "ok", authorized.payload)

    def test_draft_next_step_without_plan_id_publishes_over_the_draft(self):
        """The draft's own `next_step` omits --plan-id; the stable id applies."""
        routed = self.cli("plan", "--request", COMPLEX_REQUEST)
        plan_id = routed.payload["plan_id"]
        self.assertEqual(plan_id, stable_plan_id(COMPLEX_REQUEST))
        self.prepare_session()
        published = self.cli("plan", "--request", COMPLEX_REQUEST, "--from-prd", self.write_spec("product-spec.json"))
        self.assert_complex_publish(published, plan_id, "deny")

    def test_publish_keeps_the_session_s1_of_its_draft(self):
        """The vibe-entry path: the draft was routed by the session's --s1."""
        routed = self.cli("plan", "--request", SHORT_REQUEST, "--s1", SESSION_S1)
        self.assertEqual(routed.payload["route"], "complex", routed.payload)
        plan_id = routed.payload["plan_id"]
        self.prepare_session()
        published = self.cli(
            "plan", "--request", SHORT_REQUEST, "--plan-id", plan_id,
            "--from-prd", self.write_spec("product-spec.json", "deny"),
        )
        self.assert_complex_publish(published, plan_id, "deny")
        self.assertEqual(published.payload.get("score"), 19, published.payload)

    def test_changing_the_switch_republishes_under_a_new_id_with_the_returned_s1(self):
        """§5.3: the switch cannot change after publishing; republish instead.

        The draft is gone by then, so the S1 comes from the first publish's
        own result -- otherwise a short request falls back to its text score.
        """
        plan_id = self.cli("plan", "--request", SHORT_REQUEST, "--s1", SESSION_S1).payload["plan_id"]
        self.prepare_session()
        first = self.cli(
            "plan", "--request", SHORT_REQUEST, "--plan-id", plan_id,
            "--from-prd", self.write_spec("product-spec.json", "deny"),
        )
        self.assertEqual(first.payload.get("s1"), SESSION_S1, first.payload)
        again = self.cli(
            "plan", "--request", SHORT_REQUEST, "--plan-id", plan_id + "-r2", "--s1", first.payload["s1"],
            "--from-prd", self.write_spec("product-spec-r2.json", "allow"),
        )
        self.assertEqual(again.payload.get("status"), "ok", again.payload)
        self.assertEqual(again.payload.get("score"), 19, again.payload)
        self.assertEqual(self.card(plan_id + "-r2")["remote_git_actions"], "allow")
        self.assertEqual(self.card(plan_id)["remote_git_actions"], "deny")

    def test_switch_change_republish_follows_the_spec_location_literally(self):
        """§5.1 + §5.3, to the letter: the new id has no draft to publish over.

        The spec kept in the draft's directory for the first publish is copied
        to where §5.1 says a new id's spec goes, the switch is changed there,
        and the republish must succeed.
        """
        plan_id = self.cli("plan", "--request", SHORT_REQUEST, "--s1", SESSION_S1).payload["plan_id"]
        self.prepare_session()
        spec = self.write_spec(".vibe/plans/{}/product-spec.json".format(plan_id), "deny")
        first = self.cli("plan", "--request", SHORT_REQUEST, "--plan-id", plan_id, "--from-prd", spec)
        self.assert_complex_publish(first, plan_id, "deny")
        new_id = plan_id + "-r2"
        location = self.new_id_spec_location(new_id)
        target = self.root / location
        target.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads((self.root / spec).read_text(encoding="utf-8"))
        data["remote_git_actions"] = "allow"
        target.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        again = self.cli(
            "plan", "--request", SHORT_REQUEST, "--plan-id", new_id, "--s1", first.payload["s1"],
            "--from-prd", location,
        )
        self.assert_complex_publish(again, new_id, "allow")
        self.assertEqual(self.card(plan_id)["remote_git_actions"], "deny")

    def test_a_spec_kept_under_a_new_ids_directory_blocks_its_publish(self):
        """The code fact §5.1 rests on: only a draft directory is published over."""
        plan_id = self.cli("plan", "--request", SHORT_REQUEST, "--s1", SESSION_S1).payload["plan_id"]
        self.prepare_session()
        new_id = plan_id + "-r2"
        spec = self.write_spec(".vibe/plans/{}/product-spec.json".format(new_id), "allow")
        result = self.cli(
            "plan", "--request", SHORT_REQUEST, "--plan-id", new_id, "--s1", SESSION_S1, "--from-prd", spec,
        )
        self.assertEqual(result.payload.get("status"), "blocked", result.payload)
        self.assertIn("plan already exists", result.payload.get("reason", ""))
        self.assertEqual([p.name for p in self.plan_dir(new_id).iterdir()], ["product-spec.json"])

    def test_a_product_spec_is_never_dropped_behind_status_ok(self):
        """No draft to inherit from and a low-scoring text: refuse, loudly."""
        self.prepare_session()
        published = self.cli(
            "plan", "--request", SHORT_REQUEST, "--plan-id", "no-draft",
            "--from-prd", self.write_spec("product-spec.json"),
        )
        self.assertNotEqual(published.payload.get("status"), "ok", published.payload)
        self.assertEqual(published.payload.get("status"), "blocked", published.payload)
        self.assertIn("--s1", published.payload.get("reason", ""))
        self.assertFalse(self.plan_dir("no-draft").exists())

    def test_explicit_s1_still_wins_over_the_draft(self):
        """The draft only stands in for a missing --s1; it never overrides one."""
        routed = self.cli("plan", "--request", SHORT_REQUEST, "--s1", SESSION_S1)
        plan_id = routed.payload["plan_id"]
        self.prepare_session()
        published = self.cli(
            "plan", "--request", SHORT_REQUEST, "--plan-id", plan_id, "--s1", "4,4,4,2,2",
            "--from-prd", self.write_spec("product-spec.json"),
        )
        # Lower than the draft's 19 on purpose: a higher explicit score would
        # pass whether or not the draft is consulted.
        self.assertEqual(published.payload.get("status"), "ok", published.payload)
        self.assertEqual(published.payload.get("score"), 16, published.payload)


class DraftReplacementBoundaryTests(_Project):
    """Only a draft that never reached publication may be replaced."""

    def draft(self):
        plan_id = self.cli("plan", "--request", COMPLEX_REQUEST).payload["plan_id"]
        self.prepare_session()
        return plan_id

    def publish(self, plan_id):
        return self.cli(
            "plan", "--request", COMPLEX_REQUEST, "--plan-id", plan_id,
            "--from-prd", self.write_spec("product-spec.json"),
        )

    def test_a_published_plan_is_never_overwritten(self):
        plan_id = self.draft()
        self.assertEqual(self.publish(plan_id).payload.get("status"), "ok")
        before = (self.plan_dir(plan_id) / "authorization-card.json").read_bytes()
        again = self.publish(plan_id)
        self.assertEqual(again.payload.get("status"), "blocked", again.payload)
        self.assertIn("plan already exists", again.payload.get("reason", ""))
        self.assertEqual((self.plan_dir(plan_id) / "authorization-card.json").read_bytes(), before)

    def test_a_draft_marked_otherwise_is_not_replaced(self):
        plan_id = self.draft()
        plan_json = self.plan_dir(plan_id) / "plan.json"
        data = json.loads(plan_json.read_text(encoding="utf-8"))
        data["status"] = "confirmed_pending_authorization"
        plan_json.write_text(json.dumps(data), encoding="utf-8")
        result = self.publish(plan_id)
        self.assertEqual(result.payload.get("status"), "blocked", result.payload)
        self.assertIn("plan already exists", result.payload.get("reason", ""))

    def test_a_draft_holding_authorization_or_run_state_is_not_replaced(self):
        for marker in ("authorization-card.json", "current-run.json", "authorization-invalidated.json"):
            with self.subTest(marker=marker):
                shutil.rmtree(self.root / ".vibe", True)
                plan_id = self.draft()
                (self.plan_dir(plan_id) / marker).write_text("{}", encoding="utf-8")
                result = self.publish(plan_id)
                self.assertEqual(result.payload.get("status"), "blocked", result.payload)
                self.assertIn("plan already exists", result.payload.get("reason", ""))
                self.assertTrue((self.plan_dir(plan_id) / marker).is_file())

    def test_a_draft_with_a_subdirectory_is_not_replaced(self):
        plan_id = self.draft()
        (self.plan_dir(plan_id) / "specs").mkdir()
        result = self.publish(plan_id)
        self.assertEqual(result.payload.get("status"), "blocked", result.payload)
        self.assertTrue((self.plan_dir(plan_id) / "specs").is_dir())

    def test_a_symlinked_plan_id_never_replaces_the_draft_it_points_to(self):
        plan_id = self.draft()
        os.symlink(plan_id, str(self.plan_dir("alias")))
        result = self.publish("alias")
        self.assertEqual(result.payload.get("status"), "blocked", result.payload)
        self.assertIn("plan already exists", result.payload.get("reason", ""))
        plan = json.loads((self.plan_dir(plan_id) / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual((plan["plan_id"], plan["status"]), (plan_id, "draft"))

    def test_a_failed_swap_restores_the_draft_and_its_spec(self):
        plan_id = self.draft()
        spec = self.write_spec(".vibe/plans/{}/product-spec.json".format(plan_id))
        before = sorted(p.name for p in self.plan_dir(plan_id).iterdir())
        real_replace = os.replace

        def failing_replace(source, target):
            if str(target) == str(self.plan_dir(plan_id).resolve()) or str(target) == str(self.plan_dir(plan_id)):
                raise OSError("injected swap failure")
            return real_replace(source, target)

        with mock.patch("vibe_guide.cli.os.replace", side_effect=failing_replace):
            result = self.cli("plan", "--request", COMPLEX_REQUEST, "--plan-id", plan_id, "--from-prd", spec)
        self.assertNotEqual(result.payload.get("status"), "ok", result.payload)
        self.assertEqual(sorted(p.name for p in self.plan_dir(plan_id).iterdir()), before)
        leftovers = [p.name for p in self.plan_dir(plan_id).parent.iterdir() if p.name.startswith(".")]
        self.assertEqual(leftovers, [])

    def test_a_failed_park_leaves_no_hidden_directory(self):
        """Moving the draft aside can itself fail; nothing may be left behind."""
        plan_id = self.draft()
        before = sorted(p.name for p in self.plan_dir(plan_id).iterdir())
        import vibe_guide.cli as cli_module
        real_rename = os.rename

        def failing_park(source, target):
            if str(source).endswith(plan_id) and "draft" in str(target):
                raise OSError("injected park failure")
            return real_rename(source, target)

        with mock.patch.object(cli_module.os, "rename", side_effect=failing_park):
            result = self.publish(plan_id)
        self.assertEqual(result.payload.get("status"), "blocked", result.payload)
        self.assertIn("injected park failure", result.payload.get("reason", ""))
        self.assertEqual(sorted(p.name for p in self.plan_dir(plan_id).iterdir()), before)
        leftovers = [p.name for p in self.plan_dir(plan_id).parent.iterdir() if p.name.startswith(".")]
        self.assertEqual(leftovers, [])

    def test_an_interrupt_after_parking_never_deletes_the_draft(self):
        """The park cleanup may only remove an empty holding directory."""
        plan_id = self.draft()
        import vibe_guide.cli as cli_module
        real_rename = os.rename

        def interrupted_park(source, target):
            real_rename(source, target)
            if str(source).endswith(plan_id) and "draft" in str(target):
                raise KeyboardInterrupt

        with mock.patch.object(cli_module.os, "rename", side_effect=interrupted_park):
            with self.assertRaises(KeyboardInterrupt):
                self.publish(plan_id)
        parked = [p / "draft" / "plan.json" for p in self.plan_dir(plan_id).parent.iterdir() if p.name.startswith(".")]
        self.assertTrue(any(path.is_file() for path in parked), "the parked draft was deleted")

    def test_the_protocol_recovery_step_brings_a_parked_draft_back(self):
        """§5.3's recovery, followed literally, lets the same publish succeed."""
        plan_id = self.draft()
        spec = self.write_spec(".vibe/plans/{}/product-spec.json".format(plan_id))
        import vibe_guide.cli as cli_module
        real_rename = os.rename

        def interrupted_park(source, target):
            real_rename(source, target)
            if str(source).endswith(plan_id) and "draft" in str(target):
                raise KeyboardInterrupt

        with mock.patch.object(cli_module.os, "rename", side_effect=interrupted_park):
            with self.assertRaises(KeyboardInterrupt):
                self.cli("plan", "--request", COMPLEX_REQUEST, "--plan-id", plan_id, "--from-prd", spec)
        self.assertFalse(self.plan_dir(plan_id).exists())
        # The protocol: .vibe/plans/.<plan_id>.draft.*/draft goes back to
        # .vibe/plans/<plan_id> when that does not exist.
        parked = list((self.root / ".vibe" / "plans").glob(".{}.draft.*/draft".format(plan_id)))
        self.assertEqual(len(parked), 1, parked)
        shutil.move(str(parked[0]), str(self.plan_dir(plan_id)))
        published = self.cli("plan", "--request", COMPLEX_REQUEST, "--plan-id", plan_id, "--from-prd", spec)
        self.assertEqual(published.payload.get("status"), "ok", published.payload)
        self.assertTrue((self.root / spec).is_file())

    def test_an_interrupt_before_the_swap_leaves_the_draft_in_place(self):
        """§5.3 branch ①: no parked draft, rerun the same command."""
        plan_id = self.draft()
        spec = self.write_spec(".vibe/plans/{}/product-spec.json".format(plan_id))
        import vibe_guide.cli as cli_module
        with mock.patch.object(cli_module, "render_plan_artifacts", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.cli("plan", "--request", COMPLEX_REQUEST, "--plan-id", plan_id, "--from-prd", spec)
        self.assertEqual(list((self.root / ".vibe" / "plans").glob(".{}.draft.*/draft".format(plan_id))), [])
        published = self.cli("plan", "--request", COMPLEX_REQUEST, "--plan-id", plan_id, "--from-prd", spec)
        self.assertEqual(published.payload.get("status"), "ok", published.payload)

    def test_a_parked_draft_behind_another_publication_republishes_under_a_new_id(self):
        """§5.3 branch ③, followed literally.

        Another publisher can take the id while the draft is moved aside, so
        the card at the destination is not this publish's; the spec is taken
        back and published under a new id with the parked draft's own S1.
        """
        plan_id = self.cli("plan", "--request", SHORT_REQUEST, "--s1", SESSION_S1).payload["plan_id"]
        self.prepare_session()
        spec = self.write_spec(".vibe/plans/{}/product-spec.json".format(plan_id), "allow")
        import vibe_guide.cli as cli_module
        real_rename = os.rename
        state = {"parked": False}

        def other_publisher(source, target):
            real_rename(source, target)
            if not state["parked"] and str(source).endswith(plan_id) and "draft" in str(target):
                state["parked"] = True
                self.plan_dir(plan_id).mkdir()
                (self.plan_dir(plan_id) / "authorization-card.json").write_text('{"by": "other"}', encoding="utf-8")

        with mock.patch.object(cli_module.os, "rename", side_effect=other_publisher):
            result = self.cli("plan", "--request", SHORT_REQUEST, "--plan-id", plan_id, "--from-prd", spec)
        self.assertIn("draft could not be restored", result.payload.get("reason", ""))
        self.assertEqual(self.card(plan_id), {"by": "other"})
        parked = list((self.root / ".vibe" / "plans").glob(".{}.draft.*/draft".format(plan_id)))
        self.assertEqual(len(parked), 1, parked)
        # Take the spec back; --s1 from the parked plan.json, in the named order.
        location = self.new_id_spec_location(plan_id + "-r2")
        taken = self.root / location
        taken.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(parked[0] / "product-spec.json"), str(taken))
        dimensions = json.loads((parked[0] / "plan.json").read_text(encoding="utf-8"))["route_result"]["dimensions"]
        s1 = ",".join(str(dimensions[name]) for name in ("steps", "domains", "uncertainty", "failure_cost", "toolchain"))
        again = self.cli(
            "plan", "--request", SHORT_REQUEST, "--plan-id", plan_id + "-r2", "--s1", s1,
            "--from-prd", location,
        )
        self.assertEqual(again.payload.get("status"), "ok", again.payload)
        self.assertEqual(again.payload.get("score"), 19, again.payload)
        self.assertEqual(self.card(plan_id + "-r2")["remote_git_actions"], "allow")

    def test_an_interrupt_after_the_swap_leaves_this_publication_under_the_old_id(self):
        """§5.3 branch ③ also covers this publish having landed.

        The swap lands before the holding directory is cleaned up; an
        interrupt in between leaves a parked draft next to a real,
        unauthorized publication that nothing marks as this session's.  The
        protocol republishes under a new id and leaves the old one as it is.
        """
        plan_id = self.draft()
        spec = self.write_spec(".vibe/plans/{}/product-spec.json".format(plan_id))
        import vibe_guide.cli as cli_module
        real_rmtree = shutil.rmtree

        def interrupted_cleanup(path, *args, **kwargs):
            if ".{}.draft.".format(plan_id) in str(path):
                raise KeyboardInterrupt
            return real_rmtree(path, *args, **kwargs)

        with mock.patch.object(cli_module.shutil, "rmtree", side_effect=interrupted_cleanup):
            with self.assertRaises(KeyboardInterrupt):
                self.cli("plan", "--request", COMPLEX_REQUEST, "--plan-id", plan_id, "--from-prd", spec)
        # Branch ③: a parked draft, and the id is taken.
        parked = list((self.root / ".vibe" / "plans").glob(".{}.draft.*/draft".format(plan_id)))
        self.assertEqual(len(parked), 1, parked)
        self.assertTrue(self.plan_dir(plan_id).is_dir())
        old_plan = (self.plan_dir(plan_id) / "plan.json").read_bytes()
        self.assertNotEqual(json.loads(old_plan).get("status"), "draft")
        self.assertIn("remote_git_actions", self.card(plan_id))
        # Followed literally: new id, parked S1, old plan untouched.
        location = self.new_id_spec_location(plan_id + "-r2")
        taken = self.root / location
        taken.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(parked[0] / "product-spec.json"), str(taken))
        dimensions = json.loads((parked[0] / "plan.json").read_text(encoding="utf-8"))["route_result"]["dimensions"]
        s1 = ",".join(str(dimensions[name]) for name in ("steps", "domains", "uncertainty", "failure_cost", "toolchain"))
        again = self.cli(
            "plan", "--request", COMPLEX_REQUEST, "--plan-id", plan_id + "-r2", "--s1", s1,
            "--from-prd", location,
        )
        self.assertEqual(again.payload.get("status"), "ok", again.payload)
        # The new publish did not overwrite the old directory.
        self.assertEqual((self.plan_dir(plan_id) / "plan.json").read_bytes(), old_plan)
        # Authorization goes to the new id only; it is recorded in state.json,
        # not plan.json, so that is where the old id must stay absent.
        authorized = self.cli("authorize", "--plan", plan_id + "-r2", "--authorize", "AUTHORIZE")
        self.assertEqual(authorized.payload.get("status"), "ok", authorized.payload)
        workflow = json.loads((self.root / ".vibe" / "state.json").read_text(encoding="utf-8"))["task_workflow"]
        self.assertIn(plan_id + "-r2", workflow)
        self.assertNotIn(plan_id, workflow)

    def test_a_publication_landing_mid_swap_is_not_overwritten(self):
        """Re-check what was actually moved aside, not what was seen before."""
        plan_id = self.draft()
        import vibe_guide.cli as cli_module
        real_rename = os.rename
        state = {"raced": False}

        def racing_rename(source, target):
            if not state["raced"] and str(source).endswith(plan_id) and "draft" in str(target):
                state["raced"] = True
                # Another publisher finished in the window: the directory
                # is no longer a draft by the time it is moved aside.
                (Path(source) / "authorization-card.json").write_text("{}", encoding="utf-8")
            return real_rename(source, target)

        with mock.patch.object(cli_module.os, "rename", side_effect=racing_rename):
            result = self.publish(plan_id)
        self.assertTrue(state["raced"], "the swap never moved the draft aside")
        self.assertEqual(result.payload.get("status"), "blocked", result.payload)
        self.assertTrue((self.plan_dir(plan_id) / "authorization-card.json").is_file())
        leftovers = [p.name for p in self.plan_dir(plan_id).parent.iterdir() if p.name.startswith(".")]
        self.assertEqual(leftovers, [])

    def test_a_rejected_publish_leaves_the_draft_intact(self):
        """A publish that fails its gates must not consume the draft."""
        plan_id = self.draft()
        before = sorted(p.name for p in self.plan_dir(plan_id).iterdir())
        spec = json.loads(FIXTURE.read_text(encoding="utf-8"))
        spec["decisions"][0]["status"] = "unresolved"
        spec["decisions"][0]["selected"] = None
        (self.root / "bad.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        result = self.cli("plan", "--request", COMPLEX_REQUEST, "--plan-id", plan_id, "--from-prd", "bad.json")
        self.assertNotEqual(result.payload.get("status"), "ok", result.payload)
        self.assertEqual(sorted(p.name for p in self.plan_dir(plan_id).iterdir()), before)
        plan = json.loads((self.plan_dir(plan_id) / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(plan["status"], "draft")


if __name__ == "__main__":
    unittest.main()
