"""
Flattrade OI App — DATA ONLY (never places an order).

Runs free on Render. The user logs in with Flattrade each morning (on
Flattrade's own page); the app then polls the NIFTY (and SENSEX, if the
account's masters allow) option chain every few minutes during market
hours and serves the OI picture — support/resistance walls from strike
OI, change in OI, PCR, max pain — as a status page + JSON for the
AI supervisor in the Nifty Option Buying Assistant.

Render free services sleep when idle: reads via /api/oi fetch fresh
data on demand when the last snapshot is stale, so the supervisor's
own polling keeps the data warm through the session.
"""

import asyncio
import html
import json
import os
import re
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import compute
import flattrade
import oidb

IST = ZoneInfo("Asia/Kolkata")

FT_API_KEY = os.environ.get("FT_API_KEY", "")
FT_API_SECRET = os.environ.get("FT_API_SECRET", "")
FT_USER_ID = os.environ.get("FT_USER_ID", "")
APP_BASE_URL = os.environ.get("APP_BASE_URL", "").rstrip("/")
APP_PASSCODE = os.environ.get("APP_PASSCODE", "")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "180"))
STUB = os.environ.get("FT_STUB", "") == "1"  # local smoke tests ONLY — never set on Render

SYMBOLS = {
    "NIFTY": {"chain_exch": "NFO", "search_exch": "NFO", "index_exch": "NSE", "index_token": "26000", "step": 50},
    "SENSEX": {"chain_exch": "BFO", "search_exch": "BFO", "index_exch": "BSE", "index_token": "1", "step": 100},
}

app = FastAPI(title="Flattrade OI App")
_fetch_lock = asyncio.Lock()


def now_ist() -> datetime:
    return datetime.now(IST)


def today_str() -> str:
    return now_ist().strftime("%Y-%m-%d")


def in_market_window() -> bool:
    n = now_ist()
    if n.weekday() >= 5:
        return False
    return dtime(9, 15) <= n.time() <= dtime(15, 35)


def _session():
    if STUB:
        return {"token": "STUB", "updated_at": now_ist().isoformat()}
    return oidb.get_session(today_str())


# ---------------------------------------------------------------- stub data
def _stub_chain(symbol: str):
    spot = 22850.0 if symbol == "NIFTY" else 73800.0
    step = SYMBOLS[symbol]["step"]
    rows = []
    for i in range(-25, 26):
        k = spot + i * step
        ce_oi = 1_000_000 + (900_000 if k == spot + 3 * step else 0) + abs(i) * 10_000
        pe_oi = 1_000_000 + (850_000 if k == spot - 3 * step else 0) + abs(i) * 10_000
        rows.append({"strike": k, "optt": "CE", "lp": 100.0, "oi": ce_oi})
        rows.append({"strike": k, "optt": "PE", "lp": 100.0, "oi": pe_oi})
    return spot, rows, "STUB-EXPIRY"


# ---------------------------------------------------------------- fetching
_MONTHS = {m: i + 1 for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}


def _fut_expiry(tsym: str, symbol: str):
    m = re.match(rf"^{symbol}(\d{{2}})([A-Z]{{3}})(\d{{2}})F$", tsym or "")
    if not m:
        return None
    dd, mon, yy = int(m.group(1)), _MONTHS.get(m.group(2)), 2000 + int(m.group(3))
    if not mon:
        return None
    try:
        return datetime(yy, mon, dd).date()
    except ValueError:
        return None


async def _resolve_future(symbol: str, cfg: dict, token: str):
    values = await flattrade.search_scrip(FT_USER_ID, token, cfg["search_exch"], symbol)
    today = now_ist().date()
    best = None
    for v in values:
        tsym = v.get("tsym", "")
        exp = _fut_expiry(tsym, symbol)
        if exp is None or exp < today:
            continue
        if best is None or exp < best[1]:
            best = (tsym, exp)
    if best is None:
        raise flattrade.FlattradeError(f"No live {symbol} future found in SearchScrip reply ({len(values)} rows)")
    return best  # (tsym, expiry date)


