"""
夜盤回測：日盤收盤算出燈號後，接著的夜盤（15:00～隔日 05:00）收盤若已改變期貨相對 Flip 的位置，
隔天日盤的波動是否跟著夜盤燈號走？
- 夜盤燈號：夜盤收盤價 vs 前一個日盤算出的合併 Flip（Call 多、Put 空）；沒有 Flip 時沿用日盤燈號
- 比較隔天「日盤」的真實波幅（以夜盤收盤為前收）、日盤高低差、漲跌 ≥1.5% 的比例
輸出 backtest/night.md、backtest/night.json，並把摘要寫進 signal.json 的 _night。
"""
import json
import math
import os
import statistics as st
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(__file__))
import backtest as bt  # noqa: E402
import txo_gex as tg  # noqa: E402

OUT = os.path.join(os.path.dirname(__file__), "..", "backtest")


def night_map(day, night):
    """回傳 {日盤日期: 接在它之後的夜盤 (code, px, o, h, l, c)}，自動判斷期交所夜盤的日期歸屬"""
    ds = sorted(day)
    prev = {ds[i]: ds[i - 1] for i in range(1, len(ds))}
    same, nxt = [], []
    for d, v in night.items():
        o = v[2]
        if not (o > 0):
            continue
        if d in day:
            same.append(abs(o - day[d][5]))
        if d in prev:
            nxt.append(abs(o - day[prev[d]][5]))
    # 夜盤開盤應該接近「它前面那個日盤」的收盤
    attributed_next = st.median(nxt) < st.median(same) if nxt and same else True
    print("夜盤日期歸屬：", "次一營業日（夜盤日期＝隔天）" if attributed_next else "同一天",
          "中位數差距", round(st.median(nxt), 1) if nxt else None, round(st.median(same), 1) if same else None)
    out = {}
    if attributed_next:
        for d, v in night.items():
            if d in prev:
                out[prev[d]] = v
    else:
        out = dict(night)
    return out, attributed_next


