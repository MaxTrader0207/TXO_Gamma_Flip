"""
規則總覽：把儀表板上的每條規則放在同一張表，比較
  - 全期回測（真實買賣價成交）
  - 前段／後段（以期間中點切開；後段可視為「較新的資料」檢驗）
  - 結算價成交（舊做法）對照，看買賣價差吃掉多少
頁面再把模擬帳本的實際結果並排，偏離回測太多就標「暫停參考」。
輸出 backtest/board.md，並寫入 signal.json 的 _board。
"""
import json
import math
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(__file__))
import buyer_bt as bb  # noqa: E402
import ma_bt  # noqa: E402,F401
import strategy_bt as sb  # noqa: E402
import txo_gex as tg  # noqa: E402

OUT = os.path.join(os.path.dirname(__file__), "..", "backtest")


def summ(v):
    if len(v) < 3:
        return None
    p = [x[1] for x in v]
    sd = st.stdev(p)
    eq = pk = dd = 0
    for x in p:
        eq += x
        pk = max(pk, eq)
        dd = min(dd, eq - pk)
    return {"n": len(p), "avg": round(st.mean(p), 2), "sd": round(sd, 2), "win": round(sum(1 for x in p if x > 0) / len(p), 3),
            "total": round(sum(p), 1), "dd": round(dd, 1), "t": round(st.mean(p) / (sd / math.sqrt(len(p))), 2) if sd > 0 else None}


def ma_events(rows, C, N=10, slope=1):
    def sma(n, i):
        return sum(C[i - n + 1:i + 1]) / n if i >= n - 1 else None
    out = set()
    for n in (30, 60, 100):
        for i in range(len(rows)):
            m, m1, ms = sma(n, i), sma(n, i - 1), sma(n, i - slope)
            if m is None or m1 is None or ms is None or i - N < n - 1:
                continue
            if C[i] < m and C[i - 1] >= m1 and m > ms and all(C[k] >= sma(n, k) for k in range(i - N, i)):
                out.add(i)
    return sorted(out)


def rule_trades(rows, model):
    """回傳 {key: [(date, pnl), ...]}（依目前的成交方式）"""
    out = {}
    ts = sb.trades(rows, model)
    for key, k in (("標準", 2.5), ("保守", 3.0)):
        v = []
        for t in ts:
            if t["red"]:
                continue
            d = k * t["E"] * math.sqrt(t["n"])
            a, b = sb.leg(t, d, True), sb.leg(t, d, False)
            if a and b:
                v.append((t["date"], a["pnl"] + b["pnl"]))
        out[key] = v
    bts = bb.build(rows, model)
    out["買方跨式"] = [(t["date"], x["pnl"]) for t in bts if t["red"] and t["expand"]
                   for x in [bb.pnl(t, "straddle", "hold")] if x]
    out["買方Call"] = [(t["date"], x["pnl"]) for t in bts if t["red"] and (t["ret"] or 0) < 0
                     for x in [bb.pnl(t, "call", "hold")] if x]
    idx = {r["date"]: i for i, r in enumerate(rows)}
    spot = sum(1 for r in rows if r.get("spot")) >= 0.98 * len(rows)
    C = [(r.get("spot") or r["close"]) if spot else r["close"] for r in rows]
    out["均線Call"] = [(rows[i]["date"], x["pnl"]) for i in ma_events(rows, C)
                     for x in [ma_bt.option_trade(rows, idx, i, True, 0.01, "hold")] if x]
    return out


NAMES = {"標準": "賣方標準：綠燈、勒式 2.5×預估波幅×√天數", "保守": "賣方保守：綠燈、勒式 3.0×預估波幅×√天數",
         "買方跨式": "買方：紅燈＋波動放大 → 價平跨式", "買方Call": "買方：紅燈＋當日下跌 → 價平 Call",
         "均線Call": "均線：首次跌破上揚的 30/60/100MA → 價外 1% Call"}


def main():
    rows, model = sb.load()
    mid = rows[len(rows) // 2]["date"]
    res = {}
    for mode in ("quote", "settle"):
        sb.set_fill(mode)
        tr = rule_trades(rows, model)
        for k, v in tr.items():
            r = res.setdefault(k, {"key": k, "name": NAMES[k]})
            if mode == "quote":
                r["full"] = summ(v)
                r["first"] = summ([x for x in v if x[0] < mid])
                r["second"] = summ([x for x in v if x[0] >= mid])
                r["noQuote"] = None
            else:
                r["settle"] = summ(v)
    sb.set_fill("quote")
    rules = list(res.values())
    sig_path = os.path.join(OUT, "signal.json")
    sig = json.load(open(sig_path, encoding="utf-8"))
    sig["_board"] = {"from": rows[0]["date"], "to": rows[-1]["date"], "mid": mid, "fee": 0.5, "rules": rules}
    json.dump(sig, open(sig_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    f = lambda s: f"{s['n']}／{s['avg']:+.1f}／{s['t']}" if s else "—"  # noqa: E731
    L = ["# 規則總覽\n", f"期間 {rows[0]['date']} ～ {rows[-1]['date']}；前後段以 {mid} 切開。",
         "主要成交方式：賣出用當日最後最佳買價、買進用最後最佳賣價，另扣手續費＋稅 0.5 點／腳；持有到結算不再付出場成本。",
         "格式：筆數／平均每筆（點）／t 值。\n",
         "| 規則 | 全期（買賣價） | 前段 | 後段 | 對照：結算價成交 |", "|---|---|---|---|---|"]
    for r in rules:
        L.append(f"| {r['name']} | {f(r['full'])} | {f(r['first'])} | {f(r['second'])} | {f(r.get('settle'))} |")
    open(os.path.join(OUT, "board.md"), "w", encoding="utf-8").write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
