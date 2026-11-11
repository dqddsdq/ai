"""🧪 그림자 가상 매매 — 여러 규칙 조합을 실전과 동시에 가상으로 돌려 보고, 확실히 나은 조합을 실전 규칙으로 자동 교체.

- 조합: '현재'(지금 실전 규칙) + 백테스트 상위 조합 + 격자 주변 조합 → 약 20개 (N_VARIANTS)
- 실전 엔진이 매 바퀴 찾은 후보·시세를 그대로 받아서 모든 조합이 공유 (추가 조회는 보유 종목 시세 한 번에 묶어서만, AI 호출 없음)
- 체결은 실전 가상 매매와 같게: 0.1% 불리한 체결·수수료·세금·손해 본 종목 재진입 금지·공시 악재 제외
- 교체 조건 (조합이 많아 운으로 1등이 나오는 걸 막으려고 강화):
  조합별 끝난 거래 20건 이상 → 앞쪽 70% 거래로 '현재'보다 나은 조합 1개만 고르고 →
  뒤쪽 30% 거래(최근)에서도 같은 기간 '현재'·실전 기록보다 수수료 뺀 평균 수익이 높고 플러스일 때만 실전 규칙 교체
- 정리: 20건 이상 쌓인 조합 중 하위 25% 는 빼고 새 조합으로 채워 20개 안팎 유지 (매일 복기 뒤)
- '현재' 조합 거래만 '비슷한 거래' 표본에 가중치 0.5 로 들어감 (tradelearn.estimate)
- 30% 초과 급등 검증 조합(HOT)은 별도 보관 · 교체·정리 대상 아님
- 급락 검증 조합(FLASH: 급락청산·급락반등)도 별도 보관 · 교체·정리 대상 아님 · 20건마다 텔레그램 보고
기록: _cache/shadow_US.json / shadow_KR.json
"""

from __future__ import annotations

import hashlib
import json
import random
import statistics as st
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from . import store
from . import tradelearn as tl
from .agents import COMPANY_DIR

KST = timezone(timedelta(hours=9))
VIRTUAL = 1_000_000
TUNE_KEYS = ["stop_loss_pct", "partial_tp_pct", "take_profit_pct", "trail_pct", "trail_wide_after_pct", "trail_wide_pct",
             "surge_min_change_pct", "surge_max_change_pct", "surge_volume_ratio", "pre_volume_ratio", "swing_stop_pct",
             "swing_trail_pct", "news_unknown_ratio", "swing_trail_vol"]
GRID = {"stop_loss_pct": [2, 2.5, 3, 4, 5], "partial_tp_pct": [0, 3, 4, 5, 6, 8], "take_profit_pct": [6, 8, 10, 15, 20],
        "trail_pct": [3, 4, 5, 6], "trail_wide_after_pct": [8, 10, 15], "trail_wide_pct": [5, 6, 8],
        "surge_min_change_pct": [5, 7, 10, 15], "surge_max_change_pct": [15, 20, 25, 30, 40], "surge_volume_ratio": [2, 3, 5, 8],
        "pre_volume_ratio": [0.1, 0.2, 0.4], "swing_stop_pct": [4, 6, 8], "swing_trail_pct": [5, 8, 12],
        "news_unknown_ratio": [1, 0.5, 0.25]}
LABEL = {"stop_loss_pct": "손절", "partial_tp_pct": "나눠팔기", "take_profit_pct": "전부익절", "trail_pct": "고점대비",
         "trail_wide_after_pct": "넓힘기준", "trail_wide_pct": "넓은고점대비", "surge_min_change_pct": "급등최소",
         "surge_max_change_pct": "급등최대", "surge_volume_ratio": "거래량배수", "pre_volume_ratio": "프리장거래량",
         "swing_stop_pct": "스윙손절", "swing_trail_pct": "스윙고점대비", "news_unknown_ratio": "이유모를때금액",
         "swing_trail_vol": "스윙변동성기준"}
N_VARIANTS = 20        # 일반 조합 목표 개수
N_MAX = 23             # 이보다 많으면 거래 적은 조합부터 정리
PROMOTE_N = 20         # 교체 후보가 되려면 끝난 거래 최소 건수
HOLDOUT = 0.3          # 뒤쪽(최근) 검증 비율
CMP_MIN = 5            # 같은 기간 비교 대상('현재'·실전) 최소 건수
PRUNE_N = 20           # 이 건수 이상 쌓인 조합끼리 하위 PRUNE_FRAC 정리
PRUNE_FRAC = 0.25
_lock = threading.RLock()
# 🚀 30% 넘게 오른 급등주 검증용 (실전 상한 surge_max_change_pct 는 그대로 · 자동 교체 대상 아님 · 따로 보관)
HOT = [{"key": "A", "name": "급등A 30~50%·손절4%", "lo": 30, "hi": 50, "params": {"stop_loss_pct": 4}},
       {"key": "B", "name": "급등B 30~50%·손절6%", "lo": 30, "hi": 50, "params": {"stop_loss_pct": 6}},
       {"key": "C", "name": "급등C 50%초과·손절6%", "lo": 50, "hi": None, "params": {"stop_loss_pct": 6}}]
HOT_REPORT_EVERY = 20   # 조합마다 이 건수 쌓일 때마다 텔레그램 보고
# 📉 급락 신호 검증용 (가상 전용 · 자동 교체 대상 아님 · 따로 보관 · 실전 매수 조건 안 바꿈)
FLASH = [{"key": "FX", "name": "급락청산 (현재 규칙+급락 매도)", "origin": "거래량 급감·1분 -2% 급락 시 매도"},
         {"key": "FR", "name": "급락반등 진입", "origin": "1~3분 -5%·거래량 3배 뒤 반등 진입 · 손절은 급락 저점 아래"}]
FLASH_REPORT_EVERY = 20
FLASH_WINDOWS = (5, 15, 30)   # 신호 뒤 최고/최저 기록 구간(분)
FLASH_MAX_SYMS = 12           # 한 바퀴에 반등을 살펴볼 최대 종목 수 (조회 줄이기)
REB_TP_PCT, REB_TRAIL_PCT, REB_MAX_MIN = 3.0, 2.0, 30   # 반등 진입 익절·고점 대비·최대 보유(분)
_bars_cache: dict = {}


