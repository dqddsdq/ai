"""📚 자동매매 학습 — 장중 기록·장중 조심 규칙·장 마감 후 복기·하루마다 규칙 조정 제안.

장중 (실시간)
- 거래 일지: 산 종목마다 산 뒤 최대 상승(MFE)·최대 하락(MAE)을 계속 갱신, 매수 시점 조건(상승률·거래량 배수·프리장 여부·시간) 기록
- 놓친 종목: 골랐지만 못 산 종목이 그 뒤 얼마나 움직였는지 추적
- 조심 규칙 (자동, 더 조심하는 방향만): 연속 손절 N번 → 잠시 매수 쉬기 / 오늘 프리장 매수가 2번 다 손실 → 오늘 프리장 매수 중단

장 마감 후
- 복기: 승률·평균 수익/손실·조건별 성적·놓친 종목 → AI 가 쉬운 말로 요약
- 조정 제안: 최근 거래(최대 30건)를 근거로 숫자 규칙 변경을 제안 → 사장님이 [적용]해야 바뀜 · [되돌리기] 가능

기록: _cache/trade_journal.json (git 에 안 올라감). 자기 개선 기능은 이 파일을 못 고침.
"""

from __future__ import annotations

import json
import statistics as st
import threading
import uuid
from datetime import datetime, timedelta, timezone

from . import store
from .agents import COMPANY_DIR

FILE = COMPANY_DIR / "_cache" / "trade_journal.json"
KST = timezone(timedelta(hours=9))
_lock = threading.RLock()
TUNABLE = {"partial_tp_pct": "나눠 팔기 수익률(%)", "stop_loss_pct": "손절(%)", "trail_pct": "고점 대비 하락 매도(%)",
           "pre_volume_ratio": "프리장 거래량 기준", "us_premarket": "프리장 매매", "surge_min_change_pct": "급등 최소 상승률(%)",
           "surge_max_change_pct": "급등 최대 상승률(%)", "surge_volume_ratio": "거래량 배수 기준"}
MIN_TRADES = 5          # 이보다 적으면 규칙 조정 제안 안 함 (우연일 수 있어서)


def _load() -> dict:
    d = store.load(FILE)   # 메모리 보관 · 깨지면 백업에서 복구
    for k, v in (("trades", []), ("watch", {}), ("reviews", []), ("proposals", []), ("guard", {})):
        d.setdefault(k, v)
    return d


def _save(d: dict) -> None:
    d["trades"] = d["trades"][-500:]
    d["reviews"] = d["reviews"][-60:]
    d["proposals"] = d["proposals"][-100:]
    store.save(FILE, d)


def _now() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


def _tm(t: dict) -> str:
    """거래의 시장 (예전 기록은 종목코드로 추정)"""
    m = (t.get("features") or {}).get("mkt")
    return m if m in ("US", "KR") else ("KR" if str(t.get("sym", "")).isdigit() else "US")


def _ts(t: dict) -> str:
    """거래의 종류: surge(단타) / swing(스윙)"""
    return (t.get("features") or {}).get("style") or "surge"


def _pnl(t: dict) -> float:
    q = sum(x["qty"] for x in t["exits"])
    return sum(x["qty"] * (x["px"] / t["entry_px"] - 1) for x in t["exits"]) / q * 100 if q and t["entry_px"] else 0.0


# ── 장중 기록 ──
def open_trade(sym: str, qty: float, px: float, features: dict | None, mode: str, day: str) -> None:
    with _lock:
        d = _load()
        t = next((x for x in d["trades"] if x["sym"] == sym and not x["closed"] and x["mode"] == mode), None)
        if t:   # 같은 종목 추가 매수 → 평단 합치기
            tot = t["qty_in"] + qty
            t["entry_px"] = (t["entry_px"] * t["qty_in"] + px * qty) / tot
            t["qty_in"] = tot
        else:
            d["trades"].append({"id": uuid.uuid4().hex[:10], "day": day, "sym": sym, "mode": mode, "entry_time": _now(),
                                "entry_px": px, "qty_in": qty, "features": features or {}, "mfe": 0.0, "mae": 0.0,
                                "exits": [], "closed": False})
        d["watch"].get(day, {}).get(sym, {}).update(bought=True)
        _save(d)


