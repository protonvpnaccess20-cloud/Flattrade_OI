"""
Flattrade (Noren / PiConnectTP) client — DATA ONLY. This app never places,
modifies or cancels an order; it only reads quotes and the option chain.

Morning login flow (official docs, pi.flattrade.in/docs — Login Flow):
  1. GET /login on this app redirects the browser to
     https://auth.flattrade.in/?app_key=<api key>. The user signs in there
     with UCC + password + PAN/DOB (credentials stay on Flattrade).
  2. Flattrade redirects to the Redirect URL registered in Wall for the
     API key (this app's /callback) with ?request_code=...
  3. This app exchanges the code at https://authapi.flattrade.in/trade/apitoken
     with api_secret = SHA-256(api key + request_code + api secret). The docs
     state the token is returned only from the registered static IP.

Every endpoint and payload lives in THIS file on purpose: if Flattrade's
first live reply shows a different expectation, the raw reply is surfaced
on the status page and only this file needs a fix.
"""

import hashlib
import json
import os

import httpx

BASE = os.environ.get("FT_BASE_URL", "https://piconnect.flattrade.in/PiConnectAPI")
LOGIN_URL = os.environ.get("FT_LOGIN_URL", "https://auth.flattrade.in/")
AUTH_URL = os.environ.get("FT_AUTH_URL", "https://authapi.flattrade.in/trade/apitoken")
TIMEOUT = httpx.Timeout(20.0)

# Flattrade only accepts API traffic from the account's registered static
# IP. When FT_PROXY_URL is set (the user's static-IP tunnel), EVERY call in
# this module — the login exchange and all data polls — egresses through
# it, so Flattrade sees the registered IP even though the app runs on
# Render. When it is not set (e.g. the app running on the user's own Mac),
# calls go direct. There is deliberately NO silent fallback: if the tunnel
# is set but down, calls fail loudly and the error is surfaced.
PROXY_URL = os.environ.get("FT_PROXY_URL", "").strip()


def _client() -> httpx.AsyncClient:
    if PROXY_URL:
        return httpx.AsyncClient(timeout=TIMEOUT, proxy=PROXY_URL)
    return httpx.AsyncClient(timeout=TIMEOUT)


async def egress_ip() -> str:
    """The public IP Flattrade would see — fetched through the same route
    as the API calls, so the status page can prove it is the registered IP."""
    async with _client() as client:
        resp = await client.get("https://api.ipify.org?format=json")
    return resp.json().get("ip", "")


class FlattradeError(Exception):
    """Carries the raw reply text so the status page can show it verbatim."""


def login_url(api_key: str) -> str:
    # Official docs: the authorization URL carries ONLY the app_key. After
    # login, Flattrade redirects to the Redirect URL registered in Wall for
    # this API key — it does not accept a return address from the link, so
    # that Wall registration must point at this app's /callback.
    return f"{LOGIN_URL}?app_key={api_key}"


async def exchange_code(user_id: str, api_key: str, api_secret: str, request_code: str) -> dict:
    """Exchange the one-time request_code for the day session token.

    Official docs flow: POST JSON {"api_key", "request_code",
    "api_secret": SHA-256(api_key + request_code + api_secret)} to
    AUTH_URL (https://authapi.flattrade.in/trade/apitoken); the reply is
    {"token", "client", "status": "Ok", "emsg"}. The docs state the token
    is returned ONLY when this call originates from the registered static
    IP — the FT_PROXY_URL tunnel provides exactly that.
    Returns {"token": ..., "raw": <full reply dict>}.
    Raises FlattradeError with the raw reply when the exchange is refused.
    """
    hashed = hashlib.sha256(f"{api_key}{request_code}{api_secret}".encode()).hexdigest()
    payload = {"api_key": api_key, "request_code": request_code, "api_secret": hashed}
    async with _client() as client:
        resp = await client.post(AUTH_URL, json=payload)
    try:
        body = resp.json()
    except Exception:
        raise FlattradeError(f"apitoken HTTP {resp.status_code}: {resp.text[:400]}")
    token = body.get("token") or body.get("susertoken")
    if token:
        return {"token": token, "raw": body}
    raise FlattradeError(f"apitoken refused: {json.dumps(body)[:400]}")


async def _post(path: str, user_id: str, token: str, payload: dict) -> dict:
    body = dict(payload)
    body.setdefault("uid", user_id)
    async with _client() as client:
        resp = await client.post(
            f"{BASE}/{path}",
            data={"jData": json.dumps(body), "jKey": token},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    try:
        data = resp.json()
    except Exception:
        raise FlattradeError(f"{path} HTTP {resp.status_code}: {resp.text[:400]}")
    if isinstance(data, dict) and data.get("stat") not in ("Ok", None):
        raise FlattradeError(f"{path} refused: {json.dumps(data)[:400]}")
    return data


async def search_scrip(user_id: str, token: str, exchange: str, searchtext: str) -> list:
    data = await _post("SearchScrip", user_id, token, {"exch": exchange, "stext": searchtext})
    return data.get("values", []) if isinstance(data, dict) else []


async def get_quotes(user_id: str, token: str, exchange: str, exch_token: str) -> dict:
    return await _post("GetQuotes", user_id, token, {"exch": exchange, "token": exch_token})


async def get_option_chain(user_id: str, token: str, exchange: str, tsym: str, strike: float, count: int) -> list:
    """Option chain around `strike` for the underlying future `tsym`.

    Returns the raw `values` list; each row carries tsym / strprc / optt /
    lp and an open-interest field (name varies by master — oidb of names is
    handled in compute.normalise_chain).
    """
    data = await _post(
        "GetOptionChain",
        user_id,
        token,
        {"exch": exchange, "tsym": tsym, "strprc": _fmt_strike(strike), "cnt": str(count)},
    )
    return data.get("values", []) if isinstance(data, dict) else []


def _fmt_strike(strike: float) -> str:
    return str(int(strike)) if float(strike).is_integer() else str(strike)
