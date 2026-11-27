"""🏟️ AI 생존전 — 성격이 다른 AI 여러 개가 **가상으로만** 같은 후보를 두고 매매 → 성적으로 탈락·교체.

- 가상 전용: 이 파일은 주문 함수를 절대 부르지 않음 (시세는 실전 엔진이 넘겨준 것 + 읽기 전용 조회만)
- 후보: 실전 엔진이 매 바퀴 고른 후보(규칙 관문 통과한 것)를 그대로 받음
- 📒 모든 AI 가 같은 공유 노트(1호·2호·생존전·주말 공부 교훈 + 팀 기억 교훈)를 보고 판단
- AI 호출: 후보 한 종목당 1번에 참가자 전원이 같이 판단 (가상은 표본 제한 없음: arena_ai_per_day 0 = 무제한 · 같은 종목도 60분 뒤 다시 판단)
- 후보 넓히기: 실전 후보(관문에 걸린 것 포함) + 30%↑ 급등 검증 후보 · 참가자당 동시 보유 10종목
- 📘 '안 산다' 판단도 채점: 샀다면 자기 규칙으로 어땠을지 → 정확도 · 가중치 0.3 (산 거래 1.0)
- 질 관리: 실전 자격은 앞 70%·뒤 30% 모두 플러스 + 제일 크게 번 종목 빼도 플러스 + 가중 점수 플러스
- 매도: 참가자마다 자기 익절·손절·최대 보유 시간 + 마감 전 정리 (단타)
- 체결: 0.1% 불리하게 + 수수료·세금 (실전 가상 매매와 같게)
- 매주 토요일 아침(주말 공부) 성적 발표(텔레그램) · 탈락 없음 — 전원 가상 매매로 계속 같이 공부
- 💰 실전 자격: 거래 arena_min_trades 건↑ · 최근 평균 플러스 · 앞·뒤 절반 모두 플러스인 참가자 중 상위 arena_live_n(2)명
  → 이 AI 가 '산다'고 한 종목은 실전 엔진이 실전으로 매수 (주문은 실전 엔진이 기존 안전장치 그대로: 현금만·봇 장부·손실 한도·추격 확인)
  → 매일 복기 때 평균이 마이너스로 떨어지면 바로 자격 내림 (가상으로 돌아가 다시 공부)
- 🏆 1등 전략은 2호 급등주 AI 판단에도 참고로 붙임
기록: _cache/arena_{US|KR}.json
"""

from __future__ import annotations

import json
import random
import re
import statistics as st
import threading
import time
from datetime import datetime, timedelta, timezone

from . import store
from .agents import COMPANY_DIR

KST = timezone(timedelta(hours=9))
SLIP = 0.1                      # % 불리한 체결 (한쪽)
FEE = {"US": 0.2, "KR": 0.21}   # % 왕복 수수료+세금 (미장 0.1%×2 · 국장 0.015%×2 + 매도세 0.18%)
MAX_POS = 10                    # 참가자당 동시 보유 (가상이라 넉넉히 → 표본 빨리)
W_BUY, W_SKIP = 1.0, 0.3        # 표본 가중치: 산 거래 1.0 · '안 산다' 판단 채점 0.3 (쉽게 쌓이는 표본이 판정을 좌우 못 하게)
REJUDGE_MIN = 60                # 같은 종목도 60분 지나면 다시 판단 (안 들고 있을 때)
_lock = threading.Lock()
_pending: dict[str, float] = {}   # "시장:종목" → AI 판단 시작 시각

