#!/usr/bin/env python3
"""
PRAT // TERMINAL — PF data updater
Fetches live quotes + history from TradingView / Yahoo / NSE, then rebuilds PF_Dashboard.html
Run:  python3 ~/Claude/dashboard/pf_update.py
Ask Hermes: "update pf dashboard"
"""
import json, re, math, calendar, csv, gzip, io, shutil, subprocess, urllib.request, urllib.parse, os, sys, datetime, time


# ---- market-hours guard (IST): skip silently outside live session
_ist_now = datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)
if _ist_now.weekday() >= 5:
    print("Market closed (weekend) — skipping"); sys.exit(0)
_t = _ist_now.hour * 60 + _ist_now.minute
if _t < 555 or _t > 935:  # before 09:15 or after 15:35 IST
    print("Outside market hours — skipping"); sys.exit(0)

# Relocatable: when run from _pipeline (GitHub Actions), operate on repo root
if os.path.basename(os.path.dirname(os.path.abspath(__file__))) == "_pipeline":
    DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root
    HERE = os.path.dirname(os.path.abspath(__file__))
    for _f in ("holdings.json", "pf_dashboard_template.html"):
        _target = os.path.join(DIR, _f)
        if not os.path.exists(_target):
            import shutil as _sh
            _sh.copy(os.path.join(HERE, _f), _target)
else:
    HOME = os.path.expanduser("~")
    DIR = os.path.join(HOME, "Claude", "dashboard")
HOLDINGS_FILE = os.path.join(DIR, "holdings.json")
TEMPLATE = os.path.join(DIR, "pf_dashboard_template.html")
OUT_HTML = os.path.join(DIR, "PF_Dashboard.html")
OUT_JSON = os.path.join(DIR, "pf_data.json")
CONSTITUENTS_CACHE = os.path.join(DIR, "nifty50_constituents.json")
CACHE_TTL_DAYS = 7

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

def get(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")

def tv_scan(tickers, cols):
    """TradingView India scanner — batched ≤10 tickers."""
    out = {}
    for i in range(0, len(tickers), 10):
        batch = tickers[i:i+10]
        payload = {"symbols": {"tickers": batch, "query": {"types": []}}, "columns": cols}
        req = urllib.request.Request("https://scanner.tradingview.com/india/scan",
            data=json.dumps(payload).encode(),
            headers={**UA, "Content-Type": "application/json"}, method="POST")
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=25) as r:
                    res = json.loads(r.read())
                for row in res.get("data", []):
                    out[row["s"]] = dict(zip(cols, row["d"]))
                break
            except Exception as e:
                if attempt == 2: print(f"  ! TV scan batch {i} failed: {e}", file=sys.stderr)
                time.sleep(1.5)
    return out

def yahoo_chart(sym, rng="1y", iv="1d"):
    return yahoo_chart_full(sym, rng, iv)[0]

def yahoo_chart_full(sym, rng="1y", iv="1d"):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?range={rng}&interval={iv}"
    try:
        d = json.loads(get(url, timeout=15))
        r = d["chart"]["result"][0]
        meta = r.get("meta", {})
        ts = r.get("timestamp") or []
        cl = r["indicators"]["quote"][0]["close"]
        pts = [[t, round(c, 2)] for t, c in zip(ts, cl) if c is not None]
        return pts, meta
    except Exception:
        return [], {}