def _file(m: str):
    return COMPANY_DIR / "_cache" / f"shadow_{m}.json"


def _load(m: str) -> dict:
    d = store.load(_file(m))
    d.setdefault("variants", []); d.setdefault("promotions", []); d.setdefault("base_hash", "")
    d.setdefault("seq", 0); d.setdefault("pruned", [])
    if not isinstance(d.get("retired"), list):
        d["retired"] = []
    if not isinstance(d.get("hot"), list):
        d["hot"] = []
    have = {v.get("key") for v in d["hot"]}
    for h in HOT:   # 급등 검증 조합은 실전 규칙이 바뀌어도 기록 유지
        if h["key"] not in have:
            v = _new_variant(h["name"], dict(h["params"]), "30% 초과 급등 검증")
            v.update(key=h["key"], reported=0)
            d["hot"].append(v)
    if not isinstance(d.get("flash"), list):
        d["flash"] = []
    have = {v.get("key") for v in d["flash"]}
    for h in FLASH:   # 급락 검증 조합도 따로 보관
        if h["key"] not in have:
            v = _new_variant(h["name"], {}, h["origin"])
            v.update(key=h["key"], reported=0, signals=[], copied={}, seen={})
            d["flash"].append(v)
    return d


def _save(m: str, d: dict) -> None:
    for v in d.get("flash", []):
        v["signals"] = v.get("signals", [])[-300:]
    for v in d["variants"] + d.get("hot", []) + d.get("flash", []):
        v["trades"] = v["trades"][-(1000 if v.get("key") else 300):]
    d["promotions"] = d["promotions"][-50:]
    d["retired"] = d["retired"][-300:]
    d["pruned"] = d["pruned"][-30:]
    store.save(_file(m), d)


def _now() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


def base_params(c: dict) -> dict:
    return {k: c[k] for k in TUNE_KEYS if k in c}


def _hash(p: dict) -> str:
    return hashlib.md5(json.dumps(p, sort_keys=True).encode()).hexdigest()[:10]


def _full(p: dict, c: dict) -> dict:
    """비교용: 조합에 없는 값은 실전 값으로 채움 (예전 조합 호환)"""
    return {k: p.get(k, c.get(k)) for k in TUNE_KEYS if k in p or k in c}


def _phash(p: dict, c: dict) -> str:
    f = _full(p, c)
    if not f.get("swing_trail_vol"):   # 꺼짐(기본)은 예전 조합과 같은 값으로 (기존 기록·정리 목록 유지)
        f.pop("swing_trail_vol", None)
    return _hash({k: float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v for k, v in f.items()})


def _caps(c: dict) -> dict:
    """조합이 넘으면 안 되는 범위: 손절은 자동 범위 안, 급등 최대는 실전 상한 이하 (후보가 실전 상한까지만 오니까)"""
    return {"stop_loss_pct": (float(c.get("stop_min", 2)), float(c.get("stop_max", 6))),
            "surge_max_change_pct": (0.0, float(c.get("surge_max_change_pct", 40)))}


def _clean(p: dict, c: dict) -> dict | None:
    """범위 안으로 자르고, 말이 안 되는 조합(최대≤최소+3, 전부익절≤나눠팔기)은 버림"""
    q = dict(p)
    for k, (lo, hi) in _caps(c).items():
        if k in q and q[k] is not None:
            q[k] = lo if q[k] < lo else hi if q[k] > hi else q[k]
    if q.get("surge_max_change_pct") is not None and q.get("surge_min_change_pct") is not None \
            and q["surge_max_change_pct"] < q["surge_min_change_pct"] + 3:
        return None
    if q.get("partial_tp_pct") and q.get("take_profit_pct") and q["take_profit_pct"] <= q["partial_tp_pct"]:
        return None
    return q