def mark(prices: dict[str, float], mode: str) -> None:
    """보유 중인 거래의 최대 상승·하락 갱신 (10초마다)"""
    with _lock:
        d = _load()
        changed = False
        for t in d["trades"]:
            if t["closed"] or t["mode"] != mode or t["sym"] not in prices or not t["entry_px"]:
                continue
            r = (prices[t["sym"]] / t["entry_px"] - 1) * 100
            if r > t["mfe"]:
                t["mfe"] = round(r, 2); changed = True
            if r < t["mae"]:
                t["mae"] = round(r, 2); changed = True
        if changed:
            _save(d)


def on_sell(sym: str, qty: float, px: float, reason: str, mode: str, closed_all: bool, cfg: dict) -> None:
    with _lock:
        d = _load()
        t = next((x for x in d["trades"] if x["sym"] == sym and not x["closed"] and x["mode"] == mode), None)
        if not t:
            return
        t["exits"].append({"t": _now(), "qty": qty, "px": px, "reason": reason})
        r = (px / t["entry_px"] - 1) * 100 if t["entry_px"] else 0
        t["mfe"], t["mae"] = max(t["mfe"], round(r, 2)), min(t["mae"], round(r, 2))
        if closed_all:
            t["closed"] = True
            t["pnl_pct"] = round(_pnl(t), 2)
            t["exit_kind"] = _kind(t["exits"][-1]["reason"])
            try:
                t["hold_min"] = round((datetime.strptime(t["exits"][-1]["t"], "%Y-%m-%d %H:%M:%S") -
                                       datetime.strptime(t["entry_time"], "%Y-%m-%d %H:%M:%S")).total_seconds() / 60)
            except ValueError:
                t["hold_min"] = None
            _update_guard(d, t, cfg)
        _save(d)


def _kind(reason: str) -> str:
    for key, name in (("AI 판단", "AI 판단"), ("손절", "손절"), ("본전", "본전 정리"), ("익절", "익절"), ("고점 대비", "수익 지키기"), ("마감", "마감 정리")):
        if key in reason:
            return name
    return "기타"


def _update_guard(d: dict, t: dict, cfg: dict) -> None:
    """장중 조심 규칙 (자동): 연속 손실 → 잠시 쉬기 / 오늘 프리장 매수 2번 다 손실 → 오늘 프리장 매수 중단"""
    gk = f"{_tm(t)}:{t['day']}"
    if not isinstance(d["guard"].get(gk), dict) or "day" in d["guard"]:
        d["guard"] = {k: v for k, v in d["guard"].items() if isinstance(v, dict) and ":" in k}   # 예전 형식 정리
    for k in sorted(d["guard"])[:-6]:
        d["guard"].pop(k)
    g = d["guard"].setdefault(gk, {"day": gk})
    if t["pnl_pct"] < 0:
        g["streak"] = g.get("streak", 0) + 1
    else:
        g["streak"] = 0
    if g["streak"] >= int(cfg.get("loss_streak_pause", 3)):
        until = datetime.now(KST) + timedelta(minutes=int(cfg.get("pause_min", 30)))
        g["pause_until"] = until.strftime("%Y-%m-%d %H:%M:%S")
        g["pause_why"] = f"{g['streak']}번 연속 손실 → {cfg.get('pause_min', 30)}분 매수 쉬기"
        g["streak"] = 0
    if t.get("features", {}).get("pre"):
        pre = [x for x in d["trades"] if x["day"] == t["day"] and _tm(x) == _tm(t) and x["closed"] and x.get("features", {}).get("pre")]
        if len(pre) >= 2 and all(x["pnl_pct"] < 0 for x in pre):
            g["no_premarket"] = f"오늘 프리장 매수 {len(pre)}번 모두 손실 → 오늘은 프리장 매수 중단"


def guard(day: str, premarket_now: bool) -> str:
    """지금 매수를 쉬어야 하면 이유, 아니면 ''"""
    with _lock:
        g = _load()["guard"].get(day) or {}
    if not isinstance(g, dict) or not g:
        return ""
    if g.get("pause_until") and _now() < g["pause_until"]:
        return g.get("pause_why", "잠시 쉬기") + f" (~{g['pause_until'][11:16]})"
    if premarket_now and g.get("no_premarket"):
        return g["no_premarket"]
    return ""


