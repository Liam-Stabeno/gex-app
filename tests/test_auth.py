"""Schwab login automation: the login window opens by itself, at most once per 15 min."""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import auth  # noqa: E402
import gex   # noqa: E402


def test_code_from_redirect_url():
    url = "https://127.0.0.1/?code=C0.b2F1dGg%40&session=abc-123"
    assert auth.code_from_url(url) == "C0.b2F1dGg@"          # %40 decoded, like before
    assert auth.code_from_url("  " + url + "\r\n") == "C0.b2F1dGg@"
    assert auth.code_from_url("https://127.0.0.1/") is None
    assert auth.code_from_url("not a url") is None


def test_save_tokens_is_atomic_and_records_login_time(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "TOKENS_FILE", str(tmp_path / "data" / "tokens.json"))
    monkeypatch.setattr(auth, "AUTH_STATE_FILE", str(tmp_path / "data" / "auth_state.json"))
    auth.save_tokens({"access_token": "a", "refresh_token": "r"})
    assert json.loads((tmp_path / "data" / "tokens.json").read_text())["refresh_token"] == "r"
    assert not (tmp_path / "data" / "tokens.json.tmp").exists()
    assert abs(json.loads((tmp_path / "data" / "auth_state.json").read_text())["logged_in_at"] - time.time()) < 5


def test_request_reauth_opens_one_window_per_15_min(monkeypatch):
    import subprocess
    opened = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: opened.append((cmd, kw)))
    monkeypatch.delenv("GEX_NO_AUTO_REAUTH", raising=False)
    monkeypatch.setattr(gex, "_reauth_opened_at", 0.0)
    assert gex.request_reauth() is True
    assert gex.request_reauth() is False                     # within 15 min: no second window
    assert len(opened) == 1
    cmd, kw = opened[0]
    assert cmd[1].endswith("auth.py") and cmd[2] == "--auto"
    assert kw["creationflags"] & subprocess.CREATE_NEW_CONSOLE
    monkeypatch.setattr(gex, "_reauth_opened_at", time.time() - gex.REAUTH_EVERY_SEC - 1)
    assert gex.request_reauth(renew=True) is True and opened[1][0][2] == "--renew"
    monkeypatch.setenv("GEX_NO_AUTO_REAUTH", "1")
    monkeypatch.setattr(gex, "_reauth_opened_at", 0.0)
    assert gex.request_reauth() is False and len(opened) == 2   # switched off


def test_login_age_days(tmp_path, monkeypatch):
    monkeypatch.setattr(gex, "_PROJECT_DIR", str(tmp_path))
    assert gex.login_age_days() is None                      # never logged in through auth.py
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "auth_state.json").write_text(json.dumps({"logged_in_at": time.time() - 6.5 * 86400}))
    assert abs(gex.login_age_days() - 6.5) < 0.01


def test_renew_prompt_only_when_login_is_6_days_old(monkeypatch):
    import background
    asked = []
    monkeypatch.setattr(background, "request_reauth", lambda renew=False: asked.append(renew) or True)
    for age, expect in ((None, []), (5.9, []), (6.2, [True])):
        asked.clear()
        monkeypatch.setattr(background, "login_age_days", lambda a=age: a)
        background.renew_login_if_old()
        assert asked == expect


def test_tokens_shared_between_processes(tmp_path, monkeypatch):
    f = tmp_path / "tokens.json"
    monkeypatch.setattr(gex, "TOKENS_FILE", str(f))
    refreshed = []
    def fake_refresh(rt):
        refreshed.append(rt)
        t = {"access_token": f"NEW{len(refreshed)}", "refresh_token": rt}
        f.write_text(json.dumps(t))
        return t
    monkeypatch.setattr(gex, "refresh_access_token", fake_refresh)
    f.write_text(json.dumps({"access_token": "A", "refresh_token": "R"}))
    monkeypatch.setattr(gex, "_cached_token", {"access_token": "B", "expires_at": time.time() + 600, "file_mtime": 0.0})
    assert gex.get_access_token() == "A" and refreshed == []        # another process refreshed: adopt
    assert gex.get_access_token() == "A"                             # no file change: cached
    assert gex.get_access_token(force=True) == "NEW1" and refreshed == ["R"]   # A rejected, file still A
    f.write_text(json.dumps({"access_token": "C", "refresh_token": "R"}))      # the other process again
    assert gex.get_access_token(force=True) == "C" and len(refreshed) == 1    # newer in the file: no refresh


def test_history_request_retries_after_401(monkeypatch):
    import option_history as oh
    codes, forced = iter([401, 200]), []
    class R:
        def __init__(self, c): self.status_code, self.ok, self.text = c, c == 200, ""
        def json(self): return {"candles": [{"datetime": 1}]}
    monkeypatch.setattr(oh.requests, "get", lambda url, **kw: R(next(codes)))
    monkeypatch.setattr(oh, "get_access_token", lambda force=False: forced.append(force) or "t")
    assert oh.fetch_bars("X", 0, 1) == [{"datetime": 1}]
    assert True in forced                                            # a fresh token was fetched
