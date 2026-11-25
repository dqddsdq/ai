"""🗣️ 생존전 AI 개개인의 의견 → 의견이 생기면 바로 사장님 채팅으로 (모아서 보내지 않음).

- 언제: 생존전 거래가 닫힐 때마다 확인 → 그 AI 가 지난번 뒤로 거래 arena_voice_every(5)건 더 쌓였거나
  3번 연속 손실이면 그 AI 에게만 물어봄. 할 말이 없다고 하면 안 보냄.
  AI 한 명은 하루 1번 · 하루 전체 arena_voice_per_day(8)번까지 · 🤖 AI 팀 화면 [지금 의견 받기]도 있음
- 어디로: 채팅의 '🏟️ 생존전 AI 의견 (미장/국장)' 대화 하나에 계속 이어 붙음 (자동매매 팀 대화라 이어서 물어보면 답함)
- 무엇: AI 마다 메시지 하나 — 왜 지금 · 배운 것·바꾸고 싶은 것
  · 자기 규칙(익절·손절·최대 보유·스스로 정한 규칙 한 줄) — 범위 안이면 바로 바뀜 + 알림 + [되돌리기] 카드
    (그 AI 판단에만 쓰임 · 공통 규칙·안전 한도와 무관)
  · 자동매매 전체 설정 — 기존 채팅 [적용] 카드와 같은 검사 (실전 켜기·모드 바꾸기는 안 됨)
  · 코드 개선 아이디어 — 🛠️ 개선 카드 (하루 MAX_CODE_PER_DAY 개까지 · 사장님이 만들기·적용을 눌러야 함)
- 공통 설정·코드는 사장님이 [적용]을 눌러야 바뀜
기록: _cache/arena_voice.json (시장별 대화 id · 오늘 코드 카드 수)
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from . import arena, store
from .agents import COMPANY_DIR

KST = timezone(timedelta(hours=9))
MAX_CODE_PER_DAY = 3   # 하루 🛠️ 개선 카드 최대 (나머지는 글로만)
_lock = threading.Lock()
_running = threading.Event()


def _file():
    return COMPANY_DIR / "_cache" / "arena_voice.json"


def _setting_keys() -> str:
    from . import autotrade as at
    skip = ("mode", "market", "arena_live")
    return ", ".join(k for k in at.LABELS if k not in skip and not (k.startswith("lane_") and k.endswith("_mode")))


def _message(market: str, o: dict) -> tuple[str, list[dict], str]:
    """(본문, [적용] 카드들, 코드 아이디어)"""
    from . import autotrade as at
    s = o["stats"]
    flag = "🇺🇸" if market == "US" else "🇰🇷"
    head = (f"**🏟️ {o['name']}** {flag} · {s['n']}건 · 최근 평균 {s['wavg']:+.2f}% · 승률 {s['win']}%"
            + (f" · 대조군 대비 {s['edge']:+.2f}%p" if s["n"] >= 5 else ""))
    body = [head] + ([f"_왜 지금: {o['reason']}_"] if o.get("reason") else []) + ["", o["opinion"]]
    cards = []
    stamp = datetime.now(KST).strftime("%H%M%S")
    if o["self"] or o["habit"]:   # 자기 규칙은 범위 안이면 바로 바꾸고 [되돌리기]만 남김
        names = {"tp": "익절", "sl": "손절", "hold": "최대 보유(분)"}
        parts = [f"{names[k]} {o['rules'][k]:g}→{v:g}" for k, v in o["self"].items()] + ([f"규칙 추가 '{o['habit']}'"] if o["habit"] else [])
        r = arena.set_rules(market, o["id"], o["self"], o["habit"])
        if r.get("ok"):
            o["changed"] = " · ".join(parts)
            body += ["", f"✅ 스스로 바꿈: {o['changed']}"]
            cards.append({"id": f"au{o['id'][-6:]}{stamp}", "type": "arena_undo", "market": market, "ai": o["id"], "t": r["t"],
                          "label": f"↩️ {o['name']} 규칙 되돌리기 ({o['changed']})",
                          "why": "이 AI 판단에만 쓰는 규칙이에요. 공통 규칙·안전 한도는 그대로예요."})
        else:
            body += ["", f"⚠️ 규칙을 바꾸려 했지만 안 바꿨어요: {r.get('message', '')}"]
    eng = o.get("engine")
    if eng:
        tag = f"[[TRADE_SET: {eng.get('key')}={eng.get('value')} | {o['name']}: {str(eng.get('why') or '')[:120]}]]"
        with at.in_market(market):
            cards += at.find_actions(tag)   # 기존 검사 그대로 (모르는 키·실전 켜기는 카드 안 만듦)
    return "\n".join(body), cards, o.get("code") or ""


def _conv(market: str, meta: dict) -> dict:
    """시장별로 계속 이어 쓰는 의견 대화 (지워졌으면 새로)"""
    from . import agents as ag
    from . import web
    cid = (meta.get("conv") or {}).get(market)
    if cid:
        try:
            return web.load_conv(cid)
        except (OSError, ValueError, KeyError):
            pass
    flag = "미장" if market == "US" else "국장"
    conv = web.new_conv(f"🏟️ 생존전 AI 의견 ({flag})")
    conv["id"] += f"-arena{market}"   # 같은 초에 두 시장이 만들어도 안 겹치게
    conv["agent"] = ag.TRADER
    conv["messages"].append({"role": "assistant", "agent": ag.TRADER, "model": "", "sources": [], "drafts": [], "steps": [],
                             "content": ("생존전 AI 들이 의견이 생기면 여기로 바로 보내요. "
                                         "AI 자기 규칙은 바로 바뀌고 [되돌리기] 카드가 붙어요. 공통 설정은 [적용]을 눌러야 바뀌어요. "
                                         "궁금한 건 여기서 이어서 물어보세요.")})
    meta.setdefault("conv", {})[market] = conv["id"]
    return conv


def post(market: str, o: dict) -> dict:
    """의견 하나를 바로 채팅에 붙임 (AI 메시지 하나 + [적용] 카드)"""
    from . import agents as ag
    from . import improve as im
    from . import web
    text, cards, code = _message(market, o)
    with _lock:
        meta = store.load(_file(), lambda: {})
        today = datetime.now(KST).strftime("%Y-%m-%d")
        if meta.get("code_day") != today:
            meta.update(code_day=today, code_n=0)
        conv = _conv(market, meta)
        m = {"role": "assistant", "agent": ag.TRADER, "model": "", "sources": [], "drafts": [], "steps": [], "content": text}
        if cards:
            m["trade_actions"] = cards
        if code:
            if int(meta.get("code_n", 0)) < MAX_CODE_PER_DAY:
                job = im.create(code, f"생존전 '{o['name']}' 의견 ({o['stats']['n']}건 · 최근 평균 {o['stats']['wavg']:+.2f}%)", ag.TRADER)
                m["improvements"] = [job["id"]]; meta["code_n"] = int(meta.get("code_n", 0)) + 1
            else:
                m["content"] += f"\n\n🛠️ 코드 아이디어: {code}"
        with web.CONV_LOCK:   # 채팅 답변 저장과 겹치지 않게 · 붙이기 직전에 다시 읽기
            try:
                conv["messages"] = web.load_conv(conv["id"])["messages"]
            except (OSError, ValueError, KeyError):
                pass
            conv["messages"].append(m)
            web.save_conv(conv)
        store.save(_file(), meta)
    try:
        from . import autotrade as at
        with at.in_market(market):
            at.event("생존전", (f"🔧 {o['name']} 규칙 바꿈: {o['changed']}" if o.get("changed") else f"🗣️ {o['name']} 의견: {o['opinion'][:80]}")
                     + " — 채팅 '🏟️ 생존전 AI 의견'에서 되돌릴 수 있어요")
    except Exception:
        pass
    return {"ok": True, "conversation_id": conv["id"], "cards": len(cards)}


def ask(c: dict, market: str, due: dict, ai_fn=None) -> int:
    """{참가자id: 이유} 에게 물어보고, 할 말 있는 AI 것만 하나씩 바로 채팅으로. 보낸 수"""
    try:
        ops = arena.opinions(c, market, ai_fn, _setting_keys(), only=due)
    except Exception:
        return 0
    n = 0
    for o in ops:
        try:
            post(market, o); n += 1
        except Exception:
            pass
    return n


def ask_bg(c: dict, market: str, due: dict) -> None:
    """생존전 엔진이 부름: 매매를 안 막게 뒤에서"""
    threading.Thread(target=ask, args=(c, market, due), daemon=True, name="arena-voice").start()


arena._voice = ask_bg


def run_bg(c: dict, markets: list[str]) -> dict:
    """화면 버튼용: 거래 5건↑인 AI 전원에게 지금 물어봄 (할 말 있는 AI 것만 하나씩 바로 옴)"""
    if _running.is_set():
        return {"ok": False, "message": "이미 의견을 묻는 중이에요"}
    _running.set()

    def go():
        try:
            for m in markets:
                due = {a["id"]: "사장님이 지금 의견을 물어봤어요" for a in arena.load(m)["alive"] if len(a["trades"]) >= 5}
                if due:
                    ask(c, m, due)
        finally:
            _running.clear()
    threading.Thread(target=go, daemon=True, name="arena-voice").start()
    return {"ok": True, "message": "AI 들에게 묻는 중이에요. 할 말 있는 AI 의견이 1~2분 안에 채팅 '🏟️ 생존전 AI 의견' 으로 하나씩 와요"}
