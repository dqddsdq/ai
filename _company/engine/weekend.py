"""📚 쉬는 날 공부 — 토요일 오전(한국) 한 번, 장이 닫혀 있을 때만 (매매에 영향 없음).

1. 긴 백테스트: 평일(최근 10일) 대신 최근 30일 고른 종목 전부로 → 상위 조합을 다음 주 그림자 매매에
2. 과거 급등 패턴: 거래 많은 종목의 최근 6개월 일봉에서 '+20%↑·거래량 3배↑' 날을 찾아 다음 날·사흘 뒤 결과 통계
   → AI 매수 판단에 참고로 붙임 (승률 추정 숫자는 안 바꿈 — 단타 거래와 성격이 달라서)
3. 놓친 종목 연구: 골랐지만 안 산 종목 vs 산 종목 — 상승률·거래량·프리장 차이
4. 그림자 조합 정리: 성적 나쁜 조합 빼고 백테스트 상위로 채우기
5. 주간 종합 복기 (AI 1번): 위 결과 + 한 주 성적 → 교훈 2~3개를 공유 노트에, 요약을 텔레그램으로
기록: _cache/weekend_{US|KR}.json · _cache/patterns_{US|KR}.json
"""

from __future__ import annotations

import json
import re
import statistics as st
import threading
from datetime import datetime, timedelta, timezone

from . import store, toss
from . import tradelearn as tl
from .agents import COMPANY_DIR

KST = timezone(timedelta(hours=9))
_running = threading.Event()


def _file(name: str):
    return COMPANY_DIR / "_cache" / name


def due(now: datetime | None = None) -> bool:
    """토요일 09:00 이후(한국) · 이번 주에 아직 안 했으면"""
    now = now or datetime.now(KST)
    if now.weekday() != 5 or now.hour < 9:
        return False
    wk = now.strftime("%G-W%V")
    return store.load(_file("weekend_done.json")).get("week") != wk


def _mark_done() -> None:
    store.save(_file("weekend_done.json"), {"week": datetime.now(KST).strftime("%G-W%V"), "t": datetime.now(KST).isoformat(timespec="seconds")})


# ── 2. 과거 급등 패턴 ──
def pattern_study(market: str, log=print, max_syms: int = 400, days: int = 130) -> dict:
    from . import picker
    base = picker.load_base(market).get("rows") or {}
    syms = sorted(base, key=lambda s: -float(base[s].get("value20_krw") or 0))[:max_syms]
    if not syms:
        return {"note": "종목 기준값이 아직 없어요 (평일 장 전 준비 뒤에 생겨요)"}
    log(f"📚 {'미장' if market == 'US' else '국장'} 과거 급등 패턴: 거래 많은 {len(syms)}종목 일봉 확인 중")
    events = []
    for sym in syms:
        try:
            closes, vols, dates = toss.daily(sym, days)
        except Exception:
            continue
        for i in range(21, len(closes) - 3):
            prev, cl = closes[i - 1], closes[i]
            if not prev:
                continue
            chg = (cl / prev - 1) * 100
            v20 = sum(vols[i - 20:i]) / 20 or 1
            vr = vols[i] / v20
            if chg >= 20 and vr >= 3:
                events.append({"sym": sym, "day": dates[i][:10], "chg": round(chg, 1), "vr": round(vr, 1),
                               "d1": round((closes[i + 1] / cl - 1) * 100, 2), "d3": round((closes[i + 3] / cl - 1) * 100, 2),
                               "low3": round((min(closes[i + 1:i + 4]) / cl - 1) * 100, 2)})
    def agg(xs):
        if not xs:
            return {"n": 0}
        return {"n": len(xs), "d1_avg": round(st.fmean(x["d1"] for x in xs), 2), "d1_up": round(sum(x["d1"] > 0 for x in xs) / len(xs) * 100),
                "d3_avg": round(st.fmean(x["d3"] for x in xs), 2), "low3_avg": round(st.fmean(x["low3"] for x in xs), 2)}
    out = {"t": datetime.now(KST).isoformat(timespec="seconds"), "market": market, "n_syms": len(syms), "all": agg(events),
           "by_chg": {k: agg([e for e in events if lo <= e["chg"] < hi]) for k, lo, hi in (("20~30%", 20, 30), ("30~50%", 30, 50), ("50%+", 50, 1e9))},
           "by_vol": {k: agg([e for e in events if lo <= e["vr"] < hi]) for k, lo, hi in (("3~5배", 3, 5), ("5~10배", 5, 10), ("10배+", 10, 1e9))}}
    store.save(_file(f"patterns_{market}.json"), out)
    return out


