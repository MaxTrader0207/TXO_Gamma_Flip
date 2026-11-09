"""
模擬帳本：每個交易日依儀表板的回測規則記錄「當天會怎麼做」，到期後用最後結算價算損益。
- 綠燈日：標準（k=2.5）與保守（k=3.0）各記一組最近到期的 Call＋Put 賣方勒式，以當日結算價成交
- 紅燈日：記為不進場
- 到期日：由到期契約的價內選擇權結算價反推最後結算價（Call：K+C；Put：K−P，取中位數）
結果存 data/paper.json，並把摘要放進 latest.json 給頁面顯示。
"""
import json
import math
import os
from datetime import date, timedelta

COST = 1.0
PATH = os.path.join(os.path.dirname(__file__), "..", "data", "paper.json")


def final_settle(allr, code, F):
    est = [r[2] + r[4] if r[3] == "C" else r[2] - r[4] for r in allr
           if r[0] == code and r[4] and r[4] > 5 and ((r[3] == "C" and r[2] < F) or (r[3] == "P" and r[2] > F))]
    if len(est) < 5:
        return None
    est.sort()
    return round(est[len(est) // 2], 1)


def settle_pnl(e, S):
    """賣方：收權利金 − 內含價值；買方：內含價值 − 權利金。每腳扣 COST"""
    legs = [(e["Kc"], True), (e["Kp"], False)] if e.get("Kc") and e.get("Kp") else (
        [(e["Kc"], True)] if e.get("Kc") else [(e["Kp"], False)])
    intr = sum(max(0.0, S - K) if c else max(0.0, K - S) for K, c in legs)
    cost = COST * len(legs)
    if e.get("side") == "buy":
        return round(intr - e["debit"] - cost, 1)
    return round(e["credit"] - cost - intr, 1)


def trading_days(d, exp):
    n, x = 0, d
    while x < exp:
        x += timedelta(days=1)
        if x.weekday() < 5:
            n += 1
    return max(1, n)


def update(days, opt, fut, items, tg, model, tiers):
    led = json.load(open(PATH, encoding="utf-8")) if os.path.exists(PATH) else {"entries": [], "start": None}
    have = {(e["date"], e["tier"]) for e in led["entries"]}
    for d in days:
        ds = d.isoformat()
        F = fut[d][1]
        allr = opt[d]
        # 1) 結算今天到期的部位
        for e in led["entries"]:
            # 到期日遇休市（颱風假等）會順延，所以以「今天的資料裡這個契約到期」為準
            if e.get("status") == "open" and e["exp"] <= ds and any(r[0] == e["code"] and r[1] <= ds for r in allr):
                S = final_settle(allr, e["code"], F)
                if S is None:
                    print("模擬帳本：", ds, e["code"], "到期但無法反推結算價", flush=True)
                    continue
                e.update(status="closed", S=S, pnl=settle_pnl(e, S))
            elif e.get("status") == "open" and e["exp"] < ds and not any(r[0] == e["code"] for r in allr):
                # 契約已經消失（到期日當天的資料缺漏或休市）：以到期後第一個交易日的台指期收盤近似結算價，並標註
                print("模擬帳本：", e["code"], "到期日", e["exp"], "無資料，改用", ds, "台指期近似", flush=True)
                S = fut[d][5] if len(fut[d]) > 5 and fut[d][5] > 0 else F
                e.update(status="closed", S=S, approx=True, pnl=settle_pnl(e, S))
        # 2) 今天的新部位
        it = items.get(ds) or {}
        if not it.get("atr5"):
            continue
        red = tg.bt_bucket(F, it.get("naive_flip"), it.get("naive_gex")) == "red"
        live = [r for r in allr if date.fromisoformat(r[1]) > d and r[4]]
        if not live:
            continue
        exp = min(r[1] for r in live)
        code = next(r[0] for r in live if r[1] == exp)
        n = trading_days(d, date.fromisoformat(exp))
        E = (model["a"] + model["b"] * it["atr5"] / F * 100 + (model["c"] if red else 0)) / 100 * F
        for t in tiers:
            if (ds, t["name"]) in have:
                continue
            base = {"date": ds, "tier": t["name"], "k": t["k"], "code": code, "exp": exp, "n": n, "F": F,
                    "light": "red" if red else "green", "dist": round(t["k"] * E * math.sqrt(n))}
            if red:
                led["entries"].append({**base, "status": "skip", "reason": "紅燈不進場"})
                continue
            D = t["k"] * E * math.sqrt(n)
            calls = sorted((r[2], r[4]) for r in live if r[0] == code and r[3] == "C" and r[2] >= F + D)
            puts = sorted(((r[2], r[4]) for r in live if r[0] == code and r[3] == "P" and r[2] <= F - D), reverse=True)
            if not calls or not puts:
                led["entries"].append({**base, "status": "skip", "reason": "超出報價範圍"})
                continue
            (Kc, pc), (Kp, pp) = calls[0], puts[0]
            led["entries"].append({**base, "status": "open", "Kc": Kc, "pc": pc, "Kp": Kp, "pp": pp,
                                   "credit": round(pc + pp, 1)})
        # 買方：紅燈＋近 5 日 ATR 放大 → 價平跨式；紅燈＋當日下跌 → 價平 Call
        seq = sorted((x for x in items.values() if x["date"] <= ds), key=lambda x: x["date"])
        expand = len(seq) >= 6 and seq[-1].get("atr5") and seq[-6].get("atr5") and seq[-1]["atr5"] > seq[-6]["atr5"]
        down = len(seq) >= 2 and seq[-1].get("close") and seq[-2].get("close") and seq[-1]["close"] < seq[-2]["close"]
        Ks = sorted({r[2] for r in live if r[0] == code and r[3] == "C"} & {r[2] for r in live if r[0] == code and r[3] == "P"},
                    key=lambda k: abs(k - F))
        if red and Ks:
            K = Ks[0]
            pc = next(r[4] for r in live if r[0] == code and r[2] == K and r[3] == "C")
            pp = next(r[4] for r in live if r[0] == code and r[2] == K and r[3] == "P")
            base = {"date": ds, "code": code, "exp": exp, "n": n, "F": F, "light": "red", "side": "buy", "status": "open"}
            if expand and (ds, "買方跨式") not in have:
                led["entries"].append({**base, "tier": "買方跨式", "Kc": K, "pc": pc, "Kp": K, "pp": pp, "debit": round(pc + pp, 1)})
            if down and (ds, "買方Call") not in have:
                led["entries"].append({**base, "tier": "買方Call", "Kc": K, "pc": pc, "debit": round(pc, 1)})
        # 均線跌破（30／60／100MA 近期第一次跌破、均線上揚）→ 買當週價外 1% Call
        if (ds, "均線Call") not in have:
            ms = tg.ma_status([x.get("close") for x in seq])
            if ms["breaks"]:
                calls = sorted((r[2], r[4]) for r in live if r[0] == code and r[3] == "C" and r[2] >= F * 1.01)
                if calls:
                    K, pc = calls[0]
                    led["entries"].append({"date": ds, "code": code, "exp": exp, "n": n, "F": F, "light": "red" if red else "green",
                                           "side": "buy", "status": "open", "tier": "均線Call", "Kc": K, "pc": pc,
                                           "debit": round(pc, 1), "ma": ms["breaks"]})
        have |= {(ds, t["name"]) for t in tiers} | {(ds, "買方跨式"), (ds, "買方Call"), (ds, "均線Call")}
    led["entries"].sort(key=lambda e: (e["date"], e["tier"]))
    led["start"] = led["entries"][0]["date"] if led["entries"] else None
    json.dump(led, open(PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    return summary(led, tiers + [{"name": "買方跨式", "k": None}, {"name": "買方Call", "k": None}, {"name": "均線Call", "k": None}])


def summary(led, tiers):
    out = {"start": led["start"], "cost": COST, "tiers": [], "recent": led["entries"][-40:]}
    for t in tiers:
        es = [e for e in led["entries"] if e["tier"] == t["name"]]
        cl = [e for e in es if e["status"] == "closed"]
        p = [e["pnl"] for e in cl]
        out["tiers"].append({"name": t["name"], "k": t["k"], "days": len(es),
                             "traded": sum(1 for e in es if e["status"] in ("open", "closed")),
                             "open": sum(1 for e in es if e["status"] == "open"), "closed": len(cl),
                             "skipRed": sum(1 for e in es if e.get("reason") == "紅燈不進場"),
                             "skipRange": sum(1 for e in es if e.get("reason") == "超出報價範圍"),
                             "total": round(sum(p), 1), "win": round(sum(1 for x in p if x > 0) / len(p), 3) if p else None,
                             "worst": min(p) if p else None})
    return out
