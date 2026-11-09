"""
賣方策略回測：每天收盤以結算價賣出「最近到期」的 OTM Call／Put，持有到結算。
履約價距離 = k × 預估波幅（燈號模型：基準 + b × 近 5 日 ATR% + 紅燈加成）× √持有交易日數。
讀 backtest/daily.json、backtest/signal.json，輸出 backtest/strategy.md、backtest/strategy.json。
用法：python scripts/strategy_bt.py
"""
import json
import math
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(__file__))
import backtest as bt  # noqa: E402
import txo_gex as tg  # noqa: E402

OUT = os.path.join(os.path.dirname(__file__), "..", "backtest")
COST = 1.0      # 每口每腳成本（點）：手續費＋稅＋滑價的保守估計
PV = 50         # 每點 50 元


def load():
    rows = json.load(open(os.path.join(OUT, "daily.json"), encoding="utf-8"))
    rows = [r for r in rows if r["close"] > 0]
    rows.sort(key=lambda r: r["date"])
    bt.add_forward(rows)
    sig = json.load(open(os.path.join(OUT, "signal.json"), encoding="utf-8"))
    return rows, sig["_model"]


def trades(rows, model):
    """逐日建立可交易的樣本：最近到期、持有天數、預估波幅、到期結算價"""
    idx = {r["date"]: i for i, r in enumerate(rows)}
    out = []
    for i, r in enumerate(rows):
        e, code = r.get("near_exp"), r.get("near_code")
        if not e or not r.get("chain") or r.get("atr5p") is None or e not in idx:
            continue
        j = idx[e]
        if j <= i:
            continue
        fin = (rows[j].get("final") or {}).get(code)
        if fin and fin["n"] >= 10:
            S_T, src = fin["S"], "settle"
        else:
            S_T, src = rows[j]["close"], "txclose"
        red = tg.bt_bucket(r["F"], r.get("naive_flip"), r.get("naive_gex")) == "red"
        e_pct = model["a"] + model["b"] * r["atr5p"] + (model["c"] if red else 0)
        out.append({"date": r["date"], "exp": e, "n": j - i, "F": r["F"], "S_T": S_T, "src": src, "red": red,
                    "E": e_pct / 100 * r["F"], "chain": r["chain"]})
    return out


def pick(chain, target, call):
    """Call：≥ target 的最低履約價；Put：≤ target 的最高履約價（需有報價）"""
    if call:
        c = [(k, cp) for k, cp, pp in chain if k >= target and cp]
        return min(c) if c else None
    c = [(k, pp) for k, cp, pp in chain if k <= target and pp]
    return max(c) if c else None


def leg(t, dist, call, wing=None):
    p = pick(t["chain"], t["F"] + dist if call else t["F"] - dist, call)
    if not p:
        return None
    K, px = p
    pay = max(0.0, t["S_T"] - K) if call else max(0.0, K - t["S_T"])
    pnl = px - COST - pay
    if wing:  # 買進更外側的保護腳，變成信用價差
        q = pick(t["chain"], K + wing if call else K - wing, call)
        if not q:
            return None
        K2, px2 = q
        pay2 = max(0.0, t["S_T"] - K2) if call else max(0.0, K2 - t["S_T"])
        pnl += -px2 - COST + pay2
        return {"pnl": pnl, "credit": px - px2, "risk": abs(K2 - K) - (px - px2), "K": K}
    return {"pnl": pnl, "credit": px, "K": K}


def summarize(name, pnls):
    if not pnls:
        return None
    w = [x for x in pnls if x > 0]
    l = [x for x in pnls if x <= 0]
    return {"name": name, "n": len(pnls), "win": round(len(w) / len(pnls), 3),
            "avg": round(st.mean(pnls), 2), "total": round(sum(pnls), 1), "worst": round(min(pnls), 1),
            "pf": round(sum(w) / -sum(l), 2) if l and sum(l) < 0 else None,
            "avgWin": round(st.mean(w), 2) if w else None, "avgLoss": round(st.mean(l), 2) if l else None}


def run(ts, label, dist_fn, wing_fn=None, filt=None):
    res = {}
    for side in ("call", "put", "strangle"):
        p = []
        for t in ts:
            if filt and not filt(t):
                continue
            d = dist_fn(t)
            wg = wing_fn(t) if wing_fn else None
            if side == "strangle":
                a, b = leg(t, d, True, wg), leg(t, d, False, wg)
                if a and b:
                    p.append(a["pnl"] + b["pnl"])
            else:
                x = leg(t, d, side == "call", wg)
                if x:
                    p.append(x["pnl"])
        res[side] = summarize(label, p)
    return res


