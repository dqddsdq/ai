"""📏 성적 집계 기준 시각 (settings.json 의 stats_since, 한국 시간).

  기준 이후 거래만 승률·평균·건수에 넣고, 그 이전 기록은 지우지 않고 '학습용 표본'으로 따로 보여 줍니다.
  기준 이후 표본이 MIN_N 건 미만이면 승률 대신 '표본 부족(n건)'.
  화면·복기·요약 표시용 — 주문·실전 판단·리그 실전 자격에는 쓰지 않습니다.
"""

from __future__ import annotations

import statistics as st
import time

DEFAULT_SINCE = "2026-11-26 00:00"
MIN_N = 20
_cache = [0.0, ""]   # (읽은 시각, 기준) — 설정 파일을 너무 자주 안 읽게


def _norm(ts) -> str:
    """시각 문자열 → 'YYYY-MM-DD HH:MM' (날짜만이면 00:00)"""
    s = str(ts or "").strip().replace("T", " ")[:16]
    if len(s) == 10:
        s += " 00:00"
    return s if len(s) == 16 and s[4] == "-" and s[7] == "-" else ""


def since() -> str:
    if time.time() - _cache[0] < 10 and _cache[1]:
        return _cache[1]
    v = ""
    try:
        from . import team
        v = _norm(team.settings().get("stats_since"))
    except Exception:
        v = ""
    _cache[0], _cache[1] = time.time(), v or DEFAULT_SINCE
    return _cache[1]


def when(t: dict) -> str:
    """거래 시각: 산 시각 → 기록 시각 → 날짜 순으로"""
    for k in ("entry_time", "t", "day"):
        v = _norm(t.get(k)) if isinstance(t, dict) else ""
        if v:
            return v
    return ""


def split(ts: list, cut: str | None = None) -> tuple[list, list]:
    """(기준 이후, 기준 이전=학습용) — 시각 모르는 기록은 학습용으로"""
    cut = cut or since()
    new, old = [], []
    for t in ts:
        (new if (when(t) or "") >= cut else old).append(t)
    return new, old


def calc(rets: list[float]) -> dict:
    n = len(rets)
    return {"n": n, "win": round(sum(1 for r in rets if r > 0) / n * 100) if n else 0,
            "avg": round(st.fmean(rets), 2) if n else 0.0, "ok": n >= MIN_N}


def win_text(n: int, win) -> str:
    return f"승률 {win}%" if n >= MIN_N else f"표본 부족({n}건)"


def line(s: dict) -> str:
    """'n건 · 승률 x% · 평균 +y%' (표본 부족이면 승률 자리에 표시)"""
    if not s.get("n"):
        return "0건"
    return (f"{s['n']}건 · " if s["n"] >= MIN_N else "") + f"{win_text(s['n'], s['win'])} · 평균 {s['avg']:+.2f}%"


def view(ts: list, ret=None, cut: str | None = None) -> dict:
    """기준 이후 / 학습용 표본 집계 + 한 줄 글"""
    cut = cut or since()
    ret = ret or (lambda t: float(t.get("pnl_pct") or 0))
    new, old = split(ts, cut)
    a, b = calc([ret(t) for t in new]), calc([ret(t) for t in old])
    txt = f"기준({cut[5:10].replace('-', '/')}~) {line(a)}"
    if b["n"]:
        txt += f" · 📚 학습용 표본 {b['n']}건(승률 {b['win']}% · 평균 {b['avg']:+.2f}%, 승률엔 안 섞음)"
    return {"cut": cut, **a, "old": b, "text": txt}