SEEDS = [
    {"name": "프리장 선발", "persona": "프리장·장 초반에 이미 강하게 오른 종목만. 정규장 늦게 오르는 건 안 삼.", "tp": 4.0, "sl": 2.5, "hold": 60},
    {"name": "거래량 폭발", "persona": "거래량이 평소의 5배 이상 터지면서 오르는 초입만. 거래량이 줄며 오르면 안 삼.", "tp": 5.0, "sl": 3.0, "hold": 90},
    {"name": "눌림목", "persona": "급등 뒤 VWAP·이동평균까지 눌렸다가 다시 반등하는 자리만. 꼭대기 추격은 절대 안 함.", "tp": 3.5, "sl": 2.0, "hold": 120},
    {"name": "뉴스 확인", "persona": "공시·뉴스로 급등 이유가 설명될 때만. 이유 모르는 급등은 안 삼.", "tp": 6.0, "sl": 3.0, "hold": 180},
    {"name": "보수형", "persona": "승률 추정이 높고 기대 수익 플러스인 것만, 확신 없으면 안 삼. 작게 먹고 빨리 나옴.", "tp": 2.0, "sl": 1.5, "hold": 30},
    {"name": "추세 추종", "persona": "고점을 계속 높이며 오르는 강한 추세만. 길게 들고 가서 크게 먹음.", "tp": 8.0, "sl": 4.0, "hold": 240},
    {"name": "역발상", "persona": "남들이 다 사는 과열(+50%↑)은 피하고, 덜 오른 +10~25% 구간의 탄탄한 종목만.", "tp": 4.0, "sl": 2.5, "hold": 120},
    {"name": "속공", "persona": "오르는 속도가 가장 빠른 순간에만 짧게 타고 바로 나옴.", "tp": 1.5, "sl": 1.0, "hold": 15},
    {"name": "돌파", "persona": "장 초반 고점·전일 고점을 거래량과 함께 뚫는 순간만. 뚫지 못하면 안 삼.", "tp": 5.0, "sl": 2.5, "hold": 120},
    {"name": "통계파", "persona": "학습 기록의 비슷한 거래 표본이 많고 승률 50%↑일 때만. 차트 모양보다 숫자를 믿음.", "tp": 3.0, "sl": 2.0, "hold": 90},
]


def _file(m: str):
    return COMPANY_DIR / "_cache" / f"arena_{m}.json"


def _now() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


def _new(seed: dict, gen: int = 1, parent: str = "") -> dict:
    return {"id": f"a{int(time.time() * 1000) % 10**9}{random.randint(10, 99)}", "name": seed["name"], "persona": seed["persona"],
            "tp": float(seed["tp"]), "sl": float(seed["sl"]), "hold": int(seed["hold"]), "gen": gen, "parent": parent,
            "born": _now(), "pos": {}, "trades": [], "ghost": {}, "skips": []}


def load(m: str, size: int | None = None) -> dict:
    d = store.load(_file(m), lambda: {})
    d.setdefault("alive", []); d.setdefault("dead", []); d.setdefault("seen", {}); d.setdefault("calls", {}); d.setdefault("champion", None)
    used = {a["name"] for a in d["alive"]} | {x["name"] for x in d["dead"]}   # 탈락한 전략은 다시 안 들어옴
    for a in d["alive"]:
        a.setdefault("ghost", {}); a.setdefault("skips", [])
    for s in SEEDS if size else []:
        if len(d["alive"]) >= size:
            break
        if s["name"] not in used:
            d["alive"].append(_new(s))
    return d


def _save(m: str, d: dict) -> None:
    d["dead"] = d["dead"][-30:]
    for a in d["alive"]:
        a["trades"] = a["trades"][-500:]
        a["skips"] = a["skips"][-1000:]
    store.save(_file(m), d)


def _ret(buy: float, sell: float, m: str) -> float:
    return ((sell * (1 - SLIP / 100)) / (buy * (1 + SLIP / 100)) - 1) * 100 - FEE.get(m, 0.2)


def _avg(ts: list) -> float:
    return st.mean(t["ret"] for t in ts) if ts else 0.0


# ── 매 바퀴 ──
def _seen_ok(seen: dict, sym: str, now: float) -> bool:
    t = seen.get(sym)
    return t is None or now - float(t) >= REJUDGE_MIN * 60


def _exit_why(a: dict, p: dict, px: float, now: float, minutes, day: str) -> str:
    r = (px / p["px"] - 1) * 100
    mins = (now - p["t"]) / 60
    return ("익절" if r >= a["tp"] else "손절" if r <= -a["sl"] else "시간" if mins >= a["hold"]
            else "마감" if (minutes is not None and minutes >= 380) or p.get("day") != day else "")


