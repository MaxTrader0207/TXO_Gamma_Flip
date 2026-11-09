"""
規則改良回測：
A. 夜盤燈號接進交易規則
   S1 現行：日盤綠燈 → 收盤以買價賣最近到期勒式（k=2.5），持有到結算
   S2 現行＋夜盤轉紅就在隔天開盤買回
   S3 改在隔天開盤進場：夜盤綠燈才賣（不看日盤）
   S4 改在隔天開盤進場：日盤綠且夜盤綠才賣
   B1 現行買方：日盤紅燈且當天下跌 → 收盤買價平 Call
   B3 夜盤紅燈且夜盤收低 → 隔天開盤買價平 Call
   開盤成交：用選擇權日盤開盤價，另加「前一日收盤買賣價差的一半」當滑價，再扣手續費 0.5 點
B. 波動指標過濾（套在 S1）
   VRP＝30 天價平 IV − 近 20 日實現波動；偏斜＝最近到期價外 3% Put IV − 價外 3% Call IV
輸出 backtest/rules2.md、backtest/rules2.json，並寫入 signal.json 的 _rules2。
"""
import json
import math
import os
import statistics as st
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(__file__))
import backtest as bt  # noqa: E402
import strategy_bt as sb  # noqa: E402
import txo_gex as tg  # noqa: E402

OUT = os.path.join(os.path.dirname(__file__), "..", "backtest")
K_STD = 2.5


def row_of(chain, K):
    return next((r for r in chain if r[0] == K), None)


def half_spread(row, call):
    if not row or len(row) < 7:
        return 1.0
    b, a = (row[3], row[4]) if call else (row[5], row[6])
    return (a - b) / 2 if a and b and a > b else 1.0


def summ(v):
    if len(v) < 5:
        return None
    p = [x[1] for x in v]
    sd = st.stdev(p)
    eq = pk = dd = 0
    for x in p:
        eq += x
        pk = max(pk, eq)
        dd = min(dd, eq - pk)
    by = {}
    for d, x in v:
        by.setdefault(d[:4], []).append(x)
    return {"n": len(p), "avg": round(st.mean(p), 2), "t": round(st.mean(p) / (sd / math.sqrt(len(p))), 2) if sd > 0 else None,
            "win": round(sum(1 for x in p if x > 0) / len(p), 3), "total": round(sum(p), 1), "dd": round(dd, 1),
            "worst": round(min(p), 1), "years": {y: round(st.mean(x), 1) for y, x in sorted(by.items())}}


def settle_of(rows, idx, code, e):
    j = idx.get(e)
    if j is None:
        return None
    fin = (rows[j].get("final") or {}).get(code)
    return fin["S"] if fin and fin["n"] >= 10 else rows[j]["close"]


