"""🏟️ AI 리그 — 성격이 다른 AI 여러 개가 **가상으로만** 같은 후보를 두고 매매 → 성적으로 탈락·교체.

- 가상 전용: 이 파일은 주문 함수를 절대 부르지 않음 (시세는 실전 엔진이 넘겨준 것 + 읽기 전용 조회만)
- 후보: 실전 엔진이 매 바퀴 고른 후보(규칙 관문 통과한 것)를 그대로 받음
- 📒 모든 AI 가 같은 공유 노트(1호·2호·AI 리그·주말 공부 교훈 + 팀 기억 교훈)를 보고 판단
- AI 호출: 후보 한 종목당 1번에 참가자 전원이 같이 판단 (가상은 표본 제한 없음: arena_ai_per_day 0 = 무제한 · 30%↑ 급등 후보도 판단)
- 🧮 독립 표본: 실전 자격 판정은 같은 종목·같은 날을 1건으로 · 제일 크게 번 종목 하나를 빼도 플러스여야 함
- 🚀 30%↑ 급등 후보 가상 거래: 체결을 한쪽 1% 불리하게 · 공부용으로만 쓰고 실전 자격 판정에서는 뺌
- 매도: 참가자마다 자기 익절·손절·최대 보유 시간 + 마감 전 정리 (단타)
- 체결: 0.1% 불리하게 + 수수료·세금 (실전 가상 매매와 같게)
- 매주 토요일 아침(주말 공부) 성적 발표(텔레그램) · 탈락 없음 — 전원 가상 매매로 계속 같이 공부
- 🎲 무작위 대조군: AI 판단 없이 같은 후보를 같은 빈도로 '동전 던지기'로 사고, 같은 방식으로 팖
  → 이 대조군보다 확실히 나아야 실력으로 인정 (운으로 1등 한 AI 에 실전 돈이 가지 않게)
- ⏳ 성적은 최근 거래일수록 크게 침 (arena_half_life_days 일 지나면 절반 무게)
- 🧭 상황별 성적: 산 시점의 장 구간(프리장·장 초반·전반·후반) × 지수 분위기(하락·보합·상승) 로 따로 기록
  → 지금 상황에서 표본이 충분한데 대조군보다 못했던 AI 의 '산다'는 실전으로 안 감
- 💰 실전 자격: 거래 arena_min_trades 건↑ · 최근 평균 플러스 · 앞·뒤 절반 모두 플러스 · 대조군보다 arena_ctrl_margin %p↑
  인 참가자 중 상위 arena_live_n 명 — 대조군을 넘은 만큼 비중(weights)을 나눠, 약한 AI 판단은 금액을 줄여서 삼
  → 이 AI 가 '산다'고 한 종목은 실전 엔진이 실전으로 매수 (주문은 실전 엔진이 기존 안전장치 그대로: 현금만·봇 장부·손실 한도·추격 확인)
  → 매일 복기 때 자격 조건에서 떨어지면 바로 자격 내림 (가상으로 돌아가 다시 공부)
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
import uuid
from datetime import datetime, timedelta, timezone

from . import news, store
from .agents import COMPANY_DIR

KST = timezone(timedelta(hours=9))
SLIP = 0.1                      # % 불리한 체결 (한쪽)
FEE = {"US": 0.2, "KR": 0.21}   # % 왕복 수수료+세금 (미장 0.1%×2 · 국장 0.015%×2 + 매도세 0.18%)
HOT_SLIP = 1.0                  # % 한쪽 — 30%↑ 급등 후보는 실제 미끄러짐이 커서 가상 체결도 이만큼 불리하게
MAX_POS = 10                    # 참가자당 동시 보유 (가상이라 넉넉히 → 표본 빨리)
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
# 🎲 대조군: AI 판단 없이 무작위로 삼 · 파는 규칙은 참가자들의 중간값 (실력 비교 기준 — 실전 자격·1등 대상 아님)
CTRL_SEED = {"name": "무작위 대조군", "persona": "AI 판단 없이 같은 후보를 같은 빈도로 동전 던지기로 삼 (실력 비교 기준)",
             "tp": 4.0, "sl": 2.5, "hold": 90}
CTRL_P_DEFAULT = 0.35   # 참가자들이 '산다'고 한 비율을 아직 모를 때 대조군 매수 확률

HALF_LIFE_DAYS = 14     # 기본: 14일 지난 거래는 절반 무게
SIT_MIN = 8             # 상황별 판단에 쓸 최소 거래 수
CTRL_MARGIN = 0.3       # 기본: 대조군보다 평균 +0.3%p 이상이어야 실력으로 인정
CTRL_MIN_N = 20         # 대조군 끝난 거래(독립 표본)가 이만큼 미만이면 비교 기준이 불안정
CTRL_SHORT_EXTRA = 0.5  # 그동안은 실전 자격에 대조군 대비 여유 폭을 이만큼(%p) 더 요구 (기준이 약해지진 않음)
SESSIONS = ("프리장", "장 초반", "전반", "후반")
MOODS = ("하락장", "보합", "상승장")


def _file(m: str):
    return COMPANY_DIR / "_cache" / f"arena_{m}.json"


def _now() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


def _new(seed: dict, gen: int = 1, parent: str = "") -> dict:
    return {"id": "a" + uuid.uuid4().hex[:11], "name": seed["name"], "persona": seed["persona"],   # 같은 순간에 여럿 만들어도 안 겹치게
            "tp": float(seed["tp"]), "sl": float(seed["sl"]), "hold": int(seed["hold"]), "gen": gen, "parent": parent,
            "born": _now(), "pos": {}, "trades": []}


def load(m: str, size: int | None = None) -> dict:
    d = store.load(_file(m), lambda: {})
    d.setdefault("alive", []); d.setdefault("dead", []); d.setdefault("seen", {}); d.setdefault("calls", {}); d.setdefault("champion", None)
    if not d.get("ctrl"):
        d["ctrl"] = {**_new(CTRL_SEED), "id": "ctrl", "ctrl": True}
    d.setdefault("vote_stats", {"asked": 0, "yes": 0})
    seen_ids: set = set()
    fixed = False
    for a in d["alive"]:   # 예전 방식(밀리초+두 자리)으로 만든 id 가 겹친 참가자 고치기 — 겹치면 AI 판단·투표가 섞임
        if a["id"] in seen_ids:
            a["id"] = "a" + uuid.uuid4().hex[:11]
            fixed = True
        seen_ids.add(a["id"])
    if fixed:   # 바로 저장해야 다시 읽어도 같은 id
        store.save(_file(m), d)
    used = {a["name"] for a in d["alive"]} | {x["name"] for x in d["dead"]}   # 탈락한 전략은 다시 안 들어옴
    for s in SEEDS if size else []:
        if len(d["alive"]) >= size:
            break
        if s["name"] not in used:
            d["alive"].append(_new(s))
    return d


def _all(d: dict) -> list[dict]:
    """참가자 + 대조군 (보유 정리·시세 조회용)"""
    return d["alive"] + ([d["ctrl"]] if d.get("ctrl") else [])


def _save(m: str, d: dict) -> None:
    d["dead"] = d["dead"][-30:]
    for a in _all(d):
        a["trades"] = a["trades"][-200:]
    store.save(_file(m), d)


def _ret(buy: float, sell: float, m: str) -> float:
    return ((sell * (1 - SLIP / 100)) / (buy * (1 + SLIP / 100)) - 1) * 100 - FEE.get(m, 0.2)


def _avg(ts: list) -> float:
    return st.fmean(t["ret"] for t in ts) if ts else 0.0


def _age_days(t: dict, now: datetime | None = None) -> float:
    try:
        when = datetime.strptime(t.get("t") or "", "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
    except ValueError:
        return 0.0
    return max(0.0, ((now or datetime.now(KST)) - when).total_seconds() / 86400)


def _wavg(ts: list, half_life: float = HALF_LIFE_DAYS, now: datetime | None = None) -> float:
    """⏳ 최근 거래일수록 크게 친 평균 수익률(%) — half_life 일 지나면 절반 무게"""
    if not ts:
        return 0.0
    hl = max(1.0, float(half_life or HALF_LIFE_DAYS))
    w = [0.5 ** (_age_days(t, now) / hl) for t in ts]
    return sum(wi * t["ret"] for wi, t in zip(w, ts)) / sum(w)


def _hl(c: dict | None) -> float:
    return float((c or {}).get("arena_half_life_days", HALF_LIFE_DAYS))


# ── 🧭 상황 ──
def situation(market: str, minutes, index_chg) -> str:
    """'장 구간·지수 분위기' 한 단어 (예: '장 초반·하락장'). 모르면 구간만"""
    if minutes is None:
        sess = ""
    elif minutes < 0:
        sess = "프리장"
    elif minutes < 30:
        sess = "장 초반"
    elif minutes < 195:
        sess = "전반"
    else:
        sess = "후반"
    mood = "" if index_chg is None else ("하락장" if index_chg <= -1 else "상승장" if index_chg >= 1 else "보합")
    return "·".join(x for x in (sess, mood) if x)


def _situation_now(c: dict | None, market: str) -> str:
    try:
        from . import extras, picker
        mins = picker.session_minutes(market, extended=market == "US" and bool((c or {}).get("us_premarket")))
        try:
            chg = extras.index_change(market)
        except Exception:
            chg = None
        return situation(market, mins, chg)
    except Exception:
        return ""


def sit_stats(a: dict, half_life: float = HALF_LIFE_DAYS) -> dict:
    """상황별 {상황: {"n": 건수, "avg": 최근 가중 평균}}"""
    by: dict[str, list] = {}
    for t in a["trades"]:
        if t.get("sit"):
            by.setdefault(t["sit"], []).append(t)
    return {k: {"n": len(v), "avg": round(_wavg(v, half_life), 2)} for k, v in by.items()}


# ── 🧮 모든 판단 채점 · 🚫 AI 별 자동 거름망 ──
# 후보마다 '참가자 각자의 익절·손절·보유 시간으로 샀다면' 결과를 시세로 계산 (AI 호출 없음)
# → 산 것뿐 아니라 '안 산다' 한 것도 채점 (놓친 기회) · 표본 충분히 지는 구간은 그 AI 에게 안 물어봄
PROBE_MAX = 80        # 동시에 지켜보는 후보 최대
GRADED_KEEP = 800     # 채점 끝난 후보 보관
FILTER_MIN = 10       # 그 구간에서 '산다' 결과가 이만큼↑ 이고
FILTER_LOSS = 0.5     #   평균이 -이 % 이하면 그 AI 는 그 구간에서 거름
UNBLOCK_N = 8         # 최근 그 구간 후보(내 규칙으로 샀다면) 이만큼↑ 이 평균 플러스면 거름 풀기
EXPLORE = 0.15        # 걸러진 구간도 이 확률로 물어봄 (계속 배우게)
MISS_PCT = 1.0        # '안 산다' 했는데 이만큼↑ 올랐으면 놓친 기회


SPIKE_VOL = 3.0       # ⏱️ 1분봉 거래량이 직전 평균의 이만큼↑ 이면 '거래량 폭발' 시작
LATE_MIN = 30         #   폭발 뒤 이 분↑ 지나서
LATE_CHG = 15.0       #   이미 오늘 이 %↑ 올랐으면 '늦은 진입'


def spike_minutes(sym: str, now: float | None = None) -> float | None:
    """⏱️ 오늘 거래량 폭발(1분봉) 시작 뒤 지난 분 — 1분봉 캐시 재사용 · 못 찾으면 None"""
    try:
        from . import shadow
        bs = shadow._bars(sym, 120)
        now = now or time.time()
        for i in range(10, len(bs)):
            ep = shadow._ep(bs[i]["t"])
            if not ep or now - ep > 400 * 60:   # 오늘 장 밖 봉은 제외
                continue
            prev = bs[max(0, i - 20):i]
            base = sum(x["v"] for x in prev) / len(prev)
            if base > 0 and bs[i]["v"] >= base * SPIKE_VOL:
                return round(max(0.0, (now - ep) / 60), 1)
    except Exception:
        pass
    return None


def entry_keys(spike_min, chg) -> list[str]:
    """⏱️ 진입 시점 구간 (폭발 뒤 경과 · 늦은 진입 여부)"""
    if spike_min is None:
        return []
    out = ["폭발 후 " + ("~10분" if spike_min < 10 else "10~30분" if spike_min < LATE_MIN else "30분+")]
    try:
        late = spike_min >= LATE_MIN and float(chg) >= LATE_CHG
    except (TypeError, ValueError):
        late = False
    out.append("늦은 진입" if late else "제때 진입")
    return out


def buckets(cd: dict, sit: str = "") -> list[str]:
    """후보를 묶는 구간 이름들 (상승률·거래량·프리장·상황·진입 시점)"""
    f = cd.get("features") or {}
    out = entry_keys(cd.get("spike_min"), f.get("change"))
    try:
        chg = float(f["change"])
        out.append("상승 " + ("~20%" if chg < 20 else "20~40%" if chg < 40 else "40%+"))
    except (KeyError, TypeError, ValueError):
        pass
    try:
        ratio = float(f["ratio"])
        out.append("거래량 " + ("3배 미만" if ratio < 3 else "3~10배" if ratio < 10 else "10배+"))
    except (KeyError, TypeError, ValueError):
        pass
    if "pre" in f:
        out.append("프리장" if f.get("pre") else "정규장")
    if f.get("news"):
        out.append("공시 " + f["news"])   # 있음·없음·악재 → 공시 보는 AI 기록도 구간별로
    if (cd.get("news") or {}).get("ok"):
        out.append("뉴스 있음" if cd["news"].get("heads") else "뉴스 없음")
    if sit:
        out.append(sit)
    return out


def _grade_index(d: dict) -> dict:
    """{참가자id: {"yes": [...], "no": [...], "by": {구간: {"yes": [...], "all": [...]}}}} — 채점 기록 한 번 훑어서 (최근 순서 유지)"""
    idx: dict[str, dict] = {}
    for g in d.get("graded") or []:
        for aid, ret in (g.get("res") or {}).items():
            e = idx.setdefault(aid, {"yes": [], "no": [], "by": {}})
            v = (g.get("votes") or {}).get(aid)
            if v is True:
                e["yes"].append(ret)
            elif v is False:
                e["no"].append(ret)
            for k in g.get("keys") or []:
                b = e["by"].setdefault(k, {"yes": [], "all": []})
                b["all"].append(ret)
                if v is True:
                    b["yes"].append(ret)
    return idx


def blocked_key(e: dict | None, keys: list[str]) -> str:
    """이 후보 구간 중 그 AI 가 걸러지는 구간 (없으면 '')"""
    if not e:
        return ""
    for k in keys:
        b = e["by"].get(k)
        if not b:
            continue
        ys, al = b["yes"][-20:], b["all"][-15:]
        if len(ys) >= FILTER_MIN and st.fmean(ys) <= -FILTER_LOSS and not (len(al) >= UNBLOCK_N and st.fmean(al) > 0):
            return k
    return ""


def card(e: dict | None, tp: float = 0) -> dict:
    """판단 성적표: 산 것·안 산 것 평균, 놓친 기회, 판단력(산 것 − 안 산 것)"""
    if not e:
        return {"yes_n": 0, "no_n": 0}
    ys, ns = e["yes"][-60:], e["no"][-60:]
    out = {"yes_n": len(ys), "no_n": len(ns), "yes_avg": round(st.fmean(ys), 2) if ys else None,
           "no_avg": round(st.fmean(ns), 2) if ns else None, "missed": sum(1 for x in ns if x >= MISS_PCT)}
    if ys and ns:
        out["skill"] = round(out["yes_avg"] - out["no_avg"], 2)
    return out


def _card_line(e: dict | None, keys: list[str]) -> str:
    """판단할 때 AI 에게 붙일 자기 기록 한 줄 (숫자만 · 매번 새로 계산 — 요약이 흐려질 일 없음)"""
    cd_ = card(e)
    if not cd_["yes_n"] and not cd_["no_n"]:
        return ""
    parts = [f"산 것 {cd_['yes_n']}건 {cd_['yes_avg']:+.1f}%" if cd_["yes_n"] else "산 것 없음"]
    if cd_["no_n"]:
        parts.append(f"안 산 것 {cd_['no_n']}건 {cd_['no_avg']:+.1f}% (놓친 +{MISS_PCT:g}%↑ {cd_['missed']}건)")
    here = []
    for k in keys:
        b = (e or {}).get("by", {}).get(k)
        if b and len(b["yes"]) >= 3:
            here.append(f"{k} 산 것 {len(b['yes'])}건 {st.fmean(b['yes'][-20:]):+.1f}%")
    return "내 기록: " + " · ".join(parts) + (" / 이 종목 구간: " + " · ".join(here[:3]) if here else "")


def _probe_tick(d: dict, market: str, quotes: dict, minutes, day: str, now: float) -> int:
    """지켜보는 후보마다 참가자별로 '샀다면' 정리 → 다 끝나면 채점 기록으로. 바뀐 것 수 (0 이면 저장 안 해도 됨)"""
    done = 0
    n0 = len(d.get("probes") or [])
    people = _all(d)
    keep = []
    for pr in d.get("probes") or []:
        px = float(quotes.get(pr["sym"]) or 0)
        over = pr["day"] != day or (minutes is not None and minutes >= 380)
        if px:
            pr["last"] = px
        px = px or (float(pr.get("last") or 0) if over else 0)
        if px:
            r = (px / pr["px"] - 1) * 100
            mins = (now - pr["t"]) / 60
            for a in people:
                if a["id"] not in pr["res"] and (r >= a["tp"] or r <= -a["sl"] or mins >= a["hold"] or over):
                    pr["res"][a["id"]] = round(_ret(pr["px"], px, market), 3)
                    done += 1
        if over and not px:   # 시세를 한 번도 못 받음 → 버림
            continue
        if all(a["id"] in pr["res"] for a in people):
            d.setdefault("graded", []).append({"sym": pr["sym"], "day": pr["day"], "t": _now(), "keys": pr["keys"],
                                               "votes": pr["votes"], "res": pr["res"],
                                               "spike_min": pr.get("spike_min"), "chg": pr.get("chg")})
            done += 1
        else:
            keep.append(pr)
    d["probes"] = keep[-PROBE_MAX:]
    d["graded"] = (d.get("graded") or [])[-GRADED_KEEP:]
    return done + (n0 - len(d["probes"]))


AFTER_MAX = 100   # 시간 청산 뒤 동시에 추적하는 최대 건수


def _after_tick(d: dict, quotes: dict, minutes, day: str, now: float) -> bool:
    """시간 청산한 종목의 이후 고점을 장 마감까지 따라가 거래 기록(after_hi %, after_min 분)에 적음"""
    ws = d.get("after") or []
    if not ws:
        return False
    by_id = {a["id"]: a for a in _all(d)}
    keep, changed = [], False
    for w in ws:
        px = float(quotes.get(w["sym"]) or 0)
        if px > w["hi"]:
            w["hi"] = px
            a = by_id.get(w["aid"])
            for tr in reversed((a or {}).get("trades") or []):
                if tr.get("t") == w["t"] and tr.get("sym") == w["sym"]:
                    tr["after_hi"] = round((px / w["px"] - 1) * 100, 2)
                    tr["after_min"] = round((now - w["at"]) / 60)
                    break
            changed = True
        if w["day"] == day and not (minutes is not None and minutes >= 380) and w["aid"] in by_id:
            keep.append(w)
        else:
            changed = True   # 장 마감·날 바뀜 → 추적 끝
    d["after"] = keep[-AFTER_MAX:]
    return changed


# ── 매 바퀴 ──
def step(c: dict, market: str, cands: dict, quotes: dict, minutes, day: str, price_fn=None, ai_fn=None, extra: dict | None = None) -> list[str]:
    """보유 정리 + 새 후보 AI 판단 시작. 결과 줄 목록"""
    if not c.get("arena_on"):
        return []
    out: list[str] = []
    quotes = {k: (v.get("price") if isinstance(v, dict) else v) for k, v in (quotes or {}).items()}
    quotes = {k: float(v) for k, v in quotes.items() if v}
    # 단타 후보만 · 가상이라 30%↑ 급등 검증 후보(extra)까지 넓혀서 표본 빨리
    cands = {k: v for k, v in {**{k: {**v, "_hot": True} for k, v in (extra or {}).items()}, **(cands or {})}.items()
             if (v.get("style") or "surge") == "surge"}
    with _lock:
        d = load(market, int(c.get("arena_size", 10)))
        held = {s for a in _all(d) for s in a["pos"]} | {p["sym"] for p in d.get("probes") or []}   # 채점 중인 후보 시세도
        held |= {w["sym"] for w in d.get("after") or []}   # 시간 청산 뒤 고점 추적 중인 종목도
        need = [s for s in held | set(cands) if not quotes.get(s)][:200]   # 넓힌 후보 시세도 한 번에
    if need and price_fn:
        try:
            got = price_fn(need) or {}
            quotes = {**quotes, **{k: float(v["price"] if isinstance(v, dict) else v) for k, v in got.items() if v}}
        except Exception:
            pass
    now = time.time()
    with _lock:
        d = load(market, int(c.get("arena_size", 10)))
        for a in _all(d):
            for sym, p in list(a["pos"].items()):
                px = float(quotes.get(sym) or 0)
                if not px:
                    continue
                r = (px / p["px"] - 1) * 100
                mins = (now - p["t"]) / 60
                p["hi"] = max(float(p.get("hi") or p["px"]), px)   # 보유 중 고점
                why = ("익절" if r >= a["tp"] else "손절" if r <= -a["sl"] else "시간" if mins >= a["hold"]
                       else "마감" if (minutes is not None and minutes >= 380) or p.get("day") != day else "")
                if why:
                    ret = _ret(p["px"], px, market) - (2 * (HOT_SLIP - SLIP) if p.get("hot") else 0)   # 🚀 급등 후보는 더 불리하게
                    a["trades"].append({"sym": sym, "day": p.get("day", day), "ret": round(ret, 3), "why": why,
                                        "mins": round(mins), "t": _now(), "sit": p.get("sit", ""),
                                        "spike_min": p.get("spike_min"), "chg": p.get("chg"),
                                        "late": "늦은 진입" in entry_keys(p.get("spike_min"), p.get("chg")),
                                        **({"hot": True} if p.get("hot") else {})})
                    if why == "시간":   # ⏳ 보유 시간 조정 근거: 청산 시점 수익률 + 보유 중 고점 + 이후 고점(장 마감까지 추적)
                        tr = a["trades"][-1]
                        tr["exit_r"] = round(r, 2)
                        tr["hi_in"] = round((p["hi"] / p["px"] - 1) * 100, 2)
                        tr["after_hi"] = 0.0; tr["after_min"] = 0
                        d.setdefault("after", []).append({"aid": a["id"], "sym": sym, "t": tr["t"], "day": tr["day"],
                                                          "px": px, "hi": px, "at": now})
                    del a["pos"][sym]
                    out.append(f"🏟️ {a['name']} {sym} {why} {ret:+.2f}%")
        changed = _probe_tick(d, market, quotes, minutes, day, now) > 0   # 🧮 '샀다면' 채점 (AI 호출 없음)
        changed |= _after_tick(d, quotes, minutes, day, now)   # ⏳ 시간 청산 뒤 고점
        changed |= any(k != day for k in d["seen"])
        # 새 후보 → 참가자 전원 판단 (한 종목 하루 1번)
        calls = d["calls"].get(day, 0)
        seen = d["seen"].setdefault(day, [])
        for k in [k for k in d["seen"] if k != day]:
            d["seen"].pop(k, None)
        d["calls"] = {day: calls}
        todo = [s for s, cd in cands.items() if s not in seen and quotes.get(s)
                and (minutes is None or minutes <= 360) and f"{market}:{s}" not in _pending]
        limit = int(c.get("arena_ai_per_day", 0) or 0)   # 0 = 자동: 리그 시작 뒤 21일은 제한 없음(빨리 배우기) → 그 뒤 하루 300번 (토큰)
        if not limit and _league_days(d) > 21:
            limit = 300
        start = []
        for s in todo:
            if limit and calls >= limit:
                break
            calls += 1; seen.append(s); start.append(s)
        d["calls"][day] = calls
        due = voice_due(c, d, day) if out else {}
        if out or start or changed or due:   # ⚡ 바뀐 게 있을 때만 파일 쓰기 (매 바퀴 수백 KB 쓰지 않게 · 메모리엔 이미 반영)
            _save(market, d)
    if due and _voice:   # 🗣️ 의견 생긴 AI → 뒤에서 물어보고 바로 채팅으로
        try:
            _voice(c, market, due)
        except Exception:
            pass
    for s in start:
        _pending[f"{market}:{s}"] = now
        threading.Thread(target=_decide, args=(c, market, s, cands[s], float(quotes[s]), day, ai_fn),
                         daemon=True, name="arena-ai").start()
    return out


_notes_cache: dict[str, tuple[float, str]] = {}


def _league_days(d: dict) -> float:
    """리그 시작(가장 먼저 들어온 참가자) 뒤 지난 날 수"""
    try:
        b = min(a.get("born", "") for a in d.get("alive") or [] if a.get("born"))
        return (datetime.now(KST) - datetime.strptime(b, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)).days
    except (ValueError, TypeError):
        return 0


def _notes(market: str) -> str:
    """📒 모든 AI 가 같이 보는 노트 (1분 보관 — 판단마다 파일 다시 읽지 않게)"""
    hit = _notes_cache.get(market)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    txt = _notes_raw(market)
    _notes_cache[market] = (time.time(), txt)
    return txt


def _notes_raw(market: str) -> str:
    """공유 교훈(1호·2호·AI 리그·주말 공부) + 자동매매 팀 기억의 교훈"""
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


def _ctrl_p(d: dict) -> float:
    """🎲 대조군 매수 확률 = 지금까지 참가자들이 '산다'고 한 비율 (같은 빈도로 무작위)"""
    vs = d.get("vote_stats") or {}
    asked, yes = int(vs.get("asked", 0)), int(vs.get("yes", 0))
    if asked < 20:
        return CTRL_P_DEFAULT
    return min(0.9, max(0.05, yes / asked))


def _decide(c: dict, market: str, sym: str, cd: dict, price: float, day: str, ai_fn) -> None:
    try:
        sit = _situation_now(c, market)
        chg = (cd.get("features") or {}).get("change")
        spk = spike_minutes(sym) if chg is not None else None   # ⏱️ 거래량 폭발 뒤 경과 분 (상승률 있는 후보만)
        ent = {"spike_min": spk, "chg": chg}   # 산 시점 기록 (늦은 진입 채점용)
        keys = buckets({**cd, "spike_min": spk}, sit)
        with _lock:
            d = load(market)
            idx = _grade_index(d)
            asked, skipped = [], {}
            for a in d["alive"]:
                bk = blocked_key(idx.get(a["id"]), keys)
                if bk and random.random() >= EXPLORE:
                    skipped[a["id"]] = bk   # 🚫 이 구간에서 계속 진 AI → 안 물어보고 '안 삼'
                    continue
                asked.append({"id": a["id"], "name": a["name"],
                              "persona": a["persona"] + (f" (스스로 정한 규칙: {' / '.join(a['habits'])})" if a.get("habits") else ""),
                              "rec": _card_line(idx.get(a["id"]), keys),
                              "n": len(a["trades"])})
        r: dict = {}
        if asked:   # 다 걸러졌으면 AI 를 안 부름 (빠르고 비용 0)
            e = cd.get("est") or {}
            notes = _notes(market)
            mt = int(c.get("arena_min_trades", 20))
            prompt = ("AI 리그: 아래 참가자들이 각자 성격대로 이 종목을 지금 살지 따로 판단해요 (가상 매매 — 돈이 안 들고, 사 봐야 배워요). "
                      "성격에 대체로 맞으면 true, 성격과 반대일 때만 false. 완벽한 자리만 기다리지 마세요. "
                      "'내 기록' 은 그 참가자의 지난 판단 결과예요 — 자기 기록에서 지는 패턴이면 피하세요. "
                      "'🔰 기록 적음' 참가자는 성격에 조금이라도 맞으면 사 보세요.\n"
                      f"종목 {sym} 현재가 {price} · 구간: {', '.join(keys) or '모름'} · 고른 이유: {cd.get('reason', '')}\n"
                      + (f"진입 시점: 거래량 폭발 뒤 {spk:.0f}분 · 오늘 {float(chg):+.1f}% (폭발 뒤 {LATE_MIN}분↑ + 오늘 +{LATE_CHG:g}%↑ 면 늦은 진입)\n"
                         if spk is not None and chg is not None else "")
                      + (f"최근 공시: {news.note(cd.get('news'))}\n" if news.note(cd.get("news")) else "")
                      + (f"학습 추정: 승률 {float(e.get('p', 0)) * 100:.0f}% · 기대 수익 {float(e.get('ev', 0)):+.2f}%\n" if e else "")
                      + (f"공유 노트: {notes}\n" if notes else "")
                      + "참가자:\n" + "\n".join(f"- {a['id']} ({a['name']}): {a['persona']}" + (f" | {a['rec']}" if a["rec"] else "")
                                                + (f" | 🔰 기록 적음 (거래 {a['n']}건)" if a["n"] < mt else "") for a in asked)
                      + '\n형식: {"참가자id": true/false, ...} (성격에 맞을 때만 true)')
            got = ai_fn(prompt) if ai_fn else None
            if not isinstance(got, dict):
                return
            r = got
        with _lock:
            d = load(market)
            ids_asked = {a["id"] for a in asked}
            yes = [a["id"] for a in d["alive"] if a["id"] in ids_asked and r.get(a["id"]) is True]
            for a in d["alive"]:
                if a["id"] in yes and sym not in a["pos"] and len(a["pos"]) < MAX_POS:
                    a["pos"][sym] = {"px": price, "t": time.time(), "day": day, "sit": sit, **ent, **({"hot": True} if cd.get("_hot") else {})}
            ctrl = d["ctrl"]   # 🎲 대조군: 같은 후보를 같은 빈도로 무작위 매수
            ctrl_buy = random.random() < _ctrl_p(d)
            if ctrl_buy and sym not in ctrl["pos"] and len(ctrl["pos"]) < MAX_POS:
                ctrl["pos"][sym] = {"px": price, "t": time.time(), "day": day, "sit": sit, **ent, **({"hot": True} if cd.get("_hot") else {})}
            vs = d["vote_stats"]
            vs["asked"] = int(vs.get("asked", 0)) + len(asked); vs["yes"] = int(vs.get("yes", 0)) + len(yes)
            v = d.setdefault("votes", {})
            for k in [k for k in v if k != day]:
                v.pop(k, None)
            v.setdefault(day, {})[sym] = yes
            # 🧮 채점 시작: 물어본 AI 는 찬반, 걸러진 AI 는 None (구간 성적엔 '샀다면' 으로 들어감)
            votes = {a["id"]: (a["id"] in yes) if a["id"] in ids_asked else None for a in d["alive"]}
            votes["ctrl"] = ctrl_buy
            d.setdefault("probes", []).append({"sym": sym, "px": price, "t": time.time(), "day": day, "keys": keys, "votes": votes, "res": {}, **ent})
            if skipped:
                sv = d.setdefault("saved", {})
                if sv.get("day") != day:
                    sv.clear(); sv.update(day=day, asks=0, calls=0)
                sv["asks"] = int(sv.get("asks", 0)) + len(skipped)
                sv["calls"] = int(sv.get("calls", 0)) + (0 if asked else 1)
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
    """실전 엔진이 후보를 처음 봤을 때 바로 AI 리그 판단 시작 (하루 한 종목 1번 · 하루 횟수 안에서)"""
    with _lock:
        d = load(market)
        seen = d["seen"].setdefault(day, [])
        calls = int(d["calls"].get(day, 0))
        lim_ = int(c.get("arena_ai_per_day", 0) or 0)
        if sym in seen or f"{market}:{sym}" in _pending or (lim_ and calls >= lim_):
            return f"{market}:{sym}" in _pending
        seen.append(sym); d["calls"] = {day: calls + 1}
        _save(market, d)
    _pending[f"{market}:{sym}"] = time.time()
    threading.Thread(target=_decide, args=(c, market, sym, cd, price, day, ai_fn or ai_json), daemon=True, name="arena-ai").start()
    return True


def _weak_here(a: dict, ctrl: dict, sit: str, half_life: float) -> bool:
    """🧭 지금 상황에서 표본이 충분한데 대조군보다 못했으면 True"""
    if not sit:
        return False
    mine = [t for t in a["trades"] if t.get("sit") == sit]
    if len(mine) < SIT_MIN:
        return False
    base = [t for t in ctrl["trades"] if t.get("sit") == sit]
    bar = _wavg(base, half_life) if len(base) >= SIT_MIN else 0.0
    return _wavg(mine, half_life) <= bar


def live_vote(market: str, day: str, sym: str, c: dict | None = None) -> tuple[str, dict | None]:
    """실전 자격 AI 의 판단: ('pending'|'unseen'|'no'|'yes', 찬성한 실전 AI)
    찬성 AI 정보엔 size(0.25~1.0): 찬성한 AI 들의 비중 합 × 자격 인원 — 약한 AI 만 찬성하면 금액을 줄임 (늘리지는 않음)"""
    if f"{market}:{sym}" in _pending:
        return "pending", None
    d = load(market)
    live = d.get("live") or []
    if not live:
        return "no", None
    v = (d.get("votes") or {}).get(day, {})
    if sym not in v:
        return ("no" if sym in d["seen"].get(day, []) else "unseen"), None
    by = {a["id"]: a for a in d["alive"]}
    sit = _situation_now(c, market)
    hl = _hl(c)
    weights = d.get("weights") or {}
    yes, skipped = [], []
    for i, aid in enumerate(live):
        if aid in v[sym] and aid in by:
            if _weak_here(by[aid], d["ctrl"], sit, hl):
                skipped.append(by[aid]["name"])
            else:
                yes.append((i, aid))
    if not yes:
        return "no", ({"skipped": skipped, "sit": sit} if skipped else None)
    i, aid = yes[0]
    share = sum(float(weights.get(a, 1 / len(live))) for _, a in yes)
    size = round(min(1.0, max(0.25, share * len(live))), 2)
    return "yes", {"id": aid, "name": by[aid]["name"], "rank": i + 1, "size": size, "sit": sit,
                   "with": [by[a]["name"] for _, a in yes[1:]]}


def _ctrl_avg(d: dict, c: dict | None = None) -> float:
    ts = [t for t in d["ctrl"]["trades"] if not t.get("hot")][-30:] if d.get("ctrl") else []
    return _wavg(ts, _hl(c)) if len(ts) >= 5 else 0.0


def _indep(ts: list) -> list:
    """독립 표본: 같은 종목·같은 날 여러 건은 1건(평균)으로 — 중복이 성적을 부풀리지 않게 (순서·시각은 첫 건 기준)"""
    g: dict = {}
    for t in ts:
        k = (t.get("sym"), t.get("day"))
        if k in g:
            g[k]["_r"].append(t["ret"])
        else:
            g[k] = {**t, "_r": [t["ret"]]}
    return [{**{k: v for k, v in x.items() if k != "_r"}, "ret": sum(x["_r"]) / len(x["_r"])} for x in g.values()]


def _ctrl_n(d: dict) -> int:
    """대조군 독립 표본 수 (급등 후보 제외)"""
    return len(_indep([t for t in d["ctrl"]["trades"] if not t.get("hot")])) if d.get("ctrl") else 0


def _qualified(a: dict, min_n: int, ctrl_avg: float = 0.0, margin: float = CTRL_MARGIN, half_life: float = HALF_LIFE_DAYS,
               ctrl_n: int | None = None) -> bool:
    if ctrl_n is not None and ctrl_n < CTRL_MIN_N:   # 대조군 표본이 적으면 비교 기준이 불안정 → 여유 폭을 더 요구
        margin += CTRL_SHORT_EXTRA
    ts = _indep([t for t in a["trades"] if not t.get("hot")])[-max(min_n, 30):]   # 🚀 30%↑ 급등 후보 가상 거래는 실전 자격에서 뺌 (실전은 그 구간을 안 삼)
    if len(ts) < min_n:
        return False
    h = len(ts) // 2
    if not (_avg(ts) > 0 and _avg(ts[:h]) > 0 and _avg(ts[h:]) > 0 and _wavg(ts, half_life) > max(0.0, ctrl_avg) + margin):
        return False
    by: dict = {}
    for t in ts:
        by[t.get("sym")] = by.get(t.get("sym"), 0) + t["ret"]
    rest = [t for t in ts if t.get("sym") != max(by, key=by.get)]
    return bool(rest) and _avg(rest) > 0   # 대박 한 종목으로 1등 방지


def _edge(a: dict, ctrl_avg: float, half_life: float) -> float:
    """대조군보다 얼마나 나은지 (%p, 최근 가중)"""
    return _wavg(a["trades"][-30:], half_life) - ctrl_avg


def _weights(alive_live: list[dict], ctrl_avg: float, half_life: float) -> dict:
    """💰 실전 자격 AI 들의 비중 — 대조군을 넘은 만큼 나눔 (합 1)"""
    ed = {a["id"]: max(0.01, _edge(a, ctrl_avg, half_life)) for a in alive_live}
    tot = sum(ed.values())
    return {k: round(v / tot, 3) for k, v in ed.items()} if tot else {}


def recheck(c: dict, market: str) -> str:
    """매일 복기: 실전 자격 AI 중 성적이 떨어진 AI 는 바로 자격 내림 · 비중 다시 계산"""
    min_n = int(c.get("arena_min_trades", 20))
    margin, hl = float(c.get("arena_ctrl_margin", CTRL_MARGIN)), _hl(c)
    with _lock:
        d = load(market)
        by = {a["id"]: a for a in d["alive"]}
        ca = _ctrl_avg(d, c)
        cn = _ctrl_n(d)
        drop = [i for i in d.get("live") or [] if i not in by or not _qualified(by[i], min_n, ca, margin, hl, cn)]
        d["live"] = [i for i in d.get("live") or [] if i not in drop]
        d["weights"] = _weights([by[i] for i in d["live"]], ca, hl)
        _save(market, d)
    if not drop:
        return ""
    return "🏟️ 실전 자격 내림: " + ", ".join(by[i]["name"] if i in by else i for i in drop) + " (성적이 떨어지거나 무작위 대조군보다 못해서 가상으로 다시 공부)"


# ── 성적 ──
def score(a: dict, n: int = 30, half_life: float = HALF_LIFE_DAYS) -> dict:
    ts = a["trades"][-n:]
    if not ts:
        return {"n": len(a["trades"]), "avg": 0.0, "wavg": 0.0, "win": 0}
    return {"n": len(a["trades"]), "avg": round(_avg(ts), 2), "wavg": round(_wavg(ts, half_life), 2),
            "win": round(sum(t["ret"] > 0 for t in ts) / len(ts) * 100)}


def board(market: str, c: dict | None = None) -> list[dict]:
    d = load(market)
    hl = _hl(c)
    live = d.get("live") or []
    weights = d.get("weights") or {}
    ca = _ctrl_avg(d, c)
    idx = _grade_index(d)
    all_keys = sorted({k for g in d.get("graded") or [] for k in g.get("keys") or []})

    def row(a: dict, is_ctrl: bool = False) -> dict:
        sc = score(a, half_life=hl)
        e = idx.get(a["id"])
        blocks = [] if is_ctrl else [k for k in all_keys if blocked_key(e, [k])]
        return {"name": a["name"], "gen": a.get("gen", 1), "ctrl": is_ctrl, "card": card(e), "blocks": blocks,
                "live": (live.index(a["id"]) + 1) if a["id"] in live else 0,
                "weight": round(float(weights.get(a["id"], 0)) * 100) if a["id"] in live else 0,
                "persona": a["persona"], "tp": a["tp"], "sl": a["sl"], "hold": a["hold"], "held": len(a["pos"]),
                "edge": None if is_ctrl or sc["n"] < 5 else round(sc["wavg"] - ca, 2),
                "sits": sit_stats(a, hl), **sc}
    rows = sorted((row(a) for a in d["alive"]), key=lambda r: (-(r["n"] >= 5), -r["wavg"]))
    return rows + [row(d["ctrl"], True)]


def text(market: str) -> str:
    rows = board(market)
    if not rows:
        return ""
    ch = load(market).get("champion") or {}
    ctrl = next((r for r in rows if r["ctrl"]), None)
    return ("🏟️ AI 리그: " + " · ".join(("💰" if r["live"] else "") + r["name"] + f" {r['n']}건 {r['wavg']:+.2f}%" for r in rows if not r["ctrl"])
            + (f" · 🎲 대조군 {ctrl['n']}건 {ctrl['wavg']:+.2f}%" if ctrl else "")
            + (f" · 🏆 {ch['name']}" if ch.get("name") else "") + (" · 💰=실전 자격" if any(r["live"] for r in rows) else ""))


def champion_text(market: str) -> str:
    """2호 급등주 AI 판단에 붙일 한 줄 (챔피언 없으면 '')"""
    ch = load(market).get("champion") or {}
    if not ch.get("persona"):
        return ""
    return f"🏆 AI 리그 1등 전략(가상 {ch['n']}건 평균 {ch['avg']:+.2f}%) — 참고: {ch['persona']}"


# ── 매주: 성적 발표 · 실전 자격 ──
def evolve(c: dict, market: str, ai_fn=None, add_lesson=None, lessons: str = "") -> str:
    """주말 공부(토요일 아침): 전원 성적 발표 · 탈락 없음 · 대조군을 넘은 상위 arena_live_n 명에게 실전 자격·비중"""
    if not c.get("arena_on"):
        return ""
    min_n = int(c.get("arena_min_trades", 20))
    n_live = int(c.get("arena_live_n", 2))
    margin, hl = float(c.get("arena_ctrl_margin", CTRL_MARGIN)), _hl(c)
    msg = []
    with _lock:
        d = load(market, int(c.get("arena_size", 10)))
        ca = _ctrl_avg(d, c)
        alive = sorted(d["alive"], key=lambda a: -_edge(a, ca, hl))
        cn = _ctrl_n(d)
        q = [a for a in alive if _qualified(a, min_n, ca, margin, hl, cn)][:n_live]
        old = set(d.get("live") or [])
        d["live"] = [a["id"] for a in q]
        d["weights"] = _weights(q, ca, hl)
        best = q[0] if q else None
        if best:
            sb = score(best, half_life=hl)
            d["champion"] = {"name": best["name"], "persona": best["persona"], "tp": best["tp"], "sl": best["sl"],
                             "hold": best["hold"], "t": _now(), **sb}
        else:
            d["champion"] = None
        ctrl_sc = score(d["ctrl"], half_life=hl)
        _save(market, d)
    new = [a["name"] for a in q if a["id"] not in old]
    gone = [a["name"] for a in alive if a["id"] in old and a["id"] not in d["live"]]
    if q:
        msg.append("💰 실전 자격: " + ", ".join(f"{i + 1}등 {a['name']} (비중 {d['weights'].get(a['id'], 0) * 100:.0f}%)" for i, a in enumerate(q))
                   + (f" (새로: {', '.join(new)})" if new else ""))
    else:
        msg.append(f"💰 실전 자격 아직 없음 (거래 {min_n}건↑ · 평균 플러스 · 앞뒤 절반 모두 플러스 · 🎲 대조군보다 +{margin:g}%p↑ 인 AI 가 나오면)"
                   + (f" · 🎲 대조군 표본 {cn}/{CTRL_MIN_N}건이라 +{margin + CTRL_SHORT_EXTRA:g}%p↑ 요구" if cn < CTRL_MIN_N else ""))
    if gone:
        msg.append("⬇️ 가상으로: " + ", ".join(gone))
    if add_lesson and best:
        add_lesson(market, f"[AI 리그 1등] '{best['persona'][:80]}' 전략이 {score(best)['n']}건 평균 {score(best)['avg']:+.2f}% (무작위 대조군 {ca:+.2f}%)", "AI 리그")
    rows = board(market, c)   # 📣 성적 발표: 전원 순위
    rank = [f"{i + 1}. {'💰' if r['live'] else '📘'} {r['name']} — {r['n']}건 최근 평균 {r['wavg']:+.2f}% 승률 {r['win']}%"
            + (f" · 대조군 대비 {r['edge']:+.2f}%p" if r["edge"] is not None else "")
            + ("" if r["n"] >= min_n else f" (판정까지 {min_n - r['n']}건)") for i, r in enumerate(x for x in rows if not x["ctrl"])]
    ctrl_line = f"🎲 무작위 대조군 — {ctrl_sc['n']}건 최근 평균 {ctrl_sc['wavg']:+.2f}% 승률 {ctrl_sc['win']}% (이보다 나아야 실력)"
    return "🏟️ AI 리그 주간 성적 발표 (💰 실전 · 📘 가상 공부)\n" + "\n".join(rank) + "\n" + ctrl_line + "\n" + "\n".join(msg)


# ── 🗣️ AI 개개인의 의견 (공부한 뒤 사장님께 개선안) ──
RULE_BOUNDS = {"tp": (0.5, 30.0), "sl": (0.5, 20.0), "hold": (5, 390)}
MAX_HABITS = 3   # AI 마다 스스로 정한 규칙 최대 개수 (판단할 때 성격 뒤에 붙음)


def _brief(a: dict, ctrl_avg: float, hl: float, e: dict | None = None) -> str:
    """의견 쓸 때 볼 자기 성적 요약 한 덩어리 (+ 모든 판단 채점 · 걸러진 구간)"""
    cl = _card_line(e, sorted((e or {}).get("by", {}), key=lambda k: -len(e["by"][k]["yes"]))[:4]) if e else ""
    blocks = [k for k in (e or {}).get("by", {}) if blocked_key(e, [k])]
    extra = (f"\n  판단 채점: {cl}" if cl else "") + (f"\n  🚫 지금 걸러지는 구간(계속 져서 안 물어봄): {', '.join(blocks)}" if blocks else "")
    return _brief_core(a, ctrl_avg, hl) + extra


def _brief_core(a: dict, ctrl_avg: float, hl: float) -> str:
    sc = score(a, half_life=hl)
    ts = a["trades"][-30:]
    whys: dict[str, int] = {}
    for t in ts:
        whys[t.get("why", "?")] = whys.get(t.get("why", "?"), 0) + 1
    mins = round(st.fmean(t.get("mins", 0) for t in ts)) if ts else 0
    sits = " · ".join(f"{k} {v['n']}건 {v['avg']:+.2f}%" for k, v in sorted(sit_stats(a, hl).items(), key=lambda x: -x[1]["n"])[:4])
    habits = " / ".join(a.get("habits") or [])
    late = [t for t in ts if t.get("late") is True]   # ⏱️ 늦은 진입 vs 제때 진입 성적
    ontime = [t for t in ts if t.get("late") is False and t.get("spike_min") is not None]
    if late or ontime:
        sits += (" · " if sits else "") + " · ".join(f"{nm} {len(g)}건 {_avg(g):+.2f}%" for nm, g in (("늦은 진입", late), ("제때 진입", ontime)) if g)
    return (f"- {a['id']} ({a['name']}) 성격: {a['persona']}\n"
            f"  지금 규칙: 익절 {a['tp']}% 손절 {a['sl']}% 최대 {a['hold']}분" + (f" · 스스로 정한 규칙: {habits}" if habits else "") + "\n"
            f"  성적: {sc['n']}건 · 최근 평균 {sc['wavg']:+.2f}% · 승률 {sc['win']}% · 대조군 대비 {sc['wavg'] - ctrl_avg:+.2f}%p · 평균 보유 {mins}분\n"
            f"  파는 이유: {', '.join(f'{k} {v}' for k, v in whys.items()) or '없음'}" + (f"\n  상황별: {sits}" if sits else ""))


VOICE_PROMPT = """AI 리그 참가자가 방금 쌓인 자기 성적을 보고 사장님께 바로 의견을 보낼지 정해요. 참가자마다 자기 성적만 보고 따로 답하세요.
- opinion: 자기 성적에서 배운 것 + 앞으로 바꾸고 싶은 것 (2~3문장, 근거 숫자 포함, 사장님께 존댓말). 지금 딱히 할 말이 없으면 "" (빈칸이면 안 보냄)
- self: 자기 규칙을 바꾸고 싶으면 {"tp": 익절%, "sl": 손절%, "hold": 최대 보유 분} 중 바꿀 것만. 아니면 null (바로 바뀌어요 — 성적 근거가 있을 때만)
- habit: 앞으로 자기 판단에 붙일 짧은 규칙 한 줄 (40자 이내, 예: "장 초반 하락장에선 안 삼"). 없으면 ""
- engine: 자동매매 전체 설정을 바꾸자는 의견이 있으면 {"key": 설정키, "value": 값, "why": 이유} 하나. 아니면 null
  (설정키는 아래 목록에서만. 실전 켜기·모드 바꾸기는 안 됨)