def step(c: dict, market: str, cands: dict, quotes: dict, minutes, day: str, price_fn=None, ai_fn=None, extra: dict | None = None) -> list[str]:
    """보유·'안 산' 판단 채점 정리 + 새 후보 AI 판단 시작 (가상 전용 · 표본 제한 없음). 결과 줄 목록"""
    if not c.get("arena_on"):
        return []
    out: list[str] = []
    quotes = {k: (v.get("price") if isinstance(v, dict) else v) for k, v in (quotes or {}).items()}
    quotes = {k: float(v) for k, v in quotes.items() if v}
    # 후보 넓히기: 실전 후보(관문에 걸린 것 포함) + 30%↑ 급등 검증 후보 — 가상이라 위험 없음
    cands = {k: v for k, v in {**(extra or {}), **(cands or {})}.items() if (v.get("style") or "surge") == "surge"}
    now = time.time()
    with _lock:
        d = load(market, int(c.get("arena_size", 10)))
        seen = d["seen"].get(day)
        seen = seen if isinstance(seen, dict) else {x: 0 for x in (seen or [])}
        held = {s for a in d["alive"] for s in list(a["pos"]) + list(a["ghost"])}
        new = [s for s in cands if _seen_ok(seen, s, now)]
        need = [s for s in set(held) | set(new) if not quotes.get(s)]
    if need and price_fn:
        try:
            got = price_fn(need[:200]) or {}
            quotes = {**quotes, **{k: float(v["price"] if isinstance(v, dict) else v) for k, v in got.items() if v}}
        except Exception:
            pass
    with _lock:
        d = load(market, int(c.get("arena_size", 10)))
        for a in d["alive"]:
            for sym, p in list(a["pos"].items()):
                px = float(quotes.get(sym) or 0)
                why = _exit_why(a, p, px, now, minutes, day) if px else ""
                if why:
                    ret = _ret(p["px"], px, market)
                    a["trades"].append({"sym": sym, "day": p.get("day", day), "ret": round(ret, 3), "why": why,
                                        "mins": round((now - p["t"]) / 60), "t": _now()})
                    del a["pos"][sym]
                    out.append(f"🏟️ {a['name']} {sym} {why} {ret:+.2f}%")
            for sym, p in list(a["ghost"].items()):   # 📘 '안 산다' 판단 채점: 샀다면 자기 규칙으로 어땠을지
                px = float(quotes.get(sym) or 0)
                why = _exit_why(a, p, px, now, minutes, day) if px else ""
                if why:
                    a["skips"].append({"sym": sym, "day": p.get("day", day), "ret": round(_ret(p["px"], px, market), 3), "t": _now()})
                    del a["ghost"][sym]
        seen = d["seen"].get(day)
        seen = seen if isinstance(seen, dict) else {x: 0 for x in (seen or [])}
        d["seen"] = {day: seen}
        calls = int(d["calls"].get(day, 0))
        limit = int(c.get("arena_ai_per_day", 0) or 0)   # 0 = 제한 없음
        start = []
        for sym in cands:
            if not quotes.get(sym) or not _seen_ok(seen, sym, now) or f"{market}:{sym}" in _pending:
                continue
            if minutes is not None and minutes > 360:
                break
            if limit and calls >= limit:
                break
            calls += 1; seen[sym] = now; start.append(sym)
        d["calls"] = {day: calls}
        _save(market, d)
    for sym in start:
        _pending[f"{market}:{sym}"] = now
        threading.Thread(target=_decide, args=(c, market, sym, cands[sym], float(quotes[sym]), day, ai_fn or ai_json),
                         daemon=True, name="arena-ai").start()
    return out


def _notes(market: str) -> str:
    """📒 모든 AI 가 같이 보는 노트: 공유 교훈(1호·2호·생존전·주말 공부) + 자동매매 팀 기억의 교훈"""
    out = []
    try:
        from . import tradelearn as tl
        out.append(tl.lessons_text(market, 10))
    except Exception:
        pass
    try:
        mem = (COMPANY_DIR / "_agents" / "trader" / "memory.md").read_text(encoding="utf-8")
        tag = "교훈(미장)" if market == "US" else "교훈(국장)"
        ls = [x.split(tag + ":", 1)[1].strip() for x in mem.splitlines() if tag + ":" in x]
        from .extras import FAKE_NOTES
        ls = [x for x in ls if x not in FAKE_NOTES]
        out.append(" / ".join(dict.fromkeys(ls[-8:])))
    except Exception:
        pass
    return " / ".join(x for x in out if x)[:1500]


