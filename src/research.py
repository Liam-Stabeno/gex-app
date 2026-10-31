"""
The weekly rerun of the research tools: as the days pile up, does anything start to hold?

Once a week, from Friday 18:00 ET (clear of the close's history passes), every tool in TOOLS
runs in turn with the Python it needs, at low priority, in a process of its own so a server
restart doesn't stop it. data/research/<Friday>/ gets each tool's whole output and report.md:
what ran, each tool's headline lines, and which of them changed since the last report.

A week that was missed (server off, PC asleep) runs at the next allowed time: a weekend, or
a weekday from 18:00, so it can never reach a session. background.daily_jobs_loop checks.

The tools only read the data, except rebuild_charm_map, which only adds days with no charm
file. Their row files and caches go to data/research/work (their %TEMP%), overwritten weekly.

Run by hand:  .venv\\Scripts\\python.exe src\\research.py            this week's, now
              .venv\\Scripts\\python.exe src\\research.py --report   rebuild its report only
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESEARCH_DIR = os.path.join(ROOT, 'data', 'research')
WORK_DIR = os.path.join(RESEARCH_DIR, 'work')
VENV = os.path.join(ROOT, '.venv', 'Scripts', 'python.exe')
SYSTEM = getattr(sys, '_base_executable', sys.executable)   # it has lightgbm, the .venv doesn't

START = dtime(18, 0)
TOOL_TIMEOUT_SEC = 3600
LOCK_STALE_SEC = 2 * TOOL_TIMEOUT_SEC     # a run that died: try again
REPORT, LOCK, STATUS = 'report.md', 'running.lock', 'status.json'

# (tool, its Python), in order: tick_flow_test feeds odte_edge_test, build_move_panel feeds
# move_model_test, and odte_level_test imports odte_edge_test.
TOOLS = [
    ('rebuild_charm_map', VENV),
    ('scorecard', VENV),
    ('pin_backtest', VENV),
    ('flow_alert_test', VENV),
    ('tick_flow_test', VENV),
    ('tick_flow_size', VENV),
    ('charm_map_test', VENV),
    ('charm_signal_test', VENV),
    ('level_move_test', VENV),
    ('build_move_panel', VENV),
    ('move_model_test', SYSTEM),
    ('turning_point_test', SYSTEM),
    ('odte_edge_test', SYSTEM),
    ('odte_level_test', VENV),
]

# Each of these ends with a RESULT line: tests run, how many pass by chance, what passed.
# Their numbers drift every week; a pass appearing or going is what's worth reading.
TESTS = ('flow_alert_test', 'tick_flow_test', 'charm_signal_test', 'level_move_test', 'move_model_test',
         'turning_point_test', 'odte_edge_test')
RESULT = re.compile(r'^RESULT: (\d+) tests; about ([\d.]+) by chance; passed: (.*)$', re.M)

# The lines of each tool's output worth seeing in the report; the rest is in its .txt.
HEADLINES = {
    'rebuild_charm_map': (r'^\d{4}-\d\d-\d\d (?!kept)',),
    'scorecard': (r'^Charm flow:', r'^Expected move from', r'at 15:30 ', r'^Heatmap:'),
    'pin_backtest': (r'^\s+charm_flow\s',),
    'flow_alert_test': (r'SPX flow alerts on', r'^(EVERY ALERT|EVENTS)', r'^\s+all labelled', r'^\s+every alert'),
    'tick_flow_test': (r'^\d+ days ', r'^odte_near', r'^price alone', r'^same days'),
    'tick_flow_size': (r'^n \d+', r'customers (bought|sold) gamma'),
    'charm_map_test': (r'^\s+(Jun-Sep|Oct on)\s', r'0DTE pull within'),
    'charm_signal_test': (r'checkpoints, \d+ days', r'^flow BUY at'),
    'level_move_test': (r'real approaches', r'every fake level'),
    'move_model_test': (r'^== Part [12]:', r'^target ', r'^\s*(big_15|up_15|up_60)\s+\d+\.\d%'),
    'turning_point_test': (r'new-extreme events', r'^holds \d', r'^(holds|turn): walk', r'edge over clock',
                           r'most confident 10%', r'within \d+ pts of any level'),
    'odte_edge_test': (r'^OPTION PRICES', r'^[BE]\. ', r'model, every 5 min'),
    'odte_level_test': (r'level touches over',),
}
PASS_NOTE = ('A pass is only a lead: some pass by chance, and most of these tools have few days. It has to '
             'keep passing as the days add up, then earn its place on the scoreboard, graded live.')


# ── When ─────────────────────────────────────────────────────────────────────────────────
def week_ending(now: datetime) -> date:
    """The Friday this run is for: the latest whose START has passed."""
    d = now.date() - timedelta(days=(now.weekday() - 4) % 7)
    return d - timedelta(days=7) if d == now.date() and now.time() < START else d


def allowed(now: datetime) -> bool:
    return now.weekday() >= 5 or now.time() >= START


def run_dir(week: date, root: str = RESEARCH_DIR) -> str:
    return os.path.join(root, week.isoformat())


def due(now: datetime, root: str = RESEARCH_DIR) -> bool:
    if not allowed(now):
        return False
    d = run_dir(week_ending(now), root)
    if os.path.exists(os.path.join(d, REPORT)):
        return False
    lock = os.path.join(d, LOCK)
    return not os.path.exists(lock) or time.time() - os.path.getmtime(lock) > LOCK_STALE_SEC


def _hold(d: str):
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, LOCK), 'w') as f:
        f.write(str(os.getpid()))


def start(now: datetime, root: str = RESEARCH_DIR) -> date:
    """Run this week's in its own process; the server carries on, and can restart."""
    week = week_ending(now)
    _hold(run_dir(week, root))
    subprocess.Popen([VENV, os.path.abspath(__file__)], cwd=ROOT, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=subprocess.CREATE_NO_WINDOW)
    return week


