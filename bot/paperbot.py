#!/usr/bin/env python3
"""Paper-trading bot for the Alpaca PAPER account. Standard library only.

Runs every 5 minutes (launchd) with `tick`. On a trading day, New York time:
  08:45-09:27  plan: daily scan -> momentum setups -> two 60-session backtests per stock:
                 A) buy at the open (stop 1%, target 2R)
                 B) opening-range breakout (first 5-min close over the 9:30-9:45 high,
                    stop at the range middle, target 2R)  [same rule as the Time Machine history]
               -> place the best 1-2 "open" trades as bracket orders before 9:30
               -> keep the stocks that pass B as today's intraday watchlist
  09:50-15:30  live: every 5 minutes check the watchlist bar by bar (trader-entry); when one
               breaks out (trigger bar at most 10 min old, price not chased), size it from
               the live price, run trader-risk, place a bracket order
  10:00        cancel any open-buy that has not filled; intraday buys not filled in 10 min
               are cancelled at the next check
  15:55        flat: cancel the bot's open orders, close the bot's positions
  16:05        report: fills, R, slippage, P&L -> reports/YYYY-MM-DD.md

Safety: talks ONLY to paper-api.alpaca.markets and refuses to send any order
unless the account number starts with "PA" (Alpaca paper). Limits come from
trader/config.json (max loss per trade, daily loss, trades a day, position cap).

Manual: python3 bot/paperbot.py status | plan --dry-run | plan | live [--dry-run] |
        cancel-unfilled | flatten | report | tick
"""
import datetime, json, math, os, subprocess, sys, time, urllib.error, urllib.request
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
HOME = os.path.expanduser("~")
ROOT = os.environ.get("DT_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS = os.path.join(ROOT, "skills") if os.path.isdir(os.path.join(ROOT, "skills")) \
    else os.path.join(HOME, ".claude", "skills")
TRADE_URL = "https://paper-api.alpaca.markets"          # paper only, never live
DATA_URL = "https://data.alpaca.markets"
CFG = os.path.join(ROOT, "trader", "config.json")
STATE_DIR = os.path.join(ROOT, "bot", "state")
LOG_DIR = os.path.join(ROOT, "bot", "logs")
REPORT_DIR = os.path.join(ROOT, "reports")
T = datetime.time

# strategy settings
SETUPS = {"breakout_20d", "bull_flag"}
RSI_MIN, RSI_MAX = 55, 72
MIN_PRICE = 10
CANDIDATES = 40          # most traded setups that get the backtests
TARGET_R = 2.0
MIN_SESSIONS = 40
MIN_AVG_R = 0.05         # backtest average R needed at 2R ...
MIN_AVG_R_1R = -0.05       # ... and not negative at 1R (robustness)
# A) buy at the open
PREOPEN_MAX = 3
OPEN_CASH_SHARE = 0.5     # opening buys use at most half the cash; the rest is for intraday breakouts
STOP_PCT = 1.0
LIMIT_UP_PCT = 0.4       # limit = pre-market price + 0.4% (no chasing a gap)
MAX_GAP_PCT = 5.0        # skip if pre-market is 3%+ over yesterday's close
# B) intraday opening-range breakout
ORB_MIN_FIRED = 12       # past sessions where the rule fired
ORB_WATCH_MAX = 15
ORB_MAX_STALE_MIN = 15   # trigger bar must have closed within this many minutes
ORB_CHASE_R = 0.5        # skip if the price is already 0.3R past the trigger close
ORB_LIMIT_UP_PCT = 0.1   # limit = live price + 0.1%
ORB_CANCEL_MIN = 10      # cancel an intraday buy not filled after this many minutes

PLAN_FROM, PLAN_UNTIL = T(8, 45), T(9, 27)
LIVE_FROM = T(9, 50)
CANCEL_OPEN_AT = T(10, 0)
REPORT_AT = T(16, 5)
RISK_PCT = 3.0           # aggressive: max loss per trade = 3% of equity
DAILY_LOSS_PCT = 10.0    # stop opening trades after 10% of equity at risk/lost in a day
TAG = "pb"               # client_order_id prefix: the bot only touches its own orders


# ------------------------------------------------------------------ basics
def now_ny():
    return datetime.datetime.now(NY)


def today():
    return now_ny().date().isoformat()


def acct_id(acct):
    n = str(acct.get("account_number", ""))
    return n[:2] + "..." + n[-2:]              # logs are public in the cloud repo


def log(msg):
    os.makedirs(LOG_DIR, exist_ok=True)
    line = "%s  %s" % (now_ny().strftime("%Y-%m-%d %H:%M:%S ET"), msg)
    print(line)
    with open(os.path.join(LOG_DIR, today() + ".log"), "a") as fh:
        fh.write(line + "\n")


def load_state(day=None):
    p = os.path.join(STATE_DIR, (day or today()) + ".json")
    if os.path.exists(p):
        return json.load(open(p))
    return {"date": day or today(), "orders": []}


def save_state(st):
    os.makedirs(STATE_DIR, exist_ok=True)
    p = os.path.join(STATE_DIR, st["date"] + ".json")
    json.dump(st, open(p + ".tmp", "w"), indent=2)
    os.replace(p + ".tmp", p)


def keys():
    k, s = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
    if not (k and s):
        env = json.load(open(os.path.join(HOME, ".claude.json")))["mcpServers"]["alpaca"]["env"]
        k, s = env["ALPACA_API_KEY"], env["ALPACA_SECRET_KEY"]
    return k, s


def api(method, path, body=None, base=TRADE_URL):
    k, s = keys()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers={
        "APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raise RuntimeError("%s %s -> %s %s" % (method, path, e.code, e.read().decode()[:300]))


def paper_account():
    acct = api("GET", "/v2/account")
    if not str(acct.get("account_number", "")).startswith("PA"):
        raise SystemExit("REFUSED: account %s is not an Alpaca paper account" % acct.get("account_number"))
    return acct


def cfg():
    return json.load(open(CFG))


def sync_limits(acct):
    """Scale the dollar limits in config.json to the account's equity (aggressive mode)."""
    c = cfg(); eq = float(acct["equity"])
    c.update({"budget": round(eq, 2), "max_order_usd": round(eq, 2),
              "max_loss_per_trade_usd": round(eq * RISK_PCT / 100, 2),
              "max_daily_loss_usd": round(eq * DAILY_LOSS_PCT / 100, 2)})
    json.dump(c, open(CFG, "w"), indent=2)
    return c


def hm(s):
    return T(*map(int, s.split(":")))


def trading_day(d):
    cal = api("GET", "/v2/calendar?start=%s&end=%s" % (d, d))
    return bool(cal) and cal[0]["date"] == d


def latest_price(sym):
    t = api("GET", "/v2/stocks/%s/trades/latest?feed=iex" % sym, base=DATA_URL)
    return float(t["trade"]["p"]), t["trade"]["t"][:19]


# ------------------------------------------------------------- backtests
def yahoo_5m(sym):
    u = "https://query2.finance.yahoo.com/v8/finance/chart/%s?interval=5m&range=60d" % sym
    r = json.load(urllib.request.urlopen(urllib.request.Request(
        u, headers={"User-Agent": "Mozilla/5.0"}), timeout=30))["chart"]["result"][0]
    q, days = r["indicators"]["quote"][0], {}
    for i, t in enumerate(r.get("timestamp") or []):
        if q["open"][i] is None:
            continue
        d = datetime.datetime.fromtimestamp(t, NY)
        if T(9, 30) <= d.time() < T(16, 0):
            days.setdefault(d.date(), []).append((d.time(), q["open"][i], q["high"][i], q["low"][i], q["close"][i]))
    return days


def _walk(bars, e, s, tg, R):
    """R of a long from entry e with stop s and target tg over the bars, flat at 15:55."""
    for tm, o, h, l, c in bars:
        if tm >= T(15, 55):
            return (c - e) / (e - s)
        if l <= s:
            return -1.0              # stop first when both are in one bar (conservative)
        if h >= tg:
            return float(R)
    return (bars[-1][4] - e) / (e - s) if bars else 0.0


def _summary(res, R):
    if not res:
        return None
    return {"n": len(res), "target": sum(x >= R - 1e-9 for x in res),
            "stop": sum(x <= -1 + 1e-9 for x in res), "avg_r": sum(res) / len(res)}


def bt_open(days, stop_pct, R, before):
    res = []
    for d, b in sorted(days.items()):
        if d >= before or len(b) < 75 or b[0][0] != T(9, 30):
            continue
        e = b[0][1]; s = e * (1 - stop_pct / 100)
        res.append(_walk(b, e, s, e + R * (e - s), R))
    return _summary(res, R)


def bt_orb(days, R, before, last_entry=T(15, 30)):
    """First 5-min close above the 9:30-9:45 high; stop at the range middle."""
    res, sessions = [], 0
    for d, b in sorted(days.items()):
        if d >= before or len(b) < 75 or b[0][0] != T(9, 30):
            continue
        sessions += 1
        rng = [x for x in b if x[0] < T(9, 45)]
        if len(rng) != 3:
            continue
        hi, lo = max(x[2] for x in rng), min(x[3] for x in rng)
        mid = (hi + lo) / 2
        for i, (tm, o, h, l, c) in enumerate(b):
            close_t = (datetime.datetime.combine(d, tm) + datetime.timedelta(minutes=5)).time()
            if tm < T(9, 45) or close_t > last_entry:
                continue
            if c > hi:
                if c - mid > 0:
                    res.append(_walk(b[i + 1:], c, mid, c + R * (c - mid), R))
                break
    out = _summary(res, R)
    if out:
        out["sessions"] = sessions
    return out


# --------------------------------------------------------- place entries
def place(orders, st, kind, now_label, dry):
    """trader-risk check, then send each passing order. kind: open | orb."""
    acct = paper_account()
    d = st["date"]
    of = os.path.join(ROOT, "trader", "orders.json")
    json.dump({"created_by": "paperbot", "paper": True, "orders": orders}, open(of, "w"), indent=2)
    pf = os.path.join(ROOT, "trader", "portfolio.json")
    pos = api("GET", "/v2/positions")
    cash = min(float(acct["cash"]), float(acct["buying_power"]))
    json.dump({"cash": cash, "positions": [{"symbol": p["symbol"], "qty": float(p["qty"])} for p in pos]},
              open(pf, "w"), indent=2)
    sf = os.path.join(STATE_DIR, "broker-" + d + ".json")
    os.makedirs(STATE_DIR, exist_ok=True)
    json.dump({"orders": st["orders"]}, open(sf, "w"), indent=2)
    r = subprocess.run([sys.executable, os.path.join(SKILLS, "trader-risk", "check.py"), "--json",
                        "--orders", of, "--portfolio", pf, "--state", sf, "--now", now_label]
                       + (["--no-write"] if dry else []), cwd=ROOT, capture_output=True, text=True)
    try:
        res = json.loads(r.stdout)
    except ValueError:
        log("risk check failed: " + (r.stderr or r.stdout)[:300]); return
    for o, v in zip(orders, res["results"]):
        log("risk %s [%s] %s x%s limit %s stop %s target %s max loss $%s %s" % (
            v["verdict"], kind, o["symbol"], o["qty"], o["limit_price"], o["stop_loss_stop_price"],
            o["take_profit_limit_price"], o["plan"]["max_loss"], "; ".join(v["reasons"])))
        if v["verdict"] != "PASS":
            continue
        if dry:
            log("  dry run: not placed"); continue
        paper_account()  # re-check right before every order
        body = {"symbol": o["symbol"], "qty": o["qty"], "side": "buy", "type": "limit", "time_in_force": "day",
                "limit_price": o["limit_price"], "order_class": "bracket",
                "take_profit": {"limit_price": o["take_profit_limit_price"]},
                "stop_loss": {"stop_price": o["stop_loss_stop_price"]},
                "client_order_id": "%s-%s-%s-%s-%d" % (TAG, kind, d, o["symbol"], int(time.time()))}
        try:
            placed = api("POST", "/v2/orders", body)
        except RuntimeError as e:
            log("  ORDER FAILED %s: %s" % (o["symbol"], e)); continue
        log("  PLACED [%s] %s id %s status %s" % (kind, o["symbol"], placed["id"], placed["status"]))
        st["orders"].append({"status": "placed", "date": d, "symbol": o["symbol"], "side": "buy", "kind": "entry",
                             "how": kind, "qty": o["qty"], "max_loss": o["plan"]["max_loss"], "id": placed["id"],
                             "client_order_id": body["client_order_id"], "plan": o["plan"], "why": o["why"]})
        save_state(st)


def make_order(sym, qty, limit, stop, target, why, setup, entry_time, c):
    return {"symbol": sym, "side": "buy", "qty": str(qty), "type": "limit", "time_in_force": "day",
            "limit_price": "%.2f" % limit, "order_class": "bracket",
            "take_profit_limit_price": "%.2f" % target, "stop_loss_stop_price": "%.2f" % stop, "why": why,
            "plan": {"entry": limit, "stop": stop, "target": target, "risk_per_share": round(limit - stop, 2),
                     "max_loss": round(qty * (limit - stop), 2), "setup": setup, "entry_time": entry_time,
                     "flat_by": c["flat_by"]}}


def shares(c, limit, stop, cash):
    cap = c["budget"] * c["max_position_pct"] / 100
    return max(0, min(math.floor(c["max_loss_per_trade_usd"] / (limit - stop)), math.floor(cap / limit),
                      math.floor(c["max_order_usd"] / limit), math.floor(cash / limit)))


# ----------------------------------------------------------------- plan
def plan(dry=False, open_trades=True):
    st = load_state()
    if st.get("planned") and not dry:
        log("plan: already done today"); return
    acct = paper_account()
    c = sync_limits(acct)
    d = today()
    log("plan: account %s (paper), cash $%s" % (acct_id(acct), acct["cash"]))

    r = subprocess.run([sys.executable, os.path.join(SKILLS, "trader-scanner", "scan.py"), "daily", "--show", "0"],
                       cwd=ROOT, capture_output=True, text=True, timeout=900)
    log("scan: " + " | ".join(l for l in r.stdout.splitlines() if l.startswith(("Fetched", "Daily scan")) or "matched" in l))
    hits = json.load(open(os.path.join(ROOT, "trader", "watchlist.json")))["hits"]
    cap = c["budget"] * c["max_position_pct"] / 100
    cands = [h for h in hits if SETUPS & set(h.get("setups", []))
             and RSI_MIN <= (h.get("rsi") or 0) <= RSI_MAX and MIN_PRICE <= h["price"] <= cap][:CANDIDATES]
    log("candidates (%d): %s" % (len(cands), ", ".join(h["symbol"] for h in cands)))

    today_d = datetime.date.fromisoformat(d)
    open_ok, orb_ok = [], []
    for h in cands:
        try:
            days = yahoo_5m(h["symbol"])
        except Exception as e:
            log("  %s: no bars (%s)" % (h["symbol"], e)); continue
        a2, a1 = bt_open(days, STOP_PCT, TARGET_R, today_d), bt_open(days, STOP_PCT, 1.0, today_d)
        b2, b1 = bt_orb(days, TARGET_R, today_d), bt_orb(days, 1.0, today_d)
        oa = bool(a2 and a2["n"] >= MIN_SESSIONS and a2["avg_r"] >= MIN_AVG_R and a1["avg_r"] >= MIN_AVG_R_1R)
        ob = bool(b2 and b2["sessions"] >= MIN_SESSIONS and b2["n"] >= ORB_MIN_FIRED
                  and b2["avg_r"] >= MIN_AVG_R and b1["avg_r"] >= MIN_AVG_R_1R)
        log("  %-5s open 2R %s, 1R %s %s | breakout 2R %s (fired %s), 1R %s %s" % (
            h["symbol"], "%+.3fR" % a2["avg_r"] if a2 else "-", "%+.3fR" % a1["avg_r"] if a1 else "-",
            "KEEP" if oa else "drop", "%+.3fR" % b2["avg_r"] if b2 else "-", b2["n"] if b2 else 0,
            "%+.3fR" % b1["avg_r"] if b1 else "-", "WATCH" if ob else "drop"))
        if oa:
            open_ok.append((a2["avg_r"], h, a2))
        if ob:
            orb_ok.append((b2["avg_r"], h, b2))
        time.sleep(0.3)
    open_ok.sort(key=lambda x: -x[0]); orb_ok.sort(key=lambda x: -x[0])

    watch = [{"symbol": h["symbol"], "avg_r": round(b["avg_r"], 3), "fired": b["n"], "sessions": b["sessions"],
              "setups": h["setups"]} for _, h, b in orb_ok[:ORB_WATCH_MAX]]
    log("intraday watchlist: %s" % (", ".join("%s (%+.2fR)" % (w["symbol"], w["avg_r"]) for w in watch) or "none"))

    orders = []
    cash = (min(float(acct["cash"]), float(acct["buying_power"])) - c["budget"] * c["cash_buffer_pct"] / 100) * OPEN_CASH_SHARE
    for avg, h, b2 in (open_ok if open_trades else []):
        if len(orders) >= min(PREOPEN_MAX, c["max_trades_per_day"]):
            break
        sym = h["symbol"]
        try:
            ref, src = latest_price(sym)
        except Exception as e:
            ref, src = h["price"], "yesterday's close"
        if ref > h["price"] * (1 + MAX_GAP_PCT / 100):
            log("  %s: skip, pre-market %.2f is %.1f%% over the close" % (sym, ref, (ref / h["price"] - 1) * 100)); continue
        limit = round(ref * (1 + LIMIT_UP_PCT / 100), 2)
        stop = round(limit * (1 - STOP_PCT / 100), 2)
        target = round(limit + TARGET_R * (limit - stop), 2)
        qty = shares(c, limit, stop, cash)
        if qty < 1:
            log("  %s: 0 shares fit (cash left $%.2f)" % (sym, cash)); continue
        cash -= qty * limit
        orders.append(make_order(sym, qty, limit, stop, target,
                                 "buy at the open, %s, backtest 2R avg %+.2fR over %d sessions; ref %s (%s)" % (
                                     "/".join(sorted(SETUPS & set(h["setups"]))), b2["avg_r"], b2["n"], ref, src),
                                 "open", "9:30 AM", c))
    if dry:
        st = dict(st, orders=list(st["orders"]))
    st["orb_watch"] = watch
    if orders:
        place(orders, st, "open", d + " 09:30", dry)   # can only fill from the 9:30 open
    else:
        log("plan: no open trade today")
    if not dry:
        st["planned"] = True; save_state(st)


# ------------------------------------------------------- live breakouts
def live(dry=False, now_label=None):
    st = load_state()
    c = cfg()
    d = st["date"]
    traded = {r["symbol"] for r in st["orders"]}
    watch = [w for w in st.get("orb_watch", []) if w["symbol"] not in traded]
    if not watch:
        return
    entries = sum(1 for r in st["orders"] if r.get("kind") == "entry")
    if entries >= c["max_trades_per_day"]:
        return
    acct = paper_account()
    cash = min(float(acct["cash"]), float(acct["buying_power"])) - c["budget"] * c["cash_buffer_pct"] / 100
    now = now_ny()
    orders = []
    for w in watch:
        if entries + len(orders) >= c["max_trades_per_day"]:
            break
        sym = w["symbol"]
        args = [sys.executable, os.path.join(SKILLS, "trader-entry", "entry.py"), "check", sym, "--setup", "orb", "--json"]
        if now_label:
            args += ["--now", now_label]
        r = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=120)
        try:
            e = json.loads(r.stdout)
        except ValueError:
            log("live %s: entry check failed %s" % (sym, (r.stderr or r.stdout)[:200])); continue
        if e.get("status") != "triggered":
            continue
        trig = e["trigger"]
        ref_now = datetime.datetime.strptime(now_label, "%Y-%m-%d %H:%M").replace(tzinfo=NY) if now_label else now
        close_dt = datetime.datetime.combine(ref_now.date(), hm(trig["close_time"]), NY)
        age = (ref_now - close_dt).total_seconds() / 60
        if age > ORB_MAX_STALE_MIN:
            log("live %s: triggered at %s, %.0f min ago: too late, not taken" % (sym, trig["time"], age))
            st.setdefault("orb_skipped", []).append(sym)
            st["orb_watch"] = [x for x in st["orb_watch"] if x["symbol"] != sym]
            if not dry:
                save_state(st)
            continue
        hi, lo = e["opening_range"]["high"], e["opening_range"]["low"]
        stop = round((hi + lo) / 2, 2)
        try:
            px, src = (trig["price"], "trigger close") if now_label else latest_price(sym)
        except Exception:
            px, src = e["last"]["close"], "last 5-min close"
        risk0 = trig["price"] - stop
        if px > trig["price"] + ORB_CHASE_R * risk0:
            log("live %s: price %.2f already %.2fR past the trigger %.2f: not chased" % (
                sym, px, (px - trig["price"]) / risk0, trig["price"])); continue
        limit = round(px * (1 + ORB_LIMIT_UP_PCT / 100), 2)
        if limit <= stop:
            continue
        target = round(limit + TARGET_R * (limit - stop), 2)
        qty = shares(c, limit, stop, cash)
        if qty < 1:
            log("live %s: triggered but 0 shares fit (cash $%.2f, risk %.2f a share)" % (sym, cash, limit - stop)); continue
        cash -= qty * limit
        log("live %s: TRIGGERED %s at %.2f (range %.2f-%.2f), live %.2f (%s)" % (sym, trig["time"], trig["price"], lo, hi, px, src))
        orders.append(make_order(sym, qty, limit, stop, target,
                                 "opening-range breakout at %s, backtest 2R avg %+.2fR (fired %d of %d sessions)" % (
                                     trig["time"], w["avg_r"], w["fired"], w["sessions"]),
                                 "orb", trig["time"], c))
    if orders:
        place(orders, st, "orb", now_label or now.strftime("%Y-%m-%d %H:%M"), dry)


# ------------------------------------------------------- manage the day
def bot_orders(status="open"):
    return [o for o in api("GET", "/v2/orders?status=%s&nested=true&limit=200&after=%sT00:00:00Z" % (status, today()))
            if str(o.get("client_order_id", "")).startswith(TAG + "-")]


def _unfilled(o):
    return o["status"] in ("new", "accepted", "pending_new") and float(o.get("filled_qty") or 0) == 0


def cancel_unfilled(kind="open", older_than_min=0):
    st = load_state()
    now = datetime.datetime.now(datetime.timezone.utc)
    for o in bot_orders("open"):
        if not o["client_order_id"].startswith("%s-%s-" % (TAG, kind)) or not _unfilled(o):
            continue
        made = datetime.datetime.fromisoformat(o["created_at"][:19]).replace(tzinfo=datetime.timezone.utc)
        if (now - made).total_seconds() / 60 < older_than_min:
            continue
        paper_account()
        api("DELETE", "/v2/orders/" + o["id"])
        log("cancel: %s %s buy not filled, cancelled" % (kind, o["symbol"]))
        for r in st["orders"]:
            if r.get("id") == o["id"]:
                r["status"] = "cancelled"   # frees the slot for trader-risk's trade count
    if kind == "open":
        st["cancelled_unfilled"] = True
    save_state(st)


def flatten():
    st = load_state()
    paper_account()
    syms = {r["symbol"] for r in st["orders"]}
    for o in bot_orders("open"):
        try:
            api("DELETE", "/v2/orders/" + o["id"]); log("flatten: cancelled %s order %s" % (o["symbol"], o["id"]))
        except RuntimeError as e:
            log("flatten: cancel %s failed: %s" % (o["symbol"], e))
    time.sleep(2)
    for p in api("GET", "/v2/positions"):
        if p["symbol"] in syms:
            r = api("DELETE", "/v2/positions/" + p["symbol"])
            log("flatten: closing %s %s shares (order %s)" % (p["symbol"], p["qty"], (r or {}).get("id")))
    st["flat"] = True; save_state(st)


def report():
    st = load_state()
    acct = paper_account()
    d = today()
    fills = api("GET", "/v2/account/activities/FILL?date=%s" % d) or []
    lines = ["# Paper trading %s" % d, "",
             "Account %s (paper). Equity $%s, yesterday $%s, day P&L $%.2f." % (
                 acct_id(acct), acct["equity"], acct["last_equity"],
                 float(acct["equity"]) - float(acct["last_equity"])), "",
             "Intraday watchlist: %s." % (", ".join(w["symbol"] for w in st.get("orb_watch", [])) or "none"), ""]
    if not st["orders"]:
        lines.append("No trade today: nothing passed the backtests, the triggers and the risk check.")
    lines += ["| Stock | How | Qty | Limit | Entry fill | Exit fill | Exit | R | P&L | Entry slippage |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    total, seen = 0.0, set()
    for r in st["orders"]:
        sym, p = r["symbol"], r["plan"]
        if sym in seen:
            continue
        seen.add(sym)
        f = [x for x in fills if x["symbol"] == sym]
        buys = [x for x in f if x["side"] == "buy"]; sells = [x for x in f if x["side"] != "buy"]
        if not buys:
            lines.append("| %s | %s | %s | %.2f | not filled | | | | $0.00 | |" % (sym, r.get("how", ""), r["qty"], p["entry"])); continue
        q = sum(float(x["qty"]) for x in buys)
        ein = sum(float(x["price"]) * float(x["qty"]) for x in buys) / q
        qs = sum(float(x["qty"]) for x in sells)
        eout = sum(float(x["price"]) * float(x["qty"]) for x in sells) / qs if qs else None
        why = "open"
        if eout is not None:
            why = "target" if eout >= p["target"] - 0.01 else ("stop" if eout <= p["stop"] + 0.01 else "15:55 close")
        pnl = (eout - ein) * qs if eout is not None else 0.0
        total += pnl
        R = (eout - ein) / (ein - p["stop"]) if eout is not None and ein > p["stop"] else None
        lines.append("| %s | %s | %g | %.2f | %.2f | %s | %s | %s | $%.2f | %+.2f (%+.3f%%) |" % (
            sym, r.get("how", ""), q, p["entry"], ein, "%.2f" % eout if eout else "-", why,
            "%+.2f" % R if R is not None else "-", pnl, ein - p["entry"], (ein / p["entry"] - 1) * 100))
    lines += ["", "Realized P&L from the bot's trades: **$%.2f**." % total, ""]
    if st["orders"]:
        lines.append("Why each pick: " + "; ".join("%s: %s" % (r["symbol"], r["why"]) for r in st["orders"]))
    lines += ["", "Log: bot/logs/%s.log" % d]
    os.makedirs(REPORT_DIR, exist_ok=True)
    path = os.path.join(REPORT_DIR, d + ".md")
    open(path, "w").write("\n".join(lines) + "\n")
    log("report: wrote %s (P&L $%.2f)" % (path, total))
    st["reported"] = True; save_state(st)


def status():
    acct = paper_account()
    print("account %s (paper) cash $%s equity $%s" % (acct_id(acct), acct["cash"], acct["equity"]))
    print("clock:", api("GET", "/v2/clock"))
    print("positions:", [(p["symbol"], p["qty"], p["unrealized_pl"]) for p in api("GET", "/v2/positions")])
    print("today's state:", json.dumps(load_state(), indent=1)[:2000])


def tick():
    n = now_ny(); t = n.time(); d = today()
    if n.weekday() >= 5:
        return
    st = load_state()
    try:
        if not st.get("trading_day_checked"):
            st["is_trading_day"] = trading_day(d); st["trading_day_checked"] = True; save_state(st)
        if not st["is_trading_day"]:
            return
        c = cfg()
        last_entry, flat_by = hm(c["no_entries_after"]), hm(c["flat_by"])
        if t >= PLAN_FROM and t < LIVE_FROM and not st.get("planned"):
            plan()                                   # also catches a late start before 9:50
        if LIVE_FROM <= t <= last_entry:
            if not st.get("planned"):
                plan(open_trades=False)              # Mac was asleep: watchlist only, no opening buys
            if t >= CANCEL_OPEN_AT and not st.get("cancelled_unfilled"):
                cancel_unfilled("open")
            cancel_unfilled("orb", ORB_CANCEL_MIN)
            live()
        if t >= flat_by and st.get("orders") and not st.get("flat") and t < T(20, 0):
            flatten()
        if t >= REPORT_AT and not st.get("reported") and t < T(23, 59):
            report()
    except SystemExit as e:
        log(str(e))
    except Exception as e:
        log("ERROR %s: %s" % (type(e).__name__, e))


def git_sync(msg):
    """Cloud only (DT_GIT=1): commit and push the bot's files so the next run continues from them."""
    if os.environ.get("DT_GIT") != "1":
        return
    g = lambda *a: subprocess.run(["git"] + list(a), cwd=ROOT, capture_output=True, text=True)
    g("add", "-A")
    if g("diff", "--cached", "--quiet").returncode == 0:
        return
    g("commit", "-q", "-m", msg)
    for _ in range(3):
        if g("pull", "-q", "--rebase").returncode == 0 and g("push", "-q").returncode == 0:
            return
        time.sleep(5)
    log("git: push failed")


def loop():
    """Cloud runner: tick every 5 minutes until --until (ET) or --max-min minutes, saving after each tick.
    --window HH:MM-HH:MM: exit at once unless started inside it (crons for both summer and winter time)."""
    a = sys.argv
    until = hm(a[a.index("--until") + 1]) if "--until" in a else T(16, 15)
    max_min = float(a[a.index("--max-min") + 1]) if "--max-min" in a else 345
    if "--window" in a:
        lo, hi = a[a.index("--window") + 1].split("-")
        if not (hm(lo) <= now_ny().time() < hm(hi)):
            print("outside window %s, nothing to do (%s ET)" % (a[a.index("--window") + 1], now_ny().strftime("%H:%M")))
            return
    start = time.time()
    while now_ny().time() < until and time.time() - start < max_min * 60:
        tick()
        git_sync("bot %s" % now_ny().strftime("%Y-%m-%d %H:%M ET"))
        nxt = 300 - (time.time() % 300) + 20                   # next 5-minute mark + 20 s
        if (time.time() - start + nxt) / 60 >= max_min:
            break
        time.sleep(nxt)
    git_sync("bot %s (end of run)" % now_ny().strftime("%Y-%m-%d %H:%M ET"))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "tick"
    dry = "--dry-run" in sys.argv
    at = sys.argv[sys.argv.index("--now") + 1] if "--now" in sys.argv else None
    {"tick": tick, "status": status, "plan": lambda: plan(dry), "live": lambda: live(dry, at),
     "cancel-unfilled": cancel_unfilled, "flatten": flatten, "report": report, "loop": loop}[cmd]()
