"""🧪 그림자 가상 매매 — 여러 규칙 조합을 실전과 동시에 가상으로 돌려 보고, 확실히 나은 조합을 실전 규칙으로 자동 교체.

- 조합: '현재'(지금 실전 규칙) + 그 주변으로 조금씩 바꾼 조합 + 백테스트가 고른 조합 (최대 8개)
- 실전 엔진이 매 바퀴 찾은 후보·시세를 그대로 받아서, 조합마다 자기 규칙으로 가상 매수·매도 (각자 가상 100만 원)
- AI 판단은 쓰지 않음 → '현재'(규칙만) 성적과 실전(AI 판단 포함) 성적을 비교해 AI 판단이 도움 되는지도 보임
- 교체 조건: 거래 promote_min_trades 건 이상 + '현재'보다 총수익률이 promote_margin_pct %p 이상 높고 + 플러스
  → 실전 설정에 적용(범위 안으로 자름, 기록·되돌리기), 새 기준으로 조합 다시 만들기
기록: _cache/shadow_US.json / shadow_KR.json
"""

from __future__ import annotations

import hashlib
import json
import random
import threading
import uuid
from datetime import datetime, timedelta, timezone

from .agents import COMPANY_DIR

KST = timezone(timedelta(hours=9))
VIRTUAL = 1_000_000
TUNE_KEYS = ["stop_loss_pct", "partial_tp_pct", "trail_pct", "trail_wide_after_pct", "trail_wide_pct", "surge_min_change_pct",
             "surge_volume_ratio", "pre_volume_ratio", "swing_stop_pct", "swing_trail_pct"]
GRID = {"stop_loss_pct": [2, 2.5, 3, 4, 5], "partial_tp_pct": [0, 3, 4, 5, 6, 8], "trail_pct": [3, 4, 5, 6],
        "trail_wide_after_pct": [8, 10, 15], "trail_wide_pct": [5, 6, 8], "surge_min_change_pct": [5, 7, 10, 15],
        "surge_volume_ratio": [2, 3, 5, 8], "pre_volume_ratio": [0.1, 0.2, 0.4], "swing_stop_pct": [4, 6, 8], "swing_trail_pct": [5, 8, 12]}
LABEL = {"stop_loss_pct": "손절", "partial_tp_pct": "나눠팔기", "trail_pct": "고점대비", "trail_wide_after_pct": "넓힘기준",
         "trail_wide_pct": "넓은고점대비", "surge_min_change_pct": "급등최소", "surge_volume_ratio": "거래량배수",
         "pre_volume_ratio": "프리장거래량", "swing_stop_pct": "스윙손절", "swing_trail_pct": "스윙고점대비"}
_lock = threading.RLock()


def _file(m: str):
    return COMPANY_DIR / "_cache" / f"shadow_{m}.json"


def _load(m: str) -> dict:
    try:
        d = json.loads(_file(m).read_text(encoding="utf-8"))
    except Exception:
        d = {}
    d.setdefault("variants", []); d.setdefault("promotions", []); d.setdefault("base_hash", "")
    return d


def _save(m: str, d: dict) -> None:
    for v in d["variants"]:
        v["trades"] = v["trades"][-300:]
    d["promotions"] = d["promotions"][-50:]
    _file(m).parent.mkdir(parents=True, exist_ok=True)
    _file(m).write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")


def _now() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


def base_params(c: dict) -> dict:
    return {k: c[k] for k in TUNE_KEYS if k in c}


def _hash(p: dict) -> str:
    return hashlib.md5(json.dumps(p, sort_keys=True).encode()).hexdigest()[:10]