def watch(day: str, picks: list[dict], prices: dict[str, float]) -> None:
    """고른 종목(산 것·못 산 것 모두)의 이후 움직임 기록"""
    with _lock:
        d = _load()
        w = d["watch"].setdefault(day, {})
        for p in picks:
            w.setdefault(p["symbol"], {"pick_px": p["price"], "t": _now(), "max": p["price"], "min": p["price"], "bought": False,
                                      "change": p.get("change"), "pre": p.get("pre"), "ratio": p.get("ratio")})
        for sym, x in w.items():
            if sym in prices:
                x["max"], x["min"] = max(x["max"], prices[sym]), min(x["min"], prices[sym])
        for k in sorted(d["watch"])[:-5]:   # 최근 5일만 보관
            d["watch"].pop(k)
        _save(d)


def flag_watch(day: str, sym: str, **kw) -> None:
    """고른 종목에 표시 (예: AI 차트 판단 찬성/반대) — 복기에서 'AI 가 반대한 종목은 그 뒤 어땠나' 비교용"""
    with _lock:
        d = _load()
        x = d["watch"].get(day, {}).get(sym)
        if x is not None:
            x.update(kw); _save(d)


def watch_symbols(day: str) -> list[str]:
    with _lock:
        return list(_load()["watch"].get(day, {}))[:30]


# ── 통계 ──
def _stats(ts: list[dict]) -> dict:
    if not ts:
        return {"n": 0}
    pn = [t["pnl_pct"] for t in ts]
    wins = [p for p in pn if p > 0]
    loss = [p for p in pn if p <= 0]
    return {"n": len(ts), "win_rate": round(len(wins) / len(ts) * 100), "avg": round(st.mean(pn), 2),
            "avg_win": round(st.mean(wins), 2) if wins else 0, "avg_loss": round(st.mean(loss), 2) if loss else 0,
            "sum": round(sum(pn), 2), "mfe_med": round(st.median([t["mfe"] for t in ts]), 2),
            "mae_med": round(st.median([t["mae"] for t in ts]), 2)}


# ── 확률 추정 (비슷한 조건의 지난 거래로 승률·기대 수익) ──
CHANGE_EDGES = [0, 8, 12, 20, 1e9]
RATIO_EDGES = [0, 3, 5, 10, 1e9]
PRE_RATIO_EDGES = [0, 0.3, 0.6, 1.5, 1e9]


def _bin(v, edges) -> int:
    if v is None:
        return -1
    for i, (lo, hi) in enumerate(zip(edges[:-1], edges[1:])):
        if lo <= v < hi:
            return i
    return -1


R20_EDGES = [-1e9, 5, 10, 20, 1e9]
RSI_EDGES = [0, 55, 62, 100]


def _key(f: dict) -> tuple:
    """비슷한 조건 묶음: 시장 · 종류(단타/스윙) · (단타) 프리장·상승률·거래량 구간 / (스윙) 20일 수익률·RSI 구간"""
    mkt, style = f.get("mkt") or "US", f.get("style") or "surge"
    if style == "swing":
        return (mkt, style, _bin(f.get("r20"), R20_EDGES), _bin(f.get("rsi"), RSI_EDGES))
    pre = bool(f.get("pre"))
    return (mkt, style, pre, _bin(f.get("change"), CHANGE_EDGES), _bin(f.get("ratio"), PRE_RATIO_EDGES if pre else RATIO_EDGES))


def _tkey(t: dict) -> tuple:
    f = dict(t.get("features") or {})
    f.setdefault("mkt", _tm(t)); f.setdefault("style", _ts(t))
    return _key(f)