def nifty50_symbols():
    """Official NSE Nifty-50 constituent list, cached weekly."""
    if os.path.exists(CONSTITUENTS_CACHE):
        age_days = (time.time() - os.path.getmtime(CONSTITUENTS_CACHE)) / 86400
        if age_days < CACHE_TTL_DAYS:
            with open(CONSTITUENTS_CACHE) as f: return json.load(f)
    syms = []
    try:
        csv = get("https://www.niftyindices.com/IndexConstituent/ind_nifty50List.csv", timeout=15)
        rows = csv.strip().splitlines()[1:]
        syms = [r.split(",")[2].strip() for r in rows if len(r.split(",")) > 2]
    except Exception as e:
        print(f"  ! NSE constituents fetch failed ({e}); falling back to cache/known list", file=sys.stderr)
    if not syms and os.path.exists(CONSTITUENTS_CACHE):
        with open(CONSTITUENTS_CACHE) as f: return json.load(f)
    if not syms:
        syms = ["ADANIENT","ADANIPORTS","APOLLOHOSP","ASIANPAINT","AXISBANK","BAJAJ-AUTO","BAJFINANCE",
                "BAJAJFINSV","BEL","BHARTIARTL","CIPLA","COALINDIA","DRREDDY","EICHERMOT","ETERNAL",
                "GRASIM","HCLTECH","HDFCBANK","HDFCLIFE","HEROMOTOCO","HINDALCO","HINDUNILVR","ICICIBANK",
                "INDUSINDBK","INFY","ITC","JIOFIN","JSWSTEEL","KOTAKBANK","LT","M&M","MARUTI","NESTLEIND",
                "NTPC","ONGC","POWERGRID","RELIANCE","SBILIFE","SBIN","SHRIRAMFIN","SUNPHARMA","TATACONSUM",
                "TATAMOTORS","TATASTEEL","TCS","TECHM","TITAN","TRENT","ULTRACEMCO","WIPRO"]
    with open(CONSTITUENTS_CACHE, "w") as f: json.dump(syms, f)
    return syms

def ist_now():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)

def market_status(now):
    if now.weekday() >= 5: return "CLOSED — weekend"
    t = now.hour * 60 + now.minute
    if 555 <= t <= 930: return "LIVE"
    if t < 555: return "PRE-OPEN"
    return "CLOSED — post-market"

