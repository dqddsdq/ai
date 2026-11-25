"""🗣️ 채팅에서 AI 를 지정해 직접 묻기 — '1호 …', '2호 …', '@눌림목 …', 'AI 리그 돌파 …', '다들 …'(리그 전원이 각자)

- 그 AI 의 지시문(성향·스스로 정한 규칙) + 최근 주문·일지·교훈을 근거로 그 AI 이름으로 답함
- 읽기 전용: 주문·설정·모드는 못 바꿈 (답에서 [적용] 카드·개선 제안·도구 요청을 안 만듦)
- 하루 호출 상한 ASK_PER_DAY — AI 리그 판단 한도(arena_ai_per_day)와 따로 셈 (_cache/ai_ask.json)
- 거래 0건 리그 AI 는 처음 물을 때 '📥 지금 쓰는 자료 / 🧰 더 필요한 도구' 를 한 번 덧붙임 (_cache/ai_ask_tools.json)
- 사장님 의견: 메시지에 '의견: …' 또는 '참고: …' 가 있으면 공유 노트에 '참고 의견(검증 전)' 으로만 기록
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta, timezone

from . import store
from .agents import COMPANY_DIR

KST = timezone(timedelta(hours=9))
ASK_PER_DAY = 30   # 하루 AI 지정 질문 최대 (AI 리그 판단 한도와 별도)
_lock = threading.Lock()

_SEP = r"(?:\s*AI)?\s*(?:님|에게|한테|야|아)?\s*[,:：]?\s*"
# 부르는 말투일 때만 (@1호 · 1호야 · 2호에게 · 1호, · 2호:) — '2호 예산 몫 늘려줘' 같은 설정 말은 기존 자동매매 팀 채팅으로
LANE_RE = re.compile(r"^\s*(@\s*)?([12])\s*호(?:\s*(?:일반|급등주))?(?:\s*\((?:스윙|단타)\))?(?:\s*AI)?"
                     r"\s*(님|에게|한테|야|아)?\s*([,:：])?\s*(?=\S)")
# '다들 …' · '모두 …' · 'AI 리그 다들 …' → 리그 AI 전원이 각자 자기 이름으로 답함 (AI 호출은 한 번에 묶어서)
# '모두 팔아줘'·'전원 꺼' 같은 매매 지시와 안 헷갈리게: 모두·전원은 '리그' 를 붙였을 때만
ALL_RE = re.compile(r"^\s*(?:🏟️\s*)?(?:(?:(?:AI\s*)?리그|생존전)\s*(?:AI\s*)?(?:다들|모두|전원|여러분|AI\s*들)|(?:다들|여러분|AI\s*들))"
                    r"\s*[,:：]?\s*(?=\S)")
OPINION_RE = re.compile(r"(?:^|\s)(?:내\s*)?(?:의견|참고)\s*[:：]\s*(.+)", re.S)
READ_ONLY = ("[읽기 전용 대화] 너는 주문·설정·모드를 바꿀 수 없다. [[TRADE_…]]·[[IMPROVE…]]·도구 요청 같은 태그를 쓰지 마라. "
             "바꾸고 싶은 게 있으면 '사장님이 자동매매 팀 채팅이나 화면에서 바꿀 수 있다'고만 말해라. "
             "숫자는 아래 기록에 있는 것만 쓰고 지어내지 마라. 기록에 없으면 모른다고 해라. 사장님께 존댓말, 짧고 솔직하게.")
# 리그 AI 가 살지 판단할 때 실제로 받는 자료 (arena._decide 프롬프트와 맞춤)
DECIDE_INPUTS = ("종목·현재가 · 구간(장 상황·오늘 상승률·거래량 폭발 뒤 경과 분) · 후보 고른 이유(상승률·거래량 숫자만) · "
                 "학습 추정 승률·기대 수익 · 공유 노트 · 자기 지난 성적. 뉴스·공시 본문·차트·호가·재무는 안 받음")


def _file():
    return COMPANY_DIR / "_cache" / "ai_ask.json"


def _tools_file():
    return COMPANY_DIR / "_cache" / "ai_ask_tools.json"   # 거래 0건 AI 가 자료·도구 답을 이미 한 기록


def _tools_due(m: str) -> dict:
    """거래 0건이고 아직 자료·도구 답을 안 한 리그 AI {id: 이름}"""
    from . import arena
    done = store.load(_tools_file(), lambda: {})
    return {a["id"]: a["name"] for a in arena.load(m)["alive"] if not a.get("trades") and f"{m}:{a['id']}" not in done}


def _tools_ask(names: list[str]) -> str:
    return ("\n[거래 0건 AI: " + ", ".join(names) + "] 이 AI 는 아직 한 번도 안 샀다. 답 끝에 꼭 두 줄을 붙여라: "
            "'📥 지금 쓰는 자료: …' (판단 때 받는 자료는 이것뿐: " + DECIDE_INPUTS + ") / "
            "'🧰 더 필요한 도구: …' (내 성격대로 사려면 없어서 못 쓰는 자료·도구. 자료가 아니라 기준 문제면 그렇다고, 없으면 '없음'). "
            "도구 요청 태그는 쓰지 말고 글로만.")


def mark_tools(who: dict, answered: list[str]) -> None:
    """자료·도구 답을 한 AI 는 다시 안 물음 (한 번만)"""
    due = who.get("tools_due") or {}
    if not due:
        return
    with _lock:
        d = store.load(_tools_file(), lambda: {})
        for m, ids in due.items():
            for aid, name in ids.items():
                if name in answered:
                    d[f"{m}:{aid}"] = _today()
        store.save(_tools_file(), d)


def _today() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def _mk_hint(text: str) -> str:
    if re.search(r"미장|미국|\bUS\b", text, re.I):
        return "US"
    if re.search(r"국장|한국|\bKR\b", text, re.I):
        return "KR"
    return ""


def _mk_name(m: str) -> str:
    return "미장" if m == "US" else "국장"


def match(text: str) -> dict | None:
    """메시지가 특정 AI 를 부르면 {kind, name, markets, ...} · 아니면 None"""
    from . import autotrade as at
    hint = _mk_hint(text)
    mks = [m for m in at.markets() if not hint or m == hint] or ([hint] if hint else at.markets())
    m = LANE_RE.match(text)
    if m and (m.group(1) or m.group(3) or m.group(4)):
        style = "swing" if m.group(2) == "1" else "surge"
        return {"kind": "lane", "style": style, "name": at.LANES[style], "markets": mks, "question": text[m.end():].strip()}
    m = ALL_RE.match(text)
    if m:
        return everyone(mks[0], text[m.end():].strip())
    try:
        from . import arena
        cands = []
        for mk in mks:
            for a in arena.load(mk)["alive"]:
                cands.append((a["name"], a["id"], mk))
    except Exception:
        return None
    for name, aid, mk in sorted(cands, key=lambda x: -len(x[0])):   # 긴 이름 먼저 (겹침 방지)
        n = re.escape(name)
        # '@이름' · 'AI 리그 이름' 은 바로, 그냥 '이름' 은 뒤에 님/에게/,/: 가 붙을 때만 (평범한 말과 안 헷갈리게)
        r = (re.match(r"^\s*(?:@\s*|(?:🏟️\s*)?(?:생존전|(?:AI\s*)?리그)\s*(?:AI\s*)?)" + n + _SEP + r"(?=\S|$)", text)   # '@속공' 만 불러도 됨
             or re.match(r"^\s*" + n + r"(?:\s*AI)?\s*(?:님|에게|한테)?\s*[,:：]\s*(?=\S)", text)
             or re.match(r"^\s*" + n + r"(?:\s*AI)?\s*(?:님|에게|한테)\s+(?=\S)", text))
        if r:   # 같은 이름이 두 시장에 다 있으면 둘 다 기록을 봄
            ids = {x: i for nm, i, x in cands if nm == name}
            return {"kind": "arena", "id": aid, "name": name, "markets": list(ids), "ids": ids, "question": text[r.end():].strip()}
    return None


def everyone(market: str, question: str = "") -> dict:
    """리그 AI 전원에게 묻기 (리그 의견 대화방에서 이름 없이 물어도 이걸로)"""
    return {"kind": "all", "name": "AI 리그 전원", "markets": [market], "question": question}


def split_answers(reply: str, market: str) -> list[tuple[str, str]]:
    """전원 답(JSON {이름: 답})을 AI 별로 · 못 읽으면 []"""
    from . import arena
    m = re.search(r"\{.*\}", reply or "", re.S)
    try:
        d = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        return []
    names = [a["name"] for a in arena.load(market)["alive"]]
    return [(n, str(d[n]).strip()) for n in names if isinstance(d, dict) and str(d.get(n) or "").strip()]


def take() -> tuple[bool, int]:
    """하루 한도 안이면 1 차감 → (가능?, 오늘 쓴 수)"""
    with _lock:
        d = store.load(_file(), lambda: {})
        if d.get("day") != _today():
            d = {"day": _today(), "n": 0}
        if int(d.get("n", 0)) >= ASK_PER_DAY:
            return False, int(d["n"])
        d["n"] = int(d.get("n", 0)) + 1
        store.save(_file(), d)
        return True, d["n"]


def refund() -> None:
    """답을 못 받았으면 되돌림 (실패는 한도로 안 셈)"""
    with _lock:
        d = store.load(_file(), lambda: {})
        if d.get("day") == _today() and int(d.get("n", 0)) > 0:
            d["n"] = int(d["n"]) - 1
            store.save(_file(), d)


def _lane_block(style: str, m: str) -> str:
    from . import autotrade as at
    from . import tradelearn as tl
    with at.in_market(m):
        c, s = at.config(), at.state()
        lv = at.lanes_overview(m).get(style, {})
        with tl._lock:
            ts = [t for t in tl._load()["trades"] if tl._tm(t) == m and tl._ts(t) == style][-10:]
        syms = {t["sym"] for t in ts} | {k for k, v in (s.get("style") or {}).items() if v == style}
        ords = [o for o in s.get("orders", []) if (o.get("features") or {}).get("style") == style
                or (o.get("side") != "BUY" and o.get("symbol") in syms)][-10:]
        lines = [f"[{_mk_name(m)}] 모드 {lv.get('mode', '?')}({'실전' if lv.get('live') else '가상'}) · 예산 몫 {lv.get('share', '?')}% · "
                 f"최근 {lv.get('days', 14)}일 {lv.get('n', 0)}건" + (f" 승률 {lv.get('win_rate')}% 평균 {lv.get('avg', 0):+.2f}%" if lv.get("n") else ""),
                 "지시문(성향+공유 노트): " + at.lane_persona(style, m)]
        lines.append("최근 주문 (이유 포함):" if ords else "최근 주문: 없음")
        lines += [f"- {o['t']} {o['symbol']} {'매수' if o['side'] == 'BUY' else '매도'} {o['qty']}주 @ {o['price']:g} · "
                  f"{'실전' if o.get('mode') == 'live' else '가상'} · {o.get('status', '')} · {str(o.get('reason', ''))[:160]}" for o in ords]
        lines.append("최근 일지:" if ts else "최근 일지: 없음")
        for t in ts[::-1]:
            ex = (t.get("exits") or [{}])[-1]
            lines.append(f"- {t['entry_time'][5:16]} {t['sym']} {'실전' if t['mode'] == 'live' else '가상'} "
                         + (f"{t.get('pnl_pct', 0):+.2f}% ({t.get('exit_kind', '')}: {str(ex.get('reason', ''))[:80]})" if t["closed"] else "보유 중")
                         + f" · 최대 {t['mfe']:+.1f}% 최저 {t['mae']:+.1f}% · " + at._entry_text(t.get("features") or {}))
        if c.get("mode") == "off":
            lines.append("(지금 자동매매 전체 모드: 꺼짐)")
    return "\n".join(lines)


def _arena_block(aid: str, m: str) -> str:
    from . import arena
    from . import autotrade as at
    with at.in_market(m):
        c = at.config()
    hl = arena._hl(c)
    with arena._lock:
        d = arena.load(m)
        a = next((dict(x) for x in d["alive"] if x["id"] == aid), None)
        ca = arena._ctrl_avg(d, c)
        idx = arena._grade_index(d)
    if not a:
        return f"[{_mk_name(m)}] 이 AI 를 AI 리그 목록에서 못 찾았어요."
    lines = [f"[{_mk_name(m)} AI 리그 · 가상 전용 · {a.get('gen', 1)}세대]", "지시문·성적:\n" + arena._brief(a, ca, hl, idx.get(aid))]
    if a.get("pos"):
        lines.append("지금 가상 보유: " + ", ".join(f"{k} @ {v.get('px')}" for k, v in a["pos"].items()))
    ts = a["trades"][-10:]
    lines.append("최근 가상 거래:" if ts else "최근 가상 거래: 없음")
    lines += [f"- {t.get('t', '')[5:16]} {t['sym']} {t['ret']:+.2f}% · {t.get('why', '')} · {t.get('mins', 0)}분 · {t.get('sit', '')}" for t in ts[::-1]]
    if a.get("last_opinion"):
        lines.append(f"지난번 사장님께 한 말: {a['last_opinion']}")
    for h in (a.get("history") or [])[-3:]:
        lines.append(f"규칙 변경 기록 {h.get('t', '')[5:16]}: {h.get('before')} → {h.get('after')} {h.get('habit', '')}")
    try:
        lines.append("공유 노트: " + arena._notes(m))
    except Exception:
        pass
    return "\n".join(lines)


def system_prompt(who: dict) -> str:
    """그 AI 이름으로 답할 때 쓸 지시문 + 근거 기록"""
    if who["kind"] == "all":
        from . import arena
        m = who["markets"][0]
        names = [a["name"] for a in arena.load(m)["alive"]]
        head = ("너는 AI 리그 참가자 전원을 한꺼번에 연기한다 (모두 가상 매매만 함). 사장님이 전원에게 물었다. "
                "참가자마다 자기 성격·규칙·성적으로 따로 2~4문장 답해라. 서로 의견이 다르면 다르게 답해라. "
                f"형식: JSON 한 덩어리 {{\"참가자이름\": \"답\", ...}} 로만. 이름은 정확히: {', '.join(names)}")
        try:   # 거래 0건 AI 는 지금 쓰는 자료·더 필요한 도구도 한 번 답함
            due = _tools_due(m)
            if due:
                who["tools_due"] = {m: due}
                head += _tools_ask(list(due.values()))
        except Exception:
            pass
        body = "\n\n".join(_arena_block(a["id"], m)[:1500] for a in arena.load(m)["alive"])
        return head + "\n" + READ_ONLY + "\n\n<record>\n" + body[:20000] + "\n</record>"
    if who["kind"] == "lane":
        head = f"너는 이 앱 자동매매의 '{who['name']}' AI 다. 사장님이 너에게 직접 물었다. 네 판단(왜 샀고 왜 팔았는지)을 아래 기록으로 설명해라."
        body = "\n\n".join(_lane_block(who["style"], m) for m in who["markets"])
    else:
        head = (f"너는 AI 리그 참가자 '{who['name']}' 다 (가상 매매만 함). 사장님이 너에게 직접 물었다. "
                "네 성격·규칙·성적으로 답해라.")
        try:   # 거래 0건이면 자료·도구도 한 번 답함
            for m in who["markets"]:
                aid = (who.get("ids") or {}).get(m, who["id"])
                if aid in _tools_due(m):
                    who.setdefault("tools_due", {})[m] = {aid: who["name"]}
            if who.get("tools_due"):
                head += _tools_ask([who["name"]])
        except Exception:
            pass
        body = "\n\n".join(_arena_block((who.get("ids") or {}).get(m, who["id"]), m) for m in who["markets"])
    return head + "\n" + READ_ONLY + "\n\n<record>\n" + body[:9000] + "\n</record>"


def save_opinion(who: dict, text: str) -> str:
    """'의견: …' / '참고: …' 가 있으면 공유 노트에 참고 의견으로 (시장마다) → 기록한 문구 · 없으면 ''"""
    m = OPINION_RE.search(text)
    if not m or not m.group(1).strip():
        return ""
    from . import tradelearn as tl
    line = ""
    for mk in who["markets"]:
        line = tl.add_owner_note(mk, m.group(1), who["name"])
    return line
