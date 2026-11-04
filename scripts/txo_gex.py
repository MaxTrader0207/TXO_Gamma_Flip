"""
TXO Gamma Flip 每日資料管線
- 從期交所下載 TXO 選擇權每日行情與 TX 期貨行情（Big5 CSV）
- 以 Black-76 反推 IV、計算各到期與合併的 GEX 曲線與 Gamma Flip
- 輸出 data/latest.json（儀表板原始資料）、data/history.json（每日摘要）、data/daily/YYYYMMDD.json

用法：python scripts/txo_gex.py [--days 30]
"""
import argparse
import csv
import io
import json
import math
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests

TPE = timezone(timedelta(hours=8))
OPT_URL = "https://www.taifex.com.tw/cht/3/optDataDown"
FUT_URL = "https://www.taifex.com.tw/cht/3/futDataDown"
MULT, TX_MULT, RATE = 50, 200, 0.015
DATA = os.path.join(os.path.dirname(__file__), "..", "data")
DEBUG = []
UA = {"User-Agent": "Mozilla/5.0 (txo-gamma-flip data job)"}


# ---------------- download ----------------
def download(url, commodity, start, end):
    form = {
        "down_type": "1",
        "commodity_id": commodity,
        "commodity_id2": "",
        "queryStartDate": start.strftime("%Y/%m/%d"),
        "queryEndDate": end.strftime("%Y/%m/%d"),
    }
    for attempt in range(4):
        try:
            r = requests.post(url, data=form, headers=UA, timeout=60)
            DEBUG.append(f"{commodity} {url} status={r.status_code} len={len(r.content)} ctype={r.headers.get('content-type')}")
            r.raise_for_status()
            raw = r.content
            for enc in ("big5", "cp950", "utf-8-sig"):
                try:
                    txt = raw.decode(enc)
                    if "履約價" in txt or "到期月份" in txt:
                        return txt
                except UnicodeDecodeError:
                    continue
            DEBUG.append(raw[:600].decode("utf-8", "replace"))
            return ""  # 無資料（假日）時期交所回傳 HTML 提示
        except Exception as e:  # noqa: BLE001
            print(f"download retry {attempt+1}: {e}", file=sys.stderr)
            time.sleep(5 * (attempt + 1))
    return ""


def download_range(url, commodity, start, end):
    """期交所單次查詢上限約一個月，且當日資料公布前查詢會被拒，分段並自動退回前一日"""
    parts, cur = [], start
    while cur <= end:
        stop = min(cur + timedelta(days=27), end)
        txt = download(url, commodity, cur, stop)
        while not txt and stop > cur:
            stop -= timedelta(days=1)
            txt = download(url, commodity, cur, stop)
        if txt:
            parts.append(txt)
        cur = cur + timedelta(days=28)
    return parts


def read_csv(txt):
    rows = list(csv.reader(io.StringIO(txt)))
    hi = next((i for i, r in enumerate(rows) if any("到期月份" in c for c in r)), None)
    if hi is None:
        return [], []
    head = [c.strip() for c in rows[hi]]
    return head, [[c.strip() for c in r] for r in rows[hi + 1:] if len(r) >= len(head) - 3]


def col(head, *keys, exact=False):
    for i, h in enumerate(head):
        for k in keys:
            if (h == k) if exact else (k in h):
                return i
    return -1


def num(s):
    try:
        return float(str(s).replace(",", ""))
    except ValueError:
        return float("nan")


def ymd(s):
    d = re.sub(r"\D", "", s or "")
    return date(int(d[:4]), int(d[4:6]), int(d[6:8])) if len(d) == 8 else None


# ---------------- dates ----------------
def nth_weekday(y, m, wd, n):  # wd: Mon=0
    d = date(y, m, 1)
    return d + timedelta(days=(wd - d.weekday()) % 7 + 7 * (n - 1))


def expiry_from_code(code):
    m = re.match(r"^(\d{4})(\d{2})(?:([WF])(\d))?$", code)
    if not m:
        return None
    y, mo = int(m[1]), int(m[2])
    if not m[3]:
        return nth_weekday(y, mo, 2, 3)
    return nth_weekday(y, mo, 2 if m[3] == "W" else 4, int(m[4]))


def group_of(code):
    return "F" if re.search(r"F\d$", code) else "W" if re.search(r"W\d$", code) else "M"


