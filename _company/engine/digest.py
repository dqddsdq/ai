"""📋 제안 현황 — 흩어진 제안·적용 기록을 한곳에 (읽기 전용 · 매매 판단·주문·공유 노트와 무관).

- 대기 중: 채팅 [적용] 카드(생존전 의견 포함) · 🛠️ 개선 카드 · 📚 학습 조정 제안
- 최근 적용(24시간): 설정 변경 기록(규칙 로그) · 생존전 AI 자기 규칙 변경 · 🛠️ 코드 개선 적용
- 적용 뒤 성적: 최근 7일 설정 변경마다 바뀌기 전 7일 vs 바뀐 뒤 끝난 거래 (건수·평균)
- 언제: 장 마감 뒤 복기 끝에 하루 한 번 텔레그램 (설정 daily_digest) · 🤖 학습·기록 화면 '📋 제안 현황' 은 언제든
"""

from __future__ import annotations

import statistics as st
from datetime import datetime, timedelta, timezone

from .agents import COMPANY_DIR

KST = timezone(timedelta(hours=9))
SENT = COMPANY_DIR / "_cache" / "digest_sent.json"   # 시장별 마지막으로 보낸 날
CARD_DAYS = 14     # 이보다 오래된 대화의 대기 카드는 안 셈 (잊힌 카드)
EFFECT_DAYS = 7