def main():
    now = ist_now()
    stamp = now.strftime("%d %b %Y · %H:%M IST")
    print(f"[{stamp}] updating PF data…")

    with open(HOLDINGS_FILE) as f:
        book = json.load(f)
    holdings = book["holdings"]
    pf_tickers = [h["tv"] for h in holdings]
    index_tickers = ["NSE:NIFTY", "NSE:NIFTY_MICROCAP_250"]

    cols = ["name","close","change","change_abs","volume","market_cap_basic",
            "Perf.W","Perf.1M","Perf.3M","Perf.6M","Perf.YTD","Perf.Y",
            "price_52_week_high","price_52_week_low","RSI","SMA50","SMA200","sector"]
    quotes = tv_scan(pf_tickers + index_tickers, cols)
    if not quotes:
        print("FATAL: no quotes fetched"); sys.exit(1)

    # ---- indices
    def idx(sym):
        q = quotes.get(sym, {})
        close, chg_abs = q.get("close"), q.get("change_abs")
        prev = (close - chg_abs) if (close is not None and chg_abs is not None) else None
        return {"sym": sym, "name": q.get("name") or sym.split(":")[-1], "cmp": close,
                "chg_abs": chg_abs, "chg_pct": q.get("change"), "prev": prev,
                "w52h": q.get("price_52_week_high"), "w52l": q.get("price_52_week_low"),
                "perf": {k: q.get(k) for k in ["Perf.W","Perf.1M","Perf.3M","Perf.6M","Perf.YTD","Perf.Y"]}}
    nifty = idx("NSE:NIFTY")
    microcap = idx("NSE:NIFTY_MICROCAP_250")
    # microcap intraday from Yahoo (TV scanner doesn't carry this index)
    mc_intraday, mc_meta = yahoo_chart_full("NIFTY_MICROCAP250.NS", "1d", "1m")
    if mc_intraday:
        microcap["intraday"] = mc_intraday
        microcap["cmp"] = mc_intraday[-1][1]
    elif mc_meta.get("regularMarketPrice"):
        microcap["cmp"] = mc_meta["regularMarketPrice"]
    mc_prev = mc_meta.get("chartPreviousClose")
    if microcap.get("cmp") and mc_prev:
        microcap["chg_abs"] = round(microcap["cmp"] - mc_prev, 2)
        microcap["chg_pct"] = round((microcap["cmp"] - mc_prev) / mc_prev * 100, 2)

    # ---- holdings
    out_holdings = []
    for h in holdings:
        q = quotes.get(h["tv"], {})
        if not q or q.get("close") is None:
            print(f"  ! no quote for {h['tv']}; keeping last known", file=sys.stderr)
            out_holdings.append({**h, "cmp": None}); continue
        cmp_ = q["close"]; chg_abs = q.get("change_abs") or 0
        prev = cmp_ - chg_abs
        value = h["qty"] * cmp_
        day_pl = h["qty"] * chg_abs
        unrl = value - h["invested"]
        out_holdings.append({**h,
            "cmp": cmp_, "prev": prev, "chg_pct": q.get("change"), "chg_abs": chg_abs,
            "value": round(value), "day_pl": round(day_pl),
            "day_pl_pct": round(day_pl / (value - day_pl) * 100, 2) if (value - day_pl) else 0,
            "unrl": round(unrl), "unrl_pct": round(unrl / h["invested"] * 100, 2) if h["invested"] else 0,
            "volume": q.get("volume"), "mcap": q.get("market_cap_basic"),
            "rsi": q.get("RSI"), "sma50": q.get("SMA50"), "sma200": q.get("SMA200"),
            "w52h": q.get("price_52_week_high"), "w52l": q.get("price_52_week_low"),
            "perf": {k: q.get(k) for k in ["Perf.W","Perf.1M","Perf.3M","Perf.6M","Perf.YTD","Perf.Y"]},
            "sector": q.get("sector")})

    # ---- history: YTD from PF inception (yesterday) — 5m candles, sliced
    inception_dt = (now - datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    inception_ts = calendar.timegm((inception_dt - datetime.timedelta(hours=5, minutes=30)).timetuple())
    raw_nifty = yahoo_chart("^NSEI", "5d", "5m")
    history = {"nifty_intraday": [p for p in raw_nifty if p[0] >= inception_ts]}
    for h in out_holdings:
        if h.get("cmp") is None: continue
        ysym = h["tv"].split(":")[1] + ".NS"
        hist = yahoo_chart(ysym, "5d", "5m")
        h["intraday"] = [p for p in hist if p[0] >= inception_ts] or None

    # ---- NSE small & microcap top gainers (mcap ₹300–5,000 Cr, liquid)
    gainers = []
    try:
        gq = tv_scan([], [])  # warm nothing; direct filter query below
    except Exception:
        pass
    try:
        req = urllib.request.Request("https://scanner.tradingview.com/india/scan",
            data=json.dumps({
                "filter": [
                    {"left": "type", "operation": "equal", "right": "stock"},
                    {"left": "market_cap_basic", "operation": "greater", "right": 3e9},
                    {"left": "market_cap_basic", "operation": "less", "right": 2.5e10},
                    {"left": "volume", "operation": "greater", "right": 100000},
                    {"left": "change", "operation": "greater", "right": 0},
                ],
                "filter2": {"operator": "and"},
                "symbols": {"query": {"types": []}},
                "columns": ["name", "close", "change", "volume", "market_cap_basic", "sector"],
                "sort": {"sortBy": "change", "sortOrder": "desc"},
                "range": [0, 40]}).encode(),
            headers={**UA, "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=25) as r:
            res = json.loads(r.read())
        seen = set()
        for row in res.get("data", []):
            sym, exch = row["d"][0], row["s"].split(":")[0]
            if sym in seen: continue  # dedupe BSE/NSE twins, prefer NSE
            if exch != "NSE": continue
            seen.add(sym)
            gainers.append({"sym": sym, "cmp": row["d"][1], "chg_pct": round(row["d"][2], 2),
                            "mcap": row["d"][4]})
        gainers = gainers[:20]
    except Exception as e:
        print(f"  ! smallcap gainers failed: {e}", file=sys.stderr)

    # ---- portfolio aggregates
    tv_sum = sum(h["value"] for h in out_holdings if h.get("value"))
    inv_sum = sum(h["invested"] for h in out_holdings)
    day_sum = sum(h.get("day_pl", 0) for h in out_holdings)
    for h in out_holdings:
        h["weight"] = round(h["value"] / tv_sum * 100, 2) if tv_sum and h.get("value") else 0

    data = {
        "meta": {"generated": stamp, "generated_iso": now.isoformat(),
                 "market_status": market_status(now),
                 "book_name": book.get("book_name", "Core PF"),
                 "sources": "TradingView · Yahoo Finance · NSE Indices",
                 "excluded": "BracePort · Reliance · RE · Bhagyanagar — excluded per instruction"},
        "indices": {"nifty": nifty, "microcap": microcap},
        "holdings": out_holdings,
        "totals": {"value": tv_sum, "invested": inv_sum, "day_pl": round(day_sum),
                   "day_pl_pct": round(day_sum / (tv_sum - day_sum) * 100, 2) if (tv_sum - day_sum) else 0,
                   "unrl": tv_sum - inv_sum, "unrl_pct": round((tv_sum - inv_sum) / inv_sum * 100, 2)},
        "gainers": gainers,
        "history": history,
    }
    with open(OUT_JSON, "w") as f:
        json.dump(data, f, indent=1)
    print(f"  ✓ data → {OUT_JSON}")

    # ---- server-side render: bake every number/row/chart into static HTML
    html = render(data)
    with open(OUT_HTML, "w") as f:
        f.write(html)
    print(f"  ✓ dashboard → {OUT_HTML}")

    # ---- auto-deploy to GitHub Pages (prateekdivineautotech-ui/pf1-dashboard)
    # anti-cache strategy: unique stamped filename per build + instant redirector as index.html.
    # The stamped file is NEVER seen before by any browser => no stale cache possible.
    try:
        in_ci = os.path.basename(os.path.dirname(os.path.abspath(__file__))) == "_pipeline"
        deploy_dir = DIR if in_ci else os.path.join(DIR, "deploy")
        ts = now.strftime("%Y%m%d-%H%M")
        stamped = f"d-{ts}.html"
        shutil.copy(OUT_HTML, os.path.join(deploy_dir, stamped))
        redirector = (
            '<!DOCTYPE html><html><head><meta charset="utf-8">'
            '<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">'
            '<meta http-equiv="Pragma" content="no-cache">'
            '<meta http-equiv="refresh" content="0;url=' + stamped + '">'
            '<link rel="canonical" href="./' + stamped + '">'
            '<title>PRAT // TERMINAL — Core PF</title></head>'
            '<body><p style="font-family:monospace;color:#38bdf8;background:#02040c;'
            'padding:24px">Loading latest PF build…</p>'
            '<script>location.replace("./' + stamped + '");</script></body></html>')
        with open(os.path.join(deploy_dir, "index.html"), "w") as f:
            f.write(redirector)
        # prune stamped files older than 3 days to keep repo small
        cutoff = time.time() - 3 * 86400
        for fn in os.listdir(deploy_dir):
            if fn.startswith("d-") and fn.endswith(".html") and fn != stamped:
                try:
                    dnum = fn[2:16]
                    import calendar as _cal
                    mt = _cal.timegm(datetime.datetime.strptime(dnum, "%Y%m%d-%H%M").timetuple())
                    if mt < cutoff:
                        os.remove(os.path.join(deploy_dir, fn))
                except Exception:
                    pass
        for cmd in (["git", "add", "-A"], ["git", "commit", "-m", f"update {stamp}"],
                    ["git", "push", "-u", "origin", "main", "--quiet"]):
            rc = subprocess.run(cmd, cwd=deploy_dir, capture_output=True, timeout=120)
            if rc.returncode != 0:
                print(f"  ! deploy step failed: {cmd[-1]}: {rc.stderr.decode()[:120]}", file=sys.stderr)
        print("  ✓ deployed to GitHub Pages")
    except Exception as e:
        print(f"  ! deploy failed: {e}", file=sys.stderr)


# ================= SSR RENDERER =================
PAL = ["#7c3aed", "#38bdf8", "#f472b6", "#2dd4bf", "#fbbf24", "#e879f9", "#818cf8", "#f87171"]

def _inr(n):
    if n is None: return "—"
    n = int(round(n)); neg = n < 0; s = str(abs(n))
    last3, rest, out = s[-3:], s[:-3], s[-3:]
    while rest:
        out = rest[-2:] + "," + out; rest = rest[:-2]
    return ("−" if neg else "") + out

def _cr(n): return "—" if n is None else ("−" if n < 0 else "") + f"₹{abs(n)/1e7:.2f} Cr"
def _lakh(n): return "—" if n is None else ("−" if n < 0 else "") + f"₹{abs(n)/1e5:.1f} L"
def _pct(n): return "—" if n is None else ("+" if n >= 0 else "−") + f"{abs(n):.2f}%"
def _sgn(n): return "+" if n >= 0 else "−"
def _cls(n): return "num-pos" if n > 1e-4 else ("num-neg" if n < -1e-4 else "flat")
def _ccls(n): return "up" if n > 1e-4 else ("down" if n < -1e-4 else "flat")

def _spark(pts, w=150, h=34):
    if not pts or len(pts) < 2: return ""
    ys = [p[1] for p in pts]
    mn, mx = min(ys), max(ys); rg = (mx - mn) or 1
    path = " ".join(f"{'M' if i == 0 else 'L'}{i/(len(pts)-1)*w:.1f},{h-3-(y-mn)/rg*(h-6):.1f}"
                    for i, (_, y) in enumerate(pts))
    up = ys[-1] >= ys[0]
    col = "#34d399" if up else "#f87171"
    glow = "rgba(52,211,153,.8)" if up else "rgba(248,113,113,.8)"
    return f'<path d="{path}" fill="none" stroke="{col}" stroke-width="1.6" style="filter:drop-shadow(0 0 3px {glow})"/>'

def render(D):
    N, M, T = D["indices"]["nifty"], D["indices"]["microcap"], D["totals"]
    meta = D["meta"]
    subs = {
        "PF:META_BOOK": meta.get("book_name", "Core PF"),
        "PF:META_STATUS": meta.get("market_status", "—"),
        "PF:META_STAMP": meta.get("generated", "—"),
        "PF:FT_SRC": "SRC: " + meta.get("sources", ""),
        "PF:FT_GEN": "GEN: " + meta.get("generated", ""),
        "PF:HOLDINGS_TAG": f"{len(D['holdings'])} positions · Excluded: {meta.get('excluded','')}",
        # tape
        "PF:TAPE_NIFTY_PX": f"{N['cmp']:,.2f}" if N.get("cmp") else "—",
        "PF:TAPE_NIFTY_CLS": _ccls(N.get("chg_pct", 0)),
        "PF:TAPE_NIFTY_CH": f"{_sgn(N.get('chg_abs', 0) or 0)}{abs(N.get('chg_abs') or 0):.2f} ({_pct(N.get('chg_pct'))})",
        "PF:TAPE_MC_PX": f"{M['cmp']:,.1f}" if M.get("cmp") else "—",
        "PF:TAPE_MC_CLS": _ccls(M.get("chg_pct", 0)),
        "PF:TAPE_MC_CH": _pct(M.get("chg_pct")),
        "PF:TAPE_PF_PX": f"{_sgn(T['day_pl'])}₹{_inr(abs(T['day_pl']))}",
        "PF:TAPE_PF_CLS": _ccls(T["day_pl"]),
        "PF:TAPE_PF_CH": _pct(T["day_pl_pct"]),
        "PF:SPARK_NIFTY": _spark((D["history"].get("nifty_intraday") or D["history"].get("nifty") or [])[-80:]),
        "PF:SPARK_MC": _spark((M.get("intraday") or [])),
        # kpis
        "PF:KPI_VALUE": _cr(T["value"]), "PF:KPI_VALUE_D": "₹" + _inr(T["value"]),
        "PF:KPI_INV": _cr(T["invested"]),
        "PF:KPI_UNRL": f"{_sgn(T['unrl'])}{_cr(abs(T['unrl']))}", "PF:KPI_UNRL_CLS": _cls(T["unrl"]),
        "PF:KPI_UNRL_D": f"{_pct(T['unrl_pct'])} on invested",
        "PF:KPI_DAY": f"{_sgn(T['day_pl'])}{_lakh(abs(T['day_pl']))}", "PF:KPI_DAY_CLS": _cls(T["day_pl"]),
        "PF:KPI_DAY_D": f"{_pct(T['day_pl_pct'])} on open",
    }
    alpha = (T["day_pl_pct"] - N["chg_pct"]) if (T.get("day_pl_pct") is not None and N.get("chg_pct") is not None) else None
    subs["PF:KPI_ALPHA"] = "—" if alpha is None else f"{_sgn(alpha)}{abs(alpha):.2f} ppt"
    subs["PF:KPI_ALPHA_CLS"] = _cls(alpha or 0)

    # ---- chartsmaze RS ratings (static dataset, refreshed daily by chartsmaze)
    rs_map = {}
    try:
        # discover current bundle hash (chartsmaze rotates it on each deploy)
        page = get("https://chartsmaze.com/custom-scanner", timeout=20)
        mb = re.search(r'src="/static/js/(main\.[a-f0-9]+\.js)"', page)
        bundle = mb.group(1) if mb else "main.60b1499a.js"
        js_src = get(f"https://chartsmaze.com/static/js/{bundle}", timeout=30)
        mh = re.search(r'static/media/RS filter\.([a-f0-9]+)\.gz', js_src)
        if not mh:
            raise ValueError("RS filter asset not found in bundle")
        req = urllib.request.Request(
            f"https://chartsmaze.com/static/media/RS%20filter.{mh.group(1)}.gz",
            headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
        data = gzip.decompress(raw).decode("utf-8", "ignore")
        for r in csv.DictReader(io.StringIO(data)):
            rs_map[r["Stock Name"]] = r["RS Rating"]
        print(f"  ✓ chartsmaze RS: {len(rs_map)} stocks")
    except Exception as e:
        print(f"  ! chartsmaze RS fetch failed: {e}", file=sys.stderr)

    # holdings rows — name cell = company name only; second col = chartsmaze RS
    rows = []
    for h in D["holdings"]:
        if h.get("cmp") is None: continue
        sym = h["tv"].split(":")[1]
        rs = rs_map.get(sym)
        rs_disp = rs if rs is not None else "—"
        rs_cls = ""
        if rs is not None:
            v = float(rs)
            rs_cls = "num-pos" if v >= 70 else ("flat" if v >= 50 else "num-neg")
        rows.append(
            f'<tr><td class="sticky-col"><div class="tname"><span class="n">{h["name"]}</span></div></td>'
            f'<td class="{rs_cls}" style="font-weight:700">{rs_disp if rs_disp else "NA"}</td>'
            f'<td>{_inr(h["qty"])}</td>'
            f'<td>{h["avg"]:,.2f}</td><td>{h["cmp"]:,.2f}</td>'
            f'<td class="{_cls(h.get("chg_pct") or 0)}">{_pct(h.get("chg_pct"))}</td>'
            f'<td class="{_cls(h["day_pl"])}">{_sgn(h["day_pl"])}{_inr(abs(h["day_pl"]))}</td>'
            f'<td class="{_cls(h["unrl"])}">{_sgn(h["unrl"])}{_inr(abs(h["unrl"]))}</td>'
            f'<td class="{_cls(h["unrl"])}">{_pct(h["unrl_pct"])}</td>'
            f'<td>{_inr(h["value"])}</td>'
            f'<td><span class="wbar"><div style="width:{min(100,h["weight"]):.0f}%"></div></span> {h["weight"]:.1f}</td></tr>')
    subs["PF:TBODY"] = "".join(rows)
    subs["PF:TFOOT"] = (
        f'<tr><td class="sticky-col">TOTAL</td><td></td><td></td><td></td>'
        f'<td class="{_cls(T["day_pl_pct"])}">{_pct(T["day_pl_pct"])}</td>'
        f'<td class="{_cls(T["day_pl"])}">{_sgn(T["day_pl"])}{_inr(abs(T["day_pl"]))}</td>'
        f'<td class="{_cls(T["unrl"])}">{_sgn(T["unrl"])}{_inr(abs(T["unrl"]))}</td>'
        f'<td class="{_cls(T["unrl"])}">{_pct(T["unrl_pct"])}</td>'
        f'<td>{_inr(T["value"])}</td><td>100.0</td></tr>')

    # gainers
    if D["gainers"]:
        subs["PF:GAINERS"] = "".join(
            f'<div class="g-item{" top" if i == 0 else ""}"><span class="rk">{i+1}</span>'
            f'<span class="nm"><span class="sym">{g["sym"]}</span></span>'
            f'<span class="px">{g["cmp"]:,.2f}</span>'
            f'<span class="pc {_ccls(g["chg_pct"])}">{_pct(g["chg_pct"])}</span></div>'
            for i, g in enumerate(D["gainers"]))
    else:
        subs["PF:GAINERS"] = '<div class="micro-note">no gainers data this run</div>'

    # contribution bars
    mx = max([abs(h.get("day_pl") or 0) for h in D["holdings"]] + [1])
    subs["PF:CONTRIB"] = "".join(
        f'<div class="pb"><span class="lbl">{h["tv"].split(":")[1]}</span>'
        f'<span class="trk"><span class="fll {"pos" if h["day_pl"] >= 0 else "neg"}" style="width:{max(2, abs(h["day_pl"] or 0)/mx*100):.0f}%"></span></span>'
        f'<span class="val {_cls(h["day_pl"])}">{_sgn(h["day_pl"])}{_inr(abs(h["day_pl"]))}</span></div>'
        for h in D["holdings"])

    # donut + legend
    total = T["value"] or 1; acc = 0; segs = ""
    for i, h in enumerate(D["holdings"]):
        frac = (h.get("value") or 0) / total
        a0 = acc * 2 * math.pi - math.pi / 2; a1 = (acc + frac) * 2 * math.pi - math.pi / 2
        acc += frac
        large = 1 if frac > 0.5 else 0
        p0 = (100 + 80 * math.cos(a0), 100 + 80 * math.sin(a0))
        p1 = (100 + 80 * math.cos(a1), 100 + 80 * math.sin(a1))
        segs += (f'<path d="M{p0[0]:.2f},{p0[1]:.2f} A80,80 0 {large} 1 {p1[0]:.2f},{p1[1]:.2f}" '
                 f'stroke="{PAL[i % len(PAL)]}" stroke-width="26" fill="none" opacity="0.95"/>')
    legend = "".join(
        f'<div class="lg"><span class="sw" style="background:{PAL[i % len(PAL)]}"></span>'
        f'<span class="lgn">{h["name"]}</span><span class="pct">{h["weight"]:.1f}%</span></div>'
        for i, h in enumerate(D["holdings"]))
    subs["PF:DONUT"] = (
        f'<svg width="200" height="200" viewBox="0 0 200 200">'
        f'<circle cx="100" cy="100" r="80" stroke="rgba(56,189,248,.1)" stroke-width="26" fill="none"/>{segs}'
        f'<text x="100" y="95" text-anchor="middle" fill="#7c8db5" font-size="10" letter-spacing="2">BOOK</text>'
        f'<text x="100" y="115" text-anchor="middle" fill="#f1f5f9" font-size="16" font-weight="700" font-family="monospace">{_cr(total)}</text></svg>'
        f'<div class="legend">{legend}</div>')

    # intraday PF vs Nifty (from today's open, rebased 100) — PF inception = today
    def dmap_intraday(arr):
        return [(t, c) for t, c in (arr or [])]
    hcurves = [h["intraday"] for h in D["holdings"] if h.get("intraday") and len(h["intraday"]) > 2]
    nifty_in = D["history"].get("nifty_intraday") or []
    line_html, line_note = '<div class="micro-note">intraday history unavailable this run</div>', ""
    if nifty_in and len(nifty_in) > 2:
        # align by nearest timestamp: build PF curve from holdings' 5m closes
        def ts_close(arr):
            return {t: c for t, c in arr}
        # use Nifty timestamps as the common axis; for each, take latest available holding price
        times = [t for t, _ in nifty_in]
        nmap = ts_map = {t: c for t, c in nifty_in}
        pf_series, n_series = [], []
        hmaps = [ts_map_h for ts_map_h in ({t: c for t, c in h} for h in hcurves)]
        for t, _ in nifty_in:
            v, ok = 0.0, True
            for hm in hmaps:
                # latest holding close at or before t (within 30 min)
                best, bts = None, t - 1800
                for ht, hc in hm.items():
                    if bts <= ht <= t: best = hc
                if best is None: ok = False; break
                v += best  # value proxy = sum of prices (per-unit), rebasing removes scale
            if ok:
                pf_series.append((t, v)); n_series.append((t, nmap[t]))
        if len(pf_series) > 5:
            bP, bN = pf_series[0][1], n_series[0][1]
            pfI = [(i, v / bP * 100) for i, (_, v) in enumerate(pf_series)]
            nI = [(i, c / bN * 100) for i, (_, c) in enumerate(n_series)]
            W, H, P = 640, 300, {"l": 46, "r": 14, "t": 14, "b": 26}
            allv = [p[1] for p in pfI + nI]; mn, mxv = min(allv), max(allv)
            X = lambda i: P["l"] + i / (len(pfI) - 1) * (W - P["l"] - P["r"])
            Y = lambda v: P["t"] + (1 - (v - mn) / ((mxv - mn) or 1)) * (H - P["t"] - P["b"])
            line = lambda pts: " ".join(f"{'M' if i == 0 else 'L'}{X(p[0]):.1f},{Y(p[1]):.1f}" for i, p in enumerate(pts))
            grid = "".join(
                f'<line x1="{P["l"]}" y1="{Y(mn + (mxv-mn)*k/4):.1f}" x2="{W-P["r"]}" y2="{Y(mn + (mxv-mn)*k/4):.1f}" stroke="rgba(56,189,248,.09)"/>'
                f'<text x="{P["l"]-6}" y="{Y(mn + (mxv-mn)*k/4)+4:.1f}" text-anchor="end" fill="#4a5a80" font-size="10" font-family="monospace">{mn + (mxv-mn)*k/4:.2f}</text>'
                for k in range(5))
            def _hm(t):
                d = datetime.datetime.utcfromtimestamp(t + 19800)
                return d.strftime("%d %b") if (d.hour == 9 and d.minute < 20) else d.strftime("%H:%M")
            xlab = "".join(
                f'<text x="{X(i):.1f}" y="{H-8}" text-anchor="middle" fill="#4a5a80" font-size="10" font-family="monospace">{_hm(pf_series[i][0])}</text>'
                for i in (0, len(pf_series) // 2, len(pf_series) - 1))
            pf_end, n_end = pfI[-1][1] - 100, nI[-1][1] - 100
            line_html = (
                f'<svg width="100%" height="300" viewBox="0 0 {W} {H}">{grid}{xlab}'
                f'<path d="{line(nI)}" fill="none" stroke="#7c8db5" stroke-width="1.6" stroke-dasharray="5 4"/>'
                f'<path d="{line(pfI)}" fill="none" stroke="#38bdf8" stroke-width="2.4" style="filter:drop-shadow(0 0 5px rgba(56,189,248,.7))"/>'
                f'<text x="{W-P["r"]}" y="20" text-anchor="end" fill="#38bdf8" font-size="12" font-family="monospace">PF {pf_end:+.2f}%</text>'
                f'<text x="{W-P["r"]}" y="36" text-anchor="end" fill="#7c8db5" font-size="12" font-family="monospace">NIFTY {n_end:+.2f}%</text></svg>')
            if len(hcurves) < len([h for h in D["holdings"] if h.get("cmp") is not None]):
                line_note = '<div class="micro-note">PF curve excludes holdings without intraday history (Vivid — SME)</div>'
    subs["PF:LINECHART"] = line_html
    subs["PF:LINE_NOTE"] = line_note

    # 52-week range dials
    subs["PF:RANGE"] = "".join(
        f'<div style="margin-bottom:14px">'
        f'<div style="display:flex;justify-content:space-between;font-size:12px;font-family:monospace;margin-bottom:4px">'
        f'<span style="color:#7c8db5">{h["tv"].split(":")[1]}</span>'
        f'<span style="color:{col}">{pos:.0f}% of range</span></div>'
        f'<div style="position:relative;height:10px;background:linear-gradient(90deg,rgba(248,113,113,.25),rgba(251,191,36,.25),rgba(52,211,153,.25));border-radius:5px">'
        f'<div style="position:absolute;left:{pos:.0f}%;top:-4px;width:4px;height:18px;background:{col};border-radius:2px;box-shadow:0 0 8px {col}"></div></div>'
        f'<div style="display:flex;justify-content:space-between;font-size:10.5px;color:#4a5a80;font-family:monospace;margin-top:3px">'
        f'<span>L ₹{h["w52l"]:,.0f}</span><span>H ₹{h["w52h"]:,.0f}</span></div></div>'
        for h in D["holdings"] if h.get("w52h") and h.get("w52l") and h.get("cmp")
        for pos in [max(0, min(100, (h["cmp"] - h["w52l"]) / ((h["w52h"] - h["w52l"]) or 1) * 100))]
        for col in ["#34d399" if pos > 66 else ("#fbbf24" if pos > 33 else "#f87171")])

    html = open(TEMPLATE).read()
    for k, v in subs.items():
        html = html.replace(f"<!--{k}-->", str(v))
    # remove the fallback dashes that follow replaced placeholders (they render as stray "—")
    html = re.sub(r"(?<!--)(?:</span>|</div>|</svg>|</td>)?—(</span>|</div>|</td>)", r"\1", html)
    leftovers = re.findall(r"<!--PF:[A-Z_]+-->", html)
    if leftovers:
        print(f"  ! unreplaced placeholders: {sorted(set(leftovers))}", file=sys.stderr)
    return html

if __name__ == "__main__":
    main()