def _neighbor(p: dict, rnd: random.Random, n_changes: int, c: dict) -> dict | None:
    q = dict(p)
    caps = _caps(c)
    for k in rnd.sample([k for k in TUNE_KEYS if k in GRID], n_changes):
        lo, hi = caps.get(k, (-1e18, 1e18))
        opts = [o for o in GRID[k] if lo <= o <= hi] or GRID[k][:1]
        cur = q.get(k, opts[len(opts) // 2])
        idx = min(range(len(opts)), key=lambda i: abs(opts[i] - cur))
        q[k] = opts[max(0, min(len(opts) - 1, idx + rnd.choice([-1, 1])))]
    return _clean(q, c)


def _diff(p: dict, base: dict) -> str:
    ch = [f"{LABEL[k]} {base.get(k)}→{p[k]}" for k in TUNE_KEYS if k in p and p[k] != base.get(k)]
    return ", ".join(ch) or "지금 실전 규칙 그대로"


def _new_variant(name: str, params: dict, origin: str) -> dict:
    return {"id": uuid.uuid4().hex[:6], "name": name, "params": params, "origin": origin, "created": _now(),
            "cash": VIRTUAL, "pos": {}, "trades": [], "day": "", "buys_today": 0}


def _ret(t: dict) -> float:
    """한 거래 전체 수익률(수수료·세금·미끄러짐 뺀 값). 예전 기록은 마지막 매도 수익률"""
    return float(t.get("trade_pct", t.get("pnl_pct", 0)))


def _fin(v: dict) -> list[dict]:
    return [t for t in v.get("trades", []) if t.get("final")]


def _avg(ts: list[dict]) -> float:
    return st.mean(_ret(t) for t in ts) if ts else 0.0


def _is_base(v: dict) -> bool:
    return v.get("name") == "현재"


def _fill(d: dict, c: dict, market: str, target: int = N_VARIANTS) -> int:
    """조합을 target 개까지 채움: 백테스트 상위 조합 먼저, 그다음 격자 주변 조합 (예전에 뺀 조합은 다시 안 넣음)"""
    base = base_params(c)
    have = {_phash(v["params"], c) for v in d["variants"]} | set(d["retired"])
    added = 0
    try:
        from . import backtest as bt
        sds = bt.seeds(market)   # 파일만 읽음 (조회 없음)
    except Exception:
        sds = []
    for sd in sds:
        if len(d["variants"]) >= target:
            break
        p = _clean({**base, **{k: v for k, v in sd.items() if k in TUNE_KEYS}}, c)
        if p and _phash(p, c) not in have:
            d["seq"] += 1
            d["variants"].append(_new_variant(f"백테스트{d['seq']}", p, "백테스트 추천"))
            have.add(_phash(p, c)); added += 1
    rnd = random.Random(_hash(base) + market + str(d["seq"]))
    tries = 0
    while len(d["variants"]) < target and tries < 400:
        tries += 1
        p = _neighbor(base, rnd, 1 if tries <= 80 else 2, c)
        if p and _phash(p, c) not in have:
            d["seq"] += 1
            d["variants"].append(_new_variant(f"실험{d['seq']}", p, "격자 주변 조합"))
            have.add(_phash(p, c)); added += 1
    return added


def ensure(c: dict, market: str, seeds: list[dict] | None = None, force: bool = False) -> dict:
    """조합 준비: 없거나 force 면 새로 · 실전 규칙이 바뀌면 기존 조합 기록은 유지하고 '현재'만 새 기준으로 · 모자라면 채움"""
    base = base_params(c)
    bh = _phash(base, c)
    with _lock:
        d = _load(market)
        changed = False
        if force or not d["variants"]:
            d["variants"], d["retired"] = [_new_variant("현재", base, "현재 실전 규칙")], []
            nb = {**base, "news_unknown_ratio": 0.5 if float(base.get("news_unknown_ratio", 1)) >= 1 else 1}   # 📰 공시 반영 비교용
            d["variants"].append(_new_variant("공시비교", nb, "이유 모르는 급등 금액 비교"))
            changed = True
        elif d["base_hash"] != bh:
            for v in d["variants"]:
                if _is_base(v):   # 예전 실전 규칙도 하나의 조합으로 계속 검증
                    v["name"], v["origin"] = f"이전실전{v['id'][:3]}", "예전 실전 규칙"
            same = next((v for v in d["variants"] if _phash(v["params"], c) == bh), None)
            if same:   # 새 실전 규칙과 같은 조합이 있으면 그 기록을 '현재'로 이어 씀
                same["name"], same["origin"], same["params"] = "현재", "현재 실전 규칙", dict(base)
                d["variants"].remove(same); d["variants"].insert(0, same)
            else:
                d["variants"].insert(0, _new_variant("현재", base, "현재 실전 규칙"))
            changed = True
        # 📈 스윙 고점 대비: 변동성 자동 ↔ 일괄 기준 비교 조합 (실전과 반대 설정 · 정리된 적 있으면 다시 안 넣음)
        vb = {**base, "swing_trail_vol": not bool(base.get("swing_trail_vol"))}
        vh = _phash(vb, c)
        if vh not in {_phash(v["params"], c) for v in d["variants"]} | set(d["retired"]):
            d["variants"].append(_new_variant("변동성비교", vb, "스윙 고점 대비: 종목 변동성 자동(3~8%) vs 일괄 기준"))
            changed = True
        if seeds:
            have = {_phash(v["params"], c) for v in d["variants"]}
            for sd in seeds[:6]:
                p = _clean({**base, **{k: v for k, v in sd.items() if k in TUNE_KEYS}}, c)
                if p and _phash(p, c) not in have:
                    d["seq"] += 1
                    d["variants"].append(_new_variant(f"백테스트{d['seq']}", p, "백테스트 추천"))
                    have.add(_phash(p, c)); changed = True
        fk = f"{bh}:{len(d['variants'])}:{len(d['retired'])}"
        if len(d["variants"]) < N_VARIANTS - 1 and d.get("fill_key") != fk:   # 같은 상태에서 채우기 실패를 매 바퀴 반복 안 함
            _fill(d, c, market)
            d["fill_key"] = f"{bh}:{len(d['variants'])}:{len(d['retired'])}"
            changed = True
        if len(d["variants"]) > N_MAX:
            _shrink(d, N_MAX); changed = True
        if changed:
            d["base_hash"] = bh
            _save(market, d)
        return d


def _shrink(d: dict, cap: int, keep: list | None = None) -> None:
    """개수가 넘치면 거래가 가장 적은 조합부터 뺌 ('현재'·keep 은 유지)"""
    keep = keep or []
    while len(d["variants"]) > cap:
        rest = [v for v in d["variants"] if not _is_base(v) and v not in keep]
        if not rest:
            break
        d["variants"].remove(min(rest, key=lambda v: (len(_fin(v)), v.get("created", ""))))


def step(c: dict, market: str, cands: dict, quotes: dict, minutes, fx: float, exits_only: bool,
         signal_surge, signal_swing, market_open, daily_closes, session_day: str, hot: dict | None = None) -> None:
    """한 바퀴: 조합마다 자기 규칙으로 가상 매도·매수 (실전 엔진이 부름). hot = 30% 초과 급등 후보 (급등 검증 조합 전용)
    모든 조합이 같은 후보·같은 시세를 씀 → 조합이 늘어도 조회·AI 호출은 안 늘어남"""
    from . import toss
    hot = hot or {}
    d = ensure(c, market)
    need = ({s for v in d["variants"] + d["hot"] + d["flash"] for s in v["pos"]} | set(hot)) - set(quotes)
    if need:   # 보유 종목 시세는 모든 조합 것을 한 번에
        try:
            quotes = {**quotes, **toss.prices(sorted(need))}
        except toss.TossError:
            pass
    closes_cache: dict = {}   # 스윙 일봉도 종목마다 한 번만

    def _closes(sym):
        if sym not in closes_cache:
            try:
                closes_cache[sym] = daily_closes(sym)
            except Exception:
                closes_cache[sym] = None
        return closes_cache[sym]

    specs = {h["key"]: h for h in HOT}
    with _lock:
        d = _load(market)
        for v in d["variants"] + d["hot"]:
            vc = {**c, **v["params"]}
            v_cands = cands
            if v.get("key"):   # 급등 검증 조합: 자기 상승률 구간의 30% 초과 후보만
                h = specs.get(v["key"])
                if not h:
                    continue
                v_cands = {s: cd for s, cd in hot.items()
                           if (cd["features"].get("change") or 0) > h["lo"] and (h["hi"] is None or cd["features"]["change"] <= h["hi"])}
            if v["day"] != session_day:
                v["day"], v["buys_today"] = session_day, 0
                for p in v["pos"].values():   # 단타 최고가는 날마다 새로 (스윙은 유지)
                    if p["style"] != "swing":
                        p["high"] = p["avg"]
            f = 1 if market == "KR" else fx
            # 매도
            for sym in list(v["pos"]):
                p = v["pos"][sym]
                q = quotes.get(sym)
                if not q:
                    continue
                price = q["price"]
                p["high"] = max(p["high"], price)
                if p["style"] == "swing":
                    closes = _closes(sym)
                    if closes is None:
                        continue
                    days = (datetime.now(KST).date() - datetime.strptime(p["buy_day"], "%Y-%m-%d").date()).days
                    side, why = signal_swing(closes, price, p["qty"], p["avg"], p["high"], days, vc)
                else:
                    side, why = signal_surge(price, p["qty"], p["avg"], p["high"], minutes, False, vc, partial_done=p.get("partial", False))
                if side not in ("SELL", "SELL_PART") or not market_open(sym, "SELL"):
                    continue
                qty = p["qty"] if side == "SELL" else max(1, int(p["qty"] * vc["partial_tp_ratio"]))
                if side == "SELL_PART" and qty >= p["qty"]:
                    p["partial"] = True
                    continue
                lim = price * (1 - vc["limit_slip_pct"] / 100) if p["style"] == "surge" else price
                px = tl.fill_px(vc, price, "SELL", lim)   # 실전 가상 매매와 같은 체결 방식
                sc, bc = tl.side_cost(vc, market, "SELL") / 100, tl.side_cost(vc, market, "BUY") / 100   # 수수료·세금 포함
                got = qty * px * f * (1 - sc)
                v["cash"] += got
                net = px * (1 - sc) / (p["avg"] * (1 + bc)) - 1
                rec = {"t": _now(), "sym": sym, "style": p["style"], "qty": qty, "pnl_pct": round(net * 100, 2),
                       "pnl_krw": round(net * p["avg"] * (1 + bc) * qty * f), "why": why[:60], "final": side == "SELL"}
                if side == "SELL":
                    if p.get("cost0"):   # 나눠 판 것까지 합친 거래 전체 수익률
                        rec["trade_pct"] = round(((p.get("got", 0) + got) / p["cost0"] - 1) * 100, 2)
                    if p.get("ft"):
                        rec["features"], rec["day"] = p["ft"], p.get("sday", "")
                    v["trades"].append(rec)
                    v["pos"].pop(sym)
                    if _ret(rec) < 0 and c.get("no_rebuy_after_loss", True):   # 실전과 같게: 손해 본 종목 재진입 금지 (단타 그날 · 스윙 N일)
                        if not isinstance(v.get("no_rebuy"), dict):   # 기록 없거나 깨졌으면 새로
                            v["no_rebuy"] = {}
                        v["no_rebuy"][sym] = session_day if p["style"] != "swing" else \
                            (datetime.now(KST) + timedelta(days=int(c.get("no_rebuy_swing_days", 3)))).strftime("%Y-%m-%d")
                else:
                    v["trades"].append(rec)
                    p["qty"] -= qty; p["partial"] = True; p["got"] = p.get("got", 0) + got
            if exits_only:
                continue
            # 매수: 실전과 같은 후보, 기대 수익률 순, 이 조합의 진입 기준으로 거름
            per_pos = VIRTUAL / max(1, int(c["pick_count"]))
            for sym, cd in sorted(v_cands.items(), key=lambda kv: -kv[1]["est"]["ev"]):
                if str((v.get("no_rebuy") or {}).get(sym) or "") >= session_day:
                    continue
                if sym in v["pos"] or len(v["pos"]) >= c["pick_count"] or v["buys_today"] >= c["max_orders_per_day"]:
                    continue
                q = quotes.get(sym)
                if not q or not market_open(sym, "BUY"):
                    continue
                ft = cd.get("features") or {}
                nw = cd.get("news") or {}
                if nw.get("pending") or (nw.get("ok") and nw.get("bad")):   # 📰 확인 중이거나 악재 공시면 모든 조합이 안 삼 (실전과 같게)
                    continue
                n_ratio = float(vc.get("news_unknown_ratio", 1)) if nw.get("ok") and not nw.get("items") else 1.0
                if cd["style"] == "surge":
                    if (ft.get("change") or 0) < vc["surge_min_change_pct"]:
                        continue
                    if not v.get("key") and (ft.get("change") or 0) > vc["surge_max_change_pct"]:   # 이 조합의 급등 최대 (실전 상한 이하만)
                        continue
                    need_ratio = vc["pre_volume_ratio"] if ft.get("pre") else vc["surge_volume_ratio"]
                    if (ft.get("ratio") or 0) < need_ratio:
                        continue
                    if minutes is None or minutes > 360:
                        continue
                lim = q["price"] * (1 + vc["limit_slip_pct"] / 100) if cd["style"] == "surge" else q["price"]
                px = tl.fill_px(vc, q["price"], "BUY", lim)
                qty = int(min(per_pos * n_ratio, v["cash"]) // (px * f))
                if qty < 1:
                    continue
                cost = qty * px * f * (1 + tl.side_cost(vc, market, "BUY") / 100)
                v["cash"] -= cost
                v["buys_today"] += 1
                v["pos"][sym] = {"qty": qty, "avg": px, "high": px, "style": cd["style"], "partial": False,
                                 "buy_day": datetime.now(KST).strftime("%Y-%m-%d"), "sday": session_day, "cost0": cost, "got": 0.0,
                                 "ft": {**{k: ft.get(k) for k in ("change", "ratio", "pre", "r20", "rsi") if ft.get(k) is not None},
                                        "style": cd["style"], "mkt": market}}
        msgs = _hot_reports(d, market)
        try:   # 📉 급락 신호 검증 (오류가 나도 기존 그림자 매매는 그대로)
            _flash_step(d, c, market, cands, hot, quotes, minutes, exits_only, signal_surge, market_open, session_day,
                        1 if market == "KR" else fx)
            msgs += _flash_reports(d, market)
        except Exception as e:
            print(f"🧪 급락 검증 오류: {type(e).__name__}: {e}")
        _save(market, d)
    if msgs:
        from . import notify
        for m in msgs:
            notify.send(m)


# ── 📉 급락 신호 검증 (가상 전용) ──
def _ep(t) -> float:
    try:
        return datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _bars(sym: str, n: int = 40) -> list[dict]:
    """1분봉 (기존 chart.candles 재사용 · 45초 캐시 · 실패하면 빈 목록)"""
    k = (sym, n)
    hit = _bars_cache.get(k)
    if hit and time.time() - hit[0] < 45:
        return hit[1]
    try:
        from . import chart
        bs = chart.candles(sym, "1m", n)
    except Exception:
        bs = []
    if len(_bars_cache) > 200:
        _bars_cache.clear()
    _bars_cache[k] = (time.time(), bs)
    return bs


def _done(sym: str) -> list[dict]:
    """다 끝난 1분봉만 (지금 만들어지는 봉 제외)"""
    bs = _bars(sym)
    if bs and time.time() - _ep(bs[-1]["t"]) < 60:
        bs = bs[:-1]
    return bs


def _slip(vc: dict) -> dict:
    """최소 0.1% 불리한 체결"""
    return {**vc, "sim_slip_pct": max(0.1, float(vc.get("sim_slip_pct", 0.1)))}


def _vsell(v: dict, sym: str, side: str, price: float, why: str, vc: dict, c: dict, market: str, f: float, session_day: str) -> None:
    """급락 검증 조합 가상 매도 (수수료·세금·0.1% 미끄러짐 반영, 실전 그림자와 같은 방식)"""
    p = v["pos"][sym]
    qty = p["qty"] if side == "SELL" else max(1, int(p["qty"] * vc["partial_tp_ratio"]))
    if side == "SELL_PART" and qty >= p["qty"]:
        p["partial"] = True
        return
    px = tl.fill_px(_slip(vc), price, "SELL")
    sc, bc = tl.side_cost(vc, market, "SELL") / 100, tl.side_cost(vc, market, "BUY") / 100
    got = qty * px * f * (1 - sc)
    v["cash"] += got
    net = px * (1 - sc) / (p["avg"] * (1 + bc)) - 1
    rec = {"t": _now(), "sym": sym, "style": p["style"], "qty": qty, "pnl_pct": round(net * 100, 2),
           "pnl_krw": round(net * p["avg"] * (1 + bc) * qty * f), "why": why[:60], "final": side == "SELL"}
    if side == "SELL":
        if p.get("cost0"):
            rec["trade_pct"] = round(((p.get("got", 0) + got) / p["cost0"] - 1) * 100, 2)
        v["trades"].append(rec)
        v["pos"].pop(sym)
        if _ret(rec) < 0 and c.get("no_rebuy_after_loss", True):   # 손해 본 종목 그날 재진입 금지
            if not isinstance(v.get("no_rebuy"), dict):
                v["no_rebuy"] = {}
            v["no_rebuy"][sym] = session_day
    else:
        v["trades"].append(rec)
        p["qty"] -= qty; p["partial"] = True; p["got"] = p.get("got", 0) + got


def _signal(v: dict, sym: str, kind: str, price: float, why: str) -> None:
    v.setdefault("signals", []).append({"t": _now(), "ts": time.time(), "sym": sym, "kind": kind, "px": price, "why": why[:60], "after": {}})


def _flash_track(vs: list[dict]) -> None:
    """신호 뒤 5·15·30분 최고/최저 채우기 (한 바퀴에 최대 6종목만 조회)"""
    now, fetched = time.time(), set()
    for v in vs:
        for s in v.get("signals", []):
            todo = [w for w in FLASH_WINDOWS if str(w) not in s["after"]]
            if not todo:
                continue
            el = now - s["ts"]
            if el > 3 * 3600:   # 너무 오래돼 1분봉으로 못 채움
                for w in todo:
                    s["after"][str(w)] = None
                continue
            ready = [w for w in todo if el >= w * 60 + 60]
            if not ready or (s["sym"] not in fetched and len(fetched) >= 6):
                continue
            fetched.add(s["sym"])
            bs = _bars(s["sym"], 200)
            for w in ready:
                seg = [b for b in bs if s["ts"] < _ep(b["t"]) + 60 and _ep(b["t"]) <= s["ts"] + w * 60]
                if not seg:
                    s["after"][str(w)] = None
                    continue
                hi, lo = max(b["h"] for b in seg), min(b["l"] for b in seg)
                s["after"][str(w)] = {"hi": hi, "lo": lo, "hi_pct": round((hi / s["px"] - 1) * 100, 2),
                                      "lo_pct": round((lo / s["px"] - 1) * 100, 2)}


def _flash_step(d: dict, c: dict, market: str, cands: dict, hot: dict, quotes: dict, minutes, exits_only: bool,
                signal_surge, market_open, session_day: str, f: float) -> None:
    """급락청산(FX): '현재' 조합이 산 단타를 똑같이 사서, 기존 매도 규칙 + 급락 매도 신호로 팜 → '현재'와 비교
    급락반등(FR): 최근 1시간 안에 본 급등 후보 중 급락 뒤 반등하는 종목을 가상으로 삼 (실전 매수 조건과 무관)"""
    from . import picker, toss
    fv = {v["key"]: v for v in d["flash"]}
    fx_v, fr_v = fv.get("FX"), fv.get("FR")
    if not fx_v or not fr_v:
        return
    now = time.time()
    watch = d.setdefault("flash_watch", {})
    for s in list(cands) + list(hot):
        watch[s] = now
    for s in [s for s, t in watch.items() if now - t > 3600]:
        watch.pop(s)
    for v in (fx_v, fr_v):
        if v["day"] != session_day:
            v["day"], v["buys_today"], v["copied"], v["seen"] = session_day, 0, {}, {}
    # FX: '현재' 조합의 새 단타 진입을 같은 가격으로 따라 삼
    base_v = next((v for v in d["variants"] if _is_base(v)), None)
    if base_v:
        for sym, p in base_v["pos"].items():
            mark = f"{p.get('sday', '')}:{p['avg']}"
            if p["style"] != "surge" or p.get("partial") or not p.get("cost0") or sym in fx_v["pos"] or fx_v["copied"].get(sym) == mark:
                continue
            fx_v["copied"][sym] = mark
            if fx_v["cash"] < p["cost0"]:
                continue
            fx_v["cash"] -= p["cost0"]
            fx_v["pos"][sym] = {**{k: p[k] for k in ("qty", "avg", "style", "buy_day", "sday", "cost0") if k in p},
                                "high": p["avg"], "partial": False, "got": 0.0, "ets": now}
    # 매도
    for v in (fx_v, fr_v):
        vc = {**c, **v["params"]}
        for sym in list(v["pos"]):
            p, q = v["pos"][sym], quotes.get(sym)
            if not q:
                continue
            price = q["price"]
            p["high"] = max(p["high"], price)
            side, why = "", ""
            if v is fx_v:
                bs = _done(sym)
                if any(_ep(b["t"]) >= p.get("ets", 0) for b in bs[-1:]):   # 산 뒤 끝난 1분봉이 있을 때만
                    why = picker.flash_exit_signal(bs, price)
                if why:
                    side = "SELL"
                    _signal(v, sym, "exit", price, why)
                else:
                    side, why = signal_surge(price, p["qty"], p["avg"], p["high"], minutes, False, vc, partial_done=p.get("partial", False))
            else:
                if price < p["stop"]:
                    side, why = "SELL", f"급락 저점 {p['stop']:g} 이탈 손절"
                elif price >= p["avg"] * (1 + REB_TP_PCT / 100):
                    side, why = "SELL", f"반등 익절 +{REB_TP_PCT:g}%"
                elif p["high"] >= p["avg"] * 1.01 and price <= p["high"] * (1 - REB_TRAIL_PCT / 100):
                    side, why = "SELL", f"고점 대비 -{REB_TRAIL_PCT:g}%"
                elif now - p.get("ets", now) > REB_MAX_MIN * 60:
                    side, why = "SELL", f"{REB_MAX_MIN}분 경과"
            if side in ("SELL", "SELL_PART") and market_open(sym, "SELL"):
                _vsell(v, sym, side, price, why, vc, c, market, f, session_day)
    # FR 매수
    if not exits_only and minutes is not None and minutes <= 360:
        vc = {**c, **fr_v["params"]}
        syms = [s for s in sorted(watch, key=lambda s: -watch[s]) if s not in fr_v["pos"]][:FLASH_MAX_SYMS]
        miss = [s for s in syms if s not in quotes]
        if miss:
            try:
                quotes = {**quotes, **toss.prices(miss)}
            except toss.TossError:
                pass
        per_pos = VIRTUAL / max(1, int(c["pick_count"]))
        for sym in syms:
            if len(fr_v["pos"]) >= c["pick_count"] or fr_v["buys_today"] >= c["max_orders_per_day"]:
                break
            if str((fr_v.get("no_rebuy") or {}).get(sym) or "") >= session_day:
                continue
            q = quotes.get(sym)
            nw = (cands.get(sym) or hot.get(sym) or {}).get("news") or {}
            if not q or nw.get("pending") or (nw.get("ok") and nw.get("bad")) or not market_open(sym, "BUY"):
                continue
            sig = picker.flash_rebound_signal(_done(sym), q["price"])
            if not sig or fr_v["seen"].get(sym) == sig["crash_t"]:
                continue
            fr_v["seen"][sym] = sig["crash_t"]   # 같은 급락으로 두 번 안 삼
            px = tl.fill_px(_slip(vc), q["price"], "BUY")
            qty = int(min(per_pos, fr_v["cash"]) // (px * f))
            if qty < 1:
                continue
            cost = qty * px * f * (1 + tl.side_cost(vc, market, "BUY") / 100)
            fr_v["cash"] -= cost
            fr_v["buys_today"] += 1
            fr_v["pos"][sym] = {"qty": qty, "avg": px, "high": px, "style": "flash", "partial": False, "stop": sig["low"],
                                "buy_day": datetime.now(KST).strftime("%Y-%m-%d"), "sday": session_day, "cost0": cost,
                                "got": 0.0, "ets": now}
            _signal(fr_v, sym, "rebound", q["price"], sig["why"])
    _flash_track([fx_v, fr_v])


def _flash_reports(d: dict, market: str) -> list[str]:
    """급락 검증 조합마다 끝난 거래 20건 쌓일 때마다 보고문 (같은 기간 '현재' 비교 + 신호 뒤 5·15·30분 최고/최저 평균)"""
    msgs = []
    base_v = next((v for v in d["variants"] if _is_base(v)), None)
    flag = '🇺🇸' if market == 'US' else '🇰🇷'
    for v in d.get("flash", []):
        fin = _fin(v)
        if len(fin) < int(v.get("reported", 0)) + FLASH_REPORT_EVERY:
            continue
        v["reported"] = len(fin) // FLASH_REPORT_EVERY * FLASH_REPORT_EVERY
        n = len(fin)
        win = round(sum(1 for t in fin if _ret(t) > 0) / n * 100)
        avg = _avg(fin)
        total = sum(t["pnl_krw"] for t in v["trades"]) / VIRTUAL * 100
        line = (f"{flag} [📉 급락 검증] '{v['name']}' 끝난 거래 {n}건 (시작 {v['created'][:10]})\n"
                f"승률 {win}% · 평균 {avg:+.2f}% · 최대 손실 {min(_ret(t) for t in fin):+.2f}% · 총 {total:+.2f}% (가상 100만 원, 수수료·0.1% 미끄러짐 뺀 값)")
        if base_v:
            bf = [t for t in _fin(base_v) if t["t"] >= v["created"]]
            if bf:
                ba = _avg(bf)
                line += f"\n실전 규칙('현재' 조합, 같은 기간 {len(bf)}건): 평균 {ba:+.2f}% → 차이 {avg - ba:+.2f}%p"
        sg = v.get("signals", [])
        parts = []
        for w in FLASH_WINDOWS:
            xs = [s["after"][str(w)] for s in sg if s["after"].get(str(w))]
            if xs:
                parts.append(f"{w}분 최고 {st.mean(x['hi_pct'] for x in xs):+.1f}%/최저 {st.mean(x['lo_pct'] for x in xs):+.1f}%")
        if parts:
            line += f"\n신호 {len(sg)}건 뒤 평균: " + " · ".join(parts)
        line += "\n※ 실전 규칙 자동 교체 대상 아님 (검증용)"
        msgs.append(line)
    return msgs


def base_samples(market: str) -> list[dict]:
    """'비슷한 거래' 표본용: 실전 규칙과 같은 '현재' 조합의 끝난 거래만 (다른 조합은 안 넣음)"""
    with _lock:
        d = store.load(_file(market))
        v = next((v for v in d.get("variants") or [] if _is_base(v)), None)
        if not v:
            return []
        return [{"sym": t["sym"], "day": t.get("day", ""), "pnl_pct": _ret(t), "features": dict(t["features"]), "closed": True}
                for t in v.get("trades", []) if t.get("final") and isinstance(t.get("features"), dict)][-200:]


def hot_cands(c: dict, market: str, fx: float, log=print) -> dict:
    """급등 검증 조합용 후보: 실전과 같은 급등 찾기(거래량·거래대금·최소 주가·경고 종목 제외)를 상승률 30% 초과 구간으로만.
    실전 설정은 안 바꿈 (이 함수 안에서만 쓰는 복사본)"""
    from . import news, picker
    lo = min(h["lo"] for h in HOT)
    scfg = {**c, "surge_min_change_pct": lo, "surge_max_change_pct": 10000, "pick_count": 6,
            "max_order_krw": VIRTUAL / max(1, int(c["pick_count"])), "usd_krw": fx}   # 가격 상한은 가상 종목당 금액
    r = picker.surge_scan(scfg, log=log, exclude=set(c.get("exclude") or []))
    out = {}
    for x in r.get("picks", []):
        if (x.get("change") or 0) <= lo:   # 딱 30% 는 실전 구간 → 제외
            continue
        ft = {k: x.get(k) for k in ("change", "ratio", "pre", "minute", "score")}
        ft.update(style="surge", mkt=market)
        cd = {"style": "surge", "features": ft, "reason": x["reason"], "score": x.get("score", 0), "est": tl.estimate(ft, c)}
        if c.get("news_on") and news.enabled(market):   # 📰 실전과 같게: 확인 중·악재면 step 에서 안 삼
            cd["news"] = news.peek(x["symbol"], market)
        out[x["symbol"]] = cd
    return out


def _hot_reports(d: dict, market: str) -> list[str]:
    """급등 검증 조합마다 끝난 거래가 20건 쌓일 때마다 보고문 (실전 규칙 '현재' 조합과 같은 기간 비교)"""
    msgs = []
    base_v = next((v for v in d["variants"] if _is_base(v)), None)
    flag = '🇺🇸' if market == 'US' else '🇰🇷'
    for v in d["hot"]:
        fin = _fin(v)
        if len(fin) < int(v.get("reported", 0)) + HOT_REPORT_EVERY:
            continue
        v["reported"] = len(fin) // HOT_REPORT_EVERY * HOT_REPORT_EVERY
        n = len(fin)
        win = round(sum(1 for t in fin if _ret(t) > 0) / n * 100)
        avg = _avg(fin)
        worst = min(_ret(t) for t in fin)
        total = sum(t["pnl_krw"] for t in v["trades"]) / VIRTUAL * 100
        line = (f"{flag} [🧪 급등 검증] '{v['name']}' 끝난 거래 {n}건 (시작 {v['created'][:10]})\n"
                f"승률 {win}% · 평균 {avg:+.2f}% · 최대 손실 {worst:+.2f}% · 총 {total:+.2f}% (가상 100만 원)")
        if base_v:
            bf = [t for t in _fin(base_v) if t["t"] >= v["created"]]
            if bf:
                bw = round(sum(1 for t in bf if _ret(t) > 0) / len(bf) * 100)
                ba = _avg(bf)
                line += (f"\n실전 규칙('현재' 조합, 같은 기간 {len(bf)}건): 승률 {bw}% · 평균 {ba:+.2f}%"
                         f" → 차이 승률 {win - bw:+d}%p · 평균 {avg - ba:+.2f}%p")
            else:
                line += "\n실전 규칙('현재' 조합)은 같은 기간 끝난 거래가 없어 비교 못 함"
        line += "\n※ 실전 규칙 자동 교체 대상 아님 (검증용)"
        msgs.append(line)
    return msgs


def stats(v: dict, days: int = 14) -> dict:
    since = (datetime.now(KST) - timedelta(days=days)).strftime("%Y-%m-%d")
    ts = [t for t in v["trades"] if t["t"] >= since]
    fin = [t for t in ts if t.get("final")]
    total = sum(t["pnl_krw"] for t in ts)
    return {"n": len(fin), "ret_pct": round(total / VIRTUAL * 100, 2),
            "win_rate": round(sum(1 for t in fin if _ret(t) > 0) / len(fin) * 100) if fin else 0,
            "avg_pct": round(_avg(fin), 2), "holding": len(v["pos"])}


def summary(v: dict) -> dict:
    """조합 전체 기록 요약: 건수·승률·평균·최대 손실 (수수료 뺀 거래 전체 수익률)"""
    fin = _fin(v)
    rs = [_ret(t) for t in fin]
    return {"n_all": len(rs), "win_all": round(sum(1 for r in rs if r > 0) / len(rs) * 100) if rs else 0,
            "avg_all": round(st.mean(rs), 2) if rs else 0, "worst": round(min(rs), 2) if rs else 0}


def overview(c: dict, market: str) -> dict:
    with _lock:
        d = _load(market)
    base = base_params(c)
    rows = []
    for v in d["variants"]:
        rows.append({"id": v["id"], "name": v["name"], "origin": v["origin"], "diff": _diff(v["params"], base),
                     **stats(v, int(c.get("promote_window_days", 14))), **summary(v)})
    rows.sort(key=lambda r: (r["name"] != "현재", -r["avg_all"] if r["n_all"] >= PRUNE_N else 0, -r["n_all"]))
    hot = [{"id": v["id"], "name": v["name"], "origin": v["origin"], "diff": "30% 초과 급등 검증 (자동 교체 안 함)", **stats(v, 3650), **summary(v)}
           for v in d.get("hot", [])]
    flash = [{"id": v["id"], "name": v["name"], "origin": v["origin"], "diff": "급락 신호 검증 (자동 교체 안 함)",
              "signals": len(v.get("signals", [])), **stats(v, 3650), **summary(v)} for v in d.get("flash", [])]
    lines = [f"{r['name']}: {r['n_all']}건 · 승률 {r['win_all']}% · 평균 {r['avg_all']:+.2f}% · 최대 손실 {r['worst']:+.2f}%" for r in rows]
    return {"variants": rows, "hot": hot, "flash": flash, "promotions": d["promotions"][-10:][::-1], "pruned": d["pruned"][-10:][::-1],
            "summary": lines, "count": len(rows),
            "rule": f"교체: 조합별 {max(PROMOTE_N, int(c.get('promote_min_trades', 15)))}건↑ · 앞 {round((1 - HOLDOUT) * 100)}%로 1개 선정 → "
                    f"뒤 {round(HOLDOUT * 100)}%(최근)에서도 '현재'·실전보다 평균 수익(수수료 뺀) 높고 플러스일 때만 · "
                    f"정리: {PRUNE_N}건↑ 조합 중 하위 {round(PRUNE_FRAC * 100)}% 교체"}


def _live_avg(market: str, since: str) -> tuple[int, float]:
    """같은 기간 실전(엔진) 거래 일지의 끝난 거래 평균 수익(수수료 뺀 값)"""
    try:
        ts = [t for t in tl._load()["trades"] if t.get("closed") and tl._tm(t) == market and t.get("exits")
              and str(t["exits"][-1].get("t", "")) >= since and "pnl_pct" in t]
    except Exception:
        return 0, 0.0
    return len(ts), (st.mean(float(t["pnl_pct"]) for t in ts) if ts else 0.0)


def maybe_promote(c: dict, market: str, apply_fn) -> str:
    """'현재'보다 확실히 나은 조합이 있으면 실전 규칙으로 교체. apply_fn(params, why, evidence) -> bool
    ① 조합별 끝난 거래 20건↑ ② 앞쪽 70% 거래로 '현재'(같은 기간)보다 평균이 높은 조합 중 1개만 고름
    ③ 고른 조합이 뒤쪽 30%(최근) 거래에서도 같은 기간 '현재'·실전 거래보다 평균이 높고 플러스여야 교체"""
    with _lock:
        d = _load(market)
    base_v = next((v for v in d["variants"] if _is_base(v)), None)
    if not base_v:
        return ""
    bfin = _fin(base_v)
    min_n = max(PROMOTE_N, int(c.get("promote_min_trades", 15)))
    best = None
    for v in d["variants"]:
        if v is base_v or v.get("key"):
            continue
        fin = _fin(v)
        if len(fin) < min_n:
            continue
        cut = int(len(fin) * (1 - HOLDOUT))
        early, late = fin[:cut], fin[cut:]
        b_early = [t for t in bfin if early[0]["t"] <= t["t"] < late[0]["t"]]
        if len(b_early) < CMP_MIN or _avg(early) <= 0:
            continue
        edge = _avg(early) - _avg(b_early)
        if edge > 0 and (best is None or edge > best[0]):
            best = (edge, v, early, late, b_early)
    if not best:
        return ""
    edge, v, early, late, b_early = best   # 앞쪽 거래로 고른 1개만 뒤쪽에서 검증 (여러 개 시험하면 다시 운이 끼어서)
    since = late[0]["t"]
    b_late = [t for t in bfin if t["t"] >= since]
    la, ba = _avg(late), _avg(b_late)
    ln, lv = _live_avg(market, since)
    if len(b_late) < CMP_MIN or la <= 0 or la <= ba or (ln >= CMP_MIN and la <= lv):
        return ""
    params = _clean(dict(v["params"]), c)
    if not params:
        return ""
    ev = (f"'{v['name']}' {len(early) + len(late)}건 — 앞 {len(early)}건 평균 {_avg(early):+.2f}% vs '현재' {_avg(b_early):+.2f}% · "
          f"최근 {len(late)}건 평균 {la:+.2f}% vs '현재' {ba:+.2f}%({len(b_late)}건)"
          + (f" · 실전 {lv:+.2f}%({ln}건)" if ln >= CMP_MIN else "") + " (수수료·미끄러짐 뺀 값)")
    why = f"그림자 가상 매매에서 더 나은 규칙 조합 → 실전 적용 ({_diff(params, base_v['params'])})"
    if not apply_fn(params, why, ev):
        return ""
    with _lock:
        d = _load(market)
        d["promotions"].append({"t": _now(), "name": v["name"], "diff": _diff(params, base_v["params"]), "evidence": ev})
        _save(market, d)   # 다음 ensure 에서 이 조합 기록이 '현재'로 이어짐 (나머지 조합 기록은 유지)
    return why + " · " + ev


def prune(c: dict, market: str) -> str:
    """20건↑ 쌓인 조합 중 수수료 뺀 평균 하위 25% 는 빼고 새 조합(백테스트 상위·격자)으로 채워 20개 안팎 유지"""
    ensure(c, market)
    with _lock:
        d = _load(market)
        mature = [v for v in d["variants"] if not _is_base(v) and len(_fin(v)) >= PRUNE_N]
        k = int(len(mature) * PRUNE_FRAC)
        drop = sorted(mature, key=lambda v: _avg(_fin(v)))[:k]
        for v in drop:
            d["retired"].append(_phash(v["params"], c))
            d["pruned"].append({"t": _now(), "name": v["name"], **summary(v)})
            d["variants"].remove(v)
        added = _fill(d, c, market)
        if not drop and not added:
            return ""
        _save(market, d)
    return (f"하위 {len(drop)}개 정리 ({', '.join(v['name'] for v in drop)})" if drop else "") + (f" · 새 조합 {added}개 추가" if added else "")


def add_seeds(c: dict, market: str, seeds: list[dict]) -> int:
    """백테스트 상위 조합을 넣기 (기존 조합 기록은 유지 · 넘치면 거래 적은 조합부터 정리 · 예전에 뺀 조합은 안 넣음)"""
    ensure(c, market)
    base = base_params(c)
    with _lock:
        d = _load(market)
        have = {_phash(v["params"], c) for v in d["variants"]} | set(d["retired"])
        new = []
        for sd in seeds[:6]:
            p = _clean({**base, **{k: v for k, v in sd.items() if k in TUNE_KEYS}}, c)
            if p and _phash(p, c) not in have:
                d["seq"] += 1
                new.append(_new_variant(f"백테스트{d['seq']}", p, "백테스트 추천"))
                have.add(_phash(p, c))
        if not new:
            return 0
        d["variants"] += new
        _shrink(d, N_MAX, keep=new)
        _save(market, d)
        return len(new)