def _decide(c: dict, market: str, sym: str, cd: dict, price: float, day: str, ai_fn) -> None:
    try:
        with _lock:
            alive = [{"id": a["id"], "name": a["name"], "persona": a["persona"]} for a in load(market)["alive"]]
        e = cd.get("est") or {}
        prompt = ("AI 생존전: 아래 참가자들이 각자 성격대로 이 종목을 지금 살지 따로 판단해요 (가상 매매).\n"
                  f"종목 {sym} 현재가 {price} · 고른 이유: {cd.get('reason', '')}\n"
                  + (f"학습 추정: 승률 {float(e.get('p', 0)) * 100:.0f}% · 기대 수익 {float(e.get('ev', 0)):+.2f}%\n" if e else "")
                  + (f"공유 노트(모든 AI 가 같이 배운 것 — 각자 성격에 맞게 참고): {_notes(market)}\n" if _notes(market) else "")
                  + "참가자:\n" + "\n".join(f"- {a['id']} ({a['name']}): {a['persona']}" for a in alive)
                  + '\n형식: {"참가자id": true/false, ...} (성격에 맞을 때만 true)')
        r = ai_fn(prompt) if ai_fn else None
        if not isinstance(r, dict):
            return
        with _lock:
            d = load(market)
            yes = [a["id"] for a in d["alive"] if r.get(a["id"]) is True]
            for a in d["alive"]:
                if a["id"] in yes and sym not in a["pos"] and len(a["pos"]) < MAX_POS:
                    a["pos"][sym] = {"px": price, "t": time.time(), "day": day}
                elif a["id"] not in yes and sym not in a["ghost"] and sym not in a["pos"]:
                    a["ghost"][sym] = {"px": price, "t": time.time(), "day": day}   # 📘 안 산 판단도 결과로 채점
            v = d.setdefault("votes", {})
            for k in [k for k in v if k != day]:
                v.pop(k, None)
            v.setdefault(day, {})[sym] = {"yes": yes, "t": time.time()}
            _save(market, d)
    except Exception:
        pass
    finally:
        _pending.pop(f"{market}:{sym}", None)
        if _wake:
            try:
                _wake(market)
            except Exception:
                pass


_wake = None   # 판단 끝나면 실전 엔진을 바로 한 바퀴 (autotrade 가 넣어 줌)


def kick(c: dict, market: str, sym: str, cd: dict, price: float, day: str, ai_fn=None) -> bool:
    """실전 엔진이 후보를 처음 봤을 때 바로 생존전 판단 시작 (하루 한 종목 1번 · 하루 횟수 안에서)"""
    with _lock:
        d = load(market)
        seen = d["seen"].get(day)
        seen = seen if isinstance(seen, dict) else {x: 0 for x in (seen or [])}
        d["seen"] = {day: seen}
        calls = int(d["calls"].get(day, 0))
        limit = int(c.get("arena_ai_per_day", 0) or 0)
        if sym in seen or f"{market}:{sym}" in _pending or (limit and calls >= limit):
            return f"{market}:{sym}" in _pending
        seen[sym] = time.time(); d["calls"] = {day: calls + 1}
        _save(market, d)
    _pending[f"{market}:{sym}"] = time.time()
    threading.Thread(target=_decide, args=(c, market, sym, cd, price, day, ai_fn or ai_json), daemon=True, name="arena-ai").start()
    return True


