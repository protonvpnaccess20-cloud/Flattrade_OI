"""
Pure OI computations — no network, no state. Input: a normalised chain.
Output: the supervisor-facing view (walls, PCR, max pain, direction read).

The direction read is a transparent ruleset over change-in-OI, labelled as
a read — it is evidence for the supervisor, not a signal by itself.
"""


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def normalise_chain(values: list) -> list:
    """Map Flattrade chain rows to {strike, optt, lp, oi} defensively —
    the OI field name varies (oi / openinterest / oiQty) by master."""
    rows = []
    for v in values or []:
        if not isinstance(v, dict):
            continue
        optt = (v.get("optt") or "").upper()
        if optt not in ("CE", "PE"):
            continue
        strike = _num(v.get("strprc"))
        if strike <= 0:
            continue
        oi = v.get("oi", None)
        if oi is None:
            oi = v.get("openinterest", v.get("oiQty", 0))
        rows.append({"strike": strike, "optt": optt, "lp": _num(v.get("lp")), "oi": _num(oi)})
    return rows


def _by_strike(rows: list) -> dict:
    out = {}
    for r in rows:
        s = out.setdefault(r["strike"], {"strike": r["strike"], "ce_oi": 0.0, "pe_oi": 0.0, "ce_lp": 0.0, "pe_lp": 0.0})
        if r["optt"] == "CE":
            s["ce_oi"] = r["oi"]
            s["ce_lp"] = r["lp"]
        else:
            s["pe_oi"] = r["oi"]
            s["pe_lp"] = r["lp"]
    return out


def _max_pain(by_strike: dict) -> float | None:
    strikes = sorted(by_strike)
    if not strikes:
        return None
    best, best_pain = None, None
    for settle in strikes:
        pain = 0.0
        for k in strikes:
            row = by_strike[k]
            if settle > k:
                pain += row["ce_oi"] * (settle - k)
            if settle < k:
                pain += row["pe_oi"] * (k - settle)
        if best_pain is None or pain < best_pain:
            best, best_pain = settle, pain
    return best


def build_view(rows: list, spot: float, prev_rows: list | None, baseline_rows: list | None) -> dict:
    by = _by_strike(rows)
    prev = _by_strike(prev_rows) if prev_rows else {}
    base = _by_strike(baseline_rows) if baseline_rows else {}
    strikes = sorted(by)

    total_ce = sum(r["ce_oi"] for r in by.values())
    total_pe = sum(r["pe_oi"] for r in by.values())
    pcr = round(total_pe / total_ce, 3) if total_ce > 0 else None

    above = [r for r in by.values() if r["strike"] >= spot]
    below = [r for r in by.values() if r["strike"] <= spot]
    resistance = max(above, key=lambda r: r["ce_oi"]) if above else None
    support = max(below, key=lambda r: r["pe_oi"]) if below else None
    top_ce = sorted(by.values(), key=lambda r: r["ce_oi"], reverse=True)[:3]
    top_pe = sorted(by.values(), key=lambda r: r["pe_oi"], reverse=True)[:3]

    def enrich(row):
        if row is None:
            return None
        k = row["strike"]
        p = prev.get(k, {})
        b = base.get(k, {})
        return {
            "strike": k,
            "ce_oi": row["ce_oi"],
            "pe_oi": row["pe_oi"],
            "ce_oi_change_prev": row["ce_oi"] - p.get("ce_oi", row["ce_oi"]) if p else 0.0,
            "pe_oi_change_prev": row["pe_oi"] - p.get("pe_oi", row["pe_oi"]) if p else 0.0,
            "ce_oi_change_day": row["ce_oi"] - b.get("ce_oi", row["ce_oi"]) if b else 0.0,
            "pe_oi_change_day": row["pe_oi"] - b.get("pe_oi", row["pe_oi"]) if b else 0.0,
        }

    view = {
        "spot": spot,
        "strikes": [enrich(by[k]) for k in strikes],
        "resistance": enrich(resistance),
        "support": enrich(support),
        "top_call_oi": [enrich(r) for r in top_ce],
        "top_put_oi": [enrich(r) for r in top_pe],
        "pcr": pcr,
        "max_pain": _max_pain(by),
        "total_ce_oi": total_ce,
        "total_pe_oi": total_pe,
    }
    view["direction_read"] = direction_read(view)
    return view


def direction_read(view: dict) -> dict:
    """Transparent ΔOI read. Call writing at/above spot caps the upside;
    put writing at/below spot defends the downside; unwinding reverses it."""
    res = view.get("resistance") or {}
    sup = view.get("support") or {}
    notes = []
    score = 0  # + bullish, - bearish

    ce_day = res.get("ce_oi_change_day", 0.0)
    pe_day = sup.get("pe_oi_change_day", 0.0)
    if ce_day > 0:
        notes.append(f"Call writing at {res.get('strike')} (+{ce_day:,.0f} OI today) — upside capped there")
        score -= 1
    elif ce_day < 0:
        notes.append(f"Call unwinding at {res.get('strike')} ({ce_day:,.0f} OI today) — resistance weakening")
        score += 1
    if pe_day > 0:
        notes.append(f"Put writing at {sup.get('strike')} (+{pe_day:,.0f} OI today) — downside defended there")
        score += 1
    elif pe_day < 0:
        notes.append(f"Put unwinding at {sup.get('strike')} ({pe_day:,.0f} OI today) — support weakening")
        score -= 1

    pcr = view.get("pcr")
    if pcr is not None:
        if pcr >= 1.1:
            notes.append(f"PCR {pcr} — put-heavy, writers defending downside")
            score += 1
        elif pcr <= 0.7:
            notes.append(f"PCR {pcr} — call-heavy, writers capping upside")
            score -= 1

    if score >= 2:
        label = "bullish — writers defending the downside"
    elif score == 1:
        label = "mildly bullish"
    elif score <= -2:
        label = "bearish — writers capping the upside"
    elif score == -1:
        label = "mildly bearish"
    else:
        label = "range / no clear writer dominance"
    return {"label": label, "score": score, "notes": notes}
