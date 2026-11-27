"""📼 AI 리그 과거 장 복습 — 지난 30일 고른 후보를 '그 시점까지의 1분봉'만 보여 주고 리그 AI 들이 다시 판단 → 그 뒤 실제 1분봉으로 채점.

- 후보 두 가지: ① 우리가 고른 후보(최근 30일) ② 🌐 시장 전체에서 찾은 과거 급등일 — 거래 많은 종목(최대 MKT_SYMS 개)의
  토스 일봉에서 '상승률·거래량 기준을 넘은 날'을 찾고, 1분봉이 남아 있는 날은 실전과 같은 진입 시점(기준을 처음 넘은 봉)으로 재현
  → 우리가 놓친 급등까지 배움 (토스 데이터만 · 가입·비용 없음 · 1분봉은 받아 둔 것 다시 씀)
- 장이 닫혔을 때만 (매일 복기 뒤 · 토요일 주말 공부) · 하루 arena_replay_per_day 후보까지 (후보 하나에 AI 1번, 참가자 전원이 같이 판단)
- 미래 정보 차단: 종목 이름·날짜를 가리고, 고른 시각 이전 40분 1분봉만 보여 줌 (가격은 고른 값 대비 % 로만)
- 채점: 각 AI 의 익절·손절·최대 보유 시간으로 그 뒤 1분봉을 따라감 (한 봉 안에 손절·익절이 같이 닿으면 손절로 — 보수적)
  · 0.1% 불리한 체결 + 수수료 · 그날 장 끝나면 마지막 값
- 🎯 선별력: 그 AI 가 '산다'고 한 후보 평균 − 같은 규칙으로 후보를 전부 샀을 때 평균 (고르는 실력만 봄)
- 질 관리: 과거 장 성적은 공부용 — 실전 자격 판정에는 안 씀 · 같은 후보는 한 번만 · 결과 요약은 공유 노트에 교훈으로
- 주문 함수 안 부름 (1분봉 읽기만)
기록: _cache/arena_US.json 의 replay · 참가자별 replay
"""

from __future__ import annotations

import statistics as st
import threading
from datetime import datetime, timedelta, timezone

from . import store
from . import tradelearn as tl

KST = timezone(timedelta(hours=9))
CTX_MIN = 40          # 보여 줄 과거 1분봉 (분)
MKT_SYMS = 150        # 🌐 과거 급등일을 찾을 거래 많은 종목 수
MKT_1M_SYMS = 25      #   한 번에 1분봉까지 받을 종목 수 (받은 건 보관 → 다음엔 새 날만)
MKT_DAYS = 45         #   일봉에서 볼 기간
KEEP = 600            # 참가자별 복습 기록 보관
_running = threading.Event()


def _bars_around(sym: str, day: str, pick_t: datetime, market: str) -> tuple[list[dict], list[dict]]:
    """(고른 시각 전 CTX_MIN 분, 고른 시각 뒤 그날 끝까지) 1분봉"""
    from . import backtest as bt
    path = bt._minute_path(sym, day, pick_t - timedelta(minutes=CTX_MIN + 5), market)
    before = [b for b in path if bt._ts(b["t"]) < pick_t][-CTX_MIN:]
    after = [b for b in path if bt._ts(b["t"]) >= pick_t]
    return before, after


def _sim(after: list[dict], entry: float, tp: float, sl: float, hold: int, market: str) -> float | None:
    from .arena import _ret
    if not after or not entry:
        return None
    up, dn = entry * (1 + tp / 100), entry * (1 - sl / 100)
    for i, b in enumerate(after):
        if b["l"] <= dn:
            return _ret(entry, dn, market)
        if b["h"] >= up:
            return _ret(entry, up, market)
        if i + 1 >= hold:
            return _ret(entry, b["c"], market)
    return _ret(entry, after[-1]["c"], market)


def _chart_text(before: list[dict], entry: float, x: dict) -> str:
    """종목·날짜 없이 % 로만"""
    if not before:
        return ""
    cl = [round((b["c"] / entry - 1) * 100, 1) for b in before]
    hi = round((max(b["h"] for b in before) / entry - 1) * 100, 1)
    lo = round((min(b["l"] for b in before) / entry - 1) * 100, 1)
    return (f"오늘 상승률 {float(x.get('change') or 0):+.1f}% · 거래량 {float(x.get('ratio') or 0):.1f}배 · {'프리장' if x.get('pre') else '정규장'}\n"
            f"지금 가격 기준 최근 {len(cl)}분 1분봉 종가(%, 오래된→최근): {cl}\n그 구간 고가 {hi:+.1f}% · 저가 {lo:+.1f}%")