# ── Run ──────────────────────────────────────────────────────────────────────────────────
def run_tool(name: str, python: str, out_dir: str, timeout: float = TOOL_TIMEOUT_SEC) -> dict:
    """One tool, its output in <out_dir>/<name>.txt. A failure stays in the report and the
    next tool still runs."""
    t0 = time.time()
    env = {**os.environ, 'TEMP': WORK_DIR, 'TMP': WORK_DIR, 'PYTHONIOENCODING': 'utf-8'}
    with open(os.path.join(out_dir, f'{name}.txt'), 'w', encoding='utf-8') as out:
        try:
            code = subprocess.run([python, os.path.join('tools', f'{name}.py')], cwd=ROOT, env=env,
                                  stdout=out, stderr=subprocess.STDOUT, timeout=timeout,
                                  creationflags=subprocess.BELOW_NORMAL_PRIORITY_CLASS).returncode
        except subprocess.TimeoutExpired:
            code = 'timed out'
        except OSError as e:
            code = str(e)
    return {'tool': name, 'code': code, 'sec': round(time.time() - t0)}


def run_week(week: date, root: str = RESEARCH_DIR, tools: list = TOOLS):
    d = run_dir(week, root)
    _hold(d)
    os.makedirs(WORK_DIR, exist_ok=True)
    started = datetime.now(ET).isoformat(timespec='seconds')
    done = []
    for name, python in tools:
        _hold(d)                                     # keeps the lock fresh between tools
        done.append(run_tool(name, python, d))
        with open(os.path.join(d, STATUS), 'w', encoding='utf-8') as f:
            json.dump({'week': week.isoformat(), 'started': started, 'tools': done}, f, indent=1)
    write_report(week, root)
    os.remove(os.path.join(d, LOCK))


# ── Report ───────────────────────────────────────────────────────────────────────────────
def headlines(name: str, text: str) -> list:
    pats = HEADLINES.get(name, ())
    return [line.strip() for line in text.splitlines() if any(re.search(p, line) for p in pats)]


def result(text: str) -> dict | None:
    m = RESULT.search(text)
    if not m:
        return None
    return {'tests': int(m[1]), 'chance': float(m[2]),
            'passed': [] if m[3].strip() == 'none' else [x.strip() for x in m[3].split(' | ')]}


