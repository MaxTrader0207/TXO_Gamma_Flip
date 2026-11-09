"""
長期回測：往回抓 N 天（預設 2 年）的 TXO／台指期／三大法人資料，逐日計算
naive 與 dealer 兩種方向的合併 GEX、Flip、ATM IV，再對照隔日與未來 5 日的實際波動。
輸出到 backtest/：daily.csv（逐日明細）、stats.json（分組統計）、report.md（文字報告）。
不會改動 data/ 底下儀表板用的檔案。
用法：python scripts/backtest.py [--days 730]
"""
import argparse
import csv
import json
import math
import os
import statistics as st
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(__file__))
import txo_gex as tg  # noqa: E402

OUT = os.path.join(os.path.dirname(__file__), "..", "backtest")


def atm_iv(cs, F, val):
    """最接近 30 天到期、最接近價平的 OTM IV（作為隱含波動基準）"""
    by = {}
    for c in cs:
        by.setdefault(c[0], []).append(c)
    if not by:
        return None
    code = min(by, key=lambda k: abs(by[k][0][4] * 365 - 30))
    near = sorted(by[code], key=lambda c: abs(c[2] - F))[:4]
    v = [c[5] for c in near if c[5]]
    return round(sum(v) / len(v), 4) if v else None


def flip_fast(cs, F, mode, w, pct=0.08, n=160):
    return tg.flip(cs, F, mode, pct=pct, n=n, w=w)


def compute(days_back):
    today = datetime.now(tg.TPE).date()
    start = today - timedelta(days=days_back)
    opt, fut, inst = {}, {}, {}
    for txt in tg.download_range(tg.OPT_URL, "TXO", start, today):
        opt.update(tg.parse_options(txt))
    print("選擇權天數", len(opt), flush=True)
    for txt in tg.download_range(tg.FUT_URL, "TX", start, today):
        fut.update(tg.parse_futures(txt))
    print("期貨天數", len(fut), flush=True)
    for txt in tg.download_range(tg.INST_URL, "TXO", start, today):
        inst.update(tg.parse_institutional(txt))
    print("三大法人天數", len(inst), flush=True)
    rows_out = []
    for d in sorted(x for x in opt if x in fut):
        fcode, F = fut[d][:2]
        o, h, l, c = fut[d][2:]
        val = datetime(d.year, d.month, d.day, 13, 45, tzinfo=tg.TPE)
        live = [r for r in opt[d] if date.fromisoformat(r[1]) > d]
        oi_tot, exps = {}, {}
        for r in live:
            exps[r[0]] = r[1]
            t = oi_tot.setdefault(r[0], [0, 0])
            t[0 if r[3] == "C" else 1] += r[5]
        rows = [r for r in live if abs(r[2] - F) / F <= 0.15]
        if not rows:
            continue
        cs = tg.enrich(rows, F, val)
        w = tg.dealer_weights(inst.get(d), oi_tot)
        rec = {"date": d.isoformat(), "F": F, "open": o, "high": h, "low": l, "close": c,
               "iv": atm_iv(cs, F, val),
               "oiC": sum(v[0] for v in oi_tot.values()), "oiP": sum(v[1] for v in oi_tot.values()),
               "naive_gex": round(tg.gex(cs, F, "naive"), 3), "naive_flip": flip_fast(cs, F, "naive", None),
               "wc": w[0] if w else None, "wp": w[1] if w else None}
        if w:
            rec["dealer_gex"] = round(tg.gex(cs, F, "dealer", w), 3)
            rec["dealer_flip"] = flip_fast(cs, F, "dealer", w)
        # 只看本週到期（最近一個到期）的 GEX：週選對隔日影響可能最大
        near = min(exps, key=lambda k: exps[k]) if exps else None
        sub = [x for x in cs if x[0] == near]
        rec["near_code"] = near
        rec["near_gex"] = round(tg.gex(sub, F, "naive"), 3) if sub else None
        rows_out.append(rec)
        print(d, F, rec["naive_gex"], rec["naive_flip"], rec.get("dealer_gex"), flush=True)
    return rows_out


def add_forward(rows):
    """隔日真實波幅%、隔日報酬%、未來 5 日實現波動（年化）、RV/IV"""
    for i, r in enumerate(rows):
        r["ret"] = None if i == 0 or rows[i - 1]["close"] <= 0 else (r["close"] / rows[i - 1]["close"] - 1) * 100
    for i, r in enumerate(rows):
        if i + 1 < len(rows):
            b = rows[i + 1]
            tr = max(b["high"] - b["low"], abs(b["high"] - r["close"]), abs(b["low"] - r["close"]))
            r["tr1"] = tr / r["close"] * 100
            r["ret1"] = b["ret"]
        if i + 5 < len(rows):
            rets = [rows[j]["ret"] for j in range(i + 1, i + 6)]
            r["rv5"] = math.sqrt(sum(x * x for x in rets) / 5) * math.sqrt(252)
            if r.get("iv"):
                r["rv_iv"] = r["rv5"] / (r["iv"] * 100)
    # 60 日滾動百分位（GEX 會隨指數水位與 OI 成長，用相對位置才可跨期比較）
    for key in ("naive_gex", "dealer_gex", "near_gex"):
        for i, r in enumerate(rows):
            win = [x[key] for x in rows[max(0, i - 59):i + 1] if x.get(key) is not None]
            if r.get(key) is None or len(win) < 40:
                continue
            r[key + "_pct"] = sum(1 for x in win if x <= r[key]) / len(win)
    # 與 Flip 的距離（以 ATM IV 換算的日 σ 為單位）
    for r in rows:
        for m in ("naive", "dealer"):
            f = r.get(m + "_flip")
            if f and r.get("iv"):
                sd = r["F"] * r["iv"] / math.sqrt(252)
                r[m + "_dist"] = (r["F"] - f) / sd


