"""
盤前夜盤更新：每天早上（台北 07:20、07:50）抓台指期夜盤收盤，用前一個日盤的選擇權結構重新判斷燈號，
寫進 data/latest.json 的 night 欄位（不改動日盤的計算結果）。
- 夜盤燈號：夜盤收盤 vs 合併 Flip（Call 多、Put 空）；掃描範圍內沒有 Flip 時看夜盤價位上的總 GEX 正負
- 今天日盤預估波幅：backtest/signal.json 的 _night.modelNight（a + b×近5日ATR% + 夜盤紅燈加成）
"""
import json
import os
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(__file__))
import night_bt  # noqa: E402
import txo_gex as tg  # noqa: E402

DATA = os.path.join(os.path.dirname(__file__), "..", "data")


def main():
    lp = os.path.join(DATA, "latest.json")
    latest = json.load(open(lp, encoding="utf-8"))
    d0 = date.fromisoformat(latest["tradeDate"])
    today = datetime.now(tg.TPE).date()
    day, night = {}, {}
    for txt in tg.download_range(tg.FUT_URL, "TX", d0 - timedelta(days=10), today):
        day.update(tg.parse_futures(txt, "一般"))
        night.update(tg.parse_futures(txt, "盤後"))
    if d0 not in day or not night:
        print("沒有日盤或夜盤資料", d0 in day, len(night))
        return
    print("日盤最新", max(day), "夜盤最新", max(night))
    nm, nextattr = night_bt.night_map(day, night)
    n = nm.get(d0)
    if not n or not (n[5] > 0):
        print("夜盤尚未公布：", d0, "之後的夜盤")
        return
    snap_p = os.path.join(DATA, "daily", d0.strftime("%Y%m%d") + ".json")
    snap = json.load(open(snap_p, encoding="utf-8"))
    val = datetime.fromisoformat(snap["valuation"])
    F0 = snap["F"]
    cs = tg.enrich(snap["rows"], F0, val)
    flip = latest["summary"]["naive"]["flip"]
    Nc = n[5]
    g = round(tg.gex(cs, Nc, "naive"), 3)
    bucket = ("red" if Nc < flip else "green") if flip else ("red" if g < 0 else "green")
    sig = (latest.get("btSignal") or {}).get("stats") or {}
    mn = (sig.get("_night") or {}).get("modelNight")
    atr = latest.get("atr5")
    E = None
    if mn and atr:
        E = round(mn["a"] + mn["b"] * atr / F0 * 100 + (mn["c"] if bucket == "red" else 0), 3)
    dclose = (latest.get("ohlc") or [None] * 4)[3] or F0
    nd = [d for d, v in night.items() if v is n]
    latest["night"] = {"after": d0.isoformat(), "nightDate": nd[0].isoformat() if nd else None,
                       "contract": n[0], "open": n[2], "high": n[3], "low": n[4], "close": Nc,
                       "chg": round(Nc - dclose, 1), "chgPct": round((Nc / dclose - 1) * 100, 2),
                       "flip": flip, "gex": g, "bucket": bucket, "dayBucket": (latest.get("btSignal") or {}).get("bucket"),
                       "E": E, "updatedAt": datetime.now(tg.TPE).isoformat(timespec="seconds")}
    json.dump(latest, open(lp, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print("夜盤更新", latest["night"])


if __name__ == "__main__":
    main()
