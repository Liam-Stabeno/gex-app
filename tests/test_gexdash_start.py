"""The control panel's Start server button (desktop/gexdash.py)."""
import os
import subprocess
import sys
import types

import pytest

sys.modules.setdefault('webview', types.ModuleType('webview'))   # only in desktop\.venv
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'desktop'))
import gexdash


@pytest.fixture
def app(monkeypatch):
    runs = []

    def run(cmd, **kw):
        runs.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, 0, '', '')

    monkeypatch.setattr(gexdash.subprocess, 'run', run)
    monkeypatch.setattr(gexdash, 'server_up', lambda: False)
    a = gexdash.App(test=True)
    a.runs = runs
    return a


def test_runs_the_scheduled_task_hidden(app):
    app.start_server()
    cmd, kw = app.runs[0]
    assert cmd == ['schtasks', '/Run', '/TN', 'GEXDashboard']
    assert kw['creationflags'] == subprocess.CREATE_NO_WINDOW
    assert app.server_status() == {'server': False, 'starting': True, 'start_error': ''}


def test_a_second_click_while_starting_runs_nothing(app):
    app.start_server()
    app.start_server()
    assert len(app.runs) == 1


def test_a_task_that_fails_shows_why_and_can_be_tried_again(app, monkeypatch):
    fail = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, '', 'ERROR: task not found')
    monkeypatch.setattr(gexdash.subprocess, 'run', fail)
    app.start_server()
    assert app.server_status() == {'server': False, 'starting': False,
                                   'start_error': 'could not start: ERROR: task not found'}
    app.start_server()
    assert app.start_at == 0


def test_schtasks_missing_shows_why(app, monkeypatch):
    def missing(cmd, **kw):
        raise FileNotFoundError('schtasks')
    monkeypatch.setattr(gexdash.subprocess, 'run', missing)
    app.start_server()
    assert 'could not start' in app.server_status()['start_error']


def test_no_answer_in_time_stops_waiting(app, monkeypatch):
    app.start_server()
    now = gexdash.time.time()
    monkeypatch.setattr(gexdash.time, 'time', lambda: now + gexdash.START_WAIT_SEC + 1)
    s = app.server_status()
    assert (s['starting'], s['server']) == (False, False)
    assert 'no answer' in s['start_error']
    app.start_server()                     # the button is back: it can be tried again
    assert len(app.runs) == 2


def test_server_answering_ends_the_wait(app, monkeypatch):
    app.start_server()
    monkeypatch.setattr(gexdash, 'server_up', lambda: True)
    assert app.server_status() == {'server': True, 'starting': False, 'start_error': ''}
    assert app.start_at == 0