def estimate(features: dict, cfg: dict) -> dict:
    """살 후보의 승률·기대 수익 추정. 기록이 적으면 전체 승률(없으면 40%) 쪽으로 보수적으로 당김.
    반환: {p, n, ev, avg_win, avg_loss, basis}"""
    f0 = dict(features); f0.setdefault("mkt", "US"); f0.setdefault("style", "surge")
    with _lock:   # 같은 시장·같은 종류(단타/스윙) 거래만 기준으로
        ts = [t for t in _load()["trades"] if t["closed"] and _tm(t) == f0["mkt"] and _ts(t) == f0["style"]][-200:]
    wins_all = [t["pnl_pct"] for t in ts if t["pnl_pct"] > 0]
    loss_all = [t["pnl_pct"] for t in ts if t["pnl_pct"] <= 0]
    base_p = len(wins_all) / len(ts) if len(ts) >= 10 else 0.4
    swing = f0["style"] == "swing"
    avg_win = st.mean(wins_all) if len(wins_all) >= 3 else (float(cfg.get("swing_trail_pct", 8)) if swing else float(cfg.get("partial_tp_pct") or 5) * 0.8)
    avg_loss = st.mean(loss_all) if len(loss_all) >= 3 else -float(cfg.get("swing_stop_pct", 6) if swing else cfg.get("stop_loss_pct", 3))
    k = _key(f0)
    exact = [t for t in ts if _tkey(t) == k]
    part = [t for t in ts if _tkey(t)[:3] == k[:3] and _tkey(t)[-1] == k[-1]]   # 같은 시장·종류·(프리장/구간 하나) 묶음
    sample = exact if len(exact) >= 5 else (part if len(part) >= 5 else exact)
    n = len(sample)
    w = sum(1 for t in sample if t["pnl_pct"] > 0)
    prior = 4.0
    p = (w + base_p * prior) / (n + prior)
    if n >= 3:
        sw = [t["pnl_pct"] for t in sample if t["pnl_pct"] > 0]
        sl = [t["pnl_pct"] for t in sample if t["pnl_pct"] <= 0]
        avg_win = st.mean(sw) if sw else avg_win
        avg_loss = st.mean(sl) if sl else avg_loss
    ev = p * avg_win + (1 - p) * avg_loss
    basis = ("같은 조건" if sample is exact else "비슷한 조건") + f" {n}건" if n else f"기록 없음 → 기본 {base_p * 100:.0f}%"
    return {"p": round(p, 3), "n": n, "ev": round(ev, 2), "avg_win": round(avg_win, 2), "avg_loss": round(avg_loss, 2), "basis": basis}


def size_ratio(est: dict, cfg: dict) -> tuple[float, str]:
    """승률 → 살 금액 비율 (min_size_ratio ~ 1.0). 기록이 충분한데 기대 수익이 마이너스면 0 (건너뛰기)"""
    lo = float(cfg.get("min_size_ratio", 0.25))
    if (est["n"] >= 5 and est["ev"] < 0) or (est["n"] >= 3 and est["ev"] < -1):
        return 0.0, f"비슷한 거래 {est['n']}건 기대 수익 {est['ev']:+.2f}% → 건너뜀"
    if cfg.get("min_win_prob") and est["p"] < cfg["min_win_prob"] / 100:
        return 0.0, f"추정 승률 {est['p'] * 100:.0f}% < 기준 {cfg['min_win_prob']}% → 건너뜀"
    r = lo + (1 - lo) * min(1.0, max(0.0, (est["p"] - 0.35) / 0.30))
    return round(r, 2), f"추정 승률 {est['p'] * 100:.0f}% ({est['basis']}) · 기대 {est['ev']:+.2f}% → 금액 {r * 100:.0f}%"


def _bucket(ts: list[dict], key, edges: list[float], unit: str) -> dict:
    out = {}
    for lo, hi in zip(edges[:-1], edges[1:]):
        g = [t for t in ts if t.get("features", {}).get(key) is not None and lo <= t["features"][key] < hi]
        if g:
            out[f"{lo:g}~{hi:g}{unit}" if hi < 1e9 else f"{lo:g}{unit}+"] = _stats(g)
    return out