# ---------------- parse ----------------
def parse_options(txt):
    head, rows = read_csv(txt)
    if not head:
        return {}
    c = dict(
        d=col(head, "交易日期"), prod=col(head, "契約", "商品代號", exact=True), mon=col(head, "到期月份"),
        exp=col(head, "契約到期日"), k=col(head, "履約價"), cp=col(head, "買賣權"),
        settle=col(head, "結算價"), close=col(head, "收盤價"), oi=col(head, "未沖銷"), sess=col(head, "交易時段"),
    )
    out = {}
    for r in rows:
        if c["prod"] >= 0 and "TXO" not in r[c["prod"]].upper():
            continue
        if c["sess"] >= 0 and r[c["sess"]] and "一般" not in r[c["sess"]]:
            continue
        code = re.sub(r"\s", "", r[c["mon"]])
        if not re.match(r"^\d{6}([WF]\d)?$", code):
            continue
        k, oi = num(r[c["k"]]), num(r[c["oi"]])
        if not (k > 0) or not (oi > 0):
            continue
        px = num(r[c["settle"]])
        if not (px > 0) and c["close"] >= 0:
            px = num(r[c["close"]])
        exp = ymd(r[c["exp"]]) if c["exp"] >= 0 else None
        exp = exp or expiry_from_code(code)
        td = ymd(r[c["d"]])
        if not exp or not td:
            continue
        out.setdefault(td, []).append(
            [code, exp.isoformat(), k, "C" if ("買" in r[c["cp"]] or "call" in r[c["cp"]].lower()) else "P",
             round(px, 2) if px > 0 else None, int(oi)]
        )
    return out


def parse_futures(txt):
    head, rows = read_csv(txt)
    if not head:
        return {}
    c = dict(d=col(head, "交易日期"), prod=col(head, "契約", "商品代號", exact=True), mon=col(head, "到期月份"),
             settle=col(head, "結算價"), close=col(head, "收盤價"), sess=col(head, "交易時段"))
    best = {}
    for r in rows:
        if c["prod"] >= 0 and r[c["prod"]].upper() != "TX":
            continue
        if c["sess"] >= 0 and r[c["sess"]] and "一般" not in r[c["sess"]]:
            continue
        code = re.sub(r"\s", "", r[c["mon"]])
        if not re.match(r"^\d{6}$", code):  # 只取月契約，排除價差
            continue
        px = num(r[c["settle"]])
        if not (px > 0):
            px = num(r[c["close"]])
        td = ymd(r[c["d"]])
        if td and px > 0 and (td not in best or code < best[td][0]):
            best[td] = (code, px)
    return best


# ---------------- math ----------------
def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def b76(F, K, T, s, call):
    sq = s * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * s * s * T) / sq
    d2 = d1 - sq
    df = math.exp(-RATE * T)
    return df * (F * ncdf(d1) - K * ncdf(d2)) if call else df * (K * ncdf(-d2) - F * ncdf(-d1))


def gamma(F, K, T, s):
    sq = s * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * s * s * T) / sq
    return math.exp(-RATE * T) * math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi) / (F * sq)


def implied_vol(px, F, K, T, call):
    if px is None or px <= 0:
        return None
    intr = math.exp(-RATE * T) * max(F - K if call else K - F, 0)
    if px <= intr + 0.05 or b76(F, K, T, 3.0, call) < px:
        return None
    lo, hi = 0.01, 3.0
    for _ in range(70):
        m = (lo + hi) / 2
        lo, hi = (lo, m) if b76(F, K, T, m, call) > px else (m, hi)
    return (lo + hi) / 2


def year_frac(exp_iso, val):
    e = datetime.fromisoformat(exp_iso).replace(hour=13, minute=30, tzinfo=TPE)
    return max((e - val).total_seconds() / 86400, 0.15) / 365


def enrich(rows, F, val, fallback=0.18):
    """回傳 [(code, grp, K, call, T, iv, oi)]，與儀表板相同的 IV 補值規則"""
    by = {}
    for code, exp, k, cp, px, oi in rows:
        T = year_frac(exp, val)
        call = cp == "C"
        by.setdefault(code, []).append(dict(code=code, K=k, call=call, T=T, oi=oi, iv=implied_vol(px, F, k, T, call)))
    out = []
    for code, lst in by.items():
        otm = lambda x: (x["call"] and x["K"] >= F) or (not x["call"] and x["K"] <= F)  # noqa: E731
        pool = [x for x in lst if x["iv"] and otm(x)] or [x for x in lst if x["iv"]]
        for x in lst:
            iv = x["iv"] if (x["iv"] and otm(x)) else None
            if iv is None:
                iv = min(pool, key=lambda p: abs(p["K"] - x["K"]))["iv"] if pool else fallback
            out.append((code, group_of(code), x["K"], x["call"], x["T"], iv, x["oi"]))
    return out