async def fetch_symbol(symbol: str) -> dict:
    """Fetch one chain snapshot, compute the view, store it. Returns status dict."""
    cfg = SYMBOLS[symbol]
    sess = _session()
    if not sess:
        return {"symbol": symbol, "status": "no_session"}
    token = sess["token"]
    try:
        if STUB:
            spot, rows, expiry = _stub_chain(symbol)
        else:
            fut_tsym, expiry_date = await _resolve_future(symbol, cfg, token)
            expiry = expiry_date.isoformat()
            spot = 0.0
            try:
                q = await flattrade.get_quotes(FT_USER_ID, token, cfg["index_exch"], cfg["index_token"])
                spot = float(q.get("lp") or 0)
            except Exception:
                spot = 0.0
            if spot <= 0:
                q = await flattrade.get_quotes(FT_USER_ID, token, cfg["chain_exch"], fut_tsym)
                spot = float(q.get("lp") or 0)
            if spot <= 0:
                raise flattrade.FlattradeError(f"Could not read {symbol} spot from Flattrade quotes")
            atm = round(spot / cfg["step"]) * cfg["step"]
            raw = await flattrade.get_option_chain(FT_USER_ID, token, cfg["chain_exch"], fut_tsym, atm, 25)
            rows = compute.normalise_chain(raw)
            if not rows:
                raise flattrade.FlattradeError(f"Option chain reply had no usable rows ({len(raw)} raw rows)")

        snaps = oidb.last_snapshots(today_str(), symbol, 1)
        prev_rows = snaps[0]["payload"].get("rows") if snaps else None
        base = oidb.day_baseline(today_str(), symbol)
        base_rows = base["payload"].get("rows") if base else None
        view = compute.build_view(rows, spot, prev_rows, base_rows)
        payload = {"rows": rows, "view": view, "expiry": expiry, "fetched_at": now_ist().isoformat()}
        oidb.add_snapshot(today_str(), now_ist().isoformat(), symbol, payload)
        oidb.set_state(f"error_{symbol}", "")
        return {"symbol": symbol, "status": "ok", "asOf": payload["fetched_at"], "expiry": expiry, **view}
    except Exception as exc:  # surface raw, keep serving last good data
        oidb.set_state(f"error_{symbol}", f"{now_ist().isoformat()} {exc}")
        return {"symbol": symbol, "status": "error", "error": str(exc)}


def _stored_view(symbol: str):
    snaps = oidb.last_snapshots(today_str(), symbol, 1)
    if not snaps:
        return None
    p = snaps[0]["payload"]
    return {"symbol": symbol, "status": "ok", "asOf": p.get("fetched_at"), "expiry": p.get("expiry"), **p.get("view", {})}


def _is_stale(symbol: str) -> bool:
    snaps = oidb.last_snapshots(today_str(), symbol, 1)
    if not snaps:
        return True
    try:
        last = datetime.fromisoformat(snaps[0]["payload"].get("fetched_at"))
    except Exception:
        return True
    return (now_ist() - last).total_seconds() > POLL_SECONDS


async def ensure_fresh(symbol: str):
    if _session() and _is_stale(symbol) and (in_market_window() or not oidb.last_snapshots(today_str(), symbol, 1)):
        async with _fetch_lock:
            if _is_stale(symbol):
                await fetch_symbol(symbol)


async def poller():
    while True:
        try:
            if in_market_window() and _session():
                for symbol in SYMBOLS:
                    if _is_stale(symbol):
                        await fetch_symbol(symbol)
        except Exception as exc:
            print(f"[poller] {exc}")
        await asyncio.sleep(20)


@app.on_event("startup")
async def _startup():
    asyncio.create_task(poller())


# ---------------------------------------------------------------- routes
@app.get("/health")
async def health():
    return {"ok": True, "storage": oidb.storage_backend(), "session_today": bool(_session()), "stub": STUB}


@app.get("/api/egress")
async def api_egress():
    """Shows the public IP Flattrade will see (through the tunnel when set),
    so it can be checked against the registered static IP before login."""
    try:
        ip = await flattrade.egress_ip()
        return {"proxy_set": bool(flattrade.PROXY_URL), "egress_ip": ip}
    except Exception as exc:
        return {"proxy_set": bool(flattrade.PROXY_URL), "error": str(exc)}


@app.get("/login")
async def login(request: Request):
    if APP_PASSCODE and request.cookies.get("oi_pass") != APP_PASSCODE:
        return HTMLResponse(
            "<h3>Flattrade OI App</h3><form method='post' action='/unlock'>"
            "Passcode: <input type='password' name='pass'> <button type='submit'>Continue</button></form>"
        )
    if not FT_API_KEY:
        return HTMLResponse("<h3>FT_API_KEY is not set on the server yet.</h3>", status_code=500)
    return RedirectResponse(flattrade.login_url(FT_API_KEY))


@app.post("/unlock")
async def unlock(request: Request):
    form = await request.form()
    resp = RedirectResponse("/login", status_code=303)
    if APP_PASSCODE and form.get("pass") == APP_PASSCODE:
        resp.set_cookie("oi_pass", APP_PASSCODE, max_age=86400 * 30, httponly=True)
    return resp


