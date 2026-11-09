"""📼 밤마다 백테스트 — 지난 며칠 실제로 본 후보로 '이 규칙이었으면?'을 수백 가지 조합으로 계산.

- 단타: 고른 종목(학습 기록의 '고른 종목' + 거래 일지)마다 그날 1분봉을 받아, 고른 시각부터 장 마감까지 매도 규칙을 그대로 돌려 봄
  진입 기준(급등 최소 상승률·거래량 배수)도 같이 바꿔 가며 '어떤 후보만 샀으면'까지
- 스윙: 고른 스윙 후보마다 이후 일봉으로 스윙 매도 규칙을 돌려 봄
- 결과 상위 조합은 그림자 가상 매매(shadow)로 넘겨 '앞으로의 실제 시장'에서 다시 검증 → 과거에만 맞는 규칙이 바로 실전에 들어가지 않게
기록: _cache/backtest_US.json / backtest_KR.json · 1분봉은 _cache/bt/ 에 보관
"""

from __future__ import annotations

import itertools
import json
import statistics as st
from datetime import datetime, timedelta, timezone

from . import picker, toss
from .agents import COMPANY_DIR

KST = timezone(timedelta(hours=9))
CACHE = COMPANY_DIR / "_cache" / "bt"
SURGE_GRID = {"stop_loss_pct": [2, 3, 4, 5], "partial_tp_pct": [0, 3, 4, 6], "trail_pct": [3, 4, 6],
              "surge_min_change_pct": [5, 8, 12], "vol": [0, 1, 2]}        # vol: 거래량 기준 단계 (낮음/보통/높음)
VOL_STEPS = {False: [2, 3, 6], True: [0.1, 0.2, 0.5]}                        # 정규장 배수 / 프리장 비율
SWING_GRID = {"swing_stop_pct": [4, 6, 8], "swing_trail_pct": [5, 8, 12]}


def _file(m: str):
    return COMPANY_DIR / "_cache" / f"backtest_{m}.json"