def today_stats(day: str, mode: str, market: str | None = None) -> dict:
    with _lock:
        d = _load()
    ts = [t for t in d["trades"] if t["day"] == day and t["closed"] and t["mode"] == mode and (not market or _tm(t) == market)]
    open_ = [t for t in d["trades"] if not t["closed"] and t["mode"] == mode and (not market or _tm(t) == market)]
    s = _stats(ts)
    s["open"] = [{"sym": t["sym"], "mfe": t["mfe"], "mae": t["mae"]} for t in open_]
    g = d["guard"].get(f"{market}:{day}") if market else None
    s["guard"] = g if isinstance(g, dict) else {}
    return s


# ── 복기·제안 ──
def _propose(d: dict, cfg: dict, param: str, to, why: str, evidence: str) -> dict | None:
    cur = cfg.get(param)
    if cur == to or param not in TUNABLE:
        return None
    for p in d["proposals"]:   # 같은 규칙의 예전 대기 제안은 새 제안으로 교체
        if p["param"] == param and p["status"] == "pending":
            p["status"] = "replaced"
    p = {"id": uuid.uuid4().hex[:8], "param": param, "label": TUNABLE[param], "from": cur, "to": to, "why": why,
         "evidence": evidence, "created": _now(), "status": "pending"}
    d["proposals"].append(p)
    return p


def _r(x: float, step: float = 0.5) -> float:
    return round(round(x / step) * step, 2)


