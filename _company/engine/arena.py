"""🏟️ AI 생존전 — 성격이 다른 AI 여러 개가 **가상으로만** 같은 후보를 두고 매매 → 성적으로 탈락·교체.

- 가상 전용: 이 파일은 주문 함수를 절대 부르지 않음 (시세는 실전 엔진이 넘겨준 것 + 읽기 전용 조회만)
- 후보: 실전 엔진이 매 바퀴 고른 후보(규칙 관문 통과한 것)를 그대로 받음
- AI 호출: 후보 한 종목당 1번에 참가자 전원이 같이 판단 (하루 arena_ai_per_day 번까지 · 실전 AI 판단 한도와 따로)
- 매도: 참가자마다 자기 익절·손절·최대 보유 시간 + 마감 전 정리 (단타)
- 체결: 0.1% 불리하게 + 수수료·세금 (실전 가상 매매와 같게)
- 매주 토요일(주말 공부): 거래 arena_min_trades 건↑ 참가자끼리 비교 → 꼴찌(평균 마이너스·1등보다 0.3%p↑ 나쁨) 탈락,
  1등을 살짝 바꾼 새 AI 로 채움 · 탈락 이유는 공유 노트에 교훈으로
- 🏆 1등이 거래 충분 + 앞·뒤 절반 모두 플러스면 '챔피언' → 2호 급등주 AI 판단에 그 전략을 참고로 붙임
  (실전 매수는 여전히 기존 관문·실전 증명을 다 통과해야 함 — 돈 흐름은 그대로)
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
MAX_POS = 3                     # 참가자당 동시 보유
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
]


def _file(m: str):
    return COMPANY_DIR / "_cache" / f"arena_{m}.json"


def _now() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


def _new(seed: dict, gen: int = 1, parent: str = "") -> dict:
    return {"id": f"a{int(time.time() * 1000) % 10**9}{random.randint(10, 99)}", "name": seed["name"], "persona": seed["persona"],
            "tp": float(seed["tp"]), "sl": float(seed["sl"]), "hold": int(seed["hold"]), "gen": gen, "parent": parent,
            "born": _now(), "pos": {}, "trades": []}


def load(m: str, size: int | None = None) -> dict:
    d = store.load(_file(m), lambda: {})
    d.setdefault("alive", []); d.setdefault("dead", []); d.setdefault("seen", {}); d.setdefault("calls", {}); d.setdefault("champion", None)
    used = {a["name"] for a in d["alive"]}
    for s in SEEDS if size else []:
        if len(d["alive"]) >= size:
            break
        if s["name"] not in used:
            d["alive"].append(_new(s))
    return d


def _save(m: str, d: dict) -> None:
    d["dead"] = d["dead"][-30:]
    for a in d["alive"]:
        a["trades"] = a["trades"][-200:]
    store.save(_file(m), d)


def _ret(buy: float, sell: float, m: str) -> float:
    return ((sell * (1 - SLIP / 100)) / (buy * (1 + SLIP / 100)) - 1) * 100 - FEE.get(m, 0.2)


def _avg(ts: list) -> float:
    return st.mean(t["ret"] for t in ts) if ts else 0.0


# ── 매 바퀴 ──
def step(c: dict, market: str, cands: dict, quotes: dict, minutes, day: str, price_fn=None, ai_fn=None) -> list[str]:
    """보유 정리 + 새 후보 AI 판단 시작. 결과 줄 목록"""
    if not c.get("arena_on"):
        return []
    out: list[str] = []
    quotes = {k: (v.get("price") if isinstance(v, dict) else v) for k, v in (quotes or {}).items()}
    quotes = {k: float(v) for k, v in quotes.items() if v}
    cands = {k: v for k, v in (cands or {}).items() if (v.get("style") or "surge") == "surge"}   # 단타 후보만
    with _lock:
        d = load(market, int(c.get("arena_size", 5)))
        held = {s for a in d["alive"] for s in a["pos"]}
        need = [s for s in held if not quotes.get(s)]
    if need and price_fn:
        try:
            got = price_fn(need) or {}
            quotes = {**quotes, **{k: float(v["price"] if isinstance(v, dict) else v) for k, v in got.items() if v}}
        except Exception:
            pass
    now = time.time()
    with _lock:
        d = load(market, int(c.get("arena_size", 5)))
        for a in d["alive"]:
            for sym, p in list(a["pos"].items()):
                px = float(quotes.get(sym) or 0)
                if not px:
                    continue
                r = (px / p["px"] - 1) * 100
                mins = (now - p["t"]) / 60
                why = ("익절" if r >= a["tp"] else "손절" if r <= -a["sl"] else "시간" if mins >= a["hold"]
                       else "마감" if (minutes is not None and minutes >= 380) or p.get("day") != day else "")
                if why:
                    ret = _ret(p["px"], px, market)
                    a["trades"].append({"sym": sym, "day": p.get("day", day), "ret": round(ret, 3), "why": why,
                                        "mins": round(mins), "t": _now()})
                    del a["pos"][sym]
                    out.append(f"🏟️ {a['name']} {sym} {why} {ret:+.2f}%")
        # 새 후보 → 참가자 전원 판단 (한 종목 하루 1번)
        calls = d["calls"].get(day, 0)
        seen = d["seen"].setdefault(day, [])
        for k in [k for k in d["seen"] if k != day]:
            d["seen"].pop(k, None)
        d["calls"] = {day: calls}
        todo = [s for s, cd in cands.items() if s not in seen and quotes.get(s)
                and (minutes is None or minutes <= 360) and f"{market}:{s}" not in _pending]
        limit = int(c.get("arena_ai_per_day", 40))
        start = []
        for s in todo:
            if calls >= limit:
                break
            calls += 1; seen.append(s); start.append(s)
        d["calls"][day] = calls
        _save(market, d)
    for s in start:
        _pending[f"{market}:{s}"] = now
        threading.Thread(target=_decide, args=(c, market, s, cands[s], float(quotes[s]), day, ai_fn),
                         daemon=True, name="arena-ai").start()
    return out


def _decide(c: dict, market: str, sym: str, cd: dict, price: float, day: str, ai_fn) -> None:
    try:
        with _lock:
            alive = [{"id": a["id"], "name": a["name"], "persona": a["persona"]} for a in load(market)["alive"]]
        e = cd.get("est") or {}
        prompt = ("AI 생존전: 아래 참가자들이 각자 성격대로 이 종목을 지금 살지 따로 판단해요 (가상 매매).\n"
                  f"종목 {sym} 현재가 {price} · 고른 이유: {cd.get('reason', '')}\n"
                  + (f"학습 추정: 승률 {float(e.get('p', 0)) * 100:.0f}% · 기대 수익 {float(e.get('ev', 0)):+.2f}%\n" if e else "")
                  + "참가자:\n" + "\n".join(f"- {a['id']} ({a['name']}): {a['persona']}" for a in alive)
                  + '\n형식: {"참가자id": true/false, ...} (성격에 맞을 때만 true)')
        r = ai_fn(prompt) if ai_fn else None
        if not isinstance(r, dict):
            return
        with _lock:
            d = load(market)
            for a in d["alive"]:
                if r.get(a["id"]) is True and sym not in a["pos"] and len(a["pos"]) < MAX_POS:
                    a["pos"][sym] = {"px": price, "t": time.time(), "day": day}
            _save(market, d)
    except Exception:
        pass
    finally:
        _pending.pop(f"{market}:{sym}", None)


# ── 성적 ──
def score(a: dict, n: int = 30) -> dict:
    ts = a["trades"][-n:]
    if not ts:
        return {"n": len(a["trades"]), "avg": 0.0, "win": 0}
    return {"n": len(a["trades"]), "avg": round(_avg(ts), 2), "win": round(sum(t["ret"] > 0 for t in ts) / len(ts) * 100)}


def board(market: str) -> list[dict]:
    d = load(market)
    rows = [{"name": a["name"], "gen": a["gen"], "persona": a["persona"], "tp": a["tp"], "sl": a["sl"], "hold": a["hold"],
             "held": len(a["pos"]), **score(a)} for a in d["alive"]]
    return sorted(rows, key=lambda r: (-(r["n"] >= 5), -r["avg"]))


def text(market: str) -> str:
    rows = board(market)
    if not rows:
        return ""
    d = load(market)
    ch = d.get("champion") or {}
    return ("🏟️ AI 생존전(가상): " + " · ".join(r["name"] + ("" if r["gen"] == 1 else f"({r['gen']}세대)") + f" {r['n']}건 {r['avg']:+.2f}%" for r in rows)
            + (f" · 🏆 {ch['name']}" if ch.get("name") else "") + (f" · 탈락 {len(d['dead'])}" if d["dead"] else ""))


def champion_text(market: str) -> str:
    """2호 급등주 AI 판단에 붙일 한 줄 (챔피언 없으면 '')"""
    ch = load(market).get("champion") or {}
    if not ch.get("persona"):
        return ""
    return f"🏆 AI 생존전 1등 전략(가상 {ch['n']}건 평균 {ch['avg']:+.2f}%) — 참고: {ch['persona']}"


# ── 매주: 탈락·교체 ──
def _mutate(best: dict, rnd: random.Random, ai_fn=None, lessons: str = "") -> dict:
    seed = {"name": best["name"].split("·")[0] + "·변형", "persona": best["persona"],
            "tp": round(max(1.0, min(15.0, best["tp"] * rnd.uniform(0.8, 1.25))), 1),
            "sl": round(max(0.8, min(8.0, best["sl"] * rnd.uniform(0.8, 1.25))), 1),
            "hold": int(max(10, min(390, best["hold"] * rnd.uniform(0.7, 1.4))))}
    if ai_fn:
        try:
            r = ai_fn(f"AI 생존전 1등 전략: '{best['persona']}' (평균 {score(best)['avg']:+.2f}%).\n"
                      f"공유 노트: {lessons[:800]}\n이 전략을 한 가지만 바꿔 더 나아질 것 같은 새 전략을 한 줄로.\n"
                      '형식: {"name": "짧은 이름(6자 이내)", "persona": "한 줄"}')
            if isinstance(r, dict) and str(r.get("persona", "")).strip():
                seed["persona"] = str(r["persona"]).strip()[:200]
                seed["name"] = (str(r.get("name") or "").strip()[:8] or seed["name"])
        except Exception:
            pass
    return seed


def evolve(c: dict, market: str, ai_fn=None, add_lesson=None, lessons: str = "") -> str:
    """주말 공부: 꼴찌 탈락 → 1등 변형으로 채움 · 챔피언 정하기. 한 줄 요약"""
    if not c.get("arena_on"):
        return ""
    min_n = int(c.get("arena_min_trades", 20))
    with _lock:
        d = load(market, int(c.get("arena_size", 5)))
        ok = [a for a in d["alive"] if len(a["trades"]) >= min_n]
    msg = []
    if len(ok) >= 2:
        ok.sort(key=lambda a: -score(a)["avg"])
        best, worst = ok[0], ok[-1]
        sb, sw = score(best), score(worst)
        if sw["avg"] < 0 and sb["avg"] - sw["avg"] >= 0.3:
            seed = _mutate(best, random.Random(), ai_fn, lessons)
            with _lock:
                d = load(market, int(c.get("arena_size", 5)))
                d["alive"] = [a for a in d["alive"] if a["id"] != worst["id"]]
                d["dead"].append({"name": worst["name"], "persona": worst["persona"], "gen": worst["gen"], **sw, "t": _now()})
                d["alive"].append(_new(seed, best["gen"] + 1, best["name"]))
                _save(market, d)
            msg.append(f"❌ {worst['name']} 탈락({sw['n']}건 {sw['avg']:+.2f}%) → 🆕 {seed['name']} 투입(1등 {best['name']} 변형)")
            if add_lesson:
                add_lesson(market, f"[생존전] '{worst['persona'][:80]}' 전략은 {sw['n']}건 평균 {sw['avg']:+.2f}% 로 탈락", "AI 생존전")
        # 🏆 챔피언: 1등이 앞·뒤 절반 모두 플러스
        ts = best["trades"][-max(min_n, 30):]
        h = len(ts) // 2
        if _avg(ts[:h]) > 0 and _avg(ts[h:]) > 0:
            with _lock:
                d = load(market)
                d["champion"] = {"name": best["name"], "persona": best["persona"], "tp": best["tp"], "sl": best["sl"],
                                 "hold": best["hold"], "t": _now(), **sb}
                _save(market, d)
            msg.append(f"🏆 챔피언 {best['name']} ({sb['n']}건 평균 {sb['avg']:+.2f}% · 승률 {sb['win']}%)")
        else:
            with _lock:
                d = load(market)
                if d.get("champion"):
                    d["champion"] = None   # 1등이 앞·뒤 모두 플러스가 아니면 챔피언 내림
                    _save(market, d)
                    msg.append("🏆 챔피언 내림 (1등 성적이 꾸준하지 않음)")
    else:
        msg.append(f"아직 거래 {min_n}건 넘은 참가자가 {len(ok)}명이라 탈락 판정은 다음에")
    return "🏟️ AI 생존전: " + " · ".join(msg)


def overview(market: str) -> dict:
    d = load(market)
    return {"board": board(market), "champion": d.get("champion"), "dead": d["dead"][-5:], "text": text(market)}


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