def main():
    rows, model = sb.load()
    sb.set_fill("quote")
    idx = {r["date"]: i for i, r in enumerate(rows)}
    nmap = json.load(open(os.path.join(OUT, "night_daily.json"), encoding="utf-8"))
    sig = json.load(open(os.path.join(OUT, "signal.json"), encoding="utf-8"))
    mn = (sig.get("_night") or {}).get("modelNight") or model
    # 實現波動與指標
    for i, r in enumerate(rows):
        rets = [rows[k]["ret"] for k in range(max(1, i - 19), i + 1) if rows[k].get("ret") is not None]
        r["rv20"] = math.sqrt(sum(x * x for x in rets) / len(rets)) * math.sqrt(252) / 100 if len(rets) >= 15 else None
        r["vrp"] = (r["iv"] - r["rv20"]) if r.get("iv") and r.get("rv20") else None
        r["dayRed"] = tg.bt_bucket(r["F"], r.get("naive_flip"), r.get("naive_gex")) == "red"
        n = nmap.get(r["date"])
        f = r.get("naive_flip")
        r["nightC"] = n[3] if n else None
        r["nightRed"] = (n[3] < f if f else r["dayRed"]) if n else None
    for i, r in enumerate(rows):  # 偏斜的 60 日百分位
        w = [x["skew"] for x in rows[max(0, i - 59):i + 1] if x.get("skew") is not None]
        r["skewPct"] = (sum(1 for x in w if x <= r["skew"]) / len(w)) if r.get("skew") is not None and len(w) >= 40 else None
    vrps = sorted(r["vrp"] for r in rows if r.get("vrp") is not None)
    vmed = vrps[len(vrps) // 2]

    def sell_close(i, k=K_STD, red_flag=None):
        """收盤以買價賣最近到期勒式；回傳 (code, exp, Kc, Kp, credit, row_c, row_p) 或 None"""
        r = rows[i]
        if not r.get("chain") or r.get("atr5p") is None or not r.get("near_exp"):
            return None
        e, code = r["near_exp"], r["near_code"]
        if e not in idx:
            return None
        n = idx[e] - i
        red = r["dayRed"] if red_flag is None else red_flag
        E = (model["a"] + model["b"] * r["atr5p"] + (model["c"] if red else 0)) / 100 * r["F"]
        D = k * E * math.sqrt(n)
        a = sb.pick(r["chain"], r["F"] + D, True, "sell")
        b = sb.pick(r["chain"], r["F"] - D, False, "sell")
        if not a or not b:
            return None
        return code, e, a[0], b[0], a[1] + b[1]

    def payoff(S, Kc, Kp):
        return max(0.0, S - Kc) + max(0.0, Kp - S)

    res = {}
    # S1 現行
    v = []
    for i in range(len(rows) - 1):
        if rows[i]["dayRed"]:
            continue
        t = sell_close(i)
        if not t:
            continue
        code, e, Kc, Kp, cr = t
        S = settle_of(rows, idx, code, e)
        v.append((rows[i]["date"], cr - 2 * sb.COST - payoff(S, Kc, Kp)))
    res["S1 現行：日盤綠燈收盤賣"] = summ(v)
    # S2 夜盤轉紅隔天開盤買回
    v = []
    for i in range(len(rows) - 1):
        if rows[i]["dayRed"]:
            continue
        t = sell_close(i)
        if not t:
            continue
        code, e, Kc, Kp, cr = t
        nx = rows[i + 1]
        if rows[i].get("nightRed") and nx["date"] != e and nx.get("near_code") == code:
            rc, rp = row_of(nx["chain"], Kc), row_of(nx["chain"], Kp)
            oc, op = sb.price(rc, True, "open") if rc else None, sb.price(rp, False, "open") if rp else None
            if oc and op:
                cost = oc + half_spread(rc, True) + op + half_spread(rp, False)
                v.append((rows[i]["date"], cr - cost - 4 * sb.COST))
                continue
        S = settle_of(rows, idx, code, e)
        v.append((rows[i]["date"], cr - 2 * sb.COST - payoff(S, Kc, Kp)))
    res["S2 現行＋夜盤轉紅隔天開盤買回"] = summ(v)

    def sell_open(i, need_day_green):
        """依第 i 天之後的夜盤燈號，在第 i+1 天開盤賣最近到期勒式"""
        r, nx = rows[i], rows[i + 1]
        if r.get("nightRed") is None or r["nightRed"] or (need_day_green and r["dayRed"]):
            return None
        if not nx.get("chain") or r.get("atr5p") is None or nx.get("near_exp") not in idx:
            return None
        e, code = nx["near_exp"], nx["near_code"]
        n = idx[e] - (i + 1) + 1
        F = r["nightC"]
        E = (mn["a"] + mn["b"] * r["atr5p"]) / 100 * F
        D = K_STD * E * math.sqrt(n)
        prev = {x[0]: x for x in r.get("chain") or []}
        best = None
        for call in (True, False):
            cand = [x for x in nx["chain"] if (x[0] >= F + D if call else x[0] <= F - D) and sb.price(x, call, "open")]
            if not cand:
                return None
            x = min(cand, key=lambda z: z[0]) if call else max(cand, key=lambda z: z[0])
            px = sb.price(x, call, "open") - half_spread(prev.get(x[0]) or x, call)
            best = (best or []) + [(x[0], max(px, 0.0))]
        (Kc, pc), (Kp, pp) = best
        S = settle_of(rows, idx, code, e)
        return (nx["date"], pc + pp - 2 * sb.COST - payoff(S, Kc, Kp))
    res["S3 隔天開盤賣：夜盤綠燈"] = summ([x for x in (sell_open(i, False) for i in range(len(rows) - 1)) if x])
    res["S4 隔天開盤賣：日盤綠＋夜盤綠"] = summ([x for x in (sell_open(i, True) for i in range(len(rows) - 1)) if x])

    # 買方
    def atm_row(chain, F):
        both = [x for x in chain if x[1] and x[2]]
        return min(both, key=lambda x: abs(x[0] - F)) if both else None
    v = []
    for i in range(len(rows) - 1):
        r = rows[i]
        if not (r["dayRed"] and (r.get("ret") or 0) < 0) or not r.get("chain") or r.get("near_exp") not in idx:
            continue
        x = atm_row(r["chain"], r["F"])
        p = sb.price(x, True, "buy") if x else None
        if not p:
            continue
        S = settle_of(rows, idx, r["near_code"], r["near_exp"])
        v.append((r["date"], max(0.0, S - x[0]) - p - sb.COST))
    res["B1 現行：日盤紅燈＋當天下跌，收盤買 Call"] = summ(v)
    v = []
    for i in range(len(rows) - 1):
        r, nx = rows[i], rows[i + 1]
        if not r.get("nightRed") or r["nightC"] is None or r["nightC"] >= r["close"]:
            continue
        if not nx.get("chain") or nx.get("near_exp") not in idx:
            continue
        x = atm_row(nx["chain"], r["nightC"])
        p = sb.price(x, True, "open") if x else None
        if not p:
            continue
        p += half_spread({y[0]: y for y in r.get("chain") or []}.get(x[0]) or x, True)
        S = settle_of(rows, idx, nx["near_code"], nx["near_exp"])
        v.append((nx["date"], max(0.0, S - x[0]) - p - sb.COST))
    res["B3 夜盤紅燈＋夜盤收低，隔天開盤買 Call"] = summ(v)

    # 波動指標過濾（套在 S1）
    def s1_filter(f):
        out = []
        for i in range(len(rows) - 1):
            if rows[i]["dayRed"] or not f(rows[i]):
                continue
            t = sell_close(i)
            if not t:
                continue
            code, e, Kc, Kp, cr = t
            S = settle_of(rows, idx, code, e)
            out.append((rows[i]["date"], cr - 2 * sb.COST - payoff(S, Kc, Kp)))
        return summ(out)
    res["V1 S1＋VRP>0（IV 高於實現波動）"] = s1_filter(lambda r: r.get("vrp") is not None and r["vrp"] > 0)
    res["V1' S1＋VRP≤0"] = s1_filter(lambda r: r.get("vrp") is not None and r["vrp"] <= 0)
    res[f"V2 S1＋VRP 高於中位數（{vmed:.3f}）"] = s1_filter(lambda r: r.get("vrp") is not None and r["vrp"] > vmed)
    res["V3 S1＋偏斜不在 60 日高檔（<80 百分位）"] = s1_filter(lambda r: r.get("skewPct") is not None and r["skewPct"] < 0.8)
    res["V3' S1＋偏斜在 60 日高檔（≥80 百分位）"] = s1_filter(lambda r: r.get("skewPct") is not None and r["skewPct"] >= 0.8)
    res["V4 S1＋VRP>0＋偏斜<80 百分位"] = s1_filter(lambda r: r.get("vrp") is not None and r["vrp"] > 0
                                             and r.get("skewPct") is not None and r["skewPct"] < 0.8)
    # 迴歸：隔日真實波幅 ~ ATR + 紅燈 + VRP + 偏斜百分位
    reg = [r for r in rows if r.get("tr1") is not None and r.get("atr5p") is not None and r.get("vrp") is not None
           and r.get("skewPct") is not None]
    X = [[1.0, r["atr5p"], 1.0 if r["dayRed"] else 0.0, r["vrp"] * 100, r["skewPct"]] for r in reg]
    if len(reg) < 30:
        beta, tv = [0.0] * 5, [0.0] * 5
    else:
        beta, tv = bt.ols(X, [r["tr1"] for r in reg])
    regm = {"n": len(reg), "a": round(beta[0], 4), "atr": round(beta[1], 4), "red": round(beta[2], 4), "t_red": round(tv[2], 2),
            "vrp": round(beta[3], 4), "t_vrp": round(tv[3], 2), "skew": round(beta[4], 4), "t_skew": round(tv[4], 2)}
    f = lambda s: "—" if not s else f"{s['n']}｜{s['avg']:+.1f}｜{s['t']}｜{s['win']:.0%}｜{s['dd']}｜{s['worst']}"  # noqa: E731
    L = ["# 規則改良回測（夜盤、波動指標）\n", f"期間 {rows[0]['date']} ～ {rows[-1]['date']}。賣方 k={K_STD}，"
         "收盤成交用最後最佳買／賣價，開盤成交用選擇權開盤價加前一日半個買賣價差當滑價，另扣手續費 0.5 點／腳。\n",
         "格式：筆數｜平均每筆（點）｜t｜勝率｜最大回撤｜最大單筆虧損\n", "| 規則 | 結果 | 各年平均 |", "|---|---|---|"]
    for k, s in res.items():
        L.append(f"| {k} | {f(s)} | {' '.join(f'{y}:{v:+.1f}' for y, v in (s or {}).get('years', {}).items())} |")
    L.append(f"\n迴歸（隔日真實波幅% ~ 近5日ATR% + 紅燈 + VRP(百分點) + 偏斜百分位）：紅燈 {regm['red']}（t={regm['t_red']}）、"
             f"VRP {regm['vrp']}（t={regm['t_vrp']}）、偏斜百分位 {regm['skew']}（t={regm['t_skew']}），樣本 {regm['n']}。")
    open(os.path.join(OUT, "rules2.md"), "w", encoding="utf-8").write("\n".join(L) + "\n")
    print("\n".join(L))
    sig["_rules2"] = {"from": rows[0]["date"], "to": rows[-1]["date"], "vrpMedian": round(vmed, 4), "results": res, "reg": regm}
    json.dump(sig, open(os.path.join(OUT, "signal.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