def load(m: str) -> dict:
    try:
        return json.loads(_file(m).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _parse_kst(t: str) -> datetime:
    return datetime.strptime(t[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)


def _ts(t: str) -> datetime:
    return datetime.fromisoformat(t.replace("Z", "+00:00"))


def _minute_path(sym: str, day: str, since: datetime, market: str) -> list[dict]:
    """그날(현지) 고른 시각 이후 1분봉 (보관해 둔 것 우선)"""
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"{market}_{sym}_{day}.json"
    if f.exists():
        cs = json.loads(f.read_text())
    else:
        cs, before = [], None
        for _ in range(8):   # 최대 1600분 (며칠 전까지)
            r = toss._get("/api/v1/candles", symbol=sym, interval="1m", count=200, before=before) or {}
            page = r.get("candles") or []
            cs += page
            before = r.get("nextBefore")
            if not page or not before or min(_ts(c["timestamp"]) for c in page) < since - timedelta(hours=2):
                break
        cs = [{"t": c["timestamp"], "c": float(c["closePrice"]), "h": float(c.get("highPrice") or c["closePrice"]),
               "l": float(c.get("lowPrice") or c["closePrice"])} for c in cs]
        cs.sort(key=lambda c: c["t"])
        if day < picker.session_date(market):   # 끝난 날만 보관
            f.write_text(json.dumps(cs))
    return [c for c in cs if _ts(c["t"]) >= since and picker.session_date(market, _ts(c["t"])) == day]


def _sim_surge(path: list[dict], entry: float, params: dict, base_cfg: dict, signal_surge, market: str) -> float | None:
    """1분봉 경로 위에서 단타 매도 규칙 돌리기 → 수익률(%) (나눠 팔기 반영, 끝까지 안 팔리면 마지막 값)"""
    if not path:
        return None
    vc = {**base_cfg, **params}
    px = entry * (1 + vc["limit_slip_pct"] / 100)
    qty, high, partial, got = 1.0, px, False, 0.0
    for c in path:
        mins = picker.session_minutes(market, _ts(c["t"]), extended=True)
        for price in (c["l"], c["h"], c["c"]) if c["c"] >= c["l"] else (c["c"],):   # 봉 안에서 저가→고가→종가 순으로 확인 (보수적)
            high = max(high, price)
            side, _ = signal_surge(price, qty, px, high, mins, False, vc, partial_done=partial)
            if side == "SELL_PART":
                part = vc["partial_tp_ratio"]
                got += part * (price * (1 - vc["limit_slip_pct"] / 100) / px - 1)
                qty, partial = qty - part, True
            elif side == "SELL":
                got += qty * (price * (1 - vc["limit_slip_pct"] / 100) / px - 1)
                return got * 100
    return (got + qty * (path[-1]["c"] / px - 1)) * 100


def _sim_swing(closes_after: list[float], entry: float, params: dict, base_cfg: dict, signal_swing, hist: list[float]) -> float | None:
    if not closes_after:
        return None
    vc = {**base_cfg, **params}
    high = entry
    for i, price in enumerate(closes_after):
        high = max(high, price)
        side, _ = signal_swing(hist + closes_after[:i], price, 1, entry, high, i + 1, vc)
        if side == "SELL":
            return (price / entry - 1) * 100
    return (closes_after[-1] / entry - 1) * 100


def _score(rets: list[float], n_all: int) -> dict:
    n = len(rets)
    return {"n": n, "avg": round(st.mean(rets), 2) if rets else 0, "total": round(sum(rets), 2),
            "win": round(sum(1 for r in rets if r > 0) / n * 100) if n else 0}


def run(cfg: dict, market: str, signal_surge, signal_swing, swing_hist: list[dict], log=print, days: int = 10,
        include_today: bool = False) -> dict:
    from . import tradelearn as tl
    since_day = (datetime.now(KST) - timedelta(days=days)).strftime("%Y-%m-%d")
    today = picker.session_date(market)
    # 단타 표본: '고른 종목' 기록 (못 산 것 포함) — 끝난 날만
    j = tl._load()
    samples = []
    for key, w in j["watch"].items():
        mk, _, day = key.partition(":")
        if not day:
            mk, day = market, key
        if mk != market or day < since_day or (day >= today and not include_today):
            continue
        for sym, x in w.items():
            if x.get("pick_px") and x.get("t"):
                samples.append({"sym": sym, "day": day, "t": x["t"], "px": x["pick_px"], "change": x.get("change") or 0,
                                "pre": bool(x.get("pre")), "ratio": x.get("ratio")})
    samples = samples[-60:]
    log(f"📼 {'미장' if market == 'US' else '국장'} 백테스트: 단타 표본 {len(samples)}개 1분봉 받는 중")
    paths = []
    for s in samples:
        try:
            p = _minute_path(s["sym"], s["day"], _parse_kst(s["t"]), market)
        except (toss.TossError, ValueError):
            p = []
        if p:
            paths.append((s, p))
    results = []
    if paths:
        keys = list(SURGE_GRID)
        for combo in itertools.product(*SURGE_GRID.values()):
            prm = dict(zip(keys, combo))
            vol_i = prm.pop("vol")
            prm["surge_volume_ratio"], prm["pre_volume_ratio"] = VOL_STEPS[False][vol_i], VOL_STEPS[True][vol_i]
            rets = []
            for s, path in paths:
                if s["change"] < prm["surge_min_change_pct"]:
                    continue
                need = prm["pre_volume_ratio"] if s["pre"] else prm["surge_volume_ratio"]
                if s.get("ratio") is not None and s["ratio"] < need:
                    continue
                r = _sim_surge(path, s["px"], prm, cfg, signal_surge, market)
                if r is not None:
                    rets.append(r)
            if len(rets) >= max(5, len(paths) // 3):
                results.append({"params": prm, **_score(rets, len(paths))})
    results.sort(key=lambda r: (r["avg"], r["n"]), reverse=True)
    # 현재 규칙 기준선
    cur = {k: cfg[k] for k in ("stop_loss_pct", "partial_tp_pct", "trail_pct", "surge_min_change_pct", "surge_volume_ratio", "pre_volume_ratio")}
    base_rets = [r for r in (_sim_surge(p, s["px"], cur, cfg, signal_surge, market) for s, p in paths
                             if s["change"] >= cur["surge_min_change_pct"]) if r is not None]
    # 스윙
    sw_results = []
    sw_samples = [h for h in swing_hist if h.get("day", "") >= since_day][-20:]
    if sw_samples:
        series = {}
        for h in sw_samples:
            for x in h.get("picks", [])[:6]:
                if x["symbol"] not in series:
                    try:
                        closes, _, dates = toss.daily(x["symbol"], 90)
                        series[x["symbol"]] = (closes, [d_[:10] for d_ in dates])
                    except toss.TossError:
                        continue
        for combo in itertools.product(*SWING_GRID.values()):
            prm = dict(zip(SWING_GRID, combo))
            rets = []
            for h in sw_samples:
                for x in h.get("picks", [])[:6]:
                    if x["symbol"] not in series:
                        continue
                    closes, dates = series[x["symbol"]]
                    idx = next((i for i, d_ in enumerate(dates) if d_ >= h["day"]), None)
                    if idx is None or idx >= len(closes) - 1:
                        continue
                    r = _sim_swing(closes[idx + 1:idx + 1 + int(cfg["swing_max_days"])], closes[idx], prm, cfg, signal_swing, closes[max(0, idx - 60):idx + 1])
                    if r is not None:
                        rets.append(r)
            if len(rets) >= 3:
                sw_results.append({"params": prm, **_score(rets, len(rets))})
        sw_results.sort(key=lambda r: (r["avg"], r["n"]), reverse=True)
    out = {"day": datetime.now(KST).strftime("%Y-%m-%d %H:%M"), "market": market, "surge_samples": len(paths),
           "baseline": _score(base_rets, len(paths)) if paths else {}, "top": results[:5],
           "swing_samples": len(sw_samples), "swing_top": sw_results[:3],
           "note": (f"단타 표본 {len(paths)}개 · 조합 {len(results)}개 비교" if paths else "단타 표본이 아직 없어요 (며칠 돌리면 쌓여요)")
                   + (f" · 스윙 표본 {len(sw_samples)}일" if sw_samples else "")}
    _file(market).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    log(f"📼 백테스트 끝: {out['note']}")
    return out


def seeds(market: str) -> list[dict]:
    """그림자 가상 매매로 넘길 추천 조합 (단타 상위 2 + 스윙 상위 1을 합쳐서)"""
    b = load(market)
    out = [dict(r["params"]) for r in (b.get("top") or [])[:2]]
    if b.get("swing_top"):
        sw = b["swing_top"][0]["params"]
        out = [{**o, **sw} for o in out] or [dict(sw)]
    return out
