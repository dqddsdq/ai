"""📼 밤마다 백테스트 — 지난 며칠 실제로 본 후보로 '이 규칙이었으면?'을 수백 가지 조합으로 계산.

- 단타: 고른 종목(학습 기록의 '고른 종목' + 거래 일지)마다 그날 1분봉을 받아, 고른 시각부터 장 마감까지 매도 규칙을 그대로 돌려 봄
  진입 기준(급등 최소 상승률·거래량 배수)도 같이 바꿔 가며 '어떤 후보만 샀으면'까지
- 스윙: 고른 스윙 후보마다 이후 일봉으로 스윙 매도 규칙을 돌려 봄
- 🔄 과거 복기: 최근 종목의 과거 급등일(일봉 상승률·거래량)마다 1분봉으로 '반등 확인 진입' vs '기존 진입'을 같은 비용으로 재현, 앞/뒤 기간 나눠 비교 (표시만)
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
TP_STEPS = [8, 10, 15, 20]          # 2단계: 남은 물량 전부 익절(%)
MAX_STEPS = [15, 20, 25, 30]        # 2단계: 급등 최대 상승률(%) — 실전 상한보다 큰 값은 안 씀


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


def _sim_surge(path: list[dict], entry: float, params: dict, base_cfg: dict, signal_surge, market: str,
               info: dict | None = None) -> float | None:
    """1분봉 경로 위에서 단타 매도 규칙 돌리기 → 수익률(%) (나눠 팔기 반영, 끝까지 안 팔리면 마지막 값)
    info 를 주면 산 값(px)·판 봉 위치(i)를 적어 줌 (MFE 계산용)"""
    if not path:
        return None
    vc = {**base_cfg, **params}
    from .tradelearn import fill_px
    px = fill_px(vc, entry, "BUY", entry * (1 + vc["limit_slip_pct"] / 100))
    sell = lambda p_: fill_px(vc, p_, "SELL", p_ * (1 - vc["limit_slip_pct"] / 100)) / px - 1   # noqa: E731
    qty, high, partial, got = 1.0, px, False, 0.0
    t0 = _ts(path[0]["t"])
    ts_n, ts_up = float(vc.get("time_stop_min") or 0), float(vc.get("time_stop_up_pct", 0.5))
    if info is not None:
        info.update(px=px, i=len(path) - 1, why="장 끝", hold=0.0)
    for i_, c in enumerate(path):
        if info is not None:
            info["i"] = i_
        mins = picker.session_minutes(market, _ts(c["t"]), extended=True)
        held_min = (_ts(c["t"]) - t0).total_seconds() / 60
        for price in (c["l"], c["h"], c["c"]) if c["c"] >= c["l"] else (c["c"],):   # 봉 안에서 저가→고가→종가 순으로 확인 (보수적)
            high = max(high, price)
            side, why_ = signal_surge(price, qty, px, high, mins, False, vc, partial_done=partial)
            if side == "HOLD" and ts_n > 0 and not partial and held_min >= ts_n and (high / px - 1) * 100 < ts_up:
                side, why_ = "SELL", "⏱️ 시간 손절"   # 실전과 같은 시간 손절
            if info is not None:
                info.update(mfe=(high / px - 1) * 100, why=why_ if side == "SELL" else info.get("why"), hold=held_min)
            if side == "SELL_PART":
                part = vc["partial_tp_ratio"]
                got += part * sell(price)
                qty, partial = qty - part, True
            elif side == "SELL":
                got += qty * sell(price)
                return got * 100
    return (got + qty * sell(path[-1]["c"])) * 100


def _sim_swing(closes_after: list[float], entry: float, params: dict, base_cfg: dict, signal_swing, hist: list[float]) -> float | None:
    if not closes_after:
        return None
    vc = {**base_cfg, **params}
    from .tradelearn import fill_px
    buy = fill_px(vc, entry, "BUY", entry)
    high = entry
    for i, price in enumerate(closes_after):
        high = max(high, price)
        side, _ = signal_swing(hist + closes_after[:i], price, 1, entry, high, i + 1, vc)
        if side == "SELL":
            return (fill_px(vc, price, "SELL", price) / buy - 1) * 100
    return (fill_px(vc, closes_after[-1], "SELL", closes_after[-1]) / buy - 1) * 100


def _score(rets: list[float], n_all: int) -> dict:
    n = len(rets)
    return {"n": n, "avg": round(st.mean(rets), 2) if rets else 0, "total": round(sum(rets), 2),
            "win": round(sum(1 for r in rets if r > 0) / n * 100) if n else 0}


# ── 🔄 단타 과거 복기: 과거 급등일에 '반등 확인 진입' vs '기존 진입' (기록·화면 표시만, 매매엔 영향 없음) ──
REPLAY_SYMS = 40        # 살펴볼 종목 수 (최근 고른 종목·거래 종목)
REPLAY_MIN_SYMS = 25    # 1분봉까지 받을 종목 수
REPLAY_PAGES = 60       # 종목당 1분봉 최대 쪽수 (200개씩)
REPLAY_EVENTS = 120     # 급등일 최대 개수 (최근 것부터)


def _hist_1m(sym: str, market: str, need_from: str, today: str) -> tuple[list[dict], dict]:
    """과거 1분봉 (보관분 + 새로 받은 것 합침, 끝난 날만 보관) → (봉 목록, 토스에서 받은 범위)"""
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"{market}_{sym}_1m_hist.json"
    try:
        old = json.loads(f.read_text())
    except Exception:
        old = []
    have = old[-1]["t"] if old else ""
    got, before, pages, ended = [], None, 0, False
    for _ in range(REPLAY_PAGES):
        r = toss._get("/api/v1/candles", symbol=sym, interval="1m", count=200, before=before) or {}
        page = r.get("candles") or []
        pages += 1
        got += page
        before = r.get("nextBefore")
        if not page or not before:
            ended = True   # 토스가 더 안 줌 = 받을 수 있는 끝
            break
        lo = min(c["timestamp"] for c in page)
        if (have and lo <= have) or picker.session_date(market, _ts(lo)) < need_from:
            break
    new = [{"t": c["timestamp"], "o": float(c.get("openPrice") or c["closePrice"]), "c": float(c["closePrice"]),
            "h": float(c.get("highPrice") or c["closePrice"]), "l": float(c.get("lowPrice") or c["closePrice"]),
            "v": float(c.get("volume") or 0)} for c in got]
    rng = {"from": min((c["t"] for c in new), default=""), "to": max((c["t"] for c in new), default=""),
           "pages": pages, "ended": ended}
    merged = {c["t"]: c for c in old}
    merged.update({c["t"]: c for c in new})
    bs = sorted(merged.values(), key=lambda c: c["t"])
    rng["kept_from"] = bs[0]["t"] if bs else ""   # 보관분 포함 가장 오래된 봉
    keep = [c for c in bs if picker.session_date(market, _ts(c["t"])) < today][-40000:]   # 끝난 날만 보관
    try:
        f.write_text(json.dumps(keep))
    except OSError:
        pass
    return bs, rng


def _entries(bars: list[dict], prev: float, vol20: float, c: dict, market: str) -> tuple[int | None, int | None]:
    """그날 정규장 1분봉에서 (기존 진입 봉, 반등 확인 진입 봉) 위치. 신호는 다 끝난 봉 종가 기준"""
    lo, hi = float(c["surge_min_change_pct"]), float(c["surge_max_change_pct"])
    vr = float(c["surge_volume_ratio"])
    pmin, pmax = float(c.get("rebound_pullback_pct", 1.5)), float(c.get("rebound_pullback_max_pct", 10))
    k = max(1, min(3, int(c.get("rebound_up_bars", 2))))
    plain = reb = None
    cv = pv = 0.0
    hi_px, hi_i = 0.0, 0
    for i, b in enumerate(bars):
        cv += b["v"]
        pv += (b["h"] + b["l"] + b["c"]) / 3 * b["v"]
        if b["h"] > hi_px:
            hi_px, hi_i = b["h"], i
        mins = picker.session_minutes(market, _ts(b["t"])) or 0
        if mins >= 360:   # 장 끝 30분 전부터는 새로 안 삼
            break
        chg = (b["c"] / prev - 1) * 100
        in_band = lo <= chg <= hi
        if plain is None:
            ratio = cv / max(vol20 * min(1.0, max(10, mins) / 390), 1)
            if in_band and ratio >= vr:
                plain = i
            continue
        if not in_band or i < 5:   # 후보에서 빠진 동안은 안 삼
            continue
        # 반등 확인: 고점 후 눌림(기준 범위) → VWAP·MA5 위 회복 → 직전 k봉 연속 상승 → 거래량 증가(직전 5봉 평균보다 큼)
        low_after = min(x["l"] for x in bars[hi_i:i + 1])
        pull = (1 - low_after / hi_px) * 100 if hi_px else 0
        vwap = pv / cv if cv else 0
        ma5 = sum(x["c"] for x in bars[i - 4:i + 1]) / 5
        last = bars[i - k:i + 1]
        ups = sum(1 for j in range(1, len(last)) if last[j]["c"] > last[j]["o"] and last[j]["c"] > last[j - 1]["c"])
        v5 = sum(x["v"] for x in bars[i - 5:i]) / 5
        if pmin <= pull <= pmax and vwap and b["c"] > vwap and b["c"] > ma5 and ups >= k and b["v"] > v5:
            reb = i
            break
    return plain, reb


def _rstat(xs: list[dict]) -> dict:
    rets = [x["ret"] for x in xs]
    s = _score(rets, len(rets))
    s["mfe"] = round(st.mean(x["mfe"] for x in xs), 2) if xs else 0
    return s


def replay(cfg: dict, market: str, signal_surge, log=print) -> dict:
    """과거 급등일(일봉 상승률·거래량 기준) 종목에 기존 진입·반등 확인 진입을 같은 매도 규칙·비용으로 재현 → 앞/뒤 기간 나눠 비교"""
    from . import tradelearn as tl
    today = picker.session_date(market)
    cost = tl.rt_cost(cfg, market)
    j = tl._load()
    syms: list[str] = []
    for key, w in reversed(list(j["watch"].items())):   # 최근 고른 종목 먼저
        mk, _, day = key.partition(":")
        if (mk if day else market) == market:
            syms += [s for s in w if s not in syms]
    syms += [t["sym"] for t in reversed(j["trades"]) if tl._tm(t) == market and t["sym"] not in syms]
    syms = list(dict.fromkeys(syms))[:REPLAY_SYMS]
    if not syms:
        return {"note": "과거 복기: 살펴볼 종목이 아직 없어요 (고른 종목이 쌓이면 시작)"}
    log(f"🔄 과거 복기: {len(syms)}종목 일봉에서 급등일 찾는 중")
    lo, vr = float(cfg["surge_min_change_pct"]), float(cfg["surge_volume_ratio"])
    rng_d, events = {}, []
    for n_, s in enumerate(syms):
        try:
            closes, vols, dates = toss.daily(s, 2000 if n_ == 0 else 260)   # 첫 종목은 일봉 받을 수 있는 끝까지 확인
        except toss.TossError:
            continue
        if n_ == 0 and dates:
            rng_d = {"from": dates[0][:10], "to": dates[-1][:10], "n": len(dates), "sym": s}
        for i in range(20, len(closes)):
            d_ = dates[i][:10]
            v20 = st.mean(vols[i - 20:i])
            if d_ >= today or not closes[i - 1] or not v20:
                continue
            chg = (closes[i] / closes[i - 1] - 1) * 100
            if chg >= lo and vols[i] / v20 >= vr:
                events.append({"sym": s, "day": d_, "prev": closes[i - 1], "vol20": v20, "chg": round(chg, 1)})
    events.sort(key=lambda e: e["day"])
    rng_m: dict = {}
    if events:
        log(f"🔄 과거 복기: 급등일 {len(events)}개 → 1분봉 받는 중")
    by_sym: dict[str, list[dict]] = {}
    for e in events:
        by_sym.setdefault(e["sym"], []).append(e)
    m_from = ""   # 1분봉 받을 수 있는 가장 오래된 날 (첫 종목으로 확인)
    done = []
    for s in sorted(by_sym, key=lambda s_: by_sym[s_][-1]["day"], reverse=True)[:REPLAY_MIN_SYMS]:
        evs = by_sym[s]
        if m_from and evs[-1]["day"] < m_from:
            continue   # 급등일이 전부 1분봉 범위 밖
        try:
            bars, rng = _hist_1m(s, market, evs[0]["day"], today)
        except (toss.TossError, ValueError):
            continue
        if not rng_m and rng.get("from"):
            rng_m = {**rng, "sym": s}
            m_from = picker.session_date(market, _ts(rng["from"])) if rng["ended"] else ""
        by_day: dict[str, list[dict]] = {}
        for b in bars:
            if picker.session_minutes(market, _ts(b["t"])) is not None:   # 정규장 봉만
                by_day.setdefault(picker.session_date(market, _ts(b["t"])), []).append(b)
        for e in evs:
            day_bars = by_day.get(e["day"]) or []
            if len(day_bars) < 30:
                continue
            ip, ir = _entries(day_bars, e["prev"], e["vol20"], cfg, market)
            for tag, idx in (("plain", ip), ("rebound", ir)):
                if idx is None or idx >= len(day_bars) - 1:
                    continue
                path, info = day_bars[idx + 1:], {}
                r = _sim_surge(path, day_bars[idx]["c"], {}, cfg, signal_surge, market, info)
                if r is None:
                    continue
                mfe = (max(b["h"] for b in path[:info["i"] + 1]) / info["px"] - 1) * 100
                done.append({"tag": tag, "sym": s, "day": e["day"], "ret": round(r - cost, 2), "mfe": round(mfe, 2)})
    done.sort(key=lambda x: x["day"])
    days = sorted({x["day"] for x in done})[-REPLAY_EVENTS:]
    done = [x for x in done if x["day"] >= (days[0] if days else "")]
    mid = days[len(days) // 2] if len(days) >= 2 else None
    def part(xs):
        return {"rebound": _rstat([x for x in xs if x["tag"] == "rebound"]), "plain": _rstat([x for x in xs if x["tag"] == "plain"]),
                "from": xs[0]["day"] if xs else "", "to": xs[-1]["day"] if xs else ""}
    out = {"all": part(done), "front": part([x for x in done if mid and x["day"] < mid]),
           "back": part([x for x in done if mid and x["day"] >= mid]),
           "events": len({(x["sym"], x["day"]) for x in done}), "surge_days": len(events), "syms": len(syms),
           "range_1d": rng_d, "range_1m": rng_m, "cost_pct": round(cost, 3), "slip_pct": cfg.get("sim_slip_pct", 0.1)}
    # 판정: 앞·뒤 기간 모두 반등 진입 평균이 더 좋아야 '우세' (각 기간 양쪽 3건 이상일 때만)
    ok = [p for p in (out["front"], out["back"]) if p["rebound"]["n"] >= 3 and p["plain"]["n"] >= 3]
    if len(ok) < 2:
        out["verdict"] = "판정 보류 (앞·뒤 기간에 각각 3건 이상 필요)"
    else:
        wins = sum(1 for p in ok if p["rebound"]["avg"] > p["plain"]["avg"])
        out["verdict"] = {2: "앞·뒤 기간 모두 반등 확인 진입 우세", 0: "앞·뒤 기간 모두 기존 진입 우세"}.get(wins, "기간마다 결과가 엇갈림 (판단 보류)")
    a = out["all"]
    out["note"] = (f"과거 복기: 급등일 {out['events']}개 · 반등 {a['rebound']['n']}건 평균 {a['rebound']['avg']:+.2f}% / "
                   f"기존 {a['plain']['n']}건 평균 {a['plain']['avg']:+.2f}% · {out['verdict']}")
    log(f"🔄 {out['note']}")
    return out


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
    cost = tl.rt_cost(cfg, market)   # 사고팔기 한 번 비용(%)
    log(f"📼 {'미장' if market == 'US' else '국장'} 백테스트: 단타 표본 {len(samples)}개 1분봉 받는 중")
    paths = []
    for s in samples:
        try:
            p = _minute_path(s["sym"], s["day"], _parse_kst(s["t"]), market)
        except (toss.TossError, ValueError):
            p = []
        if p:
            paths.append((s, p))
    def _rets(prm):   # 이 조합의 진입 기준(최소·최대 상승률·거래량)으로 거른 표본의 수익률 (수수료·세금 빼고)
        out_ = []
        hi = prm.get("surge_max_change_pct", cfg.get("surge_max_change_pct", 1e9))
        for s, path in paths:
            if s["change"] < prm["surge_min_change_pct"] or s["change"] > hi:
                continue
            need = prm["pre_volume_ratio"] if s["pre"] else prm["surge_volume_ratio"]
            if s.get("ratio") is not None and s["ratio"] < need:
                continue
            r = _sim_surge(path, s["px"], prm, cfg, signal_surge, market)
            if r is not None:
                out_.append(r - cost)
        return out_

    results = []
    min_n = max(5, len(paths) // 3)
    if paths:
        keys = list(SURGE_GRID)
        for combo in itertools.product(*SURGE_GRID.values()):
            prm = dict(zip(keys, combo))
            vol_i = prm.pop("vol")
            prm["surge_volume_ratio"], prm["pre_volume_ratio"] = VOL_STEPS[False][vol_i], VOL_STEPS[True][vol_i]
            rets = _rets(prm)
            if len(rets) >= min_n:
                results.append({"params": prm, **_score(rets, len(paths))})
        results.sort(key=lambda r: (r["avg"], r["n"]), reverse=True)
        # 2단계: 상위 조합만 전부 익절·급등 최대(실전 상한 이하)를 바꿔 봄 (조합 수 폭증 방지)
        cap = float(cfg.get("surge_max_change_pct", 40))
        tps = [x for x in TP_STEPS if x > 0] + [cfg.get("take_profit_pct")]
        mxs = sorted({x for x in MAX_STEPS if x <= cap} | {cap})
        seen = {json.dumps(r["params"], sort_keys=True) for r in results}
        for r0 in results[:10]:
            for tp, mx in itertools.product(dict.fromkeys(tps), mxs):
                prm = {**r0["params"], "take_profit_pct": tp, "surge_max_change_pct": mx}
                if prm["partial_tp_pct"] and tp <= prm["partial_tp_pct"] or mx < prm["surge_min_change_pct"] + 3:
                    continue
                key = json.dumps(prm, sort_keys=True)
                if key in seen:
                    continue
                seen.add(key)
                rets = _rets(prm)
                if len(rets) >= min_n:
                    results.append({"params": prm, **_score(rets, len(paths))})
    results.sort(key=lambda r: (r["avg"], r["n"]), reverse=True)
    # 현재 규칙 기준선 (같은 진입 기준으로 거름)
    cur = {k: cfg[k] for k in ("stop_loss_pct", "partial_tp_pct", "take_profit_pct", "trail_pct", "surge_min_change_pct",
                               "surge_max_change_pct", "surge_volume_ratio", "pre_volume_ratio")}
    base_rets = _rets(cur) if paths else []
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
                        rets.append(r - cost)
            if len(rets) >= 3:
                sw_results.append({"params": prm, **_score(rets, len(rets))})
        sw_results.sort(key=lambda r: (r["avg"], r["n"]), reverse=True)
    try:   # 🔄 과거 복기 (실패해도 기존 백테스트 결과는 그대로 저장)
        rp = replay(cfg, market, signal_surge, log)
    except Exception as e:
        rp = {"note": f"과거 복기 실패: {type(e).__name__}: {e}"}
    out = {"replay": rp,"day": datetime.now(KST).strftime("%Y-%m-%d %H:%M"), "market": market, "surge_samples": len(paths),
           "baseline": _score(base_rets, len(paths)) if paths else {}, "top": results[:8],
           "swing_samples": len(sw_samples), "swing_top": sw_results[:3],
           "note": (f"단타 표본 {len(paths)}개 · 조합 {len(results)}개 비교" if paths else "단타 표본이 아직 없어요 (며칠 돌리면 쌓여요)")
                   + (f" · 스윙 표본 {len(sw_samples)}일" if sw_samples else "")}
    _file(market).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    log(f"📼 백테스트 끝: {out['note']}")
    return out


def seeds(market: str) -> list[dict]:
    """그림자 가상 매매로 넘길 추천 조합 (단타 상위 6 + 스윙 상위 1을 합쳐서)"""
    b = load(market)
    out = [dict(r["params"]) for r in (b.get("top") or [])[:6]]
    if b.get("swing_top"):
        sw = b["swing_top"][0]["params"]
        out = [{**o, **sw} for o in out] or [dict(sw)]
    return out