def pattern_text(market: str) -> str:
    """AI 매수 판단에 붙일 한 줄 (없으면 '')"""
    p = store.load(_file(f"patterns_{market}.json"))
    a = p.get("all") or {}
    if not a.get("n"):
        return ""
    return (f"과거 6개월 '+20%↑·거래량 3배↑' {a['n']}번: 다음 날 평균 {a['d1_avg']:+.1f}%(오른 비율 {a['d1_up']}%) · "
            f"사흘 뒤 {a['d3_avg']:+.1f}% · 사흘 안 최저 {a['low3_avg']:+.1f}% (급등 다음 날은 이 정도로 움직였음 — 참고용)")


# ── 3. 놓친 종목 연구 ──
def missed_study(market: str, days: int = 30) -> dict:
    since = (datetime.now(KST) - timedelta(days=days)).strftime("%Y-%m-%d")
    with tl._lock:
        j = tl._load()
    picks = []
    for key, w in j["watch"].items():
        mk, _, day = key.partition(":")
        if not day or mk != market or day < since:
            continue
        for sym, x in w.items():
            if x.get("pick_px"):
                picks.append({**x, "sym": sym, "day": day, "up": (x["max"] / x["pick_px"] - 1) * 100, "down": (x["min"] / x["pick_px"] - 1) * 100})
    def agg(xs):
        if not xs:
            return {"n": 0}
        return {"n": len(xs), "up_avg": round(st.fmean(x["up"] for x in xs), 2), "down_avg": round(st.fmean(x["down"] for x in xs), 2),
                "chg_avg": round(st.fmean(float(x.get("change") or 0) for x in xs), 1),
                "ratio_avg": round(st.fmean(float(x.get("ratio") or 0) for x in xs), 2), "pre_pct": round(sum(bool(x.get("pre")) for x in xs) / len(xs) * 100)}
    bought, missed = [p for p in picks if p.get("bought")], [p for p in picks if not p.get("bought")]
    big = sorted(missed, key=lambda p: -p["up"])[:5]
    return {"bought": agg(bought), "missed": agg(missed), "missed_top": [(p["sym"], p["day"], round(p["up"], 1)) for p in big]}