def _candidates(market: str, days: int = 30) -> list[dict]:
    since = (datetime.now(KST) - timedelta(days=days)).strftime("%Y-%m-%d")
    from . import picker
    today = picker.session_date(market)
    with tl._lock:
        w = dict(tl._load()["watch"])
    out = []
    for key, rows in w.items():
        mk, _, day = key.partition(":")
        if not day or mk != market or day < since or day >= today:   # 끝난 날만
            continue
        for sym, x in rows.items():
            if x.get("pick_px") and x.get("t"):
                out.append({"key": f"{day}:{sym}", "sym": sym, "day": day, **x})
    out.sort(key=lambda r: r["key"], reverse=True)   # 최근 것부터
    return out


def _market_events(c: dict, market: str, done: set, want: int, log=print) -> list[dict]:
    """🌐 시장 전체 과거 급등일 → 실전과 같은 진입 시점의 (전 40분, 뒤 그날 끝까지) 1분봉. 이미 복습한 것·우리가 고른 것은 뺌"""
    from . import backtest as bt
    from . import picker, toss
    if want <= 0:
        return []
    base = picker.load_base(market).get("rows") or {}
    syms = sorted(base, key=lambda s_: -float(base[s_].get("value20_krw") or 0))[:MKT_SYMS]
    if not syms:
        return []
    today = picker.session_date(market)
    lo, vr = float(c.get("surge_min_change_pct", 10)), float(c.get("surge_volume_ratio", 3))
    events = []
    for s_ in syms:
        try:
            closes, vols, dates = toss.daily(s_, MKT_DAYS + 21)
        except Exception:
            continue
        for i in range(20, len(closes)):
            d_ = dates[i][:10]
            v20 = st.fmean(vols[i - 20:i]) if vols[i - 20:i] else 0
            if d_ >= today or not closes[i - 1] or not v20 or f"{d_}:{s_}" in done or f"{d_}:{s_}:m" in done:
                continue
            chg = (closes[i] / closes[i - 1] - 1) * 100
            if chg >= lo and vols[i] / v20 >= vr:
                events.append({"sym": s_, "day": d_, "prev": closes[i - 1], "vol20": v20})
    if not events:
        return []
    by: dict[str, list[dict]] = {}
    for e in sorted(events, key=lambda e: e["day"], reverse=True):
        by.setdefault(e["sym"], []).append(e)
    out = []
    for s_ in list(by)[:MKT_1M_SYMS]:
        evs = by[s_]
        try:
            bars, _ = bt._hist_1m(s_, market, min(e["day"] for e in evs), today)
        except Exception:
            continue
        by_day: dict[str, list[dict]] = {}
        for b in bars:
            if picker.session_minutes(market, bt._ts(b["t"])) is not None:
                by_day.setdefault(picker.session_date(market, bt._ts(b["t"])), []).append(b)
        for e in evs:
            db = by_day.get(e["day"]) or []
            if len(db) < 30:
                continue
            try:
                ip, _ = bt._entries(db, e["prev"], e["vol20"], c, market)
            except Exception:
                ip = None
            if ip is None or ip < 10 or ip >= len(db) - 5:
                continue
            cv = sum(b.get("v", 0) for b in db[:ip + 1])
            mins = picker.session_minutes(market, bt._ts(db[ip]["t"])) or 0
            ratio = cv / max(e["vol20"] * min(1.0, max(10, mins) / 390), 1)
            out.append({"key": f"{e['day']}:{s_}:m", "sym": s_, "day": e["day"], "src": "시장",
                        "change": round((db[ip]["c"] / e["prev"] - 1) * 100, 1), "ratio": round(ratio, 1), "pre": False,
                        "_bars": (db[max(0, ip + 1 - CTX_MIN):ip + 1], db[ip + 1:])})
            if len(out) >= want:
                return out
    log(f"📼 과거 장 복습: 시장 전체에서 놓친 급등 후보 {len(out)}개 찾음")
    return out