def gex(contracts, S, mode):
    g = 0.0
    for _, _, K, call, T, iv, oi in contracts:
        sign = -1 if mode == "short" else (1 if call else -1)
        g += sign * gamma(S, K, T, iv) * oi * MULT * S * S * 0.01
    return g / 1e8


def flip(contracts, F, mode, pct=0.08, n=400):
    if not contracts:
        return None
    grid = [F * (1 - pct + 2 * pct * i / n) for i in range(n + 1)]
    vals = [gex(contracts, s, mode) for s in grid]
    best = None
    for i in range(1, len(grid)):
        a, b = vals[i - 1], vals[i]
        if (a <= 0 < b) or (a >= 0 > b):
            x = grid[i - 1] + (grid[i] - grid[i - 1]) * (-a) / (b - a)
            if best is None or abs(x - F) < abs(best - F):
                best = x
    return round(best, 1) if best else None


def summarize(rows, F, val):
    cs = enrich(rows, F, val)
    out = {}
    for mode in ("naive", "short"):
        m = {"gex": round(gex(cs, F, mode), 3), "flip": flip(cs, F, mode)}
        for g in "WFM":
            sub = [c for c in cs if c[1] == g]
            m[f"flip{g}"] = flip(sub, F, mode)
            m[f"gex{g}"] = round(gex(sub, F, mode), 3) if sub else None
        out[mode] = m
    return out


# ---------------- main ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, help="往回抓幾個日曆天（首次建議 30）")
    a = ap.parse_args()
    today = datetime.now(TPE).date()
    start = today - timedelta(days=a.days)
    opt, fut = {}, {}
    for txt in download_range(OPT_URL, "TXO", start, today):
        opt.update(parse_options(txt))
    for txt in download_range(FUT_URL, "TX", start, today):
        fut.update(parse_futures(txt))
    days = sorted(d for d in opt if d in fut)
    if not days:
        print("沒有可用的交易日資料（假日或期交所尚未公布）")
        os.makedirs(DATA, exist_ok=True)
        with open(os.path.join(DATA, "_debug.txt"), "w", encoding="utf-8") as f:
            f.write(datetime.now(TPE).isoformat() + "\n" + f"opt days={len(opt)} fut days={len(fut)}\n" + "\n----\n".join(DEBUG))
        return
    os.makedirs(os.path.join(DATA, "daily"), exist_ok=True)
    hist_path = os.path.join(DATA, "history.json")
    hist = json.load(open(hist_path, encoding="utf-8")) if os.path.exists(hist_path) else {"items": []}
    items = {it["date"]: it for it in hist["items"]}

    latest = None
    for d in days:
        fcode, F = fut[d]
        val = datetime(d.year, d.month, d.day, 13, 45, tzinfo=TPE)
        rows = [r for r in opt[d] if date.fromisoformat(r[1]) > d and abs(r[2] - F) / F <= 0.15]
        if (d.isoformat() in items) and d != days[-1]:
            continue
        summ = summarize(rows, F, val)
        snap = {"tradeDate": d.isoformat(), "valuation": val.isoformat(), "F": F, "futContract": fcode,
                "fields": ["code", "expiry", "strike", "cp", "settle", "oi"], "rows": rows, "summary": summ,
                "generatedAt": datetime.now(TPE).isoformat(timespec="seconds")}
        json.dump(snap, open(os.path.join(DATA, "daily", d.strftime("%Y%m%d") + ".json"), "w", encoding="utf-8"),
                  ensure_ascii=False, separators=(",", ":"))
        items[d.isoformat()] = {"date": d.isoformat(), "F": F, **{f"{m}_{k}": v for m, s in summ.items() for k, v in s.items()}}
        latest = snap
        print(d, "F", F, "flip", summ["naive"]["flip"], "rows", len(rows))

    if latest:
        json.dump(latest, open(os.path.join(DATA, "latest.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, separators=(",", ":"))
    hist["items"] = sorted(items.values(), key=lambda x: x["date"])[-250:]
    hist["updatedAt"] = datetime.now(TPE).isoformat(timespec="seconds")
    json.dump(hist, open(hist_path, "w", encoding="utf-8"), ensure_ascii=False, indent=0)


if __name__ == "__main__":
    main()
