"""
The weekly research rerun: when it may run, and what it leaves behind.

Run from the project root:   python -m pytest tests/test_research.py -q
"""
import json
import os
import sys
import time
from datetime import date, datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))
import research  # noqa: E402

FRI = date(2026, 10, 30)


def at(day, hhmm):
    h, m = hhmm.split(':')
    return datetime(2026, 10, day, int(h), int(m), tzinfo=research.ET)


def test_the_week_turns_over_friday_at_the_start_time():
    assert research.week_ending(at(30, '17:59')) == date(2026, 10, 23)
    assert research.week_ending(at(30, '18:00')) == FRI
    assert research.week_ending(at(31, '06:00')) == FRI              # Saturday
    assert research.week_ending(datetime(2026, 11, 1, 12, 0, tzinfo=research.ET)) == FRI   # Sunday
    assert research.week_ending(datetime(2026, 11, 5, 20, 0, tzinfo=research.ET)) == FRI   # Thursday


def test_it_never_starts_in_a_weekday_session():
    assert all(not research.allowed(at(d, t)) for d in (26, 27, 28, 29, 30)
               for t in ('06:00', '09:30', '12:00', '16:00', '17:59'))
    assert research.allowed(at(30, '18:00')) and research.allowed(at(31, '06:00'))


def test_due_until_the_report_exists(tmp_path):
    root = str(tmp_path)
    assert research.due(at(30, '18:00'), root)
    assert not research.due(at(30, '12:00'), root)
    d = research.run_dir(FRI, root)
    os.makedirs(d)
    open(os.path.join(d, research.REPORT), 'w').close()
    assert not research.due(at(30, '18:00'), root)
    assert research.due(datetime(2026, 11, 6, 18, 0, tzinfo=research.ET), root)   # the next week's


def test_a_running_week_isnt_started_twice_but_a_dead_one_is(tmp_path):
    root = str(tmp_path)
    research._hold(research.run_dir(FRI, root))
    assert not research.due(at(30, '18:00'), root)
    old = time.time() - research.LOCK_STALE_SEC - 60
    os.utime(os.path.join(research.run_dir(FRI, root), research.LOCK), (old, old))
    assert research.due(at(30, '18:00'), root)


def test_start_holds_the_week_before_the_process_runs(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(research.subprocess, 'Popen', lambda args, **k: calls.append(args))
    root = str(tmp_path)
    assert research.start(at(30, '18:00'), root) == FRI
    assert len(calls) == 1 and calls[0][0] == research.VENV
    assert not research.due(at(30, '18:10'), root)


def test_a_failing_tool_is_reported_and_the_rest_still_run(tmp_path, monkeypatch):
    root = str(tmp_path)

    def fake(name, python, out_dir, timeout=None):
        with open(os.path.join(out_dir, f'{name}.txt'), 'w', encoding='utf-8') as f:
            f.write('Traceback' if name == 'b' else 'tests passed: none\n')
        return {'tool': name, 'code': 1 if name == 'b' else 0, 'sec': 90}

    monkeypatch.setattr(research, 'run_tool', fake)
    monkeypatch.setattr(research, 'HEADLINES', {'a': ('^tests',), 'c': ('^tests',)})
    research.run_week(FRI, root, [('a', 'py'), ('b', 'py'), ('c', 'py')])
    d = research.run_dir(FRI, root)
    assert [t['tool'] for t in json.load(open(os.path.join(d, research.STATUS)))['tools']] == ['a', 'b', 'c']
    text = research.report_text(FRI, root)
    assert '1 of 3 FAILED' in text and '- b: 1 (see b.txt)' in text and '- tests passed: none' in text
    assert 'First report' in text
    assert not os.path.exists(os.path.join(d, research.LOCK))


def test_a_tool_that_will_not_start_keeps_its_output(tmp_path):
    r = research.run_tool('no_such_tool', sys.executable, str(tmp_path))
    assert r['code'] != 0
    assert 'no_such_tool' in open(tmp_path / 'no_such_tool.txt', encoding='utf-8').read()


def week_of(root, week, passed, tests=5, name='a'):
    d = research.run_dir(week, root)
    os.makedirs(d)
    with open(os.path.join(d, f'{name}.txt'), 'w', encoding='utf-8') as f:
        f.write(f'noise\nRESULT: {tests} tests; about 0.3 by chance; passed: {passed}\n' if passed else 'noise\n')
    with open(os.path.join(d, research.STATUS), 'w', encoding='utf-8') as f:
        json.dump({'week': week.isoformat(), 'started': f'{week}T18:00:00-04:00',
                   'tools': [{'tool': name, 'code': 0, 'sec': 60}]}, f)


def test_result_reads_the_line_wherever_it_is():
    assert research.result('table\nRESULT: 48 tests; about 0.5 by chance; passed: none\nmore') == \
        {'tests': 48, 'chance': 0.5, 'passed': []}
    assert research.result('RESULT: 9 tests; about 0.1 by chance; passed: x on y | z')['passed'] == ['x on y', 'z']
    assert research.result('no such line') is None


def test_the_report_shows_what_started_and_stopped_passing(tmp_path):
    root = str(tmp_path)
    week_of(root, date(2026, 10, 23), 'x | y')
    week_of(root, FRI, 'y | z')
    research.write_report(FRI, root)
    text = research.report_text(FRI, root)
    assert '## Changed since 2026-10-23' in text
    assert '- new pass: a: z' in text and '- stopped passing: a: x' in text
    assert 'pass: a: y' not in text
    assert '- a: 5 tests, about 0.3 by chance, 2 passed: y | z' in text


def test_numbers_drifting_is_not_a_change(tmp_path):
    root = str(tmp_path)
    week_of(root, date(2026, 10, 23), 'none', tests=5)
    week_of(root, FRI, 'none', tests=6)
    research.write_report(FRI, root)
    text = research.report_text(FRI, root)
    assert 'Nothing new passed, and nothing stopped passing.' in text
    assert '- a: 6 tests, about 0.3 by chance, none passed' in text


def test_a_test_tool_that_loses_its_result_line_is_flagged_not_read_as_no_pass(tmp_path):
    root = str(tmp_path)
    week_of(root, FRI, None, name='level_move_test')
    research.write_report(FRI, root)
    assert 'level_move_test: NO RESULT LINE' in research.report_text(FRI, root)


def test_every_tool_exists_and_every_test_prints_its_result():
    tools = os.path.join(research.ROOT, 'tools')
    for name, _ in research.TOOLS:
        assert os.path.exists(os.path.join(tools, f'{name}.py')), name
    for name in research.TESTS:
        assert name in [t for t, _ in research.TOOLS]
        with open(os.path.join(tools, f'{name}.py'), encoding='utf-8') as f:
            assert 'RESULT: ' in f.read(), name


def test_report_text_defaults_to_the_latest_and_skips_the_work_folder(tmp_path):
    root = str(tmp_path)
    assert research.report_text(None, root) is None
    for week in (date(2026, 10, 23), FRI):
        os.makedirs(research.run_dir(week, root))
        with open(os.path.join(research.run_dir(week, root), research.REPORT), 'w') as f:
            f.write(week.isoformat())
    os.makedirs(os.path.join(root, 'work'))
    assert research.report_text(None, root) == FRI.isoformat()
    assert research.report_text(date(2026, 10, 23), root) == '2026-10-23'
    assert research.report_text(date(2026, 1, 2), root) is None