def run(c: dict, market: str, log=print, limit: int | None = None, ai_fn=None) -> str:
    """과거 장 복습 한 번 (뒤에서 · 장 닫혔을 때). 요약 한 줄"""
    from . import arena
    if not c.get("arena_on") or not c.get("arena_replay", True) or _running.is_set():
        return ""
    limit = int(limit if limit is not None else c.get("arena_replay_per_day", 40))
    if limit <= 0:
        return ""
    _running.set()
    try:
        with arena._lock:
            d = arena.load(market, int(c.get("arena_size", 10)))
            done = set((d.get("replay") or {}).get("done") or [])
        todo = [x for x in _candidates(market) if x["key"] not in done][:limit]
        if len(todo) < limit and c.get("arena_replay_market", True):   # 🌐 모자라면 시장 전체 과거 급등일로 채움
            try:
                todo += _market_events(c, market, done | {x["key"] for x in todo}, limit - len(todo), log)
            except Exception as e:
                log(f"📼 시장 전체 후보 찾기 실패: {type(e).__name__}: {e}")
        if not todo:
            return ""
        n_ok = 0
        notes = arena._notes(market)
        for x in todo:
            try:
                if x.get("_bars"):
                    before, after = x["_bars"]
                else:
                    pick_t = datetime.strptime(x["t"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
                    before, after = _bars_around(x["sym"], x["day"], pick_t, market)
            except Exception:
                before, after = [], []
            entry = float(after[0]["c"]) if after else 0.0
            with arena._lock:
                d = arena.load(market)
                rp = d.setdefault("replay", {"done": []})
                rp["done"] = (rp.get("done") or [])[-3000:] + [x["key"]]
                alive = [{"id": a["id"], "name": a["name"], "persona": a["persona"], "tp": a["tp"], "sl": a["sl"], "hold": a["hold"]}
                         for a in d["alive"]]
                store.save(arena._file(market), d)
            if len(before) < 10 or len(after) < 5 or not entry:
                continue
            prompt = ("AI 리그 과거 장 복습: 아래는 지난 장의 급등 후보예요. 종목과 날짜는 가렸고, 그 시점까지의 정보만 보여요. "
                      "각 참가자가 자기 성격대로 '지금 산다/안 산다'를 판단해요 (가상 공부용).\n"
                      + _chart_text(before, entry, x) + "\n"
                      + (f"공유 노트: {notes[:800]}\n" if notes else "")
                      + "참가자:\n" + "\n".join(f"- {a['id']} ({a['name']}): {a['persona']}" for a in alive)
                      + '\n형식: {"참가자id": true/false, ...} (성격에 맞을 때만 true)')
            r = (ai_fn or arena.ai_json)(prompt)
            if not isinstance(r, dict):
                continue
            n_ok += 1
            with arena._lock:
                d = arena.load(market)
                for a in d["alive"]:
                    ret = _sim(after, entry, float(a["tp"]), float(a["sl"]), int(a["hold"]), market)
                    if ret is None:
                        continue
                    a.setdefault("replay", []).append({"k": x["key"], "buy": r.get(a["id"]) is True, "ret": round(ret, 3),
                                                       **({"src": "시장"} if x.get("src") else {})})
                    a["replay"] = a["replay"][-KEEP:]
                store.save(arena._file(market), d)
        summ = summary(market)
        best = summ[0] if summ else None
        n_m = sum(1 for x in todo if x.get("src"))
        line = f"📼 AI 리그 과거 장 복습: 후보 {n_ok}개 다시 판단" + (f" (🌐 시장 전체 놓친 급등 {n_m}개 포함)" if n_m else "") + (
            f" · 선별력 1등 {best['name']} {best['edge']:+.2f}%p ({best['n_buy']}건 산 평균 {best['avg_buy']:+.2f}%)" if best and best["n_buy"] >= 5 else "")
        if best and best["n_buy"] >= 10 and best["edge"] > 0.3:
            tl.add_lesson(market, f"[과거 장 복습] '{best['persona'][:70]}' 전략이 후보 {best['n']}개 중 {best['n_buy']}개를 골라 "
                                  f"전부 샀을 때보다 {best['edge']:+.2f}%p 나았음", "AI 리그 과거 장 복습")
        with arena._lock:
            d = arena.load(market)
            d.setdefault("replay", {})["last"] = {"t": datetime.now(KST).strftime("%Y-%m-%d %H:%M"), "n": n_ok, "line": line}
            store.save(arena._file(market), d)
        log(line)
        return line
    finally:
        _running.clear()


def summary(market: str) -> list[dict]:
    """참가자별 과거 장 복습 성적 (선별력 순)"""
    from . import arena
    d = arena.load(market)
    out = []
    for a in d["alive"]:
        rs = a.get("replay") or []
        if not rs:
            continue
        buys = [x["ret"] for x in rs if x["buy"]]
        allr = [x["ret"] for x in rs]
        avg_all = st.fmean(allr)
        avg_buy = st.fmean(buys) if buys else 0.0
        skips = [x["ret"] for x in rs if not x["buy"]]
        out.append({"name": a["name"], "persona": a["persona"], "n": len(rs), "n_buy": len(buys), "avg_buy": round(avg_buy, 2),
                    "avg_all": round(avg_all, 2), "edge": round(avg_buy - avg_all, 2) if buys else 0.0,
                    "skip_ok": round(sum(x <= 0 for x in skips) / len(skips) * 100) if skips else 0})
    return sorted(out, key=lambda r: (-(r["n_buy"] >= 5), -r["edge"]))


def text(market: str) -> str:
    rows = summary(market)
    if not rows:
        return ""
    return "📼 과거 장 복습(공부용·실전 자격엔 안 씀): " + " · ".join(
        f"{r['name']} 선별력 {r['edge']:+.2f}%p({r['n_buy']}/{r['n']})" for r in rows[:5])


def start(c: dict, market: str, log=print, limit: int | None = None) -> None:
    """뒤에서 돌리기 (매매 루프 안 막음)"""
    def go():
        from . import autotrade as at
        try:
            with at.in_market(market):
                run(at.config(), market, log, limit)
        except Exception as e:
            log(f"📼 과거 장 복습 오류: {type(e).__name__}: {e}")
    threading.Thread(target=go, daemon=True, name=f"arena-replay-{market}").start()
