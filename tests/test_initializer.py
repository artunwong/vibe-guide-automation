import json, tempfile, unittest
import hashlib
from pathlib import Path
from vibe_guide.paths import ProjectPaths
from vibe_guide.initializer import init_project

class InitializerTests(unittest.TestCase):
    def test_no_confirm_does_not_write_and_confirm_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            p=ProjectPaths.from_cwd(Path(d)); self.assertFalse(init_project(p, False).changed)
            first=init_project(p, True); second=init_project(p, True)
            self.assertTrue(first.changed); self.assertFalse(second.changed)
            self.assertTrue((p.root/'.vibe/knowledge').is_dir())
            self.assertFalse((p.root/'AGENTS.md').exists())

    def test_skill_proposal_registers_remote_source_and_repeat_preserves_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            p = ProjectPaths.from_cwd(Path(d))
            first = init_project(p, True)
            proposal = p.root / '.vibe/proposals/skills/proposal.md'
            self.assertIn('source: https://github.com/lov-team/architecture-skill-pack', proposal.read_text())
            self.assertIn('source_status: remote', proposal.read_text())
            before = proposal.read_bytes()
            init_project(p, True)
            self.assertEqual(proposal.read_bytes(), before)

    def test_symlinked_skill_proposal_is_rejected_before_initialization(self):
        with tempfile.TemporaryDirectory() as d:
            project = Path(d)
            proposal_dir = project / '.vibe/proposals/skills'
            proposal_dir.mkdir(parents=True)
            outside = project.parent / 'outside-proposal'
            outside.write_text('keep\n', encoding='utf-8')
            (proposal_dir / 'proposal.md').symlink_to(outside)
            with self.assertRaises(ValueError):
                init_project(ProjectPaths.from_cwd(project), True)
            self.assertEqual(outside.read_text(), 'keep\n')

    def test_symlinked_vibe_is_rejected_without_outside_write(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            project = base / 'project'
            outside = base / 'outside'
            project.mkdir()
            outside.mkdir()
            (project / '.vibe').symlink_to(outside, target_is_directory=True)

            with self.assertRaises(ValueError):
                init_project(ProjectPaths.from_cwd(project), True)

            self.assertEqual(list(outside.iterdir()), [])

    def test_nested_symlink_is_rejected_before_any_initialization_write(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            project = base / 'project'
            outside = base / 'outside'
            (project / '.vibe').mkdir(parents=True)
            outside.mkdir()
            (project / '.vibe' / 'knowledge').symlink_to(
                outside, target_is_directory=True
            )

            with self.assertRaises(ValueError):
                init_project(ProjectPaths.from_cwd(project), True)

            self.assertEqual(list(outside.iterdir()), [])
            self.assertFalse((project / '.vibe' / 'config.json').exists())
            self.assertFalse((project / '.vibe' / 'state.json').exists())
            self.assertFalse((project / '.vibe' / 'proposals').exists())

    def test_non_directory_vibe_is_rejected_without_replacement(self):
        with tempfile.TemporaryDirectory() as d:
            project = Path(d)
            marker = project / '.vibe'
            marker.write_text('keep\n', encoding='utf-8')

            try:
                init_project(ProjectPaths.from_cwd(project), True)
            except Exception as exc:
                self.assertIsInstance(exc, ValueError)
            else:
                self.fail('non-directory .vibe must be rejected')

            self.assertEqual(marker.read_text(encoding='utf-8'), 'keep\n')

    def test_legacy_state_migration_evidence_has_verified_recoverable_backup(self):
        with tempfile.TemporaryDirectory() as d:
            project = Path(d)
            (project / '.vibe').mkdir()
            legacy = {"workflow_version": 3, "session_gate": "s0_required", "keep": "history"}
            (project / '.vibe' / 'state.json').write_text(json.dumps(legacy) + '\n', encoding='utf-8')

            init_project(ProjectPaths.from_cwd(project), True)

            evidence = json.loads((project / '.vibe' / 'migration-evidence.json').read_text())
            self.assertNotIn('backup_verified', evidence)
            self.assertEqual(evidence['target_version'], '4.2.0')
            backup_path = Path(evidence['backup_path'])
            manifest = json.loads((backup_path / 'manifest.json').read_text())
            state_entry = next(item for item in manifest['files'] if item['path'] == '.vibe/state.json')
            self.assertEqual(
                state_entry['sha256'],
                hashlib.sha256((backup_path / 'payload' / '.vibe' / 'state.json').read_bytes()).hexdigest(),
            )
            self.assertEqual(evidence['backup_manifest'], {'manifest': str(backup_path / 'manifest.json'), 'files': manifest['files']})

    def test_legacy_state_with_unverifiable_existing_evidence_is_blocked(self):
        with tempfile.TemporaryDirectory() as d:
            project = Path(d)
            (project / '.vibe').mkdir()
            legacy = {"workflow_version": 3, "session_gate": "s0_required"}
            (project / '.vibe' / 'state.json').write_text(json.dumps(legacy) + '\n', encoding='utf-8')
            (project / '.vibe' / 'migration-evidence.json').write_text(
                json.dumps({'target_version': '4.2.0', 'backup_verified': True}), encoding='utf-8'
            )

            with self.assertRaisesRegex(ValueError, 'migration evidence is unverifiable'):
                init_project(ProjectPaths.from_cwd(project), True)
            self.assertEqual(json.loads((project / '.vibe' / 'state.json').read_text()), legacy)

    def test_outdated_engineering_principles_block_surfaces_drift_note(self):
        from vibe_guide.scanner import (
            ENGINEERING_PRINCIPLES_CURRENT_SENTINEL,
            ENGINEERING_PRINCIPLES_MARKER,
        )
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            # An AGENTS.md carrying an outdated (or locally rewritten)
            # principles block: marker present, current sentinel absent.
            (root / "AGENTS.md").write_text(
                "# Project\n\n## " + ENGINEERING_PRINCIPLES_MARKER + "\n\n- 旧版措辞。\n",
                encoding="utf-8",
            )
            result = init_project(ProjectPaths.from_cwd(root), True)
            self.assertTrue(
                any(
                    ENGINEERING_PRINCIPLES_MARKER in note and "不一致" in note
                    for note in result.notes
                ),
                result.notes,
            )
