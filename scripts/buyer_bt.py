"""
買方策略回測：什麼情況適合買選擇權？
每天收盤以結算價買進「最近到期」的選擇權，比較兩種出場：
  - 隔日出場：隔一個交易日以結算價賣出（同一契約；若隔天就是到期日則用最後結算價）
  - 持有到結算
策略：價平跨式（Call＋Put）、價外勒式（±1 個預估波幅 × √天數）、單買價平 Call／Put（順勢、逆勢）
條件：燈號、權利金相對預估波幅的貴賤、離 Flip 遠近、距到期天數、近期波動是否放大
每腳每次進出扣 1 點成本。輸出 backtest/buyer.md、backtest/buyer.json。
"""
import json
import math
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(__file__))
import strategy_bt as sb  # noqa: E402
import txo_gex as tg  # noqa: E402

OUT = os.path.join(os.path.dirname(__file__), "..", "backtest")
COST = 1.0


def px(row, K, call):
    for k, c, p in row.get("chain") or []:
        if k == K:
            return c if call else p
    return None


def build(rows, model):
    idx = {r["date"]: i for i, r in enumerate(rows)}
    out = []
    for i, r in enumerate(rows[:-1]):
        e, code = r.get("near_exp"), r.get("near_code")
        if not e or e not in idx or not r.get("chain") or r.get("atr5p") is None:
            continue
        j = idx[e]
        fin = (rows[j].get("final") or {}).get(code)
        S_T = fin["S"] if fin and fin["n"] >= 10 else rows[j]["close"]
        nxt = rows[i + 1]
        same = nxt.get("near_code") == code and i + 1 < j
        both = [(k, c, p) for k, c, p in r["chain"] if c and p]
        if not both:
            continue
        K = min(both, key=lambda x: abs(x[0] - r["F"]))[0]
        red = tg.bt_bucket(r["F"], r.get("naive_flip"), r.get("naive_gex")) == "red"
        n = j - i
        E = (model["a"] + model["b"] * r["atr5p"] + (model["c"] if red else 0)) / 100 * r["F"]
        # 5 日前的 ATR，判斷波動是否在放大
        prev = rows[i - 5].get("atr5p") if i >= 5 else None
        out.append({"i": i, "date": r["date"], "F": r["F"], "K": K, "n": n, "red": red, "E": E, "S_T": S_T,
                    "next": nxt if same else None, "nextS": S_T if i + 1 == j else None,
                    "ret": r.get("ret"), "dist": r.get("naive_dist"), "expand": prev is not None and r["atr5p"] > prev,
                    "row": r, "pct": r.get("naive_gex_pct")})
    return out


def value_next(t, K, call):
    """隔日出場的價值：隔天仍在交易用隔天結算價；隔天就是到期日用最後結算價的內含價值"""
    if t["next"] is not None:
        v = px(t["next"], K, call)
        return v, 1  # 需要再付一次賣出成本
    if t["nextS"] is not None:
        return (max(0.0, t["nextS"] - K) if call else max(0.0, K - t["nextS"])), 0
    return None, 0


def legs_for(t, kind):
    """回傳 [(K, call)]"""
    F, K = t["F"], t["K"]
    if kind == "straddle":
        return [(K, True), (K, False)]
    if kind == "strangle":
        d = t["E"] * math.sqrt(t["n"])
        c = sb.pick(t["row"]["chain"], F + d, True)
        p = sb.pick(t["row"]["chain"], F - d, False)
        return [(c[0], True), (p[0], False)] if c and p else None
    if kind == "call":
        return [(K, True)]
    if kind == "put":
        return [(K, False)]
    if kind == "trend":  # 順勢：今天漲買 Call、跌買 Put
        return [(K, (t["ret"] or 0) >= 0)]
    if kind == "fade":   # 逆勢
        return [(K, (t["ret"] or 0) < 0)]
    return None


def pnl(t, kind, exit_):
    lg = legs_for(t, kind)
    if not lg:
        return None
    total, cost = 0.0, 0.0
    for K, call in lg:
        p0 = px(t["row"], K, call)
        if not p0:
            return None
        if exit_ == "hold":
            v = max(0.0, t["S_T"] - K) if call else max(0.0, K - t["S_T"])
            extra = 0
        else:
            v, extra = value_next(t, K, call)
            if v is None:
                return None
        total += v - p0
        cost += COST * (1 + extra)
    prem = sum(px(t["row"], K, c) for K, c in lg)
    return {"pnl": total - cost, "prem": prem, "ret": (total - cost) / prem if prem else None}


