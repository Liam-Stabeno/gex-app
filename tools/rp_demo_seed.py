"""
Seed a synthetic 0DTE session so the Rolling Profile panel can be tested after hours.

    python tools/rp_demo_seed.py            # write demo data for today
    python tools/rp_demo_seed.py --remove   # delete today's SPX day file

Then restart dashboard.py and hard refresh — the panel reloads today's file.

Safety: refuses to run during market hours (09:30–16:00 ET, Mon–Fri) or if today's
file already exists, so demo data never mixes with real snapshots. --force overrides.
"""
import argparse
import math
import random
import sys
from datetime import datetime, time as dtime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import rolling_profile as rp  # noqa: E402

SYM = "SPX"
STEP = 15                        # seconds between snapshots
STRIKES = [7600 + 5 * i for i in range(71)]   # 7600–7950


def session_bounds(now_et):
    d = now_et.date()
    o = datetime.combine(d, dtime(9, 30), rp.ET)
    c = datetime.combine(d, dtime(16, 0), rp.ET)
    return o, c


def fake_chain(spot, cum, exp_key):
    cm, pm = {exp_key: {}}, {exp_key: {}}
    for k in STRIKES:
        # calls concentrate just above spot + a call wall at 7780; puts below + walls at 7750/7700
        cw = 900 * math.exp(-((k - spot - 5) ** 2) / 120) + 700 * math.exp(-((k - 7780) ** 2) / 60)
        pw = 700 * math.exp(-((k - spot + 10) ** 2) / 150) + 500 * math.exp(-((k - 7750) ** 2) / 50) \
            + 350 * math.exp(-((k - 7700) ** 2) / 20)
        cum[k][0] += int(cw * random.uniform(0.3, 1.7))
        cum[k][1] += int(pw * random.uniform(0.3, 1.7))
        cm[exp_key][f"{k:.1f}"] = [{"putCall": "CALL", "totalVolume": cum[k][0]}]
        pm[exp_key][f"{k:.1f}"] = [{"putCall": "PUT", "totalVolume": cum[k][1]}]
    return {"underlyingPrice": round(spot, 2), "callExpDateMap": cm, "putExpDateMap": pm}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--remove", action="store_true", help="delete today's SPX day file")
    ap.add_argument("--force", action="store_true", help="skip safety checks")
    a = ap.parse_args()

    now_et = datetime.now(rp.ET)
    path = rp._day_file(SYM, now_et.timestamp())

    if a.remove:
        if path.exists():
            path.unlink()
            print(f"Removed {path}")
        else:
            print(f"Nothing to remove ({path} not found)")
        return

    o, c = session_bounds(now_et)
    in_rth = now_et.weekday() < 5 and o <= now_et < c
    if not a.force:
        if in_rth:
            sys.exit("Market is open — demo data would mix with live snapshots. Run after 16:00 ET.")
        if path.exists():
            sys.exit(f"{path.name} already exists (real data?). Use --remove first, or --force.")
    if now_et < o:
        sys.exit("Before 09:30 ET there's no session to fake for today. Run after 16:00 ET.")

    end = min(now_et, c)
    exp_key = f"{now_et.date().isoformat()}:0"
    cum = {k: [0, 0] for k in STRIKES}
    spot, t, n = 7730.0, o.timestamp(), 0
    rp._reset_for_tests()
    while t <= end.timestamp():
        spot += random.gauss(0.012, 0.9)          # gentle up-drift
        rp.record_chain(SYM, fake_chain(spot, cum, exp_key), ts=t)
        t += STEP
        n += 1
    print(f"Wrote {n} demo snapshots -> {path}")
    print("Restart dashboard.py, hard refresh (Ctrl+Shift+R).")
    print("Remove later with:  python tools/rp_demo_seed.py --remove")


if __name__ == "__main__":
    main()