- code: 앱 코드나 기능을 이렇게 바꾸면 좋겠다는 아이디어가 있으면 한 문장. 아니면 ""
거래가 5건 미만이면 opinion 을 빈칸으로 두세요. 근거 없는 제안·지난번과 같은 말은 하지 마세요.
형식: {"참가자id": {"opinion": "...", "self": null, "habit": "", "engine": null, "code": ""}, ...} JSON 한 덩어리로만."""


def opinions(c: dict, market: str, ai_fn=None, setting_keys: str = "", only: dict | None = None) -> list[dict]:
    """🗣️ 참가자마다 자기 성적을 보고 의견·개선안 (AI 1번 호출) — 실행은 안 함, 사장님 [적용] 전용
    only: {참가자id: 지금 의견을 묻는 이유} — 주면 그 참가자만"""
    hl = _hl(c)
    with _lock:
        d = load(market)
        ca = _ctrl_avg(d, c)
        alive = [dict(a) for a in d["alive"] if only is None or a["id"] in only]
        ctrl_sc = score(d["ctrl"], half_life=hl)
        idx = _grade_index(d)
    if not alive:
        return []
    notes = _notes(market)
    prompt = (VOICE_PROMPT + f"\n\n시장: {'미장' if market == 'US' else '국장'} · 🎲 무작위 대조군: {ctrl_sc['n']}건 최근 평균 {ctrl_sc['wavg']:+.2f}%\n"
              + (f"공유 노트: {notes}\n" if notes else "") + (f"바꿀 수 있는 설정키: {setting_keys}\n" if setting_keys else "")
              + "참가자:\n" + "\n".join(_brief(a, ca, hl, idx.get(a["id"]))
                                         + (f"\n  지금 묻는 이유: {only[a['id']]}" if only and only.get(a["id"]) else "")
                                         + (f"\n  지난번에 한 말: {a['last_opinion']}" if a.get("last_opinion") else "") for a in alive))
    r = (ai_fn or ai_json)(prompt)
    if not isinstance(r, dict):
        return []
    out = []
    for a in alive:
        o = r.get(a["id"])
        if not isinstance(o, dict) or not str(o.get("opinion") or "").strip():
            continue
        self_ = {}
        for k, (lo, hi) in RULE_BOUNDS.items():
            v = (o.get("self") or {}).get(k) if isinstance(o.get("self"), dict) else None
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            if lo <= v <= hi and v != float(a[k]):
                self_[k] = int(v) if k == "hold" else round(v, 1)
        eng = o.get("engine") if isinstance(o.get("engine"), dict) and o["engine"].get("key") else None
        out.append({"id": a["id"], "name": a["name"], "opinion": str(o["opinion"]).strip()[:600],
                    "self": self_, "habit": str(o.get("habit") or "").strip()[:60], "engine": eng,
                    "code": str(o.get("code") or "").strip()[:300], "stats": {**score(a, half_life=hl), "edge": round(score(a, half_life=hl)["wavg"] - ca, 2)},
                    "rules": {k: a[k] for k in RULE_BOUNDS}, "reason": (only or {}).get(a["id"], "")})
    if out:   # 지난번 말 기억 (같은 말 반복 막기)
        with _lock:
            d = load(market)
            said = {o["id"]: o["opinion"][:200] for o in out}
            for a in d["alive"]:
                if a["id"] in said:
                    a["last_opinion"] = said[a["id"]]
            _save(market, d)
    return out


# 🗣️ 의견 생길 때 바로: 거래가 닫힐 때마다 확인 → 조건 맞으면 그 AI 에게만 물어봄 (할 말 없으면 안 보냄)
VOICE_EVERY = 5        # 지난번 확인 뒤 거래가 이만큼 더 쌓이면
VOICE_STREAK = 3       # 또는 연속 손실 이만큼
VOICE_PER_DAY = 8      # 하루 전체 최대 (AI 한 명은 하루 1번)
_voice = None          # (c, market, {id: 이유}) → 뒤에서 의견 받아 채팅으로 (arena_voice 가 넣어 줌)


def voice_due(c: dict, d: dict, day: str) -> dict:
    """지금 의견을 물어볼 AI {id: 이유} — 확인 표시까지 해 둠 (같은 이유로 또 안 물음)"""
    if not (c.get("arena_on") and c.get("arena_voice", True)):
        return {}
    every = int(c.get("arena_voice_every", VOICE_EVERY))
    cap = int(c.get("arena_voice_per_day", VOICE_PER_DAY))
    vd = d.setdefault("voice_day", {})
    if vd.get("day") != day:
        vd.clear(); vd.update(day=day, n=0)
    out = {}
    for a in d["alive"]:
        if vd["n"] >= cap:
            break
        v = a.setdefault("voice", {"seen": 0, "day": ""})
        n = len(a["trades"])
        new = n - int(v.get("seen", 0))
        if new <= 0 or v.get("day") == day or n < 5:
            continue
        last = a["trades"][-VOICE_STREAK:]
        if len(last) == VOICE_STREAK and all(t["ret"] < 0 for t in last):
            why = f"방금 {VOICE_STREAK}번 연속 손실 (" + ", ".join(f"{t['ret']:+.1f}%" for t in last) + ")"
        elif new >= every:
            why = f"지난번 뒤로 거래 {new}건 더 쌓임 (지금 {n}건 · 최근 평균 {score(a)['wavg']:+.2f}%)"
        else:
            continue
        v.update(seen=n, day=day)
        vd["n"] += 1
        out[a["id"]] = why
    return out


def set_rules(market: str, ai_id: str, changes: dict | None = None, habit: str = "") -> dict:
    """AI 가 스스로 정한 개인 규칙 변경 (범위 안만 · 그 AI 판단에만 쓰임 — 공통 규칙·안전 한도와 무관)"""
    if market not in ("US", "KR"):
        return {"ok": False, "message": "시장이 이상해요"}
    clean = {}
    for k, v in (changes or {}).items():
        if k not in RULE_BOUNDS:
            return {"ok": False, "message": f"바꿀 수 없는 항목이에요: {k}"}
        try:
            v = float(v)
        except (TypeError, ValueError):
            return {"ok": False, "message": f"{k} 값이 숫자가 아니에요"}
        lo, hi = RULE_BOUNDS[k]
        if not lo <= v <= hi:
            return {"ok": False, "message": f"{k} 는 {lo}~{hi} 사이만 돼요"}
        clean[k] = int(v) if k == "hold" else round(v, 1)
    habit = str(habit or "").strip()[:60]
    if not clean and not habit:
        return {"ok": False, "message": "바꿀 내용이 없어요"}
    with _lock:
        d = load(market)
        a = next((x for x in d["alive"] if x["id"] == ai_id), None)
        if not a:
            return {"ok": False, "message": "그 AI 를 못 찾았어요 (AI 리그 목록이 바뀌었을 수 있어요)"}
        before = {k: a[k] for k in clean}
        habits_before = list(a.get("habits") or [])
        a.update(clean)
        if habit:
            a["habits"] = ([h for h in a.get("habits") or [] if h != habit] + [habit])[-MAX_HABITS:]
        t = uuid.uuid4().hex[:10]   # 되돌리기용 (같은 초에 두 번 바뀌어도 안 헷갈리게)
        a.setdefault("history", []).append({"t": _now(), "id": t, "before": before, "after": clean, "habit": habit,
                                            "habits_before": habits_before})
        a["history"] = a["history"][-20:]
        _save(market, d)
    parts = [f"{k} {before[k]}→{v}" for k, v in clean.items()] + ([f"규칙 추가 '{habit}'"] if habit else [])
    return {"ok": True, "t": t, "message": f"{a['name']}: " + " · ".join(parts)}


def undo_rules(market: str, ai_id: str, t: str) -> dict:
    """[되돌리기]: 그 변경(t)이 마지막 변경일 때만 원래대로 (그 뒤에 또 바뀌었으면 꼬이니까 안 함)"""
    with _lock:
        d = load(market)
        a = next((x for x in d["alive"] if x["id"] == ai_id), None)
        h = (a or {}).get("history") or []
        if not t or not h or h[-1].get("id") != t:
            return {"ok": False, "message": "그 뒤에 규칙이 또 바뀌어서 되돌리지 않았어요"}
        last = h.pop()
        a.update(last["before"])
        if "habits_before" in last:
            a["habits"] = last["habits_before"]
        _save(market, d)
    return {"ok": True, "message": f"{a['name']}: 원래 규칙으로 되돌렸어요"}


def overview(market: str, c: dict | None = None) -> dict:
    d = load(market)
    sv = d.get("saved") or {}   # 거름망으로 아낀 물음 (그 장 날짜 기준 · 새 날 첫 판단 때 0부터)
    return {"board": board(market, c), "champion": d.get("champion"), "text": text(market),
            "situation": _situation_now(c, market), "ctrl_p": round(_ctrl_p(d) * 100),
            "graded": len(d.get("graded") or []), "probes": len(d.get("probes") or []),
            "saved": {"day": sv.get("day", ""), "asks": int(sv.get("asks", 0)), "calls": int(sv.get("calls", 0))} if sv else {}}


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