def result_line(name: str, r: dict | None) -> str:
    if r is None:
        return f'- {name}: NO RESULT LINE (see {name}.txt)'
    of = f"{r['tests']} tests, about {r['chance']:g} by chance"
    return f"- {name}: {of}, {len(r['passed'])} passed: {' | '.join(r['passed'])}" if r['passed'] else f'- {name}: {of}, none passed'


def previous_week(week: date, root: str = RESEARCH_DIR) -> date | None:
    """The latest finished run before this one."""
    found = []
    for name in os.listdir(root) if os.path.isdir(root) else ():
        try:
            w = date.fromisoformat(name)
        except ValueError:
            continue
        if w < week and os.path.exists(os.path.join(root, name, STATUS)):
            found.append(w)
    return max(found) if found else None


def _read(path: str) -> str:
    with open(path, encoding='utf-8', errors='replace') as f:
        return f.read()


def write_report(week: date, root: str = RESEARCH_DIR) -> str:
    d = run_dir(week, root)
    status = json.loads(_read(os.path.join(d, STATUS)))
    tools = status['tools']
    failed = [t for t in tools if t['code'] != 0]
    prev = previous_week(week, root)
    text = {t['tool']: _read(os.path.join(d, f"{t['tool']}.txt")) for t in tools}
    now = {name: result(text[name]) for name in text if name in TESTS or result(text[name])}
    old = {}
    for name in now if prev else ():
        path = os.path.join(run_dir(prev, root), f'{name}.txt')
        old[name] = (result(_read(path)) if os.path.exists(path) else None) or {'passed': []}
    minutes = sum(t['sec'] for t in tools) / 60
    out = [f'# Weekly research, week ending {week.isoformat()}', '',
           f"Started {status['started'][:16].replace('T', ' ')} ET, {minutes:.0f} min of tools. "
           + (f'{len(failed)} of {len(tools)} FAILED.' if failed else f'All {len(tools)} tools ran.'), '']
    if failed:
        out += ['## Failed', ''] + [f"- {t['tool']}: {t['code']} (see {t['tool']}.txt)" for t in failed] + ['']
    out += [f'## Changed since {prev}' if prev else '## Changed since last week', '']
    if not prev:
        out += ['First report: nothing to compare with yet.', '']
    else:
        changes = []
        for name, r in now.items():
            passed = r['passed'] if r else []
            changes += [f'- new pass: {name}: {x}' for x in passed if x not in old[name]['passed']]
            changes += [f'- stopped passing: {name}: {x}' for x in old[name]['passed'] if x not in passed]
        out += (changes or ['Nothing new passed, and nothing stopped passing.']) + ['']
    out += ['## Did anything pass?', ''] + [result_line(name, r) for name, r in now.items()] + ['', PASS_NOTE, '']
    out += ['## The numbers', '']
    for t in tools:
        lines = headlines(t['tool'], text[t['tool']])
        if lines:
            out += [f"{t['tool']}:", ''] + [f'- {x}' for x in lines] + ['']
    with open(os.path.join(d, REPORT), 'w', encoding='utf-8') as f:
        f.write('\n'.join(out))
    return os.path.join(d, REPORT)


def report_text(week: date | None = None, root: str = RESEARCH_DIR) -> str | None:
    """A week's report, default the latest (None if there isn't one)."""
    if week is None:
        weeks = []
        for name in os.listdir(root) if os.path.isdir(root) else ():
            if re.fullmatch(r'\d{4}-\d\d-\d\d', name) and os.path.exists(os.path.join(root, name, REPORT)):
                weeks.append(name)
        if not weeks:
            return None
        week = date.fromisoformat(max(weeks))
    path = os.path.join(run_dir(week, root), REPORT)
    return _read(path) if os.path.exists(path) else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--report', action='store_true', help="rebuild this week's report from its saved output")
    a = ap.parse_args()
    now = datetime.now(ET)
    week = week_ending(now)
    if a.report:
        print(write_report(week))
    else:
        run_week(week)


if __name__ == '__main__':
    main()