# ── 전체 ──
def run(log=print) -> dict:
    """토요일 오전 한 번 (두 시장). 오래 걸려도 장이 닫혀 있어서 괜찮음"""
    if _running.is_set():
        return {"ok": False, "message": "이미 공부 중이에요"}
    _running.set()
    from . import autotrade as at
    from . import backtest as bt
    from . import notify, shadow
    out = {}
    try:
        for m in at.markets():
            with at.in_market(m):
                c = at.config()
                r = {"market": m}
                try:
                    b = bt.run(c, m, at.signal_surge, at.signal_swing, at.state().get("swing_hist", []), log=log, days=30, include_today=True)
                    r["backtest"] = b.get("note", "")
                    if c.get("shadow_on"):
                        r["seeds"] = shadow.add_seeds(at.config(), m, bt.seeds(m))
                        r["prune"] = shadow.prune(at.config(), m)
                except Exception as e:
                    r["backtest"] = f"실패 {type(e).__name__}: {e}"
                try:   # 🔧 손실 원인별로 규칙 하나씩 고쳐 보기 → 처음 보는 날에서도 나으면 그림자에
                    from . import fixloop
                    fx_ = fixloop.run(at.config(), m, at.signal_surge, log)
                    r["fixloop"] = fixloop.text(fx_)
                    if fx_.get("seed") and c.get("shadow_on"):
                        r["fix_seed"] = shadow.add_seeds(at.config(), m, [fx_["seed"]])
                except Exception as e:
                    r["fixloop"] = f"🔧 원인별 수정 실패: {type(e).__name__}: {e}"
                try:
                    r["pattern"] = pattern_study(m, log)
                except Exception as e:
                    r["pattern"] = {"note": f"실패 {type(e).__name__}"}
                r["missed"] = missed_study(m)

                try:   # 📼 AI 리그 과거 장 복습 (토요일은 평일의 3배)
                    from . import arena_replay
                    r["replay"] = arena_replay.run(at.config(), m, log, limit=int(at.config().get("arena_replay_per_day", 40)) * 3)
                except Exception as e:
                    r["replay"] = f"📼 과거 장 복습 실패: {type(e).__name__}"
                try:   # 🏟️ AI 리그: 꼴찌 탈락 · 1등 변형 투입 · 챔피언
                    from . import arena
                    r["arena"] = arena.evolve(at.config(), m, arena.ai_json, tl.add_lesson, tl.lessons_text(m, 8))
                except Exception as e:
                    r["arena"] = f"🏟️ AI 리그 정리 실패: {type(e).__name__}"
                try:
                    s = at.state()
                    fx = 1.0 if m == "KR" else at.usd_krw(c)
                    from . import extras
                    sc = extras.scorecard(m, "live" if c["mode"] == "live" else "paper", fx, float((s.get("month") or {}).get("start") or 0), s.get("orders", []))
                    r["score"] = extras.scorecard_text(m, sc)
                except Exception as e:
                    r["score"] = f"성적표 실패 {type(e).__name__}"
                r["ai"] = _ai_review(m, r)
                if at.config().get("notes_council", True):   # 📒 이번 주 교훈까지 넣고 AI 셋이 회의해서 공유 노트 정리
                    try:
                        from . import notes_council
                        r["notes"] = notes_council.run_and_post(at.config(), m, log=log).get("message", "")
                    except Exception as e:
                        r["notes"] = f"노트 정리 실패: {type(e).__name__}"
                store.save(_file(f"weekend_{m}.json"), {"t": datetime.now(KST).isoformat(timespec="seconds"), **r})
                out[m] = r
                flag = "🇺🇸 미장" if m == "US" else "🇰🇷 국장"
                pt = pattern_text(m)
                ms = r["missed"]
                msg = (f"📚 {flag} 주말 공부 끝\n{r['score']}\n🧪 백테스트(30일): {r.get('backtest', '')}"
                       + (f" · 그림자에 {r['seeds']}개 추가" if r.get("seeds") else "") + (f" · {r['prune']}" if r.get("prune") else "")
                       + (f"\n{r['fixloop']}" if r.get("fixloop") else "") + (" (그림자에 추가)" if r.get("fix_seed") else "")
                       + (f"\n📈 {pt}" if pt else "") + (f"\n{r['arena']}" if r.get("arena") else "") + (f"\n{r['replay']}" if r.get("replay") else "")
                       + (f"\n🔎 놓친 종목 {ms['missed']['n']}개 평균 최고 {ms['missed']['up_avg']:+.1f}% vs 산 종목 {ms['bought']['n']}개 {ms['bought'].get('up_avg', 0):+.1f}%" if ms["missed"].get("n") else "")
                       + (f"\n🤖 {r['ai']}" if r.get("ai") else "") + (f"\n📒 {r['notes']}" if r.get("notes") else ""))
                notify.send(msg[:3500])
                at.event("주말 공부", msg[:400])
        _mark_done()
    finally:
        _running.clear()
    return {"ok": True, "result": out}


def _ai_review(market: str, r: dict) -> str:
    """주간 종합 복기 (AI 1번) → 교훈 2~3개를 공유 노트에"""
    from . import providers as pv
    from . import team as tm
    data = {"성적": r.get("score", ""), "AI 리그": r.get("arena", ""), "백테스트": r.get("backtest", ""), "원인별수정": r.get("fixloop", ""), "과거급등패턴": (r.get("pattern") or {}).get("all"),
            "급등구간별": (r.get("pattern") or {}).get("by_chg"), "놓친vs산": r.get("missed"), "공유노트": tl.lessons_text(market, 8)}
    prompt = ("자동매매 엔진의 이번 주 기록이에요. 다음 주 매매에 바로 쓸 교훈 2~3개를 한 줄씩 써 주세요 "
              "(숫자는 아래 자료만, 이미 공유 노트에 있는 건 빼고, 투자 권유 문구 없이).\n"
              f"{json.dumps(data, ensure_ascii=False)[:6000]}\n"
              '형식: {"lessons": ["...", "..."], "summary": "사장님께 한두 줄 요약"}')
    try:
        reply = pv.chat(tm.fast_spec(), "너는 트레이딩 코치다. 짧고 구체적으로.", [{"role": "user", "content": prompt}], effort="low", timeout=180)
        m = re.search(r"\{.*\}", reply, re.S)
        j = json.loads(m.group(0)) if m else {}
    except Exception as e:
        return f"(AI 복기 실패: {type(e).__name__})"
    for x in [str(x).strip() for x in (j.get("lessons") or []) if str(x).strip()][:3]:
        tl.add_lesson(market, x, "주말 공부")
    return str(j.get("summary", ""))[:300]