def daily_review(day: str, cfg: dict, mode: str, use_ai: bool = True) -> dict:
    """장 마감 후 복기 + 규칙 조정 제안. 같은 날 다시 부르면 덮어씀."""
    with _lock:
        d = _load()
    mk = cfg.get("market", "US")
    today = [t for t in d["trades"] if t["day"] == day and t["closed"] and t["mode"] == mode and _tm(t) == mk]
    recent = [t for t in d["trades"] if t["closed"] and t["mode"] == mode and _tm(t) == mk][-30:]
    w = d["watch"].get(f"{mk}:{day}") or d["watch"].get(day, {})
    missed = [(s, (x["max"] / x["pick_px"] - 1) * 100) for s, x in w.items() if not x.get("bought") and x.get("pick_px")]
    rep = {"day": day, "mode": mode, "market": mk, "created": _now(),
           "by_style": {k: v for k, v in (("단타", _stats([t for t in recent if _ts(t) == "surge"])), ("스윙", _stats([t for t in recent if _ts(t) == "swing"]))) if v["n"]}, "today": _stats(today), "recent": _stats(recent),
           "by_exit": {k: _stats([t for t in today if t.get("exit_kind") == k]) for k in {t.get("exit_kind") for t in today}},
           "by_change": _bucket(recent, "change", [0, 8, 12, 20, 1e9], "%"),
           "by_ratio": _bucket(recent, "ratio", [0, 3, 5, 10, 1e9], "배"),
           "by_session": {k: v for k, v in (("프리장", _stats([t for t in recent if t.get("features", {}).get("pre")])),
                                            ("정규장", _stats([t for t in recent if not t.get("features", {}).get("pre")]))) if v["n"]},
           "missed": {"n": len(missed), "avg_max_up": round(st.mean([m for _, m in missed]), 2) if missed else None,
                      "top": sorted(missed, key=lambda x: -x[1])[:3]},
           "by_prob": _bucket(recent, "p", [0, 0.4, 0.5, 0.6, 1.01], ""),   # 추정 승률 구간별 실제 성적 (추정이 맞는지)
           "ai_chart": {"찬성해서 산 거래": _stats([t for t in recent if (t.get("features") or {}).get("ai_chart") == "찬성"]),
                        "반대한 종목 그 뒤 최대 상승 평균": round(st.mean([(x["max"] / x["pick_px"] - 1) * 100 for x in w.values()
                                                             if x.get("ai") == "반대" and x.get("pick_px")]), 2)
                        if any(x.get("ai") == "반대" for x in w.values()) else None},
           "best": max(today, key=lambda t: t["pnl_pct"], default=None), "worst": min(today, key=lambda t: t["pnl_pct"], default=None)}
    for k in ("best", "worst"):
        if rep[k]:
            t = rep[k]; rep[k] = {"sym": t["sym"], "pnl": t["pnl_pct"], "mfe": t["mfe"], "mae": t["mae"], "exit": t.get("exit_kind")}

    proposals = []
    with _lock:
        d = _load()
        if len(recent) >= MIN_TRADES:
            n = len(recent)
            mfes = sorted(t["mfe"] for t in recent)
            # 1) 나눠 팔기 수익률: 거의 안 닿으면 낮추고, 대부분 닿으면 높임
            if cfg.get("partial_tp_pct"):
                reach = sum(1 for m in mfes if m >= cfg["partial_tp_pct"]) / n
                if reach < 0.25 and st.median(mfes) >= 2:
                    to = max(3.0, min(cfg["partial_tp_pct"] - 0.5, _r(mfes[int(n * 0.6)])))
                    proposals.append(_propose(d, cfg, "partial_tp_pct", to, "나눠 팔기 가격에 닿는 거래가 너무 적어요",
                                              f"최근 {n}건 중 +{cfg['partial_tp_pct']}% 도달 {reach * 100:.0f}% · 산 뒤 최대 상승 중간값 {st.median(mfes):+.1f}%"))
                elif reach > 0.7:
                    proposals.append(_propose(d, cfg, "partial_tp_pct", min(cfg["take_profit_pct"] - 1, cfg["partial_tp_pct"] + 1),
                                              "대부분 나눠 팔기 가격을 넘어 더 올라요 — 조금 더 들고 가기",
                                              f"최근 {n}건 중 +{cfg['partial_tp_pct']}% 도달 {reach * 100:.0f}%"))
            # 2) 손절: 결국 수익 난 거래도 손절선 근처까지 자주 빠졌다면 → 손절이 너무 빡빡
            winners = [t for t in recent if t["pnl_pct"] > 0]
            if len(winners) >= 3:
                near = sum(1 for t in winners if t["mae"] <= -cfg["stop_loss_pct"] * 0.8) / len(winners)
                if near >= 0.3 and cfg["stop_loss_pct"] < 8:
                    proposals.append(_propose(d, cfg, "stop_loss_pct", _r(cfg["stop_loss_pct"] + 0.5), "수익 난 거래도 손절선 근처까지 자주 빠졌어요 — 조금 여유",
                                              f"수익 거래 {len(winners)}건 중 {near * 100:.0f}%가 -{cfg['stop_loss_pct'] * 0.8:.1f}% 이하까지 하락 후 반등"))
            # 3) 고점 대비 하락 매도: 최고점에서 너무 많이 돌려주면 → 더 빨리 팔기
            trail = [t for t in recent if t.get("exit_kind") == "수익 지키기"]
            if len(trail) >= 3:
                give = st.mean(t["mfe"] - t["pnl_pct"] for t in trail)
                if give > cfg["trail_pct"] * 1.2 and cfg["trail_pct"] > 2:
                    proposals.append(_propose(d, cfg, "trail_pct", _r(cfg["trail_pct"] - 0.5), "최고점에서 수익을 많이 돌려주고 팔고 있어요",
                                              f"'수익 지키기' 매도 {len(trail)}건 평균: 최고 +{st.mean(t['mfe'] for t in trail):.1f}% → 판 가격 {st.mean(t['pnl_pct'] for t in trail):+.1f}%"))
            # 4) 프리장 성적
            pre = [t for t in recent if t.get("features", {}).get("pre")]
            if len(pre) >= 3:
                s_ = _stats(pre)
                if s_["win_rate"] < 20 and len(pre) >= 5 and cfg.get("us_premarket"):
                    proposals.append(_propose(d, cfg, "us_premarket", False, "프리장 매수 성적이 계속 나빠요 — 정규장부터만",
                                              f"프리장 {len(pre)}건 승률 {s_['win_rate']}% · 평균 {s_['avg']:+.2f}%"))
                elif s_["win_rate"] < 35 and s_["avg"] < 0:
                    proposals.append(_propose(d, cfg, "pre_volume_ratio", round(min(5, cfg.get("pre_volume_ratio", 0.2) * 1.5), 2),
                                              "프리장은 거래가 더 많이 몰린 종목만", f"프리장 {len(pre)}건 승률 {s_['win_rate']}% · 평균 {s_['avg']:+.2f}%"))
            # 5) 매수 시점 상승률: 덜 오른 걸 산 게 나쁘면 기준 올리고, 너무 오른 걸 산 게 나쁘면 상한 내림
            ch = [t for t in recent if t.get("features", {}).get("change") is not None]
            if len(ch) >= 6:
                med = st.median(t["features"]["change"] for t in ch)
                low = [t["pnl_pct"] for t in ch if t["features"]["change"] < med]
                high = [t["pnl_pct"] for t in ch if t["features"]["change"] >= med]
                if low and high and st.mean(low) < 0 < st.mean(high) and st.mean(high) - st.mean(low) > 2:
                    proposals.append(_propose(d, cfg, "surge_min_change_pct", _r(max(cfg["surge_min_change_pct"] + 0.5, med)),
                                              "덜 오른 종목을 산 게 성적이 나빠요", f"상승률 {med:.1f}% 미만에서 산 거래 평균 {st.mean(low):+.2f}% vs 이상 {st.mean(high):+.2f}%"))
                elif low and high and st.mean(high) < 0 < st.mean(low) and st.mean(low) - st.mean(high) > 2:
                    proposals.append(_propose(d, cfg, "surge_max_change_pct", _r(max(cfg["surge_min_change_pct"] + 3, med), 1),
                                              "이미 너무 오른 종목을 산 게 성적이 나빠요", f"상승률 {med:.1f}% 이상에서 산 거래 평균 {st.mean(high):+.2f}% vs 미만 {st.mean(low):+.2f}%"))
            # 6) 거래량 배수
            rt = [t for t in recent if t.get("features", {}).get("ratio") is not None and not t["features"].get("pre")]
            if len(rt) >= 6:
                med = st.median(t["features"]["ratio"] for t in rt)
                low = [t["pnl_pct"] for t in rt if t["features"]["ratio"] < med]
                high = [t["pnl_pct"] for t in rt if t["features"]["ratio"] >= med]
                if low and high and st.mean(low) < 0 < st.mean(high) and st.mean(high) - st.mean(low) > 2:
                    proposals.append(_propose(d, cfg, "surge_volume_ratio", _r(max(cfg["surge_volume_ratio"] + 0.5, med)),
                                              "거래량이 덜 붙은 종목을 산 게 성적이 나빠요", f"거래량 {med:.1f}배 미만 평균 {st.mean(low):+.2f}% vs 이상 {st.mean(high):+.2f}%"))
        rep["proposals"] = [p["id"] for p in proposals if p]
        rep["note"] = "" if len(recent) >= MIN_TRADES else f"거래가 {len(recent)}건뿐이라 규칙 조정 제안은 {MIN_TRADES}건 이상 쌓이면 시작해요"
        d["reviews"] = [r for r in d["reviews"] if not (r["day"] == day and r["mode"] == mode and r.get("market", "US") == mk)] + [rep]
        _save(d)

    if use_ai and (today or missed):
        rep["text"] = _ai_text(rep, [p for p in proposals if p])
        with _lock:
            d = _load()
            for r in d["reviews"]:
                if r["day"] == day and r["mode"] == mode and r.get("market", "US") == mk:
                    r["text"] = rep["text"]
            _save(d)
    return rep