def live_vote(market: str, day: str, sym: str) -> tuple[str, dict | None]:
    """실전 자격 AI 의 판단: ('pending'|'unseen'|'no'|'yes', 찬성한 실전 AI)"""
    if f"{market}:{sym}" in _pending:
        return "pending", None
    d = load(market)
    live = d.get("live") or []
    if not live:
        return "no", None
    v = (d.get("votes") or {}).get(day, {})
    if sym not in v:
        return ("no" if sym in (d["seen"].get(day) or {}) else "unseen"), None
    x = v[sym] if isinstance(v[sym], dict) else {"yes": v[sym], "t": time.time()}
    if time.time() - float(x.get("t", 0)) > 600:   # 10분 지난 판단은 실전에 안 씀 (가격이 달라졌을 수 있음)
        return "no", None
    by = {a["id"]: a for a in d["alive"]}
    for i, aid in enumerate(live):
        if aid in x["yes"] and aid in by:
            return "yes", {"id": aid, "name": by[aid]["name"], "rank": i + 1}
    return "no", None


def _qualified(a: dict, min_n: int) -> bool:
    """💰 실전 자격 (질 관리): 산 거래 min_n 건↑ ·
    ① 앞 70% · 뒤 30%(처음 보는 기간) 모두 평균 플러스 ② 제일 크게 번 종목 하나를 빼도 플러스 (대박 하나로 1등 방지)
    ③ '안 산 판단' 채점까지 합친 가중 점수도 플러스"""
    ts = a["trades"][-max(min_n, 50):]
    if len(a["trades"]) < min_n or not ts:
        return False
    k = max(1, int(len(ts) * 0.7))
    if not (_avg(ts[:k]) > 0 and _avg(ts[k:]) > 0):
        return False
    by: dict = {}
    for t in ts:
        by[t["sym"]] = by.get(t["sym"], 0) + t["ret"]
    top = max(by, key=by.get)
    rest = [t for t in ts if t["sym"] != top]
    if not rest or _avg(rest) <= 0:
        return False
    return score(a)["q"] > 0


def recheck(c: dict, market: str) -> str:
    """매일 복기: 실전 자격 AI 중 성적이 떨어진 AI 는 바로 자격 내림"""
    min_n = int(c.get("arena_min_trades", 20))
    with _lock:
        d = load(market)
        by = {a["id"]: a for a in d["alive"]}
        drop = [i for i in d.get("live") or [] if i not in by or not _qualified(by[i], min_n)]
        if not drop:
            return ""
        d["live"] = [i for i in d["live"] if i not in drop]
        _save(market, d)
    return "🏟️ 실전 자격 내림: " + ", ".join(by[i]["name"] if i in by else i for i in drop) + " (성적이 떨어져서 가상으로 다시 공부)"


# ── 성적 ──
def score(a: dict, n: int = 50) -> dict:
    """성적: 산 거래(최근 n건) 평균·승률 + '안 산 판단' 정확도 + 가중 점수 q (산 거래 1.0 · 안 산 판단 0.3)"""
    ts, sk = a["trades"][-n:], (a.get("skips") or [])[-n * 2:]
    wsum = W_BUY * len(ts) + W_SKIP * len(sk)
    q = (W_BUY * sum(t["ret"] for t in ts) - W_SKIP * sum(t["ret"] for t in sk)) / wsum if wsum else 0.0
    return {"n": len(a["trades"]), "avg": round(_avg(ts), 2), "win": round(sum(t["ret"] > 0 for t in ts) / len(ts) * 100) if ts else 0,
            "skips": len(a.get("skips") or []), "skip_ok": round(sum(t["ret"] <= 0 for t in sk) / len(sk) * 100) if sk else 0, "q": round(q, 3)}


def board(market: str) -> list[dict]:
    d = load(market)
    live = d.get("live") or []
    rows = [{"name": a["name"], "gen": a["gen"], "live": (live.index(a["id"]) + 1) if a["id"] in live else 0, "persona": a["persona"], "tp": a["tp"], "sl": a["sl"], "hold": a["hold"],
             "held": len(a["pos"]), **score(a)} for a in d["alive"]]
    return sorted(rows, key=lambda r: (-(r["n"] >= 5), -r["q"]))


