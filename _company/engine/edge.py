"""🎯 판단 질 점검 — 실전 엔진의 판단이 정말 돈이 되는지 숫자로 확인하고 자동으로 맞춤 (주문 함수 안 부름).

1. 🤖 AI 반대 판단 검증: AI 가 '안 산다'로 막은 종목을 그때 가격으로 샀다면(실전 규칙: 익절·손절·시간 손절·마감)
   어땠을지 채점 → 막은 종목 평균이 AI 가 산 거래보다 확실히 좋으면(20건↑·앞뒤 절반 모두) AI 반대를 '금액 50%로 사기'로 약하게,
   다시 막은 종목이 손해 쪽이면 원래대로 '안 삼'
2. 🚧 지는 구간 차단: 끝난 거래(실전+가상)를 구간(상승률·거래량 배수·시간대·프리장)별로 → 20건↑이고 앞·뒤 절반 모두 평균 마이너스인
   구간은 실전으로 안 삼 (가상으로는 계속 사서 배움 → 좋아지면 자동으로 풀림)
3. 🎲 승률 추정 보정: '승률 p' 라고 추정한 거래들이 실제로 얼마나 이겼는지 구간별로 → 금액 정할 때 추정 승률을 실제에 맞게 고침
기록: _cache/edge_{US|KR}.json (매일 복기 때 다시 계산)
"""

from __future__ import annotations

import statistics as st
import threading
import time
from datetime import datetime, timedelta, timezone

from . import store
from . import tradelearn as tl
from .agents import COMPANY_DIR

KST = timezone(timedelta(hours=9))
_lock = threading.Lock()
VETO_MIN, VETO_MARGIN = 20, 0.3      # 막은 종목 20건↑ · AI 가 산 거래보다 +0.3%p↑ 나으면 AI 반대 약하게
VETO_KEEP = 300                      # 채점 끝난 기록 보관
VETO_TRACK_MAX = 60                  # 동시에 지켜보는 막은 종목
BUCKET_MIN = 20
CAL_MIN = 20                         # 승률 구간마다 이만큼↑ 이어야 보정
CAL_BINS = ((0.0, 0.40), (0.40, 0.50), (0.50, 0.60), (0.60, 1.01))


def _file(m: str):
    return COMPANY_DIR / "_cache" / f"edge_{m}.json"


def _now() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


def load(m: str) -> dict:
    d = store.load(_file(m), lambda: {})
    for k, v in (("veto_open", []), ("veto_done", []), ("veto_mode", "skip"), ("blocked", {}), ("calib", {})):
        d.setdefault(k, v)
    return d


def _avg(xs: list[float]) -> float:
    return st.fmean(xs) if xs else 0.0


# ── 1. AI 반대 판단 검증 ──
def veto_mode(market: str) -> str:
    """'skip'(원래: AI 가 반대하면 안 삼) / 'shrink'(반대해도 금액 50%로 삼 — 막은 종목이 더 잘 됐을 때)"""
    return load(market).get("veto_mode", "skip")


def veto_add(market: str, sym: str, price: float, c: dict, day: str, kind: str = "AI 판단") -> None:
    if not price:
        return
    with _lock:
        d = load(market)
        if any(x["sym"] == sym and x["day"] == day for x in d["veto_open"] + d["veto_done"][-100:]):
            return   # 같은 종목·같은 날은 한 번만 (독립 표본)
        if len(d["veto_open"]) >= VETO_TRACK_MAX:
            return
        d["veto_open"].append({"sym": sym, "px": float(price), "t": time.time(), "at": _now(), "day": day, "kind": kind,
                               "tp": float(c.get("take_profit_pct", 5)), "sl": float(c.get("stop_loss_pct", 3)),
                               "hold": float(c.get("time_stop_min") or 60), "hold_up": float(c.get("time_stop_up_pct") or 0.5), "hi": float(price)})
        store.save(_file(market), d)


def veto_syms(market: str) -> list[str]:
    return [x["sym"] for x in load(market)["veto_open"]]