def main():
    rows, model = load()
    ts = trades(rows, model)
    srcs = {}
    for t in ts:
        srcs[t["src"]] = srcs.get(t["src"], 0) + 1
    sq = lambda t: math.sqrt(t["n"])  # noqa: E731
    wing = lambda t: max(100, round(t["E"] / 100) * 100)  # noqa: E731
    results = []
    for k in (1.0, 1.5, 2.0, 2.5):
        results.append(("A", f"k={k}（×√持有天數）", run(ts, f"k={k}", lambda t, k=k: k * t["E"] * sq(t))))
    for k in (1.0, 1.5, 2.0):
        results.append(("B", f"k={k}（不乘 √天數）", run(ts, f"k={k}", lambda t, k=k: k * t["E"])))
    # 固定百分比距離對照：與 A 組 k=1.5 的平均距離相同，但不隨波動調整
    avg_pct = st.mean(1.5 * t["E"] * sq(t) / t["F"] for t in ts)
    results.append(("C", f"固定 {avg_pct*100:.2f}%（不看波動）", run(ts, "fixed", lambda t: avg_pct * t["F"])))
    for k in (1.5, 2.0):
        results.append(("D", f"k={k} 信用價差（保護腳 +1 個預估波幅）",
                        run(ts, f"spread{k}", lambda t, k=k: k * t["E"] * sq(t), wing)))
    for k in (1.5, 2.0):
        results.append(("E", f"k={k}｜只做綠燈日", run(ts, f"green{k}", lambda t, k=k: k * t["E"] * sq(t), filt=lambda t: not t["red"])))
        results.append(("E", f"k={k}｜只做紅燈日", run(ts, f"red{k}", lambda t, k=k: k * t["E"] * sq(t), filt=lambda t: t["red"])))
    # 混合：綠燈賣單腳、紅燈改信用價差
    for k in (1.5, 2.0):
        res = {}
        for side in ("call", "put", "strangle"):
            p = []
            for t in ts:
                d, wg = k * t["E"] * sq(t), (wing(t) if t["red"] else None)
                if side == "strangle":
                    a, b = leg(t, d, True, wg), leg(t, d, False, wg)
                    if a and b:
                        p.append(a["pnl"] + b["pnl"])
                else:
                    x = leg(t, d, side == "call", wg)
                    if x:
                        p.append(x["pnl"])
            res[side] = summarize(f"mix{k}", p)
        results.append(("F", f"k={k}｜綠燈賣單腳、紅燈改信用價差", res))

    L = [f"# 賣方策略回測（依燈號預估波幅決定履約價）\n",
         f"期間 {rows[0]['date']} ～ {rows[-1]['date']}；可交易樣本 {len(ts)} 筆（每天收盤進場、持有到最近到期結算）。",
         f"結算價來源：{srcs}（settle＝由到期日價內選擇權結算價反推，txclose＝以台指期收盤近似）。",
         f"成交價用當日結算價，每腳扣 {COST} 點成本；損益單位為點（1 點 = {PV} 元）。",
         f"預估波幅 E = {model['a']} + {model['b']} × 近 5 日 ATR% + {model['c']}（紅燈）× 期貨價；履約價距離 = k × E × √持有交易日數。\n",
         "| 組 | 規則 | 標的 | 筆數 | 勝率 | 平均損益 | 累計損益 | 最大單筆虧損 | 獲利因子 | 平均賺 | 平均賠 |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    js = []
    for g, name, res in results:
        for side in ("call", "put", "strangle"):
            s = res.get(side) if res else None
            if not s:
                continue
            zh = {"call": "賣 Call", "put": "賣 Put", "strangle": "勒式（兩腳）"}[side]
            L.append(f"| {g} | {name} | {zh} | {s['n']} | {s['win']:.0%} | {s['avg']} | {s['total']} | {s['worst']} | {s['pf']} | {s['avgWin']} | {s['avgLoss']} |")
            js.append({"group": g, "rule": name, "side": side, **s})
    open(os.path.join(OUT, "strategy.md"), "w", encoding="utf-8").write("\n".join(L) + "\n")
    json.dump({"from": rows[0]["date"], "to": rows[-1]["date"], "samples": len(ts), "settleSrc": srcs,
               "cost": COST, "model": model, "results": js},
              open(os.path.join(OUT, "strategy.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n".join(L))


if __name__ == "__main__":
    main()
