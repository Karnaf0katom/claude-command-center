"""CLI captures remain visible when Codex creates no native rollout."""
import json
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import server


class EphemeralSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        (self.repo / ".git").mkdir()
        self.log = self.repo / 'spawn-codex-check-status-20261001T120000.log'
        self.log.write_text('\n'.join(json.dumps(row) for row in [
            {'type': 'thread.started', 'thread_id': 'capture-thread'},
            {'type': 'item.completed', 'item': {'id': 'answer', 'type': 'agent_message', 'text': 'Finished checking.'}},
            {'type': 'turn.completed', 'usage': {'input_tokens': 20, 'output_tokens': 5}},
        ]) + '\n')
        for patch in [
            mock.patch.object(server, '_codex_fetch_threads', return_value=[]),
            mock.patch.object(server, '_codex_spawn_pid_by_thread_id', return_value={'capture-thread': {
                'pid': 42, 'log': str(self.log), 'cwd': str(self.repo), 'repo_path': str(self.repo),
                'prompt': 'Check status', 'model': 'example-model', 'alive': False}}),
            mock.patch.object(server, '_recent_codex_ccc_log_paths', return_value=[self.log]),
            mock.patch.object(server, '_codex_thread_row', return_value=None),
            mock.patch.object(server, '_resolve_codex_rollout_path', return_value=None),
            mock.patch.object(server, '_codex_logs_for_session', return_value=[(1, str(self.log))]),
            mock.patch.object(server, '_load_repo_pins', return_value={}),
            mock.patch.object(server, '_load_session_name_overrides', return_value={}),
            mock.patch.object(server, '_load_conversation_lifecycle_sets', return_value=(set(), set())),
            mock.patch.object(server, '_load_verified_conversations', return_value=[]),
            mock.patch.object(server, '_codex_spawn_parent_by_child', return_value={}),
            mock.patch.object(server, '_load_codex_parent_links', return_value={}),
            mock.patch.object(server, '_codex_goals_snapshot', return_value={}),
        ]:
            patch.start()
            self.addCleanup(patch.stop)

    def test_completed_capture_has_real_card_without_native_thread(self):
        rows = server.find_codex_conversations(repo_path=str(self.repo), resolve_pr_states=False, resolve_worktree_dirty=False)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['session_id'], 'capture-thread')
        self.assertEqual(rows[0]['spawn_pid'], 42)
        self.assertEqual(rows[0]['last_assistant_text'], 'Finished checking.')
        self.assertFalse(rows[0]['is_live'])
        self.assertEqual(rows[0]['last_event_type'], 'result')

    def test_capture_without_native_path_recovers_answer(self):
        result = server._codex_recover_log_conversation('capture-thread', None)
        self.assertIsNotNone(result)
        self.assertEqual(result['events'][-1]['type'], 'result')

    def test_other_repo_capture_is_excluded(self):
        other = self.repo / 'other'
        other.mkdir()
        (other / ".git").mkdir()
        rows = server.find_codex_conversations(repo_path=str(other), resolve_pr_states=False, resolve_worktree_dirty=False)
        self.assertEqual(rows, [])

    def test_capture_stream_works_without_native_rollout(self):
        handler = object.__new__(server.CommandCenterHandler)
        handler.wfile = io.BytesIO()
        handler.send_response = lambda *args: None
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        with mock.patch.object(server.time, "sleep", side_effect=BrokenPipeError):
            handler._stream_codex_capture('capture-thread', None, 0)
        self.assertIn(b'Finished checking.', handler.wfile.getvalue())

    def test_capture_list_reads_only_bounded_log_content(self):
        from ccc_server import codex_log_recovery
        with self.log.open('a') as sink:
            sink.write('x' * 1000000)
        with mock.patch.object(server, '_codex_recover_log_conversation', side_effect=AssertionError('full transcript parsed on list path')):
            rows = server.find_codex_conversations(repo_path=str(self.repo), resolve_pr_states=False, resolve_worktree_dirty=False)
        self.assertEqual(len(rows), 1)

    def test_native_thread_suppresses_duplicate_capture_card(self):
        rows = server._codex_capture_rows([{'id': 'capture-thread'}], {}, str(self.repo))
        self.assertEqual(rows, [])

    def test_removed_capture_does_not_abort_list_metadata(self):
        self.log.unlink()
        self.assertEqual(server._codex_capture_tail('capture-thread', self.log), {})

    def test_capture_append_invalidates_archive_codex_rows(self):
        first = server._codex_capture_corpus_signature()
        with self.log.open('a') as sink:
            sink.write('new output\n')
        second = server._codex_capture_corpus_signature()
        self.assertNotEqual(first, second)
        old, new = 'capture-old', 'capture-new'
        key = 'ccc-codex-captures'
        with mock.patch.dict(server._ARCHIVE_STATMAP_BY_SIG, {
            old: ({}, {key: first}), new: ({}, {key: second}),
        }):
            self.assertEqual(server._archive_signature_delta(old, new), ([], [], {'codex'}))

    def test_discovery_replaces_cached_unknown_engine(self):
        with mock.patch.dict(server._ENGINE_DETECT_CACHE, {'capture-thread': ('claude', float('inf'))}):
            server.find_codex_conversations(repo_path=str(self.repo), resolve_pr_states=False, resolve_worktree_dirty=False)
            self.assertEqual(server._detect_session_engine('capture-thread'), 'codex')

    def test_direct_capture_lookup_works_before_list_scan(self):
        from ccc_server import codex_log_recovery
        sid = '00000000-0000-7000-8000-000000000001'
        self.log.write_text(self.log.read_text().replace('capture-thread', sid))
        codex_log_recovery._CAPTURE_ROWS.pop(sid, None)
        with mock.patch.object(server, '_known_repo_paths', return_value=[str(self.repo)]), \
             mock.patch.object(server, 'repo_log_dir', return_value=self.repo):
            row = server._codex_capture_thread_row(sid)
        self.assertIsNotNone(row)
        self.assertEqual(row['_ccc_capture'], str(self.log))