def veto_tick(market: str, quotes: dict, minutes, day: str) -> int:
    """막은 종목 채점: 실전 규칙대로 샀다면 언제 팔렸을지 (현재가로) · 끝난 수"""
    q = {k: float(v.get("price") if isinstance(v, dict) else v) for k, v in (quotes or {}).items() if v}
    now = time.time()
    done = 0
    with _lock:
        d = load(market)
        keep = []
        n0 = len(d["veto_open"])
        for x in d["veto_open"]:
            px = q.get(x["sym"])
            if not px:
                if x.get("day") != day:   # 날이 바뀌었는데 시세가 없으면 버림
                    continue
                keep.append(x); continue
            x["hi"] = max(x["hi"], px)
            r = (px / x["px"] - 1) * 100
            mins = (now - x["t"]) / 60
            hi_r = (x["hi"] / x["px"] - 1) * 100
            why = ("익절" if r >= x["tp"] else "손절" if r <= -x["sl"] else
                   "시간 손절" if mins >= x["hold"] and hi_r < x["hold_up"] else
                   "마감" if (minutes is not None and minutes >= 385) or x.get("day") != day else "")
            if why:
                fee = 0.2 if market == "US" else 0.21
                d["veto_done"].append({"sym": x["sym"], "day": x["day"], "kind": x["kind"], "at": x["at"], "why": why,
                                       "ret": round(r - fee - 0.2, 3)})   # 수수료 + 양쪽 0.1% 불리한 체결
                done += 1
            else:
                keep.append(x)
        d["veto_open"] = keep
        d["veto_done"] = d["veto_done"][-VETO_KEEP:]
        if done or len(keep) != n0:
            store.save(_file(market), d)
    return done


def _ai_bought(market: str, days: int = 45) -> list[float]:
    since = (datetime.now(KST) - timedelta(days=days)).strftime("%Y-%m-%d")
    with tl._lock:
        ts = [t for t in tl._load()["trades"] if t.get("closed") and tl._tm(t) == market and t.get("day", "") >= since]
    return [t["pnl_pct"] for t in ts if (t.get("features") or {}).get("ai_decide") == "산다" or (t.get("features") or {}).get("ai_chart") == "찬성"]


def veto_review(market: str) -> str:
    vs = [x["ret"] for x in load(market)["veto_done"]][-60:]
    if len(vs) < VETO_MIN:
        return ""
    bought = _ai_bought(market)
    va, ba = _avg(vs), _avg(bought) if bought else 0.0
    h = len(vs) // 2
    old = veto_mode(market)
    new = old
    if va > 0 and va > ba + VETO_MARGIN and _avg(vs[:h]) > 0 and _avg(vs[h:]) > 0:
        new = "shrink"   # AI 가 막은 종목이 더 잘 됨 → 반대를 '금액 줄이기'로
    elif va < 0 or va < ba:
        new = "skip"
    with _lock:
        d = load(market)
        d["veto_mode"] = new
        d["veto_stat"] = {"n": len(vs), "veto_avg": round(va, 2), "bought_n": len(bought), "bought_avg": round(ba, 2), "t": _now()}
        store.save(_file(market), d)
    if new == old:
        return ""
    return (f"🤖 AI 반대 판단 검증: 막은 종목 {len(vs)}건 평균 {va:+.2f}% vs AI 가 산 거래 {len(bought)}건 {ba:+.2f}% → "
            + ("AI 가 반대해도 금액 50%로 사요 (막은 게 손해였음)" if new == "shrink" else "AI 가 반대하면 다시 안 사요 (막은 게 맞았음)"))


# ── 2. 지는 구간 차단 ──
def bucket_keys(f: dict, minutes=None) -> list[str]:
    f = f or {}
    out = []
    ch = f.get("change")
    if ch is not None:
        ch = float(ch)
        out.append("상승률 " + ("~10%" if ch < 10 else "10~20%" if ch < 20 else "20~30%" if ch < 30 else "30%+"))
    rt = f.get("ratio")
    if rt is not None:
        rt = float(rt)
        out.append("거래량 " + ("~3배" if rt < 3 else "3~5배" if rt < 5 else "5~10배" if rt < 10 else "10배+"))
    if f.get("pre"):
        out.append("시간 프리장")
    else:
        mn = f.get("minute", minutes)
        if mn is not None:
            mn = float(mn)
            out.append("시간 " + ("장 초반 30분" if mn < 30 else "30~120분" if mn < 120 else "120분 뒤"))
    return out


def bucket_review(market: str, days: int = 60) -> str:
    since = (datetime.now(KST) - timedelta(days=days)).strftime("%Y-%m-%d")
    with tl._lock:
        ts = [t for t in tl._load()["trades"] if t.get("closed") and tl._tm(t) == market and t.get("day", "") >= since and tl._ts(t) == "surge"]
    ts.sort(key=lambda t: t.get("entry_time", ""))
    g: dict[str, list[float]] = {}
    for t in ts:
        for k in bucket_keys(t.get("features") or {}):
            g.setdefault(k, []).append(float(t["pnl_pct"]))
    blocked = {}
    for k, xs in g.items():
        h = len(xs) // 2
        if len(xs) >= BUCKET_MIN and _avg(xs) < 0 and _avg(xs[:h]) < 0 and _avg(xs[h:]) < 0:
            blocked[k] = {"n": len(xs), "avg": round(_avg(xs), 2)}
    with _lock:
        d = load(market)
        old = set(d["blocked"])
        d["blocked"] = blocked
        d["blocked_t"] = _now()
        store.save(_file(market), d)
    new, gone = set(blocked) - old, old - set(blocked)
    if not new and not gone:
        return ""
    return ("🚧 지는 구간 차단" + (f" · 새로: {', '.join(f'{k}({blocked[k]['n']}건 {blocked[k]['avg']:+.2f}%)' for k in sorted(new))}" if new else "")
            + (f" · 풀림: {', '.join(sorted(gone))}" if gone else "") + " (실전만 — 가상으로는 계속 배움)")