def _neighbor(p: dict, rnd: random.Random, n_changes: int = 2) -> dict:
    q = dict(p)
    for k in rnd.sample(TUNE_KEYS, n_changes):
        opts = GRID[k]
        cur = q.get(k, opts[len(opts) // 2])
        idx = min(range(len(opts)), key=lambda i: abs(opts[i] - cur))
        q[k] = opts[max(0, min(len(opts) - 1, idx + rnd.choice([-1, 1])))]
    return q


def _diff(p: dict, base: dict) -> str:
    ch = [f"{LABEL[k]} {base.get(k)}→{p[k]}" for k in TUNE_KEYS if k in p and p[k] != base.get(k)]
    return ", ".join(ch) or "지금 실전 규칙 그대로"


def _new_variant(name: str, params: dict, origin: str) -> dict:
    return {"id": uuid.uuid4().hex[:6], "name": name, "params": params, "origin": origin, "created": _now(),
            "cash": VIRTUAL, "pos": {}, "trades": [], "day": "", "buys_today": 0}


def ensure(c: dict, market: str, seeds: list[dict] | None = None, force: bool = False) -> dict:
    """조합이 없거나 실전 규칙이 바뀌었으면 새로 만들기 (현재 + 주변 조합 + 백테스트 추천)"""
    base = base_params(c)
    with _lock:
        d = _load(market)
        if d["variants"] and d["base_hash"] == _hash(base) and not force and not seeds:
            return d
        rnd = random.Random(_hash(base) + market)
        vs = [_new_variant("현재", base, "현재 실전 규칙")]
        for sd in (seeds or [])[:3]:
            p = {**base, **{k: v for k, v in sd.items() if k in TUNE_KEYS}}
            if _hash(p) not in {_hash(v["params"]) for v in vs}:
                vs.append(_new_variant(f"백테스트{len(vs)}", p, "백테스트 추천"))
        tries = 0
        while len(vs) < 8 and tries < 50:
            tries += 1
            p = _neighbor(base, rnd, 1 if len(vs) < 4 else 2)
            if _hash(p) not in {_hash(v["params"]) for v in vs}:
                vs.append(_new_variant(f"실험{len(vs)}", p, "주변 조합"))
        d["variants"], d["base_hash"] = vs, _hash(base)
        _save(market, d)
        return d


def step(c: dict, market: str, cands: dict, quotes: dict, minutes, fx: float, exits_only: bool,
         signal_surge, signal_swing, market_open, daily_closes, session_day: str) -> None:
    """한 바퀴: 조합마다 자기 규칙으로 가상 매도·매수 (실전 엔진이 부름)"""
    from . import toss
    d = ensure(c, market)
    need = {s for v in d["variants"] for s in v["pos"]} - set(quotes)
    if need:
        try:
            quotes = {**quotes, **toss.prices(sorted(need))}
        except toss.TossError:
            pass
    with _lock:
        d = _load(market)
        for v in d["variants"]:
            vc = {**c, **v["params"]}
            if v["day"] != session_day:
                v["day"], v["buys_today"] = session_day, 0
                for p in v["pos"].values():   # 단타 최고가는 날마다 새로 (스윙은 유지)
                    if p["style"] != "swing":
                        p["high"] = p["avg"]
            # 매도
            for sym in list(v["pos"]):
                p = v["pos"][sym]
                q = quotes.get(sym)
                if not q:
                    continue
                price = q["price"]
                p["high"] = max(p["high"], price)
                if p["style"] == "swing":
                    try:
                        closes = daily_closes(sym)
                    except Exception:
                        continue
                    days = (datetime.now(KST).date() - datetime.strptime(p["buy_day"], "%Y-%m-%d").date()).days
                    side, why = signal_swing(closes, price, p["qty"], p["avg"], p["high"], days, vc)
                else:
                    side, why = signal_surge(price, p["qty"], p["avg"], p["high"], minutes, False, vc, partial_done=p.get("partial", False))
                if side not in ("SELL", "SELL_PART") or not market_open(sym, "SELL"):
                    continue
                f = 1 if market == "KR" else fx
                qty = p["qty"] if side == "SELL" else max(1, int(p["qty"] * vc["partial_tp_ratio"]))
                if side == "SELL_PART" and qty >= p["qty"]:
                    p["partial"] = True
                    continue
                px = price * (1 - vc["limit_slip_pct"] / 100) if p["style"] == "surge" else price
                v["cash"] += qty * px * f
                v["trades"].append({"t": _now(), "sym": sym, "style": p["style"], "qty": qty, "pnl_pct": round((px / p["avg"] - 1) * 100, 2),
                                    "pnl_krw": round((px - p["avg"]) * qty * f), "why": why[:60], "final": side == "SELL"})
                if side == "SELL":
                    v["pos"].pop(sym)
                else:
                    p["qty"] -= qty; p["partial"] = True
            if exits_only:
                continue
            # 매수: 실전과 같은 후보, 기대 수익률 순, 이 조합의 진입 기준으로 거름
            per_pos = VIRTUAL / max(1, int(c["pick_count"]))
            for sym, cd in sorted(cands.items(), key=lambda kv: -kv[1]["est"]["ev"]):
                if sym in v["pos"] or len(v["pos"]) >= c["pick_count"] or v["buys_today"] >= c["max_orders_per_day"]:
                    continue
                q = quotes.get(sym)
                if not q or not market_open(sym, "BUY"):
                    continue
                ft = cd.get("features") or {}
                if cd["style"] == "surge":
                    if (ft.get("change") or 0) < vc["surge_min_change_pct"]:
                        continue
                    need_ratio = vc["pre_volume_ratio"] if ft.get("pre") else vc["surge_volume_ratio"]
                    if (ft.get("ratio") or 0) < need_ratio:
                        continue
                    if minutes is None or minutes > 360:
                        continue
                f = 1 if market == "KR" else fx
                px = q["price"] * (1 + vc["limit_slip_pct"] / 100) if cd["style"] == "surge" else q["price"]
                qty = int(min(per_pos, v["cash"]) // (px * f))
                if qty < 1:
                    continue
                v["cash"] -= qty * px * f
                v["buys_today"] += 1
                v["pos"][sym] = {"qty": qty, "avg": px, "high": px, "style": cd["style"], "partial": False,
                                 "buy_day": datetime.now(KST).strftime("%Y-%m-%d")}
        _save(market, d)


def stats(v: dict, days: int = 14) -> dict:
    since = (datetime.now(KST) - timedelta(days=days)).strftime("%Y-%m-%d")
    ts = [t for t in v["trades"] if t["t"] >= since]
    fin = [t for t in ts if t.get("final")]
    total = sum(t["pnl_krw"] for t in ts)
    return {"n": len(fin), "ret_pct": round(total / VIRTUAL * 100, 2),
            "win_rate": round(sum(1 for t in fin if t["pnl_pct"] > 0) / len(fin) * 100) if fin else 0,
            "avg_pct": round(sum(t["pnl_pct"] for t in fin) / len(fin), 2) if fin else 0, "holding": len(v["pos"])}


def overview(c: dict, market: str) -> dict:
    with _lock:
        d = _load(market)
    base = base_params(c)
    rows = []
    for v in d["variants"]:
        rows.append({"id": v["id"], "name": v["name"], "origin": v["origin"], "diff": _diff(v["params"], base), **stats(v, int(c.get("promote_window_days", 14)))})
    rows.sort(key=lambda r: -r["ret_pct"])
    return {"variants": rows, "promotions": d["promotions"][-10:][::-1]}


def maybe_promote(c: dict, market: str, apply_fn) -> str:
    """'현재'보다 확실히 나은 조합이 있으면 실전 규칙으로 교체. apply_fn(params, why, evidence) -> bool"""
    win = int(c.get("promote_window_days", 14))
    with _lock:
        d = _load(market)
    if not d["variants"]:
        return ""
    base_v = next((v for v in d["variants"] if v["name"] == "현재"), None)
    if not base_v:
        return ""
    bs = stats(base_v, win)
    best, bst = None, None
    for v in d["variants"]:
        if v is base_v:
            continue
        st = stats(v, win)
        if st["n"] >= c.get("promote_min_trades", 15) and (bst is None or st["ret_pct"] > bst["ret_pct"]):
            best, bst = v, st
    if not best or bst["ret_pct"] <= 0 or bst["ret_pct"] - bs["ret_pct"] < c.get("promote_margin_pct", 1.0):
        return ""
    ev = (f"최근 {win}일 가상: '{best['name']}' {bst['n']}건 총 {bst['ret_pct']:+.2f}% (승률 {bst['win_rate']}%) vs "
          f"'현재' {bs['n']}건 {bs['ret_pct']:+.2f}%")
    why = f"그림자 가상 매매에서 더 나은 규칙 조합 → 실전 적용 ({_diff(best['params'], base_v['params'])})"
    if not apply_fn(best["params"], why, ev):
        return ""
    with _lock:
        d = _load(market)
        d["promotions"].append({"t": _now(), "name": best["name"], "diff": _diff(best["params"], base_v["params"]), "evidence": ev})
        d["variants"] = []   # 다음 ensure 에서 새 기준으로 다시 만들기
        _save(market, d)
    return why + " · " + ev


def add_seeds(c: dict, market: str, seeds: list[dict]) -> int:
    """백테스트 추천을 조합에 넣기: 예전 백테스트 조합과 성적이 가장 나쁜 주변 조합을 바꿔서 (나머지 성적은 유지)"""
    d = ensure(c, market)
    base = base_params(c)
    with _lock:
        d = _load(market)
        have = {_hash(v["params"]) for v in d["variants"]}
        new = []
        for sd in seeds[:3]:
            p = {**base, **{k: v for k, v in sd.items() if k in TUNE_KEYS}}
            if _hash(p) not in have:
                new.append(_new_variant(f"백테스트{len(d['variants']) + len(new)}", p, "백테스트 추천"))
        if not new:
            return 0
        keep = [v for v in d["variants"] if v["origin"] != "백테스트 추천"]
        explorers = sorted([v for v in keep if v["name"] != "현재"], key=lambda v: stats(v)["ret_pct"])
        while len(keep) + len(new) > 8 and explorers:
            keep.remove(explorers.pop(0))
        d["variants"] = keep + new
        _save(market, d)
        return len(new)