def _ai_text(rep: dict, proposals: list[dict]) -> str:
    from . import providers as pv
    from . import team as tm
    data = {k: rep[k] for k in ("today", "recent", "by_exit", "by_change", "by_ratio", "by_session", "missed", "best", "worst")}
    prompt = ("자동매매 하루 기록이에요. 사장님께 5~7줄로 쉬운 말 복기를 써 주세요: 오늘 결과, 잘된 점, 아쉬운 점, 내일 주의할 점. "
              "숫자는 아래 값만 쓰고 지어내지 마세요. 투자 권유 문구는 빼고요.\n"
              f"기록: {json.dumps(data, ensure_ascii=False)}\n제안된 규칙 조정: {json.dumps([{k: p[k] for k in ('label', 'from', 'to', 'why')} for p in proposals], ensure_ascii=False)}")
    try:
        return pv.chat(tm.fast_spec(), "너는 트레이딩 복기 담당이다. 짧고 정확하게.", [{"role": "user", "content": prompt}],
                       effort="low", timeout=120).strip()
    except Exception as e:
        return f"(AI 요약 실패: {type(e).__name__} — 숫자 표는 그대로 보세요)"


def record_auto(param: str, label: str, frm, to, why: str, evidence: str) -> dict:
    """자동 조정 기록 (이미 적용된 상태로 저장 → 화면에서 [되돌리기] 가능)"""
    with _lock:
        d = _load()
        p = {"id": uuid.uuid4().hex[:8], "param": param, "label": label, "from": frm, "to": to, "why": why, "evidence": evidence,
             "created": _now(), "status": "applied", "prev": frm, "applied": _now(), "auto": True}
        d["proposals"].append(p)
        _save(d)
    return p