def bucket_block(market: str, f: dict, minutes=None) -> str:
    """실전 매수 전: 지는 구간이면 이유 (아니면 '')"""
    b = load(market).get("blocked") or {}
    for k in bucket_keys(f, minutes):
        if k in b:
            return f"🚧 지는 구간 '{k}' ({b[k]['n']}건 평균 {b[k]['avg']:+.2f}% · 앞뒤 모두 마이너스) → 실전 대신 가상으로"
    return ""


# ── 3. 승률 추정 보정 ──
def calib_review(market: str, days: int = 60) -> str:
    since = (datetime.now(KST) - timedelta(days=days)).strftime("%Y-%m-%d")
    with tl._lock:
        ts = [t for t in tl._load()["trades"] if t.get("closed") and tl._tm(t) == market and t.get("day", "") >= since
              and (t.get("features") or {}).get("p") is not None]
    cal = {}
    for lo, hi in CAL_BINS:
        g = [t for t in ts if lo <= float(t["features"]["p"]) < hi]
        if len(g) < CAL_MIN:
            continue
        pred = _avg([float(t["features"]["p"]) for t in g])
        real = sum(float(t["pnl_pct"]) > 0 for t in g) / len(g)
        cal[f"{lo:.2f}-{hi:.2f}"] = {"n": len(g), "pred": round(pred, 3), "real": round(real, 3),
                                    "k": round(max(0.5, min(1.5, real / pred if pred else 1)), 3)}
    with _lock:
        d = load(market)
        d["calib"] = cal
        d["calib_t"] = _now()
        store.save(_file(market), d)
    off = [f"추정 {v['pred'] * 100:.0f}% → 실제 {v['real'] * 100:.0f}%({v['n']}건)" for v in cal.values() if abs(v["k"] - 1) >= 0.15]
    return ("🎲 승률 추정 보정: " + " · ".join(off)) if off else ""


def calib_p(market: str | None, p: float) -> float:
    """금액 정할 때 쓸 보정된 승률 (보정 기록 없으면 그대로)"""
    if not market:
        return p
    cal = load(market).get("calib") or {}
    for k, v in cal.items():
        lo, hi = (float(x) for x in k.split("-"))
        if lo <= p < hi:
            return max(0.0, min(0.95, p * float(v["k"])))
    return p


def review(market: str) -> list[str]:
    """매일 복기 때: 세 가지 다시 계산 → 바뀐 것만 알림 문구"""
    out = []
    for fn in (veto_review, bucket_review, calib_review):
        try:
            m = fn(market)
            if m:
                out.append(m)
        except Exception as e:
            out.append(f"🎯 판단 점검 오류({fn.__name__}): {type(e).__name__}: {e}")
    return out


def text(market: str) -> str:
    d = load(market)
    vs = d.get("veto_stat") or {}
    parts = []
    if vs:
        parts.append(f"🤖 AI 반대 검증: 막은 종목 {vs['n']}건 평균 {vs['veto_avg']:+.2f}% vs AI 가 산 거래 {vs['bought_n']}건 {vs['bought_avg']:+.2f}% → "
                     + ("반대해도 금액 50%로 삼" if d.get("veto_mode") == "shrink" else "반대하면 안 삼"))
    elif d["veto_done"] or d["veto_open"]:
        parts.append(f"🤖 AI 반대 검증: 채점 {len(d['veto_done'])}건 · 지켜보는 중 {len(d['veto_open'])}건 (20건부터 판정)")
    if d.get("blocked"):
        parts.append("🚧 실전 차단 구간: " + ", ".join(f"{k}({v['n']}건 {v['avg']:+.2f}%)" for k, v in d["blocked"].items()))
    cal = d.get("calib") or {}
    if cal:
        parts.append("🎲 승률 보정: " + ", ".join(f"추정 {v['pred'] * 100:.0f}%→실제 {v['real'] * 100:.0f}%" for v in cal.values()))
    return " · ".join(parts)
