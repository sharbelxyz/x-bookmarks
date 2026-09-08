import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import generate_architecture as architecture
import radar_server
import group_filter_loop


class ArchitectureRefreshTests(unittest.TestCase):
    def test_numeric_contract_changes_are_not_hidden(self):
        self.assertNotEqual(architecture.comparable('budget 28 minutes'), architecture.comparable('budget 10 minutes'))

    def test_only_explicit_runtime_observations_are_ignored(self):
        def doc(count):
            return 'budget 28\n' + architecture.SNAPSHOT_START + '\nrows ' + str(count) + '\n' + architecture.SNAPSHOT_END
        self.assertEqual(architecture.comparable(doc(1)), architecture.comparable(doc(100)))
        self.assertNotEqual(architecture.comparable(doc(1)), architecture.comparable(doc(1).replace('budget 28', 'budget 10')))

    def test_ast_constants_include_arithmetic_and_nested_sets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'constants.py'
            path.write_text('BASE = 28 * 60\nCAP = BASE + 90\nLANES = {"read": {"must_read", "excluded"}}\nUNSAFE = open("never-read")\n')
            constants = architecture.literal_constants(path)
            self.assertEqual(constants['CAP'], 1770)
            self.assertNotIn('UNSAFE', constants)
            self.assertIn('must_read', architecture.cell(constants['LANES']))

    def test_every_implemented_post_route_is_documented(self):
        post = {r['path'] for r in architecture.server_routes() if r['method'] == 'POST'}
        self.assertEqual(post, {'/api/run', '/api/verdict', '/api/outcome', '/api/negative-term'})

    def test_replaced_document_is_archived_and_unchanged_content_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'ARCHITECTURE.md'
            path.write_text('old architecture\n')
            self.assertTrue(architecture.write_document('> Generated at: now\nnew architecture\n', path))
            archives = list((path.parent / '_versions').glob('*.md'))
            self.assertEqual(len(archives), 1)
            self.assertEqual(archives[0].read_text(), 'old architecture\n')
            self.assertFalse(architecture.write_document('> Generated at: later\nnew architecture\n', path))
            self.assertEqual(len(list((path.parent / '_versions').glob('*.md'))), 1)

    def test_failed_publish_keeps_existing_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'ARCHITECTURE.md'
            path.write_text('original\n')
            with mock.patch.object(architecture.os, 'replace', side_effect=OSError('test publish failure')):
                with self.assertRaises(OSError):
                    architecture.write_document('new\n', path)
            self.assertEqual(path.read_text(), 'original\n')

    def test_source_fingerprint_changes_with_numeric_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'settings.py'
            path.write_text('MAX_BATCHES=4\n')
            with mock.patch.object(architecture, 'source_files', return_value={'settings.py': path}):
                before = architecture.source_revision()
                path.write_text('MAX_BATCHES=3\n')
                self.assertNotEqual(before, architecture.source_revision())

    def test_runtime_private_files_are_outside_source_inventory(self):
        paths = architecture.source_files()
        self.assertFalse(any(name.startswith('data/') or name == '.env' for name in paths))

    def test_viewer_refresh_uses_bounded_fresh_subprocess(self):
        stop = mock.Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = True
        with mock.patch.object(radar_server.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            radar_server.architecture_refresh_loop(stop)
        self.assertEqual(run.call_args.kwargs['timeout'], 30)
        self.assertIn('--refresh', run.call_args.args[0])
        stop.wait.assert_called_once_with(60)

    def test_viewer_refresh_failure_does_not_kill_heartbeat(self):
        stop = mock.Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = True
        with mock.patch.object(radar_server.subprocess, 'run', side_effect=subprocess.TimeoutExpired('generate', 30)):
            radar_server.architecture_refresh_loop(stop)
        stop.wait.assert_called_once()

    def test_scanner_doc_failure_is_best_effort(self):
        with mock.patch.object(group_filter_loop.subprocess, 'run', side_effect=OSError('test error')):
            self.assertFalse(group_filter_loop.refresh_architecture()['refreshed'])

    def test_scanner_refresh_is_in_finally_after_journal(self):
        import ast
        module = architecture.tree(architecture.SCRIPTS / 'group_filter_loop.py')
        workflow = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == 'run_workflow')
        final = next(n for n in workflow.body if isinstance(n, ast.Try)).finalbody
        calls = [n.func.id for statement in final for n in ast.walk(statement) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
        self.assertIn('refresh_architecture', calls)
        self.assertLess(calls.index('append_journal'), calls.index('refresh_architecture'))


if __name__ == '__main__':
    unittest.main()
