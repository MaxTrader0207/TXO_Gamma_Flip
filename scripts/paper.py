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
            if e.get("status") == "open" and e["exp"] == ds:
                S = final_settle(allr, e["code"], F)
                if S is None:
                    continue
                pay = max(0.0, S - e["Kc"]) + max(0.0, e["Kp"] - S)
                e.update(status="closed", S=S, pnl=round(e["credit"] - 2 * COST - pay, 1))
        # 2) 今天的新部位
        it = items.get(ds) or {}
        if any((ds, t["name"]) in have for t in tiers) or not it.get("atr5"):
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
        have |= {(ds, t["name"]) for t in tiers}
    led["entries"].sort(key=lambda e: (e["date"], e["tier"]))
    led["start"] = led["start"] or (led["entries"][0]["date"] if led["entries"] else None)
    json.dump(led, open(PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    return summary(led, tiers)


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