def _ago(hours: float) -> str:
    return (datetime.now(KST) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def _pending_cards() -> list[dict]:
    from . import web
    since = (datetime.now() - timedelta(days=CARD_DAYS)).isoformat(timespec="seconds")
    out = []
    for c in web.list_convs():
        if c.get("updated", "") < since:
            continue
        try:
            conv = web.load_conv(c["id"])
        except (OSError, ValueError, KeyError):
            continue
        for m in conv["messages"]:
            for a in m.get("trade_actions") or []:
                if not a.get("status"):
                    out.append({"label": a.get("label") or a.get("type", ""), "where": conv.get("title", ""), "conv": conv["id"]})
    return out


def collect(market: str) -> dict:
    from . import arena, extras, improve as im, tradelearn as tl
    d: dict = {"pending": {}, "applied": [], "effects": []}
    try:
        d["pending"]["cards"] = _pending_cards()
    except Exception:
        d["pending"]["cards"] = []
    try:
        d["pending"]["code"] = [{"label": j["request"][:80], "status": j["status"]} for j in im.history()
                                if j.get("status") in ("proposed", "ready", "conflict", "failed")][:20]
    except Exception:
        d["pending"]["code"] = []
    try:
        with tl._lock:
            props = list(tl._load()["proposals"])
        d["pending"]["learn"] = [{"label": f"{p['label']} {p['from']} → {p['to']}", "why": p.get("why", "")[:80]} for p in props if p["status"] == "pending"]
    except Exception:
        props, d["pending"]["learn"] = [], []
    since = _ago(24)
    changes: list[tuple[str, str]] = []   # (시각 YYYY-MM-DD HH:MM:SS, 무엇) — 적용 뒤 성적 계산용
    try:   # 자동 조정·되돌림 (규칙 로그)
        for x in extras._rlog()["items"]:
            what = ("되돌림 " if x.get("revert") else "자동 ") + ", ".join(f"{k} {x['before'].get(k)}→{v}" for k, v in x["after"].items())
            changes.append((x.get("t", ""), what))
    except Exception:
        pass
    try:   # 사장님이 채팅 카드로 적용한 것 (활동 기록 '채팅 지시')
        from . import autotrade as at
        year = datetime.now(KST).year
        with at.in_market(market):
            evs = list(at.state().get("events") or [])
        for e in evs:
            if e.get("kind") == "채팅 지시":
                changes.append((f"{year}-{e['t']}", f"사장님 적용: {e['text'][:60]}"))
    except Exception:
        pass
    try:   # 📚 학습 조정 제안 적용
        for p in props:
            if p.get("status") == "applied" and p.get("applied"):
                changes.append((p["applied"], f"{'자동' if p.get('auto') else '사장님'} 학습 제안: {p['label']} {p.get('prev', p['from'])}→{p['to']}"))
    except Exception:
        pass
    changes.sort()
    d["applied"] += [f"{w} ({t[5:16]})" for t, w in changes if t >= since]
    try:
        for a in arena.load(market)["alive"]:
            for h in a.get("history") or []:
                if h.get("t", "") >= since:
                    d["applied"].append(f"🏟️ {a['name']} 자기 규칙: " + ", ".join(f"{k} {h['before'].get(k)}→{v}" for k, v in h["after"].items())
                                        + (f" · '{h['habit']}'" if h.get("habit") else "") + " (가상 전용)")
    except Exception:
        pass
    try:
        for j in im.history():
            if j.get("status") == "applied" and str(j.get("applied") or "") >= (datetime.now() - timedelta(hours=24)).isoformat(timespec="seconds"):
                d["applied"].append(f"🛠️ 코드 개선: {j['request'][:60]}")
    except Exception:
        pass
    try:   # 적용 뒤 성적 (최근 7일 설정 변경)
        with tl._lock:
            ts = [t for t in tl._load()["trades"] if t.get("closed") and t.get("exits") and tl._tm(t) == market]
        end = lambda t: str(t["exits"][-1].get("t", ""))
        for t0, what in changes:
            if t0 < _ago(24 * EFFECT_DAYS) or "생존전" in what:   # 생존전 자기 규칙은 가상 전용이라 실전 성적과 무관
                continue
            try:
                pre_from = (datetime.strptime(t0, "%Y-%m-%d %H:%M:%S") - timedelta(days=EFFECT_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            pre = [float(t.get("pnl_pct") or 0) for t in ts if pre_from <= end(t) < t0]
            post = [float(t.get("pnl_pct") or 0) for t in ts if end(t) >= t0]
            d["effects"].append({"what": what, "t": t0[5:16],
                                 "pre_n": len(pre), "pre_avg": round(st.mean(pre), 2) if pre else None,
                                 "post_n": len(post), "post_avg": round(st.mean(post), 2) if post else None})
    except Exception:
        pass
    return d


def text(market: str, d: dict | None = None) -> str:
    d = d or collect(market)
    flag = "🇺🇸 미장" if market == "US" else "🇰🇷 국장"
    p = d["pending"]
    n_wait = len(p.get("cards", [])) + len(p.get("code", [])) + len(p.get("learn", []))
    lines = [f"📋 {flag} 제안 현황 — 대기 {n_wait}개 · 최근 24시간 적용 {len(d['applied'])}개"]
    if p.get("cards"):
        groups: dict[tuple, int] = {}   # 같은 대화의 같은 카드는 묶어서 (×3)
        for x in p["cards"]:
            groups[(x["label"], x["where"])] = groups.get((x["label"], x["where"]), 0) + 1
        lines.append(f"\n⏳ 채팅 [적용] 카드 대기 {len(p['cards'])}개")
        lines += [f"- {lb[:70]}" + (f" ×{n}" if n > 1 else "") + f" ({wh[:24]})" for (lb, wh), n in list(groups.items())[:8]]
        lines += [f"- …외 {len(groups) - 8}종류"] if len(groups) > 8 else []
    if p.get("code"):
        lines.append(f"\n🛠️ 코드 개선 대기 {len(p['code'])}개 (화면 🛠️ 개선)")
        lines += [f"- [{x['status']}] {x['label']}" for x in p["code"][:5]]
    if p.get("learn"):
        lines.append(f"\n📚 학습 조정 제안 대기 {len(p['learn'])}개")
        lines += [f"- {x['label']} — {x['why']}" for x in p["learn"][:5]]
    if d["applied"]:
        lines.append("\n✅ 최근 24시간 적용")
        lines += [f"- {x}" for x in d["applied"][:10]]
    eff = [e for e in d["effects"] if e["post_n"]]
    if eff:
        lines.append(f"\n📈 바꾼 뒤 성적 (최근 {EFFECT_DAYS}일 변경 · 끝난 거래 평균)")
        for e in eff[:6]:
            pre = f"{e['pre_avg']:+.2f}% ({e['pre_n']}건)" if e["pre_n"] else "기록 없음"
            lines.append(f"- {e['t']} {e['what'][:50]}: 전 {pre} → 후 {e['post_avg']:+.2f}% ({e['post_n']}건)"
                         + (" · 표본 적음" if e["post_n"] < 8 else ""))
    if n_wait == 0 and not d["applied"] and not eff:
        lines.append("대기 중인 제안도, 최근 바뀐 것도 없어요.")
    return "\n".join(lines)


def send(market: str) -> str:
    """장 마감 뒤 하루 한 번 텔레그램 (같은 날 두 번 안 보냄)"""
    from . import notify, store
    f = SENT
    day = datetime.now(KST).strftime("%Y-%m-%d")
    sent = store.load(f, lambda: {})
    if sent.get(market) == day:
        return ""
    t = text(market)
    notify.send(t[:3500])
    sent[market] = day
    store.save(f, sent)
    return t