def stats(v):
    if len(v) < 15:
        return None
    p = [x["pnl"] for x in v]
    r = [x["ret"] for x in v if x["ret"] is not None]
    eq = pk = dd = 0
    for x in p:
        eq += x
        pk = max(pk, eq)
        dd = min(dd, eq - pk)
    sd = st.stdev(p)
    return {"n": len(p), "win": round(sum(1 for x in p if x > 0) / len(p), 3), "avg": round(st.mean(p), 2),
            "total": round(sum(p), 1), "best": round(max(p), 1), "dd": round(dd, 1),
            "t": round(st.mean(p) / (sd / math.sqrt(len(p))), 2) if sd > 0 else None,
            "roi": round(st.mean(r), 3) if r else None, "prem": round(st.mean(x["prem"] for x in v), 1)}


def main():
    rows, model = sb.load()
    ts = build(rows, model)
    # 權利金貴賤：跨式價格 ÷（預估波幅 × √天數），用全期三分位切
    for t in ts:
        c, p = px(t["row"], t["K"], True), px(t["row"], t["K"], False)
        t["cheap"] = (c + p) / (t["E"] * math.sqrt(t["n"])) if c and p else None
    q = sorted(t["cheap"] for t in ts if t["cheap"])
    lo, hi = q[len(q) // 3], q[2 * len(q) // 3]
    for t in ts:
        t["vol"] = None if t["cheap"] is None else ("便宜" if t["cheap"] <= lo else "貴" if t["cheap"] >= hi else "中等")
    conds = [
        ("全部", lambda t: True),
        ("紅燈", lambda t: t["red"]),
        ("綠燈", lambda t: not t["red"]),
        ("權利金便宜（下 1/3）", lambda t: t["vol"] == "便宜"),
        ("權利金貴（上 1/3）", lambda t: t["vol"] == "貴"),
        ("紅燈＋權利金便宜", lambda t: t["red"] and t["vol"] == "便宜"),
        ("紅燈＋非貴", lambda t: t["red"] and t["vol"] != "貴"),
        ("跌破 Flip 0.5σ 以上", lambda t: t["dist"] is not None and t["dist"] < -0.5),
        ("GEX 60 日百分位 < 20%", lambda t: t["pct"] is not None and t["pct"] < 0.2),
        ("近期波動放大（ATR 高於 5 日前）", lambda t: t["expand"]),
        ("紅燈＋波動放大", lambda t: t["red"] and t["expand"]),
        ("距到期 1 天", lambda t: t["n"] == 1),
        ("距到期 2 天以上", lambda t: t["n"] >= 2),
        ("紅燈＋距到期 2 天以上", lambda t: t["red"] and t["n"] >= 2),
    ]
    kinds = [("straddle", "價平跨式"), ("strangle", "價外勒式 ±1E√n"), ("trend", "順勢單買價平"), ("fade", "逆勢單買價平"),
             ("call", "單買價平 Call"), ("put", "單買價平 Put")]
    res = []
    for exit_, ezh in (("next", "隔日出場"), ("hold", "持有到結算")):
        for kind, kzh in kinds:
            for cname, f in conds:
                v = [x for x in (pnl(t, kind, exit_) for t in ts if f(t)) if x]
                s = stats(v)
                if s:
                    res.append({"exit": ezh, "kind": kzh, "cond": cname, **s})
    # 樣本外：前半期挑出的最佳條件，在後半期是否仍成立（門檻用前半期的三分位）
    mid = rows[len(rows) // 2]["date"]
    first = [t for t in ts if t["date"] < mid]
    q1 = sorted(t["cheap"] for t in first if t["cheap"])
    lo1 = q1[len(q1) // 3]
    oos = []
    for exit_, ezh in (("next", "隔日出場"), ("hold", "持有到結算")):
        for kind, kzh in kinds[:2]:
            for cname, f in (("紅燈", lambda t: t["red"]), ("紅燈＋權利金便宜", lambda t: t["red"] and t["cheap"] is not None and t["cheap"] <= lo1),
                             ("權利金便宜", lambda t: t["cheap"] is not None and t["cheap"] <= lo1), ("全部", lambda t: True)):
                a = stats([x for x in (pnl(t, kind, exit_) for t in ts if t["date"] < mid and f(t)) if x])
                b = stats([x for x in (pnl(t, kind, exit_) for t in ts if t["date"] >= mid and f(t)) if x])
                oos.append({"exit": ezh, "kind": kzh, "cond": cname, "first": a, "second": b})
    L = ["# 買方策略回測：什麼情況適合買選擇權\n",
         f"期間 {rows[0]['date']} ～ {rows[-1]['date']}，樣本 {len(ts)} 天。每天收盤以結算價買進最近到期的選擇權；"
         f"每腳每次進出扣 {COST} 點。損益單位：點（1 點 = 50 元）；roi = 平均每筆損益 ÷ 付出的權利金。",
         f"權利金貴賤 = 價平跨式價格 ÷（預估波幅 × √持有天數）；便宜 ≤ {lo:.2f}、貴 ≥ {hi:.2f}。\n"]
    for ezh in ("隔日出場", "持有到結算"):
        for _, kzh in kinds:
            L.append(f"\n## {kzh}｜{ezh}\n\n| 條件 | 筆數 | 勝率 | 平均 | 累計 | 單筆最大獲利 | 最大回撤 | t | roi | 平均權利金 |\n|---|---|---|---|---|---|---|---|---|---|")
            for x in res:
                if x["exit"] == ezh and x["kind"] == kzh:
                    L.append(f"| {x['cond']} | {x['n']} | {x['win']:.0%} | {x['avg']} | {x['total']} | {x['best']} | {x['dd']} | {x['t']} | {x['roi']} | {x['prem']} |")
    L.append(f"\n## 樣本外檢查（以 {mid} 切前後兩段，便宜門檻只用前段資料）\n\n| 出場 | 策略 | 條件 | 前段 n／平均／t | 後段 n／平均／t |\n|---|---|---|---|---|")
    for x in oos:
        f_, s_ = x["first"], x["second"]
        fmt = lambda s: f"{s['n']}／{s['avg']}／{s['t']}" if s else "—"  # noqa: E731
        L.append(f"| {x['exit']} | {x['kind']} | {x['cond']} | {fmt(f_)} | {fmt(s_)} |")
    open(os.path.join(OUT, "buyer.md"), "w", encoding="utf-8").write("\n".join(L) + "\n")
    json.dump({"from": rows[0]["date"], "to": rows[-1]["date"], "cheapLo": lo, "cheapHi": hi, "results": res, "oos": oos},
              open(os.path.join(OUT, "buyer.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n".join(L))
    # 儀表板用的買方規則摘要（全期與前後兩段）
    dn = lambda t: (t["ret"] or 0) < 0  # noqa: E731
    rules = {
        "straddle": ("straddle", lambda t: t["red"] and t["expand"]),
        "call": ("call", lambda t: t["red"] and dn(t)),
        "straddleOther": ("straddle", lambda t: not (t["red"] and t["expand"])),
        "callGreenDown": ("call", lambda t: (not t["red"]) and dn(t)),
        "otm": ("strangle", lambda t: True),
    }
    summ = {}
    for key, (kind, f) in rules.items():
        part = {}
        for lab, g in (("full", lambda t: True), ("first", lambda t: t["date"] < mid), ("second", lambda t: t["date"] >= mid)):
            part[lab] = stats([x for x in (pnl(t, kind, "hold") for t in ts if g(t) and f(t)) if x])
        summ[key] = part
    sp = os.path.join(OUT, "signal.json")
    sig = json.load(open(sp, encoding="utf-8"))
    sig["_buyer"] = {"from": rows[0]["date"], "to": rows[-1]["date"], "mid": mid, "cost": COST, **summ}
    json.dump(sig, open(sp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    L2 = ["\n## 儀表板採用的買方規則（持有到結算）\n", "| 規則 | 全期 n／平均／t／勝率 | 前段 | 後段 |", "|---|---|---|---|"]
    names = {"straddle": "紅燈＋波動放大 → 買價平跨式", "call": "紅燈＋當日下跌 → 買價平 Call",
             "straddleOther": "其他日子買價平跨式（對照）", "callGreenDown": "綠燈＋當日下跌買 Call（對照）", "otm": "買價外勒式（對照）"}
    f3 = lambda s: f"{s['n']}／{s['avg']}／{s['t']}／{s['win']:.0%}" if s else "—"  # noqa: E731
    for k, v in summ.items():
        L2.append(f"| {names[k]} | {f3(v['full'])} | {f3(v['first'])} | {f3(v['second'])} |")
    with open(os.path.join(OUT, "buyer.md"), "a", encoding="utf-8") as fh:
        fh.write("\n".join(L2) + "\n")
    print("\n".join(L2))


if __name__ == "__main__":
    main()