def bucket_stats(rows, keyf, label):
    g = {}
    for r in rows:
        k = keyf(r)
        if k is None or r.get("tr1") is None:
            continue
        g.setdefault(k, []).append(r)
    out = []
    for k in sorted(g):
        v = g[k]
        tr = [x["tr1"] for x in v]
        ar = [abs(x["ret1"]) for x in v if x.get("ret1") is not None]
        rvi = [x["rv_iv"] for x in v if x.get("rv_iv") is not None]
        rev = [x["ret"] * x["ret1"] for x in v if x.get("ret") is not None and x.get("ret1") is not None]
        big = sum(1 for x in v if x.get("ret1") is not None and abs(x["ret1"]) >= 1.5) / len(v)
        out.append({"group": label, "bucket": k, "n": len(v),
                    "tr1": round(st.mean(tr), 3), "absret1": round(st.mean(ar), 3) if ar else None,
                    "rv_iv": round(st.mean(rvi), 3) if rvi else None,
                    "lag1": round(st.mean(rev), 3) if rev else None,
                    "big_move": round(big, 3)})
    return out


def q5(key):
    def f(r):
        p = r.get(key)
        if p is None:
            return None
        return f"Q{min(5, int(p * 5) + 1)}"
    return f


def dist_bucket(key):
    def f(r):
        x = r.get(key)
        if x is None:
            return "無 Flip"
        if x < -0.5:
            return "a. 低於 Flip 0.5σ 以上"
        if x < 0:
            return "b. 低於 Flip 0.5σ 內"
        if x < 0.5:
            return "c. 高於 Flip 0.5σ 內"
        return "d. 高於 Flip 0.5σ 以上"
    return f


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--reuse", action="store_true", help="直接用既有 backtest/daily.json 重算統計")
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    jp = os.path.join(OUT, "daily.json")
    rows = json.load(open(jp, encoding="utf-8")) if a.reuse else compute(a.days)
    rows = [r for r in rows if r["close"] > 0]
    rows.sort(key=lambda r: r["date"])
    if not a.reuse:
        json.dump(rows, open(jp, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    add_forward(rows)
    stats = []
    stats += bucket_stats(rows, lambda r: "全部", "全部")
    stats += bucket_stats(rows, dist_bucket("naive_dist"), "naive 與 Flip 距離")
    stats += bucket_stats(rows, dist_bucket("dealer_dist"), "dealer 與 Flip 距離")
    stats += bucket_stats(rows, q5("naive_gex_pct"), "naive GEX 60日百分位")
    stats += bucket_stats(rows, q5("dealer_gex_pct"), "dealer GEX 60日百分位")
    stats += bucket_stats(rows, q5("near_gex_pct"), "最近到期 GEX 60日百分位")
    stats += bucket_stats(rows, lambda r: None if r.get("naive_gex") is None else ("正" if r["naive_gex"] >= 0 else "負"), "naive GEX 正負")
    # 依年份切開，檢查結論是否穩定
    for y in sorted({r["date"][:4] for r in rows}):
        sub = [r for r in rows if r["date"][:4] == y]
        stats += bucket_stats(sub, q5("naive_gex_pct"), f"{y} naive GEX 百分位")
    json.dump({"rows": len(rows), "from": rows[0]["date"], "to": rows[-1]["date"], "stats": stats},
              open(os.path.join(OUT, "stats.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    keys = ["date", "F", "open", "high", "low", "close", "iv", "oiC", "oiP", "naive_gex", "naive_flip", "naive_dist",
            "dealer_gex", "dealer_flip", "dealer_dist", "wc", "wp", "near_code", "near_gex",
            "naive_gex_pct", "dealer_gex_pct", "near_gex_pct", "ret", "tr1", "ret1", "rv5", "rv_iv"]
    with open(os.path.join(OUT, "daily.csv"), "w", encoding="utf-8-sig", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(keys)
        for r in rows:
            wr.writerow([round(r[k], 4) if isinstance(r.get(k), float) else r.get(k, "") for k in keys])
    L = [f"# TXO Gamma Flip 長期回測\n\n期間 {rows[0]['date']} ～ {rows[-1]['date']}，共 {len(rows)} 個交易日。\n",
         "欄位：tr1 = 隔日真實波幅%；absret1 = 隔日漲跌幅絕對值%；rv_iv = 未來 5 日實現波動 ÷ 當日 ATM IV；"
         "lag1 = 今日報酬 × 隔日報酬（負值代表反轉、正值代表延續）；big_move = 隔日漲跌 ≥1.5% 的比例。\n"]
    cur = None
    for s in stats:
        if s["group"] != cur:
            cur = s["group"]
            L.append(f"\n## {cur}\n\n| 分組 | 天數 | tr1 | absret1 | rv_iv | lag1 | big_move |\n|---|---|---|---|---|---|---|")
        L.append(f"| {s['bucket']} | {s['n']} | {s['tr1']} | {s['absret1']} | {s['rv_iv']} | {s['lag1']} | {s['big_move']} |")
    open(os.path.join(OUT, "report.md"), "w", encoding="utf-8").write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