def text(market: str) -> str:
    rows = board(market)
    if not rows:
        return ""
    ch = load(market).get("champion") or {}
    return ("🏟️ AI 생존전: " + " · ".join(("💰" if r["live"] else "") + r["name"] + f" {r['n']}건 {r['avg']:+.2f}%" for r in rows)
            + (f" · 🏆 {ch['name']}" if ch.get("name") else "") + (" · 💰=실전 자격" if any(r["live"] for r in rows) else ""))


def champion_text(market: str) -> str:
    """2호 급등주 AI 판단에 붙일 한 줄 (챔피언 없으면 '')"""
    ch = load(market).get("champion") or {}
    if not ch.get("persona"):
        return ""
    return f"🏆 AI 생존전 1등 전략(가상 {ch['n']}건 평균 {ch['avg']:+.2f}%) — 참고: {ch['persona']}"


# ── 매주: 탈락·교체 ──
def evolve(c: dict, market: str, ai_fn=None, add_lesson=None, lessons: str = "") -> str:
    """주말 공부(토요일 아침): 전원 성적 발표 · 탈락 없음 · 상위 arena_live_n 명에게 실전 자격"""
    if not c.get("arena_on"):
        return ""
    min_n = int(c.get("arena_min_trades", 20))
    n_live = int(c.get("arena_live_n", 2))
    msg = []
    with _lock:
        d = load(market, int(c.get("arena_size", 10)))
        alive = sorted(d["alive"], key=lambda a: -score(a)["q"])
        q = [a for a in alive if _qualified(a, min_n)][:n_live]
        old = set(d.get("live") or [])
        d["live"] = [a["id"] for a in q]
        best = q[0] if q else None
        if best:
            sb = score(best)
            d["champion"] = {"name": best["name"], "persona": best["persona"], "tp": best["tp"], "sl": best["sl"],
                             "hold": best["hold"], "t": _now(), **sb}
        else:
            d["champion"] = None
        _save(market, d)
    new = [a["name"] for a in q if a["id"] not in old]
    gone = [a["name"] for a in alive if a["id"] in old and a["id"] not in d["live"]]
    if q:
        msg.append("💰 실전 자격: " + ", ".join(f"{i + 1}등 {a['name']}" for i, a in enumerate(q)) + (f" (새로: {', '.join(new)})" if new else ""))
    else:
        msg.append(f"💰 실전 자격 아직 없음 (거래 {min_n}건↑ · 평균 플러스 · 앞뒤 절반 모두 플러스인 AI 가 나오면)")
    if gone:
        msg.append("⬇️ 가상으로: " + ", ".join(gone))
    if add_lesson and best:
        add_lesson(market, f"[생존전 1등] '{best['persona'][:80]}' 전략이 {score(best)['n']}건 평균 {score(best)['avg']:+.2f}%", "AI 생존전")
    rows = board(market)   # 📣 성적 발표: 전원 순위
    live = set(d["live"])
    ids = {a["name"]: a["id"] for a in d["alive"]}
    rank = [f"{i + 1}. {'💰' if ids.get(r['name']) in live else '📘'} {r['name']} — {r['n']}건 평균 {r['avg']:+.2f}% 승률 {r['win']}% · 안 산 판단 {r['skips']}건 정확도 {r['skip_ok']}% · 점수 {r['q']:+.2f}"
            + ("" if r["n"] >= min_n else f" (판정까지 {min_n - r['n']}건)") for i, r in enumerate(rows)]
    return "🏟️ AI 생존전 주간 성적 발표 (💰 실전 · 📘 가상 공부)\n" + "\n".join(rank) + "\n" + "\n".join(msg)


def overview(market: str) -> dict:
    d = load(market)
    return {"board": board(market), "champion": d.get("champion"), "text": text(market)}


def ai_json(prompt: str) -> dict | None:
    """기본 AI 판단기 (빠른 모델, JSON 한 줄)"""
    from . import providers as pv
    from . import team as tm
    try:
        reply = pv.chat(tm.fast_spec("claudecode:sonnet"), "너는 여러 트레이더 역할을 동시에 하는 심판이다. JSON 한 줄로만 답한다.",
                        [{"role": "user", "content": prompt}], effort="low", timeout=120)
    except Exception:
        return None
    m = re.search(r"\{.*\}", reply, re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except json.JSONDecodeError:
        return None
