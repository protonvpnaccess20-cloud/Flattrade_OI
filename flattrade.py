"""
Flattrade (Noren / PiConnectTP) client — DATA ONLY. This app never places,
modifies or cancels an order; it only reads quotes and the option chain.

Morning login flow (the user logs in on Flattrade's own page):
  1. GET /login on this app redirects the browser to LOGIN_URL with the
     app's API key. The user signs in on Flattrade's page (password + TOTP
     stay on Flattrade — this app never sees them).
  2. Flattrade redirects back to the redirect URL registered for the API app
     (this app's /callback) with ?request_code=...
  3. This app exchanges the code at GenWebToken for the day session token.

Every endpoint and payload lives in THIS file on purpose: if Flattrade's
first live reply shows a different expectation, the raw reply is surfaced
on the status page and only this file needs a fix.
"""

import hashlib
import json
import os

import httpx

BASE = os.environ.get("FT_BASE_URL", "https://piconnect.flattrade.in/PiConnectTP")
LOGIN_URL = os.environ.get("FT_LOGIN_URL", "https://piconnect.flattrade.in/login")
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


def login_url(api_key: str, return_uri: str) -> str:
    from urllib.parse import quote

    return f"{LOGIN_URL}?api_key={api_key}&return_uri={quote(return_uri, safe='')}"


async def exchange_code(user_id: str, api_key: str, api_secret: str, request_code: str) -> dict:
    """Exchange the one-time request_code for the day session token.

    Returns {"token": ..., "raw": <full reply dict>}.
    Raises FlattradeError with the raw reply when the exchange is refused.
    """
    payload = {"uid": user_id, "request_code": request_code}
    async with _client() as client:
        resp = await client.post(
            f"{BASE}/GenWebToken",
            data={"jData": json.dumps(payload)},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    try:
        body = resp.json()
    except Exception:
        raise FlattradeError(f"GenWebToken HTTP {resp.status_code}: {resp.text[:400]}")
    token = body.get("token") or body.get("susertoken")
    if body.get("stat") == "Ok" and token:
        return {"token": token, "raw": body}
    # Some app registrations expect a checksum on the exchange; retry once
    # with it when a secret is configured.
    if api_secret:
        checksum = hashlib.sha256(f"{api_key}{request_code}{api_secret}".encode()).hexdigest()
        payload2 = {"uid": user_id, "request_code": request_code, "checksum": checksum}
        async with _client() as client:
            resp2 = await client.post(
                f"{BASE}/GenWebToken",
                data={"jData": json.dumps(payload2)},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        try:
            body2 = resp2.json()
        except Exception:
            raise FlattradeError(f"GenWebToken HTTP {resp2.status_code}: {resp2.text[:400]}")
        token2 = body2.get("token") or body2.get("susertoken")
        if body2.get("stat") == "Ok" and token2:
            return {"token": token2, "raw": body2}
        raise FlattradeError(f"GenWebToken refused (checksum retry): {json.dumps(body2)[:400]}")
    raise FlattradeError(f"GenWebToken refused: {json.dumps(body)[:400]}")


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
