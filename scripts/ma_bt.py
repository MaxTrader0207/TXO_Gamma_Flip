"""
均線跌破買方回測：台指期收盤「近期第一次」跌破 30／60／100 日均線、且均線仍在上揚時，
收盤以結算價買進當週（最近到期）週選價外 1% 的選擇權。Put（順勢看跌破）與 Call（均線支撐反彈）都測。
- 跌破：今天收盤 < 均線，昨天收盤 ≥ 均線
- 近期第一次：跌破前連續 N 個交易日都收在均線之上（預設 N=10，另測 5、20）
- 均線上揚：今天的均線 > 5 個交易日前的均線
- 出場：持有到結算、隔日出場（以隔天結算價賣出）
每腳每次進出扣 1 點成本。輸出 backtest/ma.md、backtest/ma.json。
"""
import json
import math
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(__file__))
import buyer_bt as bb  # noqa: E402
import strategy_bt as sb  # noqa: E402
import txo_gex as tg  # noqa: E402

OUT = os.path.join(os.path.dirname(__file__), "..", "backtest")
COST = 1.0


def sma(xs, n, i):
    return sum(xs[i - n + 1:i + 1]) / n if i >= n - 1 else None


def option_trade(rows, idx, i, call, otm=0.01, exit_="hold"):
    r = rows[i]
    e, code = r.get("near_exp"), r.get("near_code")
    if not e or e not in idx or not r.get("chain"):
        return None
    j = idx[e]
    F = r["close"]
    tgt = F * (1 + otm) if call else F * (1 - otm)
    p = sb.pick(r["chain"], tgt, call)
    if not p:
        return None
    K, p0 = p
    if exit_ == "hold":
        fin = (rows[j].get("final") or {}).get(code)
        S = fin["S"] if fin and fin["n"] >= 10 else rows[j]["close"]
        v, c = (max(0.0, S - K) if call else max(0.0, K - S)), COST
    else:
        nx = rows[i + 1] if i + 1 < len(rows) else None
        if nx is None:
            return None
        if i + 1 == j:
            fin = (rows[j].get("final") or {}).get(code)
            S = fin["S"] if fin and fin["n"] >= 10 else rows[j]["close"]
            v, c = (max(0.0, S - K) if call else max(0.0, K - S)), COST
        elif nx.get("near_code") == code:
            v, c = bb.px(nx, K, call), 2 * COST
            if v is None:
                return None
        else:
            return None
    return {"date": r["date"], "F": F, "K": K, "prem": p0, "n": j - i, "pnl": round(v - p0 - c, 1),
            "ret": (v - p0 - c) / p0 if p0 else None, "exp": e, "code": code}


def stats(v):
    if not v:
        return None
    p = [x["pnl"] for x in v]
    out = {"n": len(p), "win": round(sum(1 for x in p if x > 0) / len(p), 3), "avg": round(st.mean(p), 1),
           "total": round(sum(p), 1), "best": round(max(p), 1), "worst": round(min(p), 1),
           "prem": round(st.mean(x["prem"] for x in v), 1),
           "roi": round(st.mean(x["ret"] for x in v if x["ret"] is not None), 3)}
    if len(p) >= 3 and st.stdev(p) > 0:
        out["t"] = round(st.mean(p) / (st.stdev(p) / math.sqrt(len(p))), 2)
    return out


