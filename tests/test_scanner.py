import json, subprocess, tempfile, unittest
from pathlib import Path
from unittest import mock
from vibe_guide.doctor import doctor
from vibe_guide.paths import ProjectPaths
from vibe_guide.scanner import scan_project, build_agentsmd_patch

class ScannerTests(unittest.TestCase):
    def test_scan_facts_and_missing_rules_proposal(self):
        with tempfile.TemporaryDirectory() as d:
            p = ProjectPaths.from_cwd(Path(d)); r = scan_project(p)
            self.assertFalse(r.agentsmd_exists); self.assertFalse(r.knowledge_exists)
            self.assertTrue(build_agentsmd_patch(None, r).proposed)
    def test_existing_agents_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d); (p/'AGENTS.md').write_text('keep', encoding='utf-8')
            self.assertEqual(scan_project(ProjectPaths.from_cwd(p)).agentsmd_content, 'keep')

    def test_scan_redacts_remote_userinfo_and_reports_agent_candidates(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            credential = 'synthetic' + '-credential'
            subprocess.run(['git', 'init', str(root)], check=True, capture_output=True)
            subprocess.run(
                [
                    'git', '-C', str(root), 'remote', 'add', 'origin',
                    'https://reader:' + credential + '@github.com/example/demo.git',
                ],
                check=True,
            )

            with mock.patch(
                'vibe_guide.scanner.shutil.which',
                side_effect=lambda command: '/usr/bin/' + command
                if command == 'codex' else None,
            ):
                report = scan_project(ProjectPaths.from_cwd(root))

            self.assertEqual(
                report.git_remote, 'https://github.com/example/demo'
            )
            self.assertNotIn(credential, repr(report))
            commands = getattr(report, 'agent_commands', {})
            self.assertTrue(commands.get('codex'))
            self.assertFalse(commands.get('claude'))

    def test_scan_discovers_bounded_configured_skill_records(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / '.vibe').mkdir()
            config = {
                'skills': [
                    {
                        'name': 'architecture-skill-pack',
                        'source': (
                            'git@github.com:lov-team/architecture-skill-pack.git'
                        ),
                        'commit': 'a' * 40,
                    }
                ]
            }
            (root / '.vibe' / 'config.json').write_text(
                json.dumps(config), encoding='utf-8'
            )

            report = scan_project(ProjectPaths.from_cwd(root))

            self.assertEqual(len(report.skills), 1)
            self.assertEqual(
                report.skills[0]['source'],
                'https://github.com/lov-team/architecture-skill-pack',
            )
            self.assertTrue(report.skills[0]['valid'])
            self.assertIsNone(getattr(report, 'skill_records_error', 'missing'))

    def test_scan_bounds_configured_skill_record_input(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / '.vibe').mkdir()
            (root / '.vibe' / 'config.json').write_text(
                ' ' * (64 * 1024 + 1), encoding='utf-8'
            )

            report = scan_project(ProjectPaths.from_cwd(root))

            self.assertEqual(report.skills, [])
            self.assertEqual(
                getattr(report, 'skill_records_error', None), 'config too large'
            )

    def test_top_level_vibe_symlink_does_not_read_external_skill_config(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            project = base / 'project'
            outside = base / 'outside'
            project.mkdir()
            outside.mkdir()
            (outside / 'config.json').write_text(
                json.dumps(
                    {
                        'skills': [
                            {
                                'name': 'outside-secret-record',
                                'source': 'https://github.com/example/outside',
                                'commit': 'c' * 40,
                            }
                        ]
                    }
                ),
                encoding='utf-8',
            )
            (project / '.vibe').symlink_to(outside, target_is_directory=True)

            report = scan_project(ProjectPaths.from_cwd(project))

            self.assertFalse(report.vibe_exists)
            self.assertEqual(report.skills, [])
            self.assertEqual(report.skill_records_error, 'invalid .vibe directory')
            self.assertNotIn('outside-secret-record', repr(report))

    def test_doctor_reports_observable_facts_without_authority_inference(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'AGENTS.md').write_text('rules\n', encoding='utf-8')
            (root / '.vibe' / 'knowledge').mkdir(parents=True)
            (root / '.vibe' / 'config.json').write_text(
                json.dumps(
                    {
                        'skills': [
                            {
                                'name': 'architecture-skill-pack',
                                'source': (
                                    'https://github.com/lov-team/'
                                    'architecture-skill-pack'
                                ),
                                'commit': 'b' * 40,
                            }
                        ]
                    }
                ),
                encoding='utf-8',
            )
            with mock.patch(
                'vibe_guide.scanner.shutil.which',
                side_effect=lambda command: '/usr/bin/' + command
                if command == 'codex' else None,
            ):
                result = doctor(scan_project(ProjectPaths.from_cwd(root)))

            self.assertTrue(hasattr(result, 'facts'))
            self.assertTrue(result.ok, result.issues)
            self.assertTrue(result.facts['python']['available'])
            self.assertTrue(result.facts['git']['available'])
            self.assertTrue(result.facts['rules']['present'])
            self.assertTrue(result.facts['skills']['required_configured'])
            self.assertEqual(result.facts['agents']['available'], ['codex'])
            serialized = json.dumps(result.facts, sort_keys=True).lower()
            for unsupported in ('login', 'approval', 'merge', 'deploy', 'authority'):
                self.assertNotIn(unsupported, serialized)


class VibeEntryRuleBlockTests(unittest.TestCase):
    """The New Session Entry block joins AGENTSMD_BLOCKS as a third, marker-
    detected section so the existing tri-state proposal machinery (proposal /
    pending-update / offered-sections) carries it with zero mechanism change."""

    def test_block_registered_last_with_unique_heading(self):
        from vibe_guide.scanner import AGENTSMD_BLOCKS, VIBE_ENTRY_RULES
        self.assertIs(AGENTSMD_BLOCKS[-2], VIBE_ENTRY_RULES)
        self.assertTrue(VIBE_ENTRY_RULES.startswith("## New Session Entry"))
        headings = [block.splitlines()[0].strip() for block in AGENTSMD_BLOCKS]
        self.assertCountEqual(headings, set(headings), "two blocks share a heading")

    def test_missing_detection_is_independent_per_marker(self):
        from vibe_guide.scanner import (
            AGENTSMD_BLOCKS, CAPABILITY_RULES, ENGINEERING_PRINCIPLES,
            PRD_GUIDE_RULES, VIBE_ENTRY_RULES, missing_agentsmd_blocks,
        )
        # An applied document carries the header the capability detection
        # requires, exactly as build_agentsmd_patch writes it.
        header = "# Vibe Guide\n\nProject guidance is maintained through the Vibe Guide.\n\n"
        both_old = header + CAPABILITY_RULES + PRD_GUIDE_RULES
        self.assertEqual(
            missing_agentsmd_blocks(both_old),
            [VIBE_ENTRY_RULES, ENGINEERING_PRINCIPLES],
        )
        all_four = both_old + VIBE_ENTRY_RULES + ENGINEERING_PRINCIPLES
        self.assertEqual(missing_agentsmd_blocks(all_four), [])
        # Regression: an AGENTS.md with nothing still gets every block, in
        # document order, with the principles block last.
        self.assertEqual(
            missing_agentsmd_blocks("# Project\n"),
            [CAPABILITY_RULES, PRD_GUIDE_RULES, VIBE_ENTRY_RULES, ENGINEERING_PRINCIPLES],
        )
        self.assertEqual(len(AGENTSMD_BLOCKS), 4)

    def test_block_points_at_materialized_skill_and_states_gate_discipline(self):
        from vibe_guide.scanner import VIBE_ENTRY_RULES
        self.assertIn(".vibe/proposals/skills/vibe-entry/SKILL.md", VIBE_ENTRY_RULES)
        self.assertIn("vibe scan", VIBE_ENTRY_RULES)
        self.assertIn("vibe plan --request", VIBE_ENTRY_RULES)
        self.assertIn("停下报告", VIBE_ENTRY_RULES)
        from vibe_guide.scanner import VIBE_ENTRY_CURRENT_SENTINEL
        self.assertIn(VIBE_ENTRY_CURRENT_SENTINEL, VIBE_ENTRY_RULES)
        self.assertIn("输出一行", VIBE_ENTRY_RULES)
        self.assertIn("S1：", VIBE_ENTRY_RULES)
        self.assertIn("排查", VIBE_ENTRY_RULES)
        self.assertIn("只读日志/生产数据分析", VIBE_ENTRY_RULES)

    def test_block_states_host_statusline_merge_rule(self):
        # Hosts with their own mandatory gate (e.g. a Johari-style alignment
        # card) must merge S1 into that status line: the D5 failure's root
        # cause was two mandatory entry protocols with no defined ordering.
        from vibe_guide.scanner import VIBE_ENTRY_RULES
        self.assertIn("并入", VIBE_ENTRY_RULES)
        self.assertIn("首个强制状态行", VIBE_ENTRY_RULES)
        self.assertIn("不另起一轮评分或第二个门", VIBE_ENTRY_RULES)

class EngineeringPrinciplesBlockTests(unittest.TestCase):
    """The Engineering Principles block joins AGENTSMD_BLOCKS as the fourth,
    marker-detected section.  It carries implementation principles to every
    adopting project -- crucially including S0/S1 direct-execution sessions
    that never touch the monitor or the worker protocol."""

    def test_block_registered_last_with_unique_heading(self):
        from vibe_guide.scanner import AGENTSMD_BLOCKS, ENGINEERING_PRINCIPLES
        self.assertIs(AGENTSMD_BLOCKS[-1], ENGINEERING_PRINCIPLES)
        self.assertTrue(ENGINEERING_PRINCIPLES.startswith("## Engineering Principles"))
        headings = [block.splitlines()[0].strip() for block in AGENTSMD_BLOCKS]
        self.assertCountEqual(headings, set(headings), "two blocks share a heading")

    def test_block_states_all_principles_and_carries_versioned_sentinel(self):
        from vibe_guide.scanner import (
            ENGINEERING_PRINCIPLES, ENGINEERING_PRINCIPLES_CURRENT_SENTINEL,
        )
        for token in (
            "兼容", "迁移或回退", "最简单实现", "最小版本", "职责",
            "维护良好", "已有依赖", "后续维护", "成熟产品",
        ):
            self.assertIn(token, ENGINEERING_PRINCIPLES)
        # PR #81 lesson: the marker that proves "a block is present" must
        # itself be versioned, or an outdated block reads as current.
        self.assertIn(ENGINEERING_PRINCIPLES_CURRENT_SENTINEL, ENGINEERING_PRINCIPLES)