def decide(pid: str, action: str, auto: bool = False, to=None) -> dict:
    """제안 적용/거절/되돌리기 (auto=True 면 자동 조정으로 적용, to 로 범위 안 값으로 바꿔 적용 가능)"""
    from . import autotrade as at
    with _lock:
        d = _load()
        p = next((x for x in d["proposals"] if x["id"] == pid), None)
        if not p:
            return {"ok": False, "message": "없는 제안이에요"}
        if action == "apply" and p["status"] == "pending":
            if to is not None:
                p["to"] = to
            prev = at.config().get(p["param"])
            if prev == p["to"]:
                p.update(status="rejected", why=p["why"] + " (이미 범위 끝이라 그대로)")
                _save(d)
                return {"ok": False, "message": "범위 끝이라 그대로"}
            r = at.save_config({p["param"]: p["to"]}, _by_auto=auto)
            if auto:
                p["auto"] = True
            if not r.get("ok"):
                return r
            p.update(status="applied", prev=prev, applied=_now())
        elif action == "reject" and p["status"] == "pending":
            p.update(status="rejected")
        elif action == "revert" and p["status"] == "applied":
            r = at.save_config({p["param"]: p["prev"]})
            if p["param"] == "mode":
                at.note_user_mode(p["prev"])
            if not r.get("ok"):
                return r
            p.update(status="reverted")
        else:
            return {"ok": False, "message": "지금 상태에서는 할 수 없어요"}
        _save(d)
    at.event("자동 조정" if auto else "학습", f"{'자동 적용' if auto else '제안 ' + dict(apply='적용', reject='거절', revert='되돌림')[action]}: {p['label']} {p['from']} → {p['to']}"
             + (f" — {p['why']}" if auto else ""))
    return {"ok": True, "proposal": p}


def overview(day: str, mode: str, market: str | None = None) -> dict:
    with _lock:
        d = _load()
    revs = [r for r in d["reviews"] if r["mode"] == mode and (not market or r.get("market", "US") == market)]
    return {"today": today_stats(day, mode, market), "review": revs[-1] if revs else None,
            "proposals": [p for p in d["proposals"] if p["status"] in ("pending", "applied")][-20:][::-1],
            "trades": [t for t in d["trades"] if t["mode"] == mode and (not market or _tm(t) == market)][-30:][::-1]}


def day_sums(mode: str, n: int = 5, market: str | None = None) -> list[float]:
    """최근 복기 n일의 하루 손익 합(%) — 오래된 것 → 최근"""
    with _lock:
        revs = [r for r in _load()["reviews"] if r["mode"] == mode and r["today"].get("n") and (not market or r.get("market", "US") == market)]
    return [r["today"]["sum"] for r in revs[-n:]]


def trades_since(mode: str, since: str, market: str | None = None) -> list[dict]:
    with _lock:
        return [t for t in _load()["trades"] if t["mode"] == mode and t["closed"] and t["entry_time"] >= since and (not market or _tm(t) == market)]


def last_review_text(mode: str, market: str | None = None) -> str:
    with _lock:
        revs = [r for r in _load()["reviews"] if r["mode"] == mode and (not market or r.get("market", "US") == market)]
    if not revs:
        return ""
    r = revs[-1]
    t = r["today"]
    head = f"최근 복기({r['day']}): 거래 {t.get('n', 0)}건" + (f" · 승률 {t['win_rate']}% · 평균 {t['avg']:+.2f}%" if t.get("n") else "")
    return head + ("\n" + r["text"] if r.get("text") else "")