@app.get("/callback")
async def callback(request: Request):
    code = request.query_params.get("request_code") or request.query_params.get("code") or ""
    if not code:
        return HTMLResponse("<h3>No request_code came back from Flattrade.</h3>", status_code=400)
    try:
        if STUB:
            oidb.save_session(today_str(), "STUB", now_ist().isoformat())
        else:
            out = await flattrade.exchange_code(FT_USER_ID, FT_API_KEY, FT_API_SECRET, code)
            oidb.save_session(today_str(), out["token"], now_ist().isoformat())
        oidb.set_state("error_login", "")
        for symbol in SYMBOLS:
            await fetch_symbol(symbol)
        return RedirectResponse("/")
    except Exception as exc:
        oidb.set_state("error_login", f"{now_ist().isoformat()} {exc}")
        return HTMLResponse(
            f"<h3>Flattrade login exchange failed</h3><pre>{html.escape(str(exc))}</pre>"
            "<p>This exact reply is what to fix from — send it to Muse.</p><p><a href='/'>Back</a></p>",
            status_code=502,
        )


@app.get("/api/oi")
async def api_oi():
    out = {"session": bool(_session()), "storage": oidb.storage_backend(), "asOf": now_ist().isoformat(), "symbols": {}}
    for symbol in SYMBOLS:
        await ensure_fresh(symbol)
        view = _stored_view(symbol)
        err = oidb.get_state(f"error_{symbol}") or ""
        out["symbols"][symbol] = view or {"symbol": symbol, "status": "no_data", "error": err}
        if view and err:
            out["symbols"][symbol]["last_error"] = err
    return JSONResponse(out)


@app.get("/api/oi/{symbol}")
async def api_oi_symbol(symbol: str):
    symbol = symbol.upper()
    if symbol not in SYMBOLS:
        return JSONResponse({"error": "unknown symbol"}, status_code=404)
    await ensure_fresh(symbol)
    view = _stored_view(symbol)
    return JSONResponse(view or {"symbol": symbol, "status": "no_data", "error": oidb.get_state(f"error_{symbol}") or ""})


@app.get("/", response_class=HTMLResponse)
async def home():
    sess = _session()
    parts = ["<html><head><meta charset='utf-8'><meta http-equiv='refresh' content='60'>"
             "<title>Flattrade OI App</title><style>body{font-family:system-ui;margin:24px;max-width:860px}"
             "table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:4px 8px}"
             ".err{background:#ffe9e9;padding:8px;white-space:pre-wrap}.ok{background:#e9f7e9;padding:8px}</style></head><body>"]
    parts.append("<h2>Flattrade OI App — data only</h2>")
    if sess:
        parts.append(f"<div class='ok'>Logged in today at {html.escape(sess['updated_at'])} · storage: {oidb.storage_backend()}</div>")
    else:
        parts.append("<div class='err'>Not logged in today. <a href='/login'><b>Login with Flattrade</b></a></div>")
    login_err = oidb.get_state("error_login") or ""
    if login_err:
        parts.append(f"<div class='err'>Last login error: {html.escape(login_err)}</div>")
    tunnel = (
        "SET — the login exchange and every data poll go through your static-IP tunnel"
        if flattrade.PROXY_URL
        else "NOT SET — calls go direct (right for the Mac, wrong for Render)"
    )
    parts.append(f"<p><b>Static-IP tunnel:</b> {tunnel} · <a href='/api/egress'>check egress IP</a> (must match your registered static IP before you log in)</p>")
    for symbol in SYMBOLS:
        view = _stored_view(symbol)
        err = oidb.get_state(f"error_{symbol}") or ""
        parts.append(f"<h3>{symbol}</h3>")
        if err:
            parts.append(f"<div class='err'>{html.escape(err)}</div>")
        if not view:
            parts.append("<p>No snapshot yet today.</p>")
            continue
        res = view.get("resistance") or {}
        sup = view.get("support") or {}
        dr = view.get("direction_read") or {}
        parts.append(
            f"<p>As of {html.escape(str(view.get('asOf')))} · spot {view.get('spot')} · expiry {html.escape(str(view.get('expiry')))} · "
            f"PCR {view.get('pcr')} · max pain {view.get('max_pain')}</p>"
            f"<table><tr><th></th><th>Strike</th><th>OI</th><th>ΔOI today</th></tr>"
            f"<tr><td>Resistance (max call OI)</td><td>{res.get('strike')}</td><td>{res.get('ce_oi')}</td><td>{res.get('ce_oi_change_day')}</td></tr>"
            f"<tr><td>Support (max put OI)</td><td>{sup.get('strike')}</td><td>{sup.get('pe_oi')}</td><td>{sup.get('pe_oi_change_day')}</td></tr></table>"
            f"<p><b>Direction read:</b> {html.escape(str(dr.get('label')))}</p>"
            + "".join(f"<div>• {html.escape(n)}</div>" for n in dr.get("notes", []))
        )
    parts.append("</body></html>")
    return "".join(parts)