def main():
    rows = json.load(open(os.path.join(OUT, "daily.json"), encoding="utf-8"))
    rows = [r for r in rows if r["close"] > 0]
    rows.sort(key=lambda r: r["date"])
    idx = {r["date"]: i for i, r in enumerate(rows)}
    C = [r["close"] for r in rows]
    for r in rows:
        r["red"] = tg.bt_bucket(r["F"], r.get("naive_flip"), r.get("naive_gex")) == "red"

    def events(ma, N, slope=5):
        out = []
        for i in range(len(rows)):
            m, m1 = sma(C, ma, i), sma(C, ma, i - 1)
            ms = sma(C, ma, i - slope)
            if m is None or m1 is None or ms is None or i - N < ma - 1:
                continue
            if not (C[i] < m and C[i - 1] >= m1 and m > ms):
                continue
            if all(C[k] >= sma(C, ma, k) for k in range(i - N, i)):
                out.append(i)
        return out

    res, ev_list = [], {}
    for ma in (30, 60, 100):
        for N in (5, 10, 20):
            ev = events(ma, N)
            if N == 10:
                ev_list[ma] = ev
            for call in (False, True):
                for exit_ in ("hold", "next"):
                    v = [x for x in (option_trade(rows, idx, i, call, 0.01, exit_) for i in ev) if x]
                    s = stats(v)
                    if s:
                        res.append({"ma": ma, "N": N, "side": "Call" if call else "Put", "exit": exit_, **s})
    # 合併三條均線（同一天多條只算一次）
    for N in (5, 10, 20):
        ev = sorted(set(events(30, N)) | set(events(60, N)) | set(events(100, N)))
        for call in (False, True):
            for exit_ in ("hold", "next"):
                s = stats([x for x in (option_trade(rows, idx, i, call, 0.01, exit_) for i in ev) if x])
                if s:
                    res.append({"ma": "任一", "N": N, "side": "Call" if call else "Put", "exit": exit_, **s})
    # 基準：所有日子買同樣的價外 1% 選擇權（從 100MA 可計算的日子開始，期間一致）
    start = 99 + 20
    base = {}
    for call in (False, True):
        for exit_ in ("hold", "next"):
            s = stats([x for x in (option_trade(rows, idx, i, call, 0.01, exit_) for i in range(start, len(rows) - 1)) if x])
            base[("Call" if call else "Put", exit_)] = s
            res.append({"ma": "基準：每天都買", "N": "-", "side": "Call" if call else "Put", "exit": exit_, **s})
    # 跌破日的 Gamma 燈號分布與分組
    split = []
    ev = sorted(set(events(30, 10)) | set(events(60, 10)) | set(events(100, 10)))
    for lab, f in (("紅燈", lambda i: rows[i]["red"]), ("綠燈", lambda i: not rows[i]["red"])):
        for call in (False, True):
            s = stats([x for x in (option_trade(rows, idx, i, call, 0.01, "hold") for i in ev if f(i)) if x])
            if s:
                split.append({"light": lab, "side": "Call" if call else "Put", **s})
    # 事件明細（N=10）
    detail = []
    for ma, evs in ev_list.items():
        for i in evs:
            p = option_trade(rows, idx, i, False, 0.01, "hold")
            c = option_trade(rows, idx, i, True, 0.01, "hold")
            j = idx.get(rows[i].get("near_exp"))
            detail.append({"ma": ma, "date": rows[i]["date"], "close": rows[i]["close"],
                           "maVal": round(sma(C, ma, i)), "light": "紅" if rows[i]["red"] else "綠",
                           "S_T": None if j is None else rows[j]["close"],
                           "put": p and {"K": p["K"], "prem": p["prem"], "pnl": p["pnl"]},
                           "call": c and {"K": c["K"], "prem": c["prem"], "pnl": c["pnl"]}})
    detail.sort(key=lambda x: (x["date"], x["ma"]))
    ezh = {"hold": "持有到結算", "next": "隔日出場"}
    L = ["# 均線跌破買方回測（當週週選價外 1%）\n",
         f"期間 {rows[0]['date']} ～ {rows[-1]['date']}（100MA 需 100 日暖機，實際可用期間較短）。台指期近月收盤計算均線；",
         "跌破＝今天收盤 < 均線且昨天 ≥ 均線；近期第一次＝跌破前連續 N 日收在均線之上；均線上揚＝均線高於 5 日前。",
         "跌破當天收盤以結算價買進最近到期週選、履約價在現價 ±1% 之外最近的一檔；每腳每次進出扣 1 點。損益單位：點。\n",
         "| 均線 | N | 買 | 出場 | 次數 | 勝率 | 平均 | 累計 | 最大獲利 | 最大虧損 | t | roi | 平均權利金 |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for x in res:
        L.append(f"| {x['ma']} | {x['N']} | {x['side']} | {ezh[x['exit']]} | {x['n']} | {x['win']:.0%} | {x['avg']} | {x['total']} | "
                 f"{x['best']} | {x['worst']} | {x.get('t', '—')} | {x['roi']} | {x['prem']} |")
    L.append("\n## 跌破當天的 Gamma 燈號（任一均線、N=10、持有到結算）\n\n| 燈號 | 買 | 次數 | 勝率 | 平均 | 累計 | t |\n|---|---|---|---|---|---|---|")
    for x in split:
        L.append(f"| {x['light']} | {x['side']} | {x['n']} | {x['win']:.0%} | {x['avg']} | {x['total']} | {x.get('t', '—')} |")
    L.append("\n## 事件明細（N=10）\n\n| 日期 | 均線 | 收盤 | 均線值 | 燈號 | 結算日收盤 | Put 履約價／權利金／損益 | Call 履約價／權利金／損益 |\n|---|---|---|---|---|---|---|---|")
    for d in detail:
        f = lambda o: f"{o['K']:.0f}／{o['prem']}／{o['pnl']}" if o else "—"  # noqa: E731
        L.append(f"| {d['date']} | {d['ma']}MA | {d['close']:.0f} | {d['maVal']} | {d['light']} | {d['S_T']} | {f(d['put'])} | {f(d['call'])} |")
    open(os.path.join(OUT, "ma.md"), "w", encoding="utf-8").write("\n".join(L) + "\n")
    json.dump({"results": res, "split": split, "events": detail}, open(os.path.join(OUT, "ma.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("\n".join(L))


if __name__ == "__main__":
    main()
