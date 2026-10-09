"""No model calls: a tiny local text adapter exercises the subprocess contract."""
import ast
import io
import json
import subprocess
import sys
import urllib.parse
from pathlib import Path

import pytest

from ccc_server import text_tools


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setenv('CCC_TEXT_TOOLS_COMMAND', json.dumps([
        sys.executable, '-c', 'import sys; print(sys.stdin.read().split("TEXT TO CORRECT:\\n", 1)[1].replace("teh", "the"))'
    ]))


def test_command_contract_and_selection_boundary_whitespace():
    result, status = text_tools.handle_text_tools({'action': 'spell', 'text': '  teh cat\n'})
    assert status == 200
    assert result['result'] == '  the cat\n'
    assert text_tools.text_tools_status()['available'] is True


def test_default_claude_is_text_only(monkeypatch):
    monkeypatch.delenv('CCC_TEXT_TOOLS_COMMAND')
    monkeypatch.setattr(text_tools.shutil, 'which', lambda _: 'claude')
    argv, label = text_tools._command()
    assert label == 'Claude Code'
    assert argv[argv.index('--tools') + 1] == ''
    assert '--no-session-persistence' in argv
    assert argv[argv.index('--mcp-config') + 1] == '{"mcpServers":{}}'
    assert '--resume' not in argv


@pytest.mark.parametrize('payload', [None, [], {'action': 'prompt', 'text': 'hi'},
                                       {'action': 'spell', 'text': 1}, {'action': 'spell', 'text': '  '}])
def test_invalid_requests_never_run(monkeypatch, payload):
    monkeypatch.setattr(text_tools, '_run', lambda *_: pytest.fail('invalid request ran'))
    assert text_tools.handle_text_tools(payload)[1] == 400


def test_size_and_unicode_boundaries(monkeypatch):
    monkeypatch.setattr(text_tools, '_run', lambda _, text: text)
    assert text_tools.handle_text_tools({'action': 'spell', 'text': 'ש' * 20000})[1] == 200
    assert text_tools.handle_text_tools({'action': 'spell', 'text': 'ש' * 20001})[1] == 413


@pytest.mark.parametrize('command', ['not json', '{}', '[]', '[1]', '["missing-correction-tool"]'])
def test_configuration_failure_is_available_to_settings(monkeypatch, command):
    monkeypatch.setenv('CCC_TEXT_TOOLS_COMMAND', command)
    assert not text_tools.text_tools_status()['available']
    assert text_tools.handle_text_tools({'action': 'spell', 'text': 'hi'})[1] == 503


def test_failure_does_not_echo_command_stderr(monkeypatch):
    monkeypatch.setenv('CCC_TEXT_TOOLS_COMMAND', json.dumps([
        sys.executable, '-c', 'import sys; sys.stderr.write("private prompt details"); sys.exit(1)']))
    result, status = text_tools.handle_text_tools({'action': 'spell', 'text': 'hi'})
    assert status == 502
    assert 'private prompt details' not in str(result)


def test_timeout_releases_command_slot(monkeypatch):
    monkeypatch.setenv('CCC_TEXT_TOOLS_COMMAND', json.dumps([
        sys.executable, '-c', 'import time; time.sleep(10)']))
    monkeypatch.setattr(text_tools, '_TIMEOUT', .05)
    assert text_tools.handle_text_tools({'action': 'spell', 'text': 'hi'})[1] == 504
    monkeypatch.setattr(text_tools, '_run', lambda *_: 'fixed')
    assert text_tools.handle_text_tools({'action': 'spell', 'text': 'hi'})[1] == 200


def test_busy_and_invalid_output(monkeypatch):
    text_tools._SLOT.acquire()
    try:
        assert text_tools.handle_text_tools({'action': 'spell', 'text': 'hi'})[1] == 429
    finally:
        text_tools._SLOT.release()
    monkeypatch.setattr(text_tools, '_run', lambda *_: '')
    assert text_tools.handle_text_tools({'action': 'spell', 'text': 'hi'})[1] == 502


def _handler():
    # Execute the actual HTTP methods without booting worker/hook services.
    server_tree = ast.parse((Path(__file__).parents[1] / 'server.py').read_text())
    cls = next(n for n in server_tree.body if isinstance(n, ast.ClassDef) and n.name == 'CommandCenterHandler')
    methods = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in ('_do_GET', 'do_POST')]
    fixture = ast.ClassDef(name='Handler', bases=[], keywords=[], body=methods, decorator_list=[])
    tree = ast.fix_missing_locations(ast.Module(body=[fixture], type_ignores=[]))
    namespace = {'json': json, 'urllib': urllib, 'MORNING_ENABLED': False}
    exec(compile(tree, 'server.py', 'exec'), namespace)
    handler = namespace['Handler']()
    handler.path = '/api/text-tools'
    handler._check_same_origin = lambda: True
    handler._phone_pin_blocked = lambda _: False
    handler._is_morning_path = lambda _: False
    handler.send_json = lambda data, status=200: setattr(handler, 'response', (data, status))
    return handler


def test_http_get_and_post_keep_unicode_and_origin_gate(monkeypatch):
    handler = _handler()
    handler._do_GET()
    assert handler.response[0]['available'] is True
    monkeypatch.setattr(text_tools, '_run', lambda _, text: text)
    body = json.dumps({'action': 'spell', 'text': 'ש' * 20000}, ensure_ascii=False).encode()
    handler.headers = {'Content-Length': str(len(body))}
    handler.rfile = io.BytesIO(body)
    handler.do_POST()
    assert handler.response[1] == 200
    assert handler.response[0]['result'] == 'ש' * 20000
    handler._check_same_origin = lambda: False
    handler.response = None
    handler.rfile = io.BytesIO(body)
    handler.do_POST()
    assert handler.rfile.tell() == 0
    assert handler.response is None


@pytest.mark.parametrize('length,body,status', [('bad', b'{}', 400), ('-1', b'', 413),
                                              ('300000', b'', 413), ('1', b'{', 400)])
def test_http_rejects_malformed_or_oversized_body(length, body, status):
    handler = _handler()
    handler.headers = {'Content-Length': length}
    handler.rfile = io.BytesIO(body)
    handler.do_POST()
    assert handler.response[1] == status
