"""🔔 텔레그램 알림·명령 — 폰으로 체결·손절·정지·오류 알림 받고, /상태 /정지 /가상 /끄기 로 조작.

설정 (🧰 도구): TELEGRAM_BOT_TOKEN (텔레그램 @BotFather 에서 /newbot → 받은 토큰)
- 토큰만 넣고 봇에게 아무 말이나 한 번 보내면, 그 대화를 '사장님 채팅'으로 등록 (.env 의 TELEGRAM_CHAT_ID)
- 등록된 채팅에서 온 명령만 받음 · 실전 켜기는 텔레그램으로 불가 (화면에서 '실전' 입력만)
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import urllib.parse
import urllib.request

API = os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org") + "/bot{token}/{method}"
_q: "queue.Queue[str]" = queue.Queue(maxsize=200)
_started = threading.Event()
_reg_until = [0.0]
_reg_who = [""]   # 채팅 등록을 받는 시각 (PC 화면에서 '텔레그램 시험'을 누른 뒤 10분만 — 남이 먼저 말 걸어 등록되는 것 방지)
_last_err = [""]
HELP = ("🤖 1인 기업 AI 자동매매\n/상태 — 지금 상태·보유·오늘 손익\n/정지 — 긴급 정지 (꺼짐 + 자동매매 미체결 취소)\n"
        "/가상 — 가상 매매로 전환\n/끄기 — 자동매매 끄기\n(실전 켜기는 PC 화면에서만)")


def token() -> str:
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def chat_id() -> str:
    return os.getenv("TELEGRAM_CHAT_ID", "").strip()


def enabled() -> bool:
    return bool(token())


def _call(method: str, timeout: int = 30, **params) -> dict:
    url = API.format(token=token(), method=method)
    data = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except Exception as e:   # 주소에 토큰이 들어 있어서 메시지에 섞이지 않게
        _last_err[0] = type(e).__name__
        return {"ok": False, "description": type(e).__name__}


def _call_updates(offset) -> dict:
    """긴 대기(25초)로 새 메시지 받기"""
    url = API.format(token=token(), method="getUpdates")
    data = urllib.parse.urlencode({k: v for k, v in {"timeout": 25, "offset": offset}.items() if v is not None}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=40) as r:
            return json.loads(r.read() or b"{}")
    except Exception as e:
        _last_err[0] = type(e).__name__
        return {"ok": False}


def send(text: str) -> None:
    """알림 보내기 (줄 세워서 따로 보냄 — 자동매매를 기다리게 하지 않음)"""
    if not enabled() or not chat_id():
        return
    try:
        _q.put_nowait(text[:3500])
    except queue.Full:
        pass


def _save_chat(cid: str) -> None:
    from . import tools
    os.environ["TELEGRAM_CHAT_ID"] = cid
    try:
        lines = tools.ENV_FILE.read_text(encoding="utf-8").splitlines() if tools.ENV_FILE.exists() else []
        lines = [l for l in lines if not l.startswith("TELEGRAM_CHAT_ID=")] + [f"TELEGRAM_CHAT_ID={cid}"]
        tools.ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def _status_text() -> str:
    from . import autotrade as at
    c = at.config()
    lines = [f"모드: {dict(off='꺼짐', paper='가상 매매', live='🔴 실전')[c['mode']]} · {at.STRAT_NAME[c['strategy']]}"]
    for m in c["markets"]:
        with at.in_market(m):
            s = at.state()
            pos = s["bot_pos"] if c["mode"] != "paper" else s["paper"]["positions"]
            b = at._brief(m)
            lines.append(f"\n{'🇺🇸 미장' if m == 'US' else '🇰🇷 국장'} 현지 {b['local_time']} · "
                         + ("장 닫힘" if b["minutes"] is None else "장중")
                         + f" · 오늘 {b['realized_today']:+,}원" + (f" · ⛔ {b['halted'][:40]}" if b["halted"] else ""))
            for k, v in pos.items():
                lines.append(f"  {k} {v['qty']:g}주 평단 {v['avg']:g}")
    return "\n".join(lines)


def _handle(text: str) -> str:
    from . import autotrade as at
    t = text.strip().split()[0].lower() if text.strip() else ""
    if t in ("/상태", "/status", "상태"):
        return _status_text()
    if t in ("/정지", "/stop", "정지"):
        return "⛔ " + at.emergency_stop()["message"]
    if t in ("/가상", "/paper", "가상"):
        r = at.save_config({"mode": "paper"})
        return "📝 가상 매매로 바꿨어요" if r.get("ok") else r.get("message", "실패")
    if t in ("/끄기", "/off", "끄기"):
        r = at.save_config({"mode": "off"})
        return "⏸️ 자동매매를 껐어요 (들고 있는 주식은 그대로)" if r.get("ok") else r.get("message", "실패")
    if t in ("/실전", "/live"):
        return "실전 켜기는 안전을 위해 PC 화면(🤖 자동매매)에서 '실전'을 입력해야 해요"
    return HELP


def _sender() -> None:
    while True:
        text = _q.get()
        if enabled() and chat_id():
            _call("sendMessage", chat_id=chat_id(), text=text, disable_web_page_preview="true")
        time.sleep(1.1)   # 텔레그램 속도 제한


def _poller(log) -> None:
    offset = None
    while True:
        if not enabled():
            time.sleep(30); continue
        r = _call_updates(offset)
        if not r.get("ok"):
            time.sleep(15); continue
        for u in r.get("result", []):
            offset = u["update_id"] + 1
            msg = u.get("message") or {}
            cid = str((msg.get("chat") or {}).get("id", ""))
            if not cid:
                continue
            if not chat_id() and time.time() > _reg_until[0]:
                continue    # 등록 시간이 아니면 무시 (PC 에서 '텔레그램 시험'을 눌러야 열림)
            if not chat_id():   # 등록 시간 안에 처음 말 건 채팅을 사장님 채팅으로 등록
                _save_chat(cid)
                _reg_until[0] = 0.0
                who = (msg.get("from") or {})
                _reg_who[0] = f"{who.get('first_name', '')} @{who.get('username', '')}".strip()
                log("🔔 텔레그램 채팅 등록됨 (이 채팅으로 알림을 보내요)")
                send("✅ 1인 기업 AI 알림이 연결됐어요.\n\n" + HELP)
                continue
            if cid != chat_id():
                continue    # 다른 사람 명령은 무시
            try:
                send(_handle(msg.get("text") or ""))
            except Exception as e:
                send(f"⚠️ 처리 중 오류: {type(e).__name__}")


def start(log=print) -> None:
    if _started.is_set():
        return
    _started.set()
    threading.Thread(target=_sender, daemon=True, name="tg-send").start()
    threading.Thread(target=_poller, args=(log,), daemon=True, name="tg-poll").start()
    if enabled():
        log("🔔 텔레그램 알림 켜짐" + ("" if chat_id() else " — 🤖 자동매매 화면에서 '텔레그램 시험'을 누르고 10분 안에 봇에게 말을 걸면 등록돼요"))


def test() -> dict:
    if not enabled():
        return {"ok": False, "message": "🧰 도구에 텔레그램 봇 토큰을 넣어 주세요"}
    me = _call("getMe", timeout=15)
    if not me.get("ok"):
        return {"ok": False, "message": f"토큰 확인 실패 ({me.get('description', _last_err[0])})"}
    name = (me.get("result") or {}).get("username", "")
    if not chat_id():
        _reg_until[0] = time.time() + 600
        return {"ok": True, "message": f"봇 @{name} 연결됨 — 지금부터 10분 안에 텔레그램에서 이 봇에게 아무 말이나 보내면 그 채팅이 알림 채팅으로 등록돼요"
                                       " (그 뒤엔 등록 안 받음 · 다른 사람 명령은 무시)"}
    send("🔔 시험 알림이에요. 잘 오면 연결 끝!")
    return {"ok": True, "message": f"봇 @{name} 로 시험 알림을 보냈어요" + (f" (등록된 사람: {_reg_who[0]})" if _reg_who[0] else "")
            + " — 다른 사람으로 등록됐다면 .env 의 TELEGRAM_CHAT_ID 줄을 지우고 다시 시험을 누르세요"}
