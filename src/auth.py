"""
auth.py — Schwab login (OAuth).

Opens by itself when the login has expired or is about to (gex.request_reauth), or
run reauth.bat. It opens the Schwab login page in the browser. After you log in,
Schwab sends the browser to https://127.0.0.1/?code=... (an error page: nothing runs
there). Copy that page's address (click the address bar, Ctrl+C) and this window
picks it up from the clipboard; pasting it here and pressing Enter works too.

New tokens go to data/tokens.json. A running dashboard reads them on its next token
refresh (within a minute); if the dashboard isn't running, it is started.
"""
import base64
import json
import os
import subprocess
import sys
import time
import webbrowser
from urllib.parse import urlparse, parse_qs

import requests
from dotenv import load_dotenv

_SRC_DIR     = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_SRC_DIR)
load_dotenv(os.path.join(_PROJECT_DIR, '.env'))

CLIENT_ID     = os.environ['SCHWAB_CLIENT_ID']
CLIENT_SECRET = os.environ['SCHWAB_CLIENT_SECRET']
REDIRECT_URI  = os.environ['SCHWAB_REDIRECT_URI']
TOKENS_FILE     = os.path.join(_PROJECT_DIR, 'data', 'tokens.json')
AUTH_STATE_FILE = os.path.join(_PROJECT_DIR, 'data', 'auth_state.json')   # when we last logged in


def get_auth_url():
    return (
        f"https://api.schwabapi.com/v1/oauth/authorize"
        f"?client_id={CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&response_type=code"
    )


def exchange_code_for_token(auth_code: str) -> dict:
    credentials = f"{CLIENT_ID}:{CLIENT_SECRET}"
    encoded = base64.b64encode(credentials.encode()).decode()

    response = requests.post(
        "https://api.schwabapi.com/v1/oauth/token",
        timeout=(5, 20),
        headers={
            "Authorization": f"Basic {encoded}",
            "Content-Type": "application/x-www-form-urlencoded"
        },
        data={
            "grant_type": "authorization_code",
            "code": auth_code,
            "redirect_uri": REDIRECT_URI
        }
    )
    if not response.ok:
        print(f"\nERROR {response.status_code}: {response.text}")
        response.raise_for_status()
    return response.json()


def code_from_url(url: str):
    """The ?code= of a Schwab redirect URL, or None."""
    return parse_qs(urlparse(url.strip()).query).get('code', [None])[0]


def save_tokens(tokens: dict):
    """Atomic write (the dashboard may read the file at any moment), plus the login time."""
    os.makedirs(os.path.dirname(TOKENS_FILE), exist_ok=True)
    tmp = TOKENS_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(tokens, f, indent=2)
    os.replace(tmp, TOKENS_FILE)
    with open(AUTH_STATE_FILE, 'w') as f:
        json.dump({'logged_in_at': time.time()}, f)


def _clipboard_text():
    try:
        import win32clipboard
        win32clipboard.OpenClipboard()
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                return win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
    except Exception:          # clipboard busy (another app has it open) or no pywin32
        pass
    return None


def wait_for_redirect_url() -> str:
    """The redirect URL, from a fresh clipboard copy or typed / pasted + Enter."""
    import msvcrt
    stale = _clipboard_text()          # whatever was copied before: never an answer
    typed = ''
    while True:
        clip = _clipboard_text()
        if clip and clip != stale and '127.0.0.1' in clip and code_from_url(clip):
            print(f"\nGot it from the clipboard.")
            return clip
        while msvcrt.kbhit():
            ch = msvcrt.getwch()
            if ch in '\r\n':
                if typed.strip():
                    print()
                    return typed.strip()
            elif ch == '\b':
                if typed:
                    typed = typed[:-1]
                    print('\b \b', end='', flush=True)
            else:
                typed += ch
                print(ch, end='', flush=True)
        time.sleep(0.3)


def _dashboard_running() -> bool:
    """The dashboard holds the named mutex single_instance.acquire() creates."""
    try:
        import ctypes
        h = ctypes.windll.kernel32.OpenMutexW(0x00100000, False, 'Local\\GEXDashboard')   # SYNCHRONIZE
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
    except Exception:
        pass
    return False


def _start_dashboard():
    r = subprocess.run(['schtasks', '/Run', '/TN', 'GEXDashboard'], capture_output=True, text=True)
    if r.returncode == 0:
        print("Starting the dashboard (it opens in Chrome in a few seconds).")
    else:                                # no scheduled task: run it in its own window
        subprocess.Popen(['cmd', '/c', 'start', 'GEX Dashboard', os.path.join(_PROJECT_DIR, 'start.bat')],
                         cwd=_PROJECT_DIR)
        print("Starting the dashboard with start.bat.")


def main():
    import single_instance
    if not single_instance.acquire('Local\\GEXDashboardReauth'):
        return                           # a login window is already open
    renew = '--renew' in sys.argv
    print("\n--- Schwab login ---\n")
    if renew:
        print("Your Schwab login is 6+ days old and lapses at 7 days. Logging in again now")
        print("keeps the dashboard from stopping mid-session.\n")
    elif '--auto' in sys.argv:
        print("Your Schwab login expired, so the dashboard can't get data. Log in to continue.\n")
    while True:
        print("1. The Schwab login page is opening in your browser. Log in and approve.")
        print("2. The browser then shows an error page at https://127.0.0.1/?code=...")
        print("   Copy that page's address (click the address bar, Ctrl+C): this window")
        print("   picks it up by itself. (Or paste it here and press Enter.)\n")
        webbrowser.open(get_auth_url())
        code = code_from_url(wait_for_redirect_url())
        if not code:
            print("No 'code' in that address. Copy the full address of the 127.0.0.1 page.\n")
            continue
        print("Exchanging the code for tokens...")
        try:
            tokens = exchange_code_for_token(code)
        except Exception as e:
            print(f"That didn't work ({e}). Codes are single-use and expire within a minute,")
            print("so log in once more.\n")
            continue
        save_tokens(tokens)
        print("\nLogged in. New tokens saved (good for 7 days).")
        break

    if _dashboard_running():
        print("The running dashboard picks up the new login within a minute.")
    else:
        _start_dashboard()
    if renew or '--auto' in sys.argv:    # opened by the dashboard: close by itself
        print("\nThis window closes in 10 seconds.")
        time.sleep(10)


if __name__ == "__main__":
    main()