def main():
    rows = json.load(open(os.path.join(OUT, "daily.json"), encoding="utf-8"))
    rows = [r for r in rows if r["close"] > 0]
    rows.sort(key=lambda r: r["date"])
    bt.add_forward(rows)
    start = date.fromisoformat(rows[0]["date"]) - timedelta(days=5)
    end = date.fromisoformat(rows[-1]["date"]) + timedelta(days=3)
    day, night = {}, {}
    for txt in tg.download_range(tg.FUT_URL, "TX", start, end):
        day.update(tg.parse_futures(txt, "一般"))
        night.update(tg.parse_futures(txt, "盤後"))
    print("日盤", len(day), "夜盤", len(night))
    nm, nextattr = night_map(day, night)
    json.dump({d.isoformat(): [v[2], v[3], v[4], v[5]] for d, v in sorted(nm.items())},
              open(os.path.join(OUT, "night_daily.json"), "w", encoding="utf-8"), indent=0)
    recs = []
    for i, r in enumerate(rows[:-1]):
        d = date.fromisoformat(r["date"])
        n = nm.get(d)
        nx = rows[i + 1]
        if not n or not (n[5] > 0):
            continue
        dayb = tg.bt_bucket(r["F"], r.get("naive_flip"), r.get("naive_gex"))
        f = r.get("naive_flip")
        nb = ("red" if n[5] < f else "green") if f else dayb
        Nc = n[5]
        # 隔天日盤：以夜盤收盤為前收的真實波幅、日盤高低差、日盤收盤相對夜盤收盤
        tr = max(nx["high"] - nx["low"], abs(nx["high"] - Nc), abs(nx["low"] - Nc)) / Nc * 100
        rng = (nx["high"] - nx["low"]) / Nc * 100
        mv = (nx["close"] / Nc - 1) * 100
        recs.append({"date": r["date"], "day": dayb, "night": nb, "nret": (Nc / r["close"] - 1) * 100,
                     "tr": tr, "rng": rng, "mv": mv, "atr5p": r.get("atr5p")})
    print("可用樣本", len(recs))

    def s(v):
        if not v:
            return None
        return {"n": len(v), "tr": round(st.mean(x["tr"] for x in v), 3), "rng": round(st.mean(x["rng"] for x in v), 3),
                "big": round(st.mean(1 if abs(x["mv"]) >= 1.5 else 0 for x in v), 3)}
    groups = {
        "日盤綠→夜盤綠": [x for x in recs if x["day"] == "green" and x["night"] == "green"],
        "日盤綠→夜盤紅（夜盤跌破 Flip）": [x for x in recs if x["day"] == "green" and x["night"] == "red"],
        "日盤紅→夜盤綠（夜盤站回 Flip）": [x for x in recs if x["day"] == "red" and x["night"] == "green"],
        "日盤紅→夜盤紅": [x for x in recs if x["day"] == "red" and x["night"] == "red"],
    }
    res = {k: s(v) for k, v in groups.items()}
    by_day = {k: s([x for x in recs if x["day"] == k]) for k in ("red", "green")}
    by_night = {k: s([x for x in recs if x["night"] == k]) for k in ("red", "green")}
    # 迴歸：隔天日盤真實波幅 ~ 近5日ATR + 日盤紅燈 + 夜盤紅燈
    reg = [x for x in recs if x["atr5p"] is not None]
    X = [[1.0, x["atr5p"], 1.0 if x["day"] == "red" else 0.0, 1.0 if x["night"] == "red" else 0.0] for x in reg]
    beta, tv = bt.ols(X, [x["tr"] for x in reg])
    model = {"a": round(beta[0], 4), "b": round(beta[1], 4), "c_day": round(beta[2], 4), "c_night": round(beta[3], 4),
             "t_day": round(tv[2], 2), "t_night": round(tv[3], 2), "n": len(reg)}
    # 只用夜盤燈號的模型（儀表板早上用這個估當天波幅）
    X2 = [[1.0, x["atr5p"], 1.0 if x["night"] == "red" else 0.0] for x in reg]
    b2, t2 = bt.ols(X2, [x["tr"] for x in reg])
    model_n = {"a": round(b2[0], 4), "b": round(b2[1], 4), "c": round(b2[2], 4), "t_red": round(t2[2], 2), "n": len(reg)}
    L = ["# 夜盤燈號回測\n", f"期間 {recs[0]['date']} ～ {recs[-1]['date']}，樣本 {len(recs)} 天。"
         f"夜盤日期歸屬：{'次一營業日' if nextattr else '同一天'}。隔天日盤的真實波幅以夜盤收盤為前收。\n",
         "| 分組 | 天數 | 隔天日盤真實波幅% | 日盤高低差% | 日盤收盤相對夜盤收盤漲跌 ≥1.5% |", "|---|---|---|---|---|"]
    for k, v in list(res.items()) + [("（對照）只看日盤紅燈", by_day["red"]), ("（對照）只看日盤綠燈", by_day["green"]),
                                     ("（對照）只看夜盤紅燈", by_night["red"]), ("（對照）只看夜盤綠燈", by_night["green"])]:
        if v:
            L.append(f"| {k} | {v['n']} | {v['tr']} | {v['rng']} | {v['big']:.0%} |")
    L.append(f"\n迴歸（隔天日盤真實波幅% = a + b×近5日ATR% + 日盤紅燈 + 夜盤紅燈）：a={model['a']}、b={model['b']}、"
             f"日盤紅燈 {model['c_day']}（t={model['t_day']}）、夜盤紅燈 {model['c_night']}（t={model['t_night']}）。")
    L.append(f"\n只用夜盤燈號：a={model_n['a']}、b={model_n['b']}、夜盤紅燈 {model_n['c']}（t={model_n['t_red']}）。")
    open(os.path.join(OUT, "night.md"), "w", encoding="utf-8").write("\n".join(L) + "\n")
    print("\n".join(L))
    sp = os.path.join(OUT, "signal.json")
    sig = json.load(open(sp, encoding="utf-8"))
    sig["_night"] = {"from": recs[0]["date"], "to": recs[-1]["date"], "nextAttr": nextattr, "groups": res,
                     "byDay": by_day, "byNight": by_night, "model": model, "modelNight": model_n}
    json.dump(sig, open(sp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
