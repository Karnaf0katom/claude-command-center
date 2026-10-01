"""Codex session diagnostics payload for the Metadata tab panel."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import server

SID = "01a0f90c-248e-7081-9082-4a3fb15ed970"


def _common_patches(tmpdir, log):
    """Patches every diagnostics collector reaches; tests override per-case."""
    return [
        mock.patch.object(server, '_find_live_spawn_entry_for_session', return_value=None),
        mock.patch.object(server, '_disk_spawn_entry_for_session', return_value=None),
        mock.patch.object(server, '_load_spawn_registry', return_value=[]),
        mock.patch.object(server, '_codex_thread_registry_entry', return_value=None),
        mock.patch.object(server, '_poll_spawn_entry', return_value=0),
        mock.patch.object(server, '_is_pid_alive', return_value=False),
        mock.patch.object(server, '_codex_app_server_transport_kind', return_value=None),
        mock.patch.object(server, '_resolve_codex_rollout_path', return_value=None),
        mock.patch.object(server, '_codex_thread_row', return_value=None),
        mock.patch.object(server, '_codex_capture_thread_row', return_value=None),
        mock.patch.object(server, '_extract_codex_thread_id_from_log', return_value=None),
        mock.patch.object(server, '_codex_desktop_app_is_running', return_value=False),
        mock.patch.object(server, '_codex_desktop_app_server_procs', return_value=[]),
        mock.patch.object(server, '_codex_shared_state_conflict', return_value=None),
        mock.patch.object(server, '_codex_thread_writer_snapshot', return_value={
            'writer': None, 'desktop_attached': False, 'external_active': False}),
        mock.patch.object(server, '_codex_app_server_is_live', return_value=False),
        mock.patch.object(server, '_codex_app_server_thread_state', return_value=None),
        mock.patch.object(server, '_codex_app_server_activity_fields', return_value={
            'needs_approval': False, 'needs_approval_message': ''}),
        mock.patch.object(server, '_codex_load_coordination_state', return_value=None),
        mock.patch.object(server, 'CODEX_TELEMETRY_FILE', Path(tmpdir) / 'telemetry.jsonl'),
    ]


class CodexDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.log = self.repo / 'spawn-codex-check-20261001T120000.log'
        self.log.write_text('\n'.join(json.dumps(row) for row in [
            {'type': 'thread.started', 'thread_id': SID},
            {'type': 'item.started', 'item': {'id': 'cmd1', 'type': 'command_execution'}},
        ]) + '\n')
        self.patches = _common_patches(str(self.repo), self.log)
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _patch(self, name, **kwargs):
        patch = mock.patch.object(server, name, **kwargs)
        patch.start()
        self.addCleanup(patch.stop)
        return patch

    def _write_telemetry(self, rows):
        path = Path(self.tmp.name) / 'telemetry.jsonl'
        path.write_text(''.join(json.dumps(r) + '\n' for r in rows))
        return path

    def _exec_entry(self):
        return {
            'pid': 4242, 'engine': 'codex', 'log': str(self.log),
            'model': 'example-model', 'cwd': str(self.repo),
            'spawned_via': 'ui', 'started': '20261001T120000',
        }

    def test_ephemeral_exec_fallback_alive(self):
        self._patch('_find_live_spawn_entry_for_session', return_value=self._exec_entry())
        self._patch('_poll_spawn_entry', return_value=None)
        self._patch('_codex_capture_thread_row', return_value={'_ccc_capture': str(self.log)})
        self._patch('_codex_shared_state_conflict', return_value={
            'holders': [{'pid': 1, 'command': 'codex'}],
            'message': 'Another Codex process is already using the shared state database',
        })
        self._patch('_codex_desktop_app_is_running', return_value=True)
        self._patch('_codex_desktop_app_server_procs', return_value=[{'pid': 1, 'command': 'x'}])
        self._patch('_codex_thread_writer_snapshot', return_value={
            'writer': 'ccc', 'desktop_attached': False, 'external_active': False})
        self._write_telemetry([
            {'event': 'codex_spawn', 'fallback': 'codex-exec',
             'fallback_reason': 'thread/start failed',
             'error': 'Another Codex process is already using the shared state database (pids 1)',
             'cwd': str(self.repo), 'ts': 100.0},
            {'event': 'codex_spawn', 'ok': True, 'via': 'codex-spawn', 'pid': 4242, 'ts': 100.2},
            {'event': 'codex_spawn', 'thread_id': 'other', 'ts': 100.4},
        ])
        diag = server.build_codex_session_diagnostics(SID)
        self.assertTrue(diag['ok'])
        self.assertEqual(diag['verdict']['state'], 'running_exec')
        self.assertEqual(diag['transport']['kind'], 'exec-fallback')
        self.assertEqual(diag['transport']['fallback_reason'], 'thread/start failed')
        self.assertIn('shared state database', diag['transport']['fallback_error'])
        self.assertTrue(diag['storage']['ephemeral'])
        self.assertTrue(diag['competition']['desktop_running'])
        self.assertEqual(len(diag['telemetry']), 2)
        self.assertTrue(diag['process']['alive'])

    def test_completed_exec(self):
        self.log.write_text('\n'.join(json.dumps(row) for row in [
            {'type': 'thread.started', 'thread_id': SID},
            {'type': 'turn.completed', 'usage': {'input_tokens': 1}},
        ]) + '\n')
        self._patch('_find_live_spawn_entry_for_session', return_value=self._exec_entry())
        self._patch('_poll_spawn_entry', return_value=0)
        self._patch('_codex_capture_thread_row', return_value={'_ccc_capture': str(self.log)})
        diag = server.build_codex_session_diagnostics(SID)
        self.assertEqual(diag['verdict']['state'], 'completed')
        self.assertEqual(diag['process']['turn_outcome'], 'completed')

    def test_native_thread_desktop_writing(self):
        rollout = self.repo / 'rollout-2026-10-01.jsonl'
        rollout.write_text('{}\n')
        self._patch('_resolve_codex_rollout_path', return_value=rollout)
        self._patch('_codex_thread_row', return_value={'id': SID})
        self._patch('_codex_thread_writer_snapshot', return_value={
            'writer': 'desktop', 'desktop_attached': True,
            'external_active': True, 'mtime_age_s': 3.0})
        diag = server.build_codex_session_diagnostics(SID)
        self.assertEqual(diag['verdict']['state'], 'external_turn')
        self.assertIn('Codex desktop', diag['verdict']['headline'])
        self.assertEqual(diag['transport']['kind'], 'external')
        self.assertFalse(diag['storage']['ephemeral'])

    def test_needs_approval_wins(self):
        self._patch('_find_live_spawn_entry_for_session', return_value=self._exec_entry())
        self._patch('_poll_spawn_entry', return_value=None)
        self._patch('_codex_app_server_activity_fields', return_value={
            'needs_approval': True, 'needs_approval_message': 'Allow rm?'})
        diag = server.build_codex_session_diagnostics(SID)
        self.assertEqual(diag['verdict']['state'], 'needs_approval')
        self.assertEqual(diag['verdict']['detail'], 'Allow rm?')

    def test_empty_sid(self):
        diag = server.build_codex_session_diagnostics('')
        self.assertFalse(diag['ok'])
        self.assertEqual(diag['error'], 'missing session_id')


if __name__ == '__main__':
    unittest.main()
