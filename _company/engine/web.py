"""브라우저 화면 (Claude 앱 스타일) — 내 PC에서만 열리는 로컬 서버.

  python -m engine web           # 자동으로 브라우저가 열립니다 (http://127.0.0.1:8765)
  python -m engine web --port 9000 --no-browser

추가 설치 없이 파이썬 기본 기능만 사용합니다.
"""

from __future__ import annotations

import contextlib
import traceback
import io
import json
import os
import re
import time
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from hmac import compare_digest as secrets_eq
from urllib.parse import parse_qsl, urlparse

from . import agents as ag
from . import providers as pv
from . import team as tm
from . import tools as tl
from . import toss
from . import autotrade as at
from . import improve as im

STATIC_DIR = Path(__file__).resolve().parent / "web"
UPLOAD_DIR = ag.COMPANY_DIR / "_cache" / "uploads"   # git 에 안 올라감
IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024
UPLOAD_RE = re.compile(r"^[\w\-.]+\.(png|jpg|webp|gif)$")
CONV_FILE = "conversation.json"
ID_RE = re.compile(r"^[\w\-]+$")
MODEL_CHOICES = [
    ("에이전트 기본값", ""),
    ("Claude (기본)", "claudecode"),
    ("Claude Opus", "claudecode:opus"),
    ("Claude Sonnet", "claudecode:sonnet"),
    ("Claude Haiku (빠름)", "claudecode:haiku"),
    ("로컬 AI (자동 감지)", "local"),
    ("Claude API", "claude"),
    ("OpenAI", "openai"),
    ("Gemini", "gemini"),
]


def log(msg: str) -> None:
    """검은 창(콘솔)에 진행 상황 표시 — 문제 생겼을 때 원인 찾기용."""
    with contextlib.suppress(Exception):
        print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


TICKER_PROMPT = """사용자 메시지(와 대화 흐름)에서 분석 대상 주식/ETF를 찾아 티커로 바꾸세요.
- 한국: 6자리코드 (예: 삼성전자 005930, 에코프로 086520) · 미국: 티커 그대로 (예: AAPL, NVDA, SPY)
- 최대 3개. 종목이 없거나 자동매매 엔진 자체에 대한 질문(오늘 뭐 샀어·성적·설정)이면 빈 배열. 확실하지 않은 코드는 넣지 마세요.
JSON 배열로만 답하세요. 예: ["005930", "NVDA"]"""

BADGE_RE = re.compile(r"^\s*`?(?:🏆|⚠️ 검토)[^\n]*\n+")


def strip_badge(text: str) -> str:
    """'🏆 검토 완료 · …' 배지 줄 제거 — 화면 표시용이라 AI 대화 기록·학습 데이터에는 안 넣음."""
    return BADGE_RE.sub("", text, count=1)


def chat_history(conv: dict) -> list[dict]:
    """저장된 메시지 → 모델용 history. 연속된 assistant 메시지는 하나로 합칩니다."""
    out: list[dict] = []
    for m in conv["messages"]:
        content = strip_badge(m["content"]) if m["role"] == "assistant" else m["content"]
        if m.get("kind") == "tool":
            content = f"[자동 실행 결과: {m.get('tool', '')}]\n{content}"
        if out and out[-1]["role"] == m["role"]:
            out[-1]["content"] += "\n\n" + content
        else:
            out.append({"role": m["role"], "content": content})
    return out


# ── 대화 저장 ─────────────────────────────────────────────────────────────
def conv_dir(cid: str) -> Path:
    if not ID_RE.match(cid):
        raise ValueError("잘못된 대화 ID")
    return ag.SESSIONS_DIR / cid


def load_conv(cid: str) -> dict:
    path = conv_dir(cid) / CONV_FILE
    return json.loads(path.read_text(encoding="utf-8"))


def save_conv(conv: dict) -> None:
    d = conv_dir(conv["id"])
    d.mkdir(parents=True, exist_ok=True)
    conv["updated"] = datetime.now().isoformat(timespec="seconds")
    (d / CONV_FILE).write_text(json.dumps(conv, ensure_ascii=False, indent=1), encoding="utf-8")
    # 학습 데이터 내보내기(export-training)와 호환되는 chat.md 도 함께 기록
    lines = []
    for m in conv["messages"]:
        who = "user" if m["role"] == "user" else m.get("agent", "assistant")
        if m.get("kind") in (None, "chat"):
            lines.append(f"\n### {who}\n\n{strip_badge(m['content']).strip()}\n")
    (d / "chat.md").write_text("".join(lines), encoding="utf-8")


CONV_LOCK = threading.RLock()   # 대화 파일 쓰기는 한 번에 하나 (채팅 답변 중에 🗣️ 생존전 의견이 붙어도 안 덮어씀)


def save_conv_merged(conv: dict, base: int) -> int:
    """conv["messages"][base:] 만 '내가 새로 붙인 것' 으로 보고, 그사이 파일에 붙은 메시지(다른 곳에서 보낸 것)를 살려서 저장.
    다음 호출에 쓸 base(지금 메시지 수) 반환"""
    with CONV_LOCK:
        mine = conv["messages"][base:]
        try:
            disk = load_conv(conv["id"])["messages"]
        except (OSError, ValueError, KeyError):
            disk = conv["messages"][:base]
        conv["messages"] = disk + mine
        save_conv(conv)
        return len(conv["messages"])


def new_conv(title: str) -> dict:
    cid = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    return {"id": cid, "title": title[:40] or "새 대화", "agent": None, "messages": []}


def list_convs() -> list[dict]:
    out = []
    for f in ag.SESSIONS_DIR.glob(f"*/{CONV_FILE}"):
        try:
            c = json.loads(f.read_text(encoding="utf-8"))
            out.append({"id": c["id"], "title": c.get("title", ""), "updated": c.get("updated", ""),
                        "agent": c.get("agent")})
        except (OSError, json.JSONDecodeError, KeyError):
            continue
    return sorted(out, key=lambda c: c["updated"], reverse=True)


# ── 요청 처리 ─────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    server_version = "CompanyAI/1.0"

    def log_message(self, fmt, *args):  # 조용히
        pass

    # 공통
    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse_start(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    _send_lock = threading.Lock()   # 여러 AI 가 동시에 실시간 글을 보내도 섞이지 않게

    def _send(self, event: str, data):
        lv = getattr(self, "_live", None)
        if lv is not None:   # 💬 진행 중인 답변을 메모리에 같이 보관 → 채팅에 다시 들어와도 이어서 보임
            if event == "delta":
                lv["text"] += data.get("text", "")
            elif event == "replace":
                lv["text"] = data.get("text", "")
            elif event == "status":
                lv["status"] = data.get("text", "")
            elif event in ("step_start", "activity", "step_activity"):
                lv["status"] = data.get("title") or data.get("text") or data.get("label") or lv["status"]
            lv["t"] = time.time()
        if lv is not None and lv.get("stop"):
            raise BrokenPipeError("사용자가 중지")   # ■ 중지 버튼 → 답변 만들기 멈춤
        if getattr(self, "_gone", False):
            return
        payload = json.dumps(data, ensure_ascii=False)
        try:
            with self._send_lock:
                self.wfile.write(f"event: {event}\ndata: {payload}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            self._gone = True   # 화면이 떠나도 답변 만들기는 계속 → 끝나면 대화에 저장

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def _local_only(self) -> bool:
        """다른 웹사이트가 이 서버로 요청을 보내지 못하도록:
        POST 는 커스텀 헤더(X-Company-AI)가 있어야 함 → 브라우저가 교차 출처 요청을 막음."""
        host = (self.headers.get("Host") or "").split(":")[0]
        client = self.client_address[0]
        local = host in ("127.0.0.1", "localhost") and client in ("127.0.0.1", "::1")
        if not local:
            # 폰 접속: 켜져 있을 때만, 주소창은 이 PC 의 IP 로만 (다른 도메인 위장 차단), PIN 등록한 기기만
            if not PHONE["on"] or host != PHONE["url"].split("//")[1].split(":")[0] or not _private_ip(client):
                self._json({"error": "로컬 접속만 허용"}, 403)
                return False
            path = urlparse(self.path).path
            if self.command == "POST" and path == "/api/pin" and self.headers.get("X-Company-AI") == "1":
                self._pin_login()
                return False
            cookies = dict(c.strip().split("=", 1) for c in (self.headers.get("Cookie") or "").split(";") if "=" in c)
            if not secrets_eq(cookies.get("cai", ""), PHONE["token"]):
                if self.command == "GET" and path in ("/", "/index.html"):
                    body = PIN_PAGE.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers(); self.wfile.write(body)
                else:
                    self._json({"error": "PIN 이 필요해요"}, 401)
                return False
        if self.command == "POST" and self.headers.get("X-Company-AI") != "1":
            self._json({"error": "잘못된 요청"}, 403)
            return False
        return True

    def _pin_login(self):
        ip = self.client_address[0]
        now = time.time()
        fails = [x for x in _PIN_FAILS.get(ip, []) if now - x < 600]
        if len(fails) >= 8:
            return self._json({"error": "너무 많이 틀렸어요. 10분 뒤에 다시 해 주세요"}, 429)
        if not secrets_eq(str(self._body().get("pin", "")), PHONE["pin"]):
            _PIN_FAILS[ip] = fails + [now]
            return self._json({"error": f"PIN 이 틀렸어요 ({len(fails) + 1}/8)"}, 403)
        _PIN_FAILS.pop(ip, None)
        log(f"📱 폰 접속 등록: {ip}")
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", f"cai={PHONE['token']}; Path=/; Max-Age=2592000; HttpOnly; SameSite=Strict")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def _autotrade_post(self, path: str, body: dict):
        if path == "/api/autotrade/config":
            return self._json(at.save_config(body if isinstance(body, dict) else {}))
        if path == "/api/autotrade/stop":
            return self._json(at.emergency_stop())
        if path == "/api/autotrade/run":
            try:
                return self._json(at.run_once(force=True))
            except Exception as e:
                return self._json({"ok": False, "message": f"{type(e).__name__}: {e}"})
        if path == "/api/autotrade/action":   # 결과를 대화 기록에 남겨서 다시 열어도 '적용됨'·'무시'로 보이게 (두 번 적용 막기)
            a = body.get("action") or {}
            conv, card = None, None
            try:
                conv = load_conv(str(body.get("conversation_id") or ""))
                card = next((x for m in conv["messages"] for x in (m.get("trade_actions") or []) if x.get("id") == a.get("id")), None)
            except (FileNotFoundError, ValueError, KeyError):
                pass
            if card and card.get("status") in ("applied", "ignored"):
                return self._json({"ok": False, "message": "이미 처리한 카드예요", "status": card["status"]})
            r = {"ok": True, "message": "무시했어요"} if body.get("ignore") else at.apply_action(card or a)   # 저장된 카드 내용 그대로 적용
            if card is not None and (r.get("ok") or body.get("ignore")):
                with CONV_LOCK:   # 다시 읽고 표시 (그사이 붙은 메시지 안 덮어씀)
                    try:
                        conv = load_conv(conv["id"])
                        card = next((x for m in conv["messages"] for x in (m.get("trade_actions") or []) if x.get("id") == a.get("id")), None)
                    except (OSError, ValueError, KeyError):
                        card = None
                    if card is not None:
                        card["status"] = "ignored" if body.get("ignore") else "applied"
                        card["result"] = str(r.get("message") or "바꿨어요")[:200]
                        save_conv(conv)
            return self._json(r)
        if path == "/api/autotrade/arena-voice":   # 🗣️ 생존전 AI 의견 지금 받기 (뒤에서 · 채팅으로 옴)
            from . import arena_voice
            c = at.config()
            if not c.get("arena_on"):
                return self._json({"ok": False, "message": "AI 생존전이 꺼져 있어요"})
            return self._json(arena_voice.run_bg(c, at.markets()))
        if path == "/api/autotrade/notes-council":   # 📒 노트 정리 회의 지금 (뒤에서 · 채팅으로 옴)
            from . import notes_council
            return self._json(notes_council.run_bg(at.config(), at.markets()))
        if path == "/api/autotrade/unhalt":   # 화면 버튼 전용 (채팅 카드로는 없음)
            return self._json(at.unhalt())
        if path == "/api/autotrade/review":
            return self._json(at.review_now())
        if path == "/api/autotrade/proposal":
            from . import tradelearn as tlearn   # 'tl' 은 도구(tools) 모듈 이름이라 겹치면 안 됨
            return self._json(tlearn.decide(str(body.get("id", "")), str(body.get("action", ""))))
        if path == "/api/autotrade/scan":
            return self._json(at.scan_now())
        if path == "/api/autotrade/notify-test":
            from . import notify
            return self._json(notify.test())
        if path == "/api/autotrade/preflight":
            return self._json(at.preflight())
        if path == "/api/autotrade/weekend":
            from . import weekend
            if weekend._running.is_set():
                return self._json({"ok": False, "message": "이미 공부 중이에요"})
            threading.Thread(target=weekend.run, args=(log,), daemon=True, name="weekend").start()
            return self._json({"ok": True, "message": "주말 공부 시작 — 10~30분 걸리고, 끝나면 텔레그램으로 알려요"})
        if path == "/api/autotrade/backtest":
            return self._json(at.backtest_now())
        if path == "/api/autotrade/lab-reset":
            from . import shadow
            shadow.ensure(at.config(), at.cur_market(), force=True)
            return self._json({"ok": True, "message": "실험 조합을 새로 만들었어요"})
        if path == "/api/autotrade/reset-paper":
            return self._json(at.reset_paper())
        return self._json({"error": "없는 주소"}, 404)

    # GET
    def do_GET(self):
        if not self._local_only():
            return
        path = urlparse(self.path).path
        if path == "/" or path == "/index.html":
            return self._static("index.html", "text/html; charset=utf-8")
        if path in ("/api/autotrade", "/api/charts/auto"):   # ?market=US|KR (없으면 첫 시장)
            qd = dict(parse_qsl(urlparse(self.path).query))
            qm = qd.get("market")
            with at.in_market(qm):   # open=펼친 종목 (그 종목만 차트 그림)
                return self._json(at.overview() if path == "/api/autotrade" else {"items": at.auto_charts(open_sym=qd.get("open", ""))})
        if path == "/api/chart":   # 📊 차트 그리기 (자동매매가 가진 종목이면 산 가격·손절·나눠 팔기선 표시)
            q = dict(parse_qsl(urlparse(self.path).query))
            try:
                r = at.draw_chart(q.get("symbol", ""), q.get("interval", "1m"), live=q.get("live") == "1")
                return self._json({**r, "v": int(time.time())})
            except Exception as e:
                return self._json({"error": str(e)}, 400)
        if path == "/api/phone":
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                return self._json({"on": PHONE["on"], "remote": True})
            return self._json({"on": PHONE["on"], "url": PHONE["url"], "pin": PHONE["pin"]})
        if path == "/api/meta":
            return self._json({
                "agents": [{"id": t, "summary": ag.agent_summary(t), "model": ag.Agent(t).model_spec(), "members": [{"id": t, "view": "자동매매"}]}
                           for t in ag.list_teams()],
                "models": [{"label": l, "value": v} for l, v in MODEL_CHOICES],
                "company": "자동매매 AI", "team_mode": "off",
            })
        if path == "/api/autotrade/digest":   # 📋 제안 현황 (읽기 전용)
            from . import digest
            out = {}
            for m in at.markets():
                try:
                    out[m] = digest.text(m)
                except Exception as e:
                    out[m] = f"제안 현황을 못 만들었어요 ({type(e).__name__})"
            return self._json(out)
        if path == "/api/conversations":
            return self._json(list_convs())
        m = re.match(r"^/api/conversations/([\w\-]+)/live$", path)
        if m:   # 💬 진행 중인 답변 (없으면 active False)
            lv = LIVE.get(m.group(1))
            return self._json({"active": bool(lv) and not lv.get("done"), **({k: lv[k] for k in ("text", "status", "started", "t") if k in lv} if lv else {})})
        m = re.match(r"^/api/conversations/([\w\-]+)$", path)
        if m:
            try:
                return self._json(load_conv(m.group(1)))
            except (FileNotFoundError, ValueError):
                return self._json({"error": "없는 대화"}, 404)
        m = re.match(r"^/api/upload/([\w\-.]+\.(?:png|jpg|webp|gif))$", path)
        if m and (UPLOAD_DIR / m.group(1)).is_file():
            ext = m.group(1).rsplit(".", 1)[1]
            body = (UPLOAD_DIR / m.group(1)).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", {"jpg": "image/jpeg"}.get(ext, f"image/{ext}"))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        if path == "/api/tools":
            return self._json(tl.overview())
        if path == "/api/improve":
            from . import gitsync
            return self._json({"jobs": im.history()[:50], "auto": im.auto_on(),
                               "sync": {"on": gitsync.enabled(), **gitsync.STATUS}})
        m = re.match(r"^/api/improve/([\w\-]+)$", path)
        if m:
            job = im.get(m.group(1))
            if not job:
                return self._json({"error": "없는 개선 요청"}, 404)
            files = im.changes(job["id"]) if (im.WORK_ROOT / job["id"]).exists() else []
            return self._json({**job, "files": files})
        self._json({"error": "not found"}, 404)

    def _static(self, name: str, ctype: str):
        body = (STATIC_DIR / name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # POST
    def do_POST(self):
        if not self._local_only():
            return
        path = urlparse(self.path).path
        try:
            body = self._body()
        except json.JSONDecodeError:
            return self._json({"error": "JSON 형식 오류"}, 400)
        routes = {"/api/chat": self._chat, "/api/improve/run": self._improve_run}
        if path in routes:
            self._sse_start()
            try:
                try:
                    routes[path](body)
                except BaseException:
                    lv = getattr(self, "_live", None)
                    if lv is not None:
                        lv.update(done=True, error=True)
                    raise
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                log("  (사용자가 중지했거나 연결이 끊김)")
            except pv.ProviderError as e:  # 설치·로그인·한도 같은 안내성 오류는 긴 오류 내역 없이
                log(f"  ⚠️ {e}")
                with contextlib.suppress(Exception):
                    self._send("error", {"message": str(e)})
            except Exception as e:
                log(f"  ⚠️ 오류: {e}")
                traceback.print_exc()
                with contextlib.suppress(Exception):
                    self._send("error", {"message": str(e)})
            return
        if path.startswith("/api/autotrade/"):   # 시장별 동작은 body.market 기준
            with at.in_market(body.get("market") if isinstance(body, dict) else None):
                return self._autotrade_post(path, body)
        if path == "/api/improve/new":
            req = str(body.get("request", "")).strip()
            return self._json(im.create(req, "", "사장님") if req else {"error": "무엇을 바꿀지 적어 주세요"})
        if path == "/api/sync":
            from . import gitsync
            return self._json(gitsync.sync_now(log, lambda: threading.Thread(target=_restart, daemon=True).start()))
        if path == "/api/improve/auto":
            return self._json(im.set_auto(bool(body.get("on"))))
        if path in ("/api/improve/apply", "/api/improve/rollback", "/api/improve/discard"):
            fn = {"apply": im.apply, "rollback": im.rollback, "discard": im.discard}[path.rsplit("/", 1)[1]]
            res = fn(str(body.get("id", "")))
            log(f"🛠️ 개선 {path.rsplit('/', 1)[1]}: {res.get('message', res)}")
            def _after(kind=path.rsplit("/", 1)[1], need=res.get("ok") and (res.get("needs_restart") or "다시 켜야" in str(res.get("message", "")))):
                from . import gitsync
                if res.get("ok") and kind in ("apply", "rollback") and gitsync.enabled():   # 🔄 GitHub 에도 올리기
                    log("🔄 " + gitsync.push(f"개선 {kind}")["message"])
                if need:   # 코드가 바뀌면(적용·되돌리기) 바퀴가 끝난 뒤 자동으로 다시 켜기
                    _restart()
            threading.Thread(target=_after, daemon=True, name="after-improve").start()
            return self._json(res)
        if path == "/api/tools/approve":
            return self._json(tl.approve(str(body.get("id", ""))))
        if path == "/api/tools/reject":
            return self._json({"ok": bool(tl.set_status(str(body.get("id", "")), "rejected", "사장님이 거절"))})
        if path == "/api/tools/key":
            return self._json(tl.save_key(str(body.get("tool_id", "")), str(body.get("value", ""))))
        if path == "/api/upload":
            return self._upload(body)
        if path == "/api/capture":
            return self._capture(body)
        m = re.match(r"^/api/conversations/([\w\-]+)/stop$", path)
        if m:
            lv = LIVE.get(m.group(1))
            if lv is not None:
                lv["stop"] = True
            return self._json({"ok": True})
        m = re.match(r"^/api/conversations/([\w\-]+)/delete$", path)
        if m:
            import shutil

            with contextlib.suppress(ValueError, FileNotFoundError):
                shutil.rmtree(conv_dir(m.group(1)))
            return self._json({"ok": True})
        self._json({"error": "not found"}, 404)

    # 이미지 (붙여넣기·첨부·화면 캡처)
    def _save_image(self, data: bytes, ext: str) -> str:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        name = datetime.now().strftime("%Y%m%d-%H%M%S-") + os.urandom(3).hex() + ext
        (UPLOAD_DIR / name).write_bytes(data)
        return name

    def _upload(self, body: dict):
        import base64

        m = re.match(r"^data:(image/[a-z]+);base64,(.+)$", body.get("data") or "", re.S)
        if not m or m.group(1) not in IMAGE_TYPES:
            return self._json({"error": "PNG, JPG, WEBP, GIF 이미지만 올릴 수 있어요"}, 400)
        data = base64.b64decode(m.group(2))
        if len(data) > MAX_IMAGE_BYTES:
            return self._json({"error": "이미지가 너무 커요 (최대 10MB)"}, 400)
        return self._json({"id": self._save_image(data, IMAGE_TYPES[m.group(1)])})

    def _capture(self, body: dict):
        """내 PC 화면을 찍습니다 (토스 창을 띄워둔 상태에서). 읽기만 하고 아무것도 클릭하지 않아요."""
        try:
            from PIL import ImageGrab
        except ImportError:
            return self._json({"error": "화면 캡처 패키지가 없어요: pip install pillow"}, 400)
        time.sleep(max(0.0, min(float(body.get("delay") or 3), 10.0)))
        try:
            img = ImageGrab.grab()
        except Exception as e:
            return self._json({"error": f"화면 캡처 실패: {e}"}, 500)
        if img.width > 2400:  # 너무 크면 줄여서 저장 (글자는 읽히는 크기)
            img = img.resize((2400, int(img.height * 2400 / img.width)))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "PNG", optimize=True)
        return self._json({"id": self._save_image(buf.getvalue(), ".png")})

    def _stock_context(self, text: str, history: list[dict]) -> str:
        """질문에 종목이 있으면 토스 시세·일봉·1분봉 차트를 붙여줌 (빠른 모델로 종목만 뽑음 · 검토자 없음)"""
        if not toss.enabled():
            return ""
        recent = "\n".join(re.sub(r"\[화면 캡처 \d+장 첨부\]", "", m["content"])[:500] for m in history[-3:])
        try:
            reply = pv.chat(tm.fast_spec(), TICKER_PROMPT, [{"role": "user", "content": recent}], effort="low", timeout=60)
            m = re.search(r"\[.*?\]", reply, re.S)
            tickers = [str(x).strip().upper() for x in (json.loads(m.group(0)) if m else [])
                       if re.fullmatch(r"\d{6}(\.(KS|KQ))?|[A-Za-z][A-Za-z.\-]{0,9}", str(x).strip())][:3]
        except Exception:
            tickers = []
        if not tickers:
            return ""
        self._send("status", {"text": "📈 " + ", ".join(tickers) + " 시세·차트 가져오는 중..."})
        from . import chart
        lines = []
        for t in tickers:
            sym = toss.symbol(t)
            try:
                q = toss.prices([sym]).get(sym) or {}
                info = toss.stock_info(sym) or {}
                closes, _, _ = toss.daily(sym, 60)
                name = info.get("name") or sym
                chg20 = (closes[-1] / closes[-21] - 1) * 100 if len(closes) > 21 else 0
                lines.append(f"- {name}({sym}): 현재가 {q.get('price')} · 전일 대비 {q.get('changeRate') or q.get('change', '')} · 20일 {chg20:+.1f}%")
                for iv in ("1d", "1m"):
                    try:
                        cid = chart.draw(sym, iv, title=name)
                        self._charts.append({"id": cid, "label": f"{name} {'일봉' if iv == '1d' else '1분봉'}"})
                    except Exception:
                        pass
            except toss.TossError as e:
                lines.append(f"- {sym}: 조회 실패 ({e})")
        if self._charts:
            self._send("charts", {"items": self._charts})
        self._send("status", {"text": ""})
        note = ("\n[첨부 차트] 토스 데이터로 그린 차트가 첨부돼 있어요 (" + ", ".join(c["label"] for c in self._charts)
                + "). 캔들·이동평균·거래량을 직접 보고 추세·지지/저항을 분석에 쓰세요.") if self._charts else ""
        return "<market>\n" + "\n".join(lines) + "\n</market>" + note

    # 채팅
    def _chat(self, body: dict):
        """자동매매 팀(trader)과 1:1 대화 — 팀 배정·검토·리서치 없이 바로 답함 (Claude 사용량 절약)"""
        text = (body.get("message") or "").strip()
        image_ids = [i for i in (body.get("images") or []) if UPLOAD_RE.match(str(i)) and (UPLOAD_DIR / i).is_file()][:6]
        images = [str(UPLOAD_DIR / i) for i in image_ids]
        if not text and images:
            text = "첨부한 화면을 분석해 줘."
        if not text:
            return self._send("error", {"message": "메시지가 비어 있어요"})
        if text.lower() in ("/login", "/logout", "login", "로그인"):
            return self._send("error", {"message": ("로그인은 여기 채팅이 아니라 PowerShell(콘솔)에서 해요. "
                                                    + ("Claude Code 는 설치돼 있어요: PowerShell 에서 claude 실행 → /login → 브라우저 승인 → 이 창을 다시 켜기"
                                                       if pv.claude_cli() else pv.LOGIN_HELP))})
        cid = body.get("conversation_id")
        conv = load_conv(cid) if cid else new_conv(text)
        try:   # 🗣️ '1호야 …' · '@눌림목 …' 처럼 AI 를 부르면 그 AI 가 읽기 전용으로 답함
            from . import ai_ask
            who = ai_ask.match(text)
        except Exception:
            who = None
        if who:
            return self._ask_ai(conv, who, text, body)
        agent_id = ag.TRADER
        override = body.get("model") or None
        conv["agent"] = agent_id
        spec = pv.parse_model(override or ag.Agent(agent_id).model_spec())
        log(f"💬 질문: {text[:40]}")
        history = chat_history(conv)
        if history and history[-1]["role"] == "user":
            history[-1]["content"] += "\n\n" + text
        else:
            history.append({"role": "user", "content": text})
        if images:
            history[-1]["content"] += f"\n\n[화면 캡처 {len(images)}장 첨부]"
        base = len(conv["messages"])
        conv["messages"].append({"role": "user", "content": text, "images": image_ids})
        base = save_conv_merged(conv, base)
        self._live = LIVE.setdefault(conv["id"], {})
        self._live.clear(); self._live.update(text="", status="답변 준비 중", t=time.time(), done=False, started=time.time())
        self._send("meta", {"conversation_id": conv["id"], "title": conv["title"], "agent": agent_id,
                            "model": str(spec), "reason": "", "sources": [], "team": 1, "router_model": "", "member_model": ""})
        self._charts = []
        extra = (tl.guide_for_agents(agent_id) or "") + "\n\n" + im.IMPROVE_GUIDE
        market = self._stock_context(text, history)
        if market:
            extra += "\n\n" + market
            images = images + [str(UPLOAD_DIR / c["id"]) for c in self._charts]   # AI 가 차트를 그림으로 직접 보게
        extra += "\n\n" + at.engine_context() + "\n\n" + at.TRADE_GUIDE   # 🤖 엔진 전체 상태 + [적용] 카드용 행동 태그
        if toss.enabled() and toss.ACCOUNT_WORDS.search(text):   # "내 잔고" 질문이면 계좌 조회 (조회 전용)
            try:
                extra += "\n\n" + toss.portfolio_for_prompt()
            except toss.TossError as e:
                self._send("account", {"ok": False, "text": str(e)})
        system = ag.Agent(agent_id).system_prompt() + "\n\n" + extra
        streamed = []
        delta = lambda t: (streamed.append(t), self._send("delta", {"text": t}))  # noqa: E731
        reply = pv.chat(spec, system, history, on_text=delta, effort=body.get("effort") or "medium", images=images,
                        on_activity=lambda a: self._send("activity", a))
        if reply.strip() != "".join(streamed).strip():
            self._send("replace", {"text": reply})
        conv["messages"].append({"role": "assistant", "content": reply, "agent": agent_id, "model": str(spec), "sources": [],
                                 "drafts": [], "steps": []})
        if self._charts:
            conv["messages"][-1]["charts"] = self._charts
        for what, why in im.find_proposals(reply):
            job = im.create(what, why, agent_id)
            conv["messages"][-1].setdefault("improvements", []).append(job["id"])
            log(f"  🛠️ 개선 제안: {what[:40]}")
            self._send("improve_request", job)
        acts = at.find_actions(reply)
        if acts:   # 바로 실행하지 않고 [적용] 카드로
            conv["messages"][-1]["trade_actions"] = acts
            for a in acts:
                log(f"  🤖 자동매매 지시 카드: {a['label']}")
                self._send("trade_action", a)
        reqs = [tl.add_request(tool, why, agent_id) for tool, why in tl.find_requests(reply)]
        if reqs:
            conv["messages"][-1]["tool_requests"] = [r["id"] for r in reqs]
            for r in reqs:
                t = tl.find(r["tool_id"]) or {}
                log(f"  🧰 도구 요청: {r['name']} ({r['status']})")
                self._send("tool_request", {**r, "desc": t.get("desc", ""), "signup": t.get("signup", ""),
                                            "steps": t.get("steps", []), "tool_status": tl.status(t) if t else ""})
        save_conv_merged(conv, base)   # 답변 만드는 사이 붙은 🗣️ 생존전 의견 등은 살림
        log("  ✅ 답변 완료")
        LIVE.pop(conv["id"], None)
        self._live = None
        self._send("done", {"conversation_id": conv["id"]})

    def _ask_ai(self, conv: dict, who: dict, text: str, body: dict):
        """🗣️ 지정한 AI(1호·2호·생존전) 이름으로 답 — 읽기 전용: [적용] 카드·개선 제안·도구 요청 안 만듦"""
        from . import ai_ask
        agent_id = ag.TRADER
        conv["agent"] = agent_id
        spec = pv.parse_model(body.get("model") or ag.Agent(agent_id).model_spec())
        log(f"💬 🗣️ {who['name']} 에게: {text[:40]}")
        history = chat_history(conv)[-8:]
        while history and history[0]["role"] != "user":   # 첫 메시지는 사용자 것이어야 함
            history.pop(0)
        if history and history[-1]["role"] == "user":
            history[-1]["content"] += "\n\n" + text
        else:
            history.append({"role": "user", "content": text})
        base = len(conv["messages"])
        conv["messages"].append({"role": "user", "content": text, "images": []})
        base = save_conv_merged(conv, base)
        self._live = LIVE.setdefault(conv["id"], {})
        self._live.clear(); self._live.update(text="", status=f"{who['name']} 답변 준비 중", t=time.time(), done=False, started=time.time())
        self._send("meta", {"conversation_id": conv["id"], "title": conv["title"], "agent": agent_id,
                            "model": str(spec), "reason": "", "sources": [], "team": 1, "router_model": "", "member_model": ""})
        ok, used = ai_ask.take()
        head = f"**🗣️ {who['name']}** · 읽기 전용 답변 (오늘 AI 지정 질문 {used}/{ai_ask.ASK_PER_DAY})\n\n"
        if not ok:
            reply = (f"**🗣️ {who['name']}**\n\n오늘 AI 지정 질문 한도({ai_ask.ASK_PER_DAY}번)를 다 썼어요. "
                     "내일 다시 물어보시거나, AI 이름 없이 물으면 자동매매 팀이 답해요.")
        else:
            self._send("status", {"text": f"🗣️ {who['name']} 기록 모으는 중..."})
            self._send("delta", {"text": head})
            try:
                system = ai_ask.system_prompt(who)
                ans = pv.chat(spec, system, history, on_text=lambda t: self._send("delta", {"text": t}),
                              effort=body.get("effort") or "low", on_activity=lambda a: self._send("activity", a))
            except BaseException:
                ai_ask.refund()   # 실패는 한도로 안 셈
                raise
            reply = head + ans.strip()
        note = ai_ask.save_opinion(who, text)
        if note:
            reply += f"\n\n📒 공유 노트에 기록했어요 (참고용 · 규칙은 안 바뀜): {note}"
            log(f"  📒 {note[:60]}")
        self._send("replace", {"text": reply})
        conv["messages"].append({"role": "assistant", "content": reply, "agent": agent_id, "model": str(spec), "sources": [],
                                 "drafts": [], "steps": [], "asked_ai": who["name"]})
        save_conv_merged(conv, base)
        log("  ✅ 답변 완료 (읽기 전용)")
        LIVE.pop(conv["id"], None)
        self._live = None
        self._send("done", {"conversation_id": conv["id"]})

    # 자기 개선 실행 (복사본에서 고치고 검사 → 결과만 보여줌, 적용은 따로)
    def _improve_run(self, body: dict):
        job = im.get(str(body.get("id", "")))
        if not job or job["status"] not in ("proposed", "failed", "blocked", "discarded", "conflict"):
            return self._send("error", {"message": "변경안을 만들 수 있는 상태가 아니에요"})
        step = tm.Step("improve", "🛠️ 코드·지시문 개선", "dev", "개발 AI · 복사본에서 작업", reads=["request"], kind="code",
                       model=body.get("model") or "claudecode")
        self._send("step_start", step.as_dict())
        log(f"🛠️ 개선 변경안 만드는 중: {job['request'][:40]}")
        try:
            res = im.run(job["id"], on_text=lambda t: self._send("step_text", {"id": "improve", "text": t}),
                         on_activity=lambda a: self._send("step_activity", {"id": "improve", **a}), model=body.get("model"))
        except pv.ProviderError as e:
            step.error = str(e)
            self._send("step_done", step.as_dict())
            return self._send("error", {"message": str(e)})
        step.text, step.verdict = res.get("summary", ""), ("pass" if res["ok"] else "fix")
        self._send("step_done", step.as_dict())
        self._send("improve_result", res)
        self._send("done", {})

LIVE: dict[str, dict] = {}   # 대화 id → 진행 중인 답변 {text, status, t, done}


# ── 폰 접속 (같은 와이파이) ──
PHONE = {"on": False, "url": "", "pin": "", "token": ""}
_PIN_FAILS: dict[str, list[float]] = {}
PIN_PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>1인 기업 AI</title><style>body{font-family:system-ui,sans-serif;background:#f6f4ee;color:#222;display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
@media (prefers-color-scheme:dark){body{background:#1d1c1a;color:#eee}input{background:#2a2926;color:#eee}}
.b{width:min(320px,90vw);text-align:center}input{font-size:28px;letter-spacing:8px;text-align:center;width:100%;padding:12px;border:1px solid #bbb;border-radius:12px;box-sizing:border-box}
button{margin-top:12px;width:100%;padding:14px;font-size:17px;border:0;border-radius:12px;background:#c96442;color:#fff}p{color:#888;font-size:14px}</style></head>
<body><div class="b"><h2>📱 1인 기업 AI</h2><p>PC 화면 왼쪽 아래 <b>📱 폰으로 열기</b>에 나온 숫자 6자리</p>
<input id="pin" inputmode="numeric" maxlength="6" autofocus><button id="go">들어가기</button><p id="msg"></p></div>
<script>const go=async()=>{const r=await fetch("/api/pin",{method:"POST",headers:{"Content-Type":"application/json","X-Company-AI":"1"},body:JSON.stringify({pin:document.getElementById("pin").value.trim()})});
const d=await r.json();if(d.ok)location.href="/";else document.getElementById("msg").textContent=d.error||"틀렸어요";};
document.getElementById("go").onclick=go;document.getElementById("pin").onkeydown=e=>{if(e.key==="Enter")go()};</script></body></html>"""


def _lan_ip() -> str:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.0.1", 9))  # 실제로 보내지 않음 — 내 PC 의 와이파이 주소만 알아냄
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


def _private_ip(ip: str) -> bool:
    """집 안 네트워크 주소인지 (192.168.x · 10.x · 172.16~31.x · 100.64~127.x[Tailscale]) — 인터넷 공개 주소면 False"""
    import ipaddress
    try:
        a = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    if a.version == 6 and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a.is_private and not a.is_loopback or a in ipaddress.ip_network("100.64.0.0/10")


def _phone_secret(name: str, make) -> str:
    """PIN·토큰은 _cache 에 보관 → 다시 켜도 폰을 또 등록할 필요 없음"""
    f = ag.COMPANY_DIR / "_cache" / name
    if f.is_file() and f.read_text().strip():
        return f.read_text().strip()
    f.parent.mkdir(parents=True, exist_ok=True)
    v = make(); f.write_text(v)
    return v


# 'AI 시작.bat' 없이 켰을 때 대신 돌봐 주는 감시 프로세스 (엔진 파일을 안 불러옴 → 고친 코드가 망가져도 돎)
# bat 과 같은 규칙: 켜기 직전마다 지킴이(guard.py) → 종료 코드 3 이면 바로 다시, 오류면 10초 뒤 다시, 0 이면 끝
_SUPERVISOR = "\n".join([
    "import os, subprocess, sys, time",
    "guard, cmd = sys.argv[1], sys.argv[2:]",
    "first, fails = True, 0",
    "try:",
    "    while True:",
    "        subprocess.call([sys.executable, guard])",
    "        env = dict(os.environ, CAI_SUPERVISED='1')",
    "        if first:",
    "            env['CAI_HANDOFF'] = '1'",
    "        first, t0 = False, time.time()",
    "        rc = subprocess.call(cmd, env=env)",
    "        if rc == 0:",
    "            break",
    "        if rc == 3:",
    "            fails = 0",
    "            continue",
    "        fails = fails + 1 if time.time() - t0 < 60 else 1",
    "        if fails >= 10:",
    "            print('[자동 재시작] 앱이 계속 바로 꺼져서 멈춰요 — 직접 다시 켜 주세요')",
    "            break",
    "        print('[자동 재시작] 앱이 꺼져서 10초 뒤 다시 켜요')",
    "        time.sleep(10)",
    "except KeyboardInterrupt:",
    "    pass",
])
_restarting = threading.Lock()


def _spawn_next() -> bool:
    """새 프로세스(감시 프로세스 → 앱)를 먼저 띄움. 새 앱은 옛 앱이 잠금을 풀 때까지 기다렸다가 켜짐"""
    import subprocess
    import sys
    args = [a for a in sys.argv[1:]] or ["web"]
    if "--no-browser" not in args:   # 다시 켤 때 브라우저 탭을 또 열지 않음
        args.append("--no-browser")
    cmd = [sys.executable, "-m", "engine"] + args
    guard = str(Path(__file__).resolve().parent / "guard.py")
    p = subprocess.Popen([sys.executable, "-c", _SUPERVISOR, guard] + cmd, cwd=str(ag.COMPANY_DIR))
    time.sleep(2)
    return p.poll() is None   # 감시 프로세스가 살아 있어야 옛 앱을 끔


def _wait_chats_done(max_sec: int = 900) -> None:
    """진행 중인 채팅 답변·자기 개선 작업이 끝날 때까지 기다림"""
    end = time.time() + max_sec
    told = False
    while time.time() < end:
        busy = [k for k, v in LIVE.items() if not v.get("done") and time.time() - v.get("t", 0) < 600]
        working = [r for r in im.history()[:20] if r.get("status") == "working"]
        if not busy and not working:
            return
        if not told:
            log(f"🔄 답변 {len(busy)}개·코딩 {len(working)}개가 진행 중이라 끝나면 다시 켜요")
            told = True
        time.sleep(3)


def _restart() -> None:
    """코드 적용 뒤 앱 다시 켜기: 주문·체결 중이면 바퀴가 끝날 때까지 대기 → 상태 저장 → (bat 이면 종료 코드 3 / 아니면 새 프로세스 먼저) → 옛 프로세스 종료"""
    if not _restarting.acquire(blocking=False):
        return   # 이미 다시 켜는 중
    supervised = os.environ.get("CAI_SUPERVISED") == "1"
    log("🔄 코드 적용 → 주문·체결 처리가 끝나고 자동매매 바퀴가 끝나면 앱을 다시 켜요")
    _wait_chats_done()   # 💬 답변·코딩 중이면 끝날 때까지 (최대 15분) — 재시작으로 답이 사라지지 않게
    at.wait_idle_for_restart(log)
    with at._lock:   # 화면 버튼 등 다른 곳의 기록·주문 처리도 끝난 뒤 저장
        try:
            info = at.save_for_restart()
            if info.get("unsaved"):
                log(f"💾 일부 기록을 못 썼어요({', '.join(info['unsaved'])}) — 백업에서 이어받아요")
        except Exception as e:
            log(f"💾 다시 켜기 전 저장 오류: {type(e).__name__}: {e}")
    try:   # '자동 개선 적용' 같은 알림이 텔레그램으로 다 나간 뒤에 끄기
        from . import notify
        notify.flush(15)
    except Exception:
        pass
    if not supervised:
        try:
            ok = _spawn_next()
        except Exception as e:
            ok = False
            log(f"🔄 새 프로세스 띄우기 실패: {type(e).__name__}: {e}")
        if not ok:
            log("🛠️ 자동으로 다시 켜지 못했어요 — 자동매매는 계속 돌고, 코드 변경은 직접 다시 켜면 적용돼요")
            at.resume_after_pause(log)
            _restarting.release()
            return
        log("🔄 새 프로세스를 띄웠어요 → 지금 앱을 끄면 새 앱이 이어받아요")
    time.sleep(1)
    os._exit(3 if supervised else 0)


_instance_lock = []


def _single_instance() -> bool:
    """이 PC 에서 앱이 하나만 돌게 (파일 잠금 — 앱이 꺼지면 자동으로 풀림)"""
    f = ag.COMPANY_DIR / "_cache" / "app.lock"
    f.parent.mkdir(parents=True, exist_ok=True)
    fh = open(f, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False
    _instance_lock.append(fh)
    return True


def _keep_awake() -> None:
    """윈도우: 앱이 켜져 있는 동안 PC 가 절전 모드로 들어가지 않게 (화면은 꺼져도 됨) — 자동매매가 밤새 돌아야 해서"""
    if os.name != "nt":
        return
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        print("💤 앱이 켜져 있는 동안 PC 절전 모드를 막아요 (화면 끄기는 괜찮아요)")
    except Exception:
        pass


def serve(port: int = 8765, open_browser: bool = True, phone: bool = False) -> None:
    import secrets
    url = f"http://127.0.0.1:{port}"
    ip = _lan_ip() if phone else ""
    public_ip = ""
    if ip and not _private_ip(ip):  # PC 가 인터넷 공개 주소를 직접 받는 경우 → 폰 접속을 열면 전 세계에 노출
        public_ip, ip = ip, ""
    handoff = os.environ.pop("CAI_HANDOFF", "") == "1"   # 자동 재시작으로 띄운 새 프로세스
    got = _single_instance()
    if not got and handoff:   # 옛 앱이 상태 저장 후 꺼질 때까지 기다림 (잠금은 그대로 → 동시에 두 개는 안 돎)
        print("🔄 이전 앱이 꺼지기를 기다리는 중...")
        for _ in range(180):
            time.sleep(1)
            if _single_instance():
                got = True
                break
    if not got:   # 앱이 두 개 켜지면 자동매매도 두 개가 돌아서 같은 기록 파일을 동시에 씀 → 막기
        if handoff:
            raise SystemExit(4)   # 감시 프로세스가 10초 뒤 다시 시도
        print(f"이미 다른 창에서 켜져 있어요. 브라우저로 {url} 을 엽니다.")
        if open_browser:
            webbrowser.open(url)
        return
    server = None
    for _ in range(30 if handoff else 1):   # 재시작이면 옛 앱의 주소가 풀릴 때까지 잠깐 다시 시도
        try:
            server = ThreadingHTTPServer(("0.0.0.0" if ip else "127.0.0.1", port), Handler)
            break
        except OSError:
            if handoff:
                time.sleep(1)
    if server is None:  # 이미 켜져 있음 (AI 시작.bat 두 번 실행 등)
        if handoff:
            raise SystemExit(4)
        print(f"이미 다른 창에서 켜져 있어요. 브라우저로 {url} 을 엽니다.")
        if open_browser:
            webbrowser.open(url)
        return
    server.daemon_threads = True
    print(f"🌐 {url} 에서 열렸어요. 끄려면 이 창에서 Ctrl+C")
    if ip:
        PHONE.update(on=True, url=f"http://{ip}:{port}",
                     pin=_phone_secret("phone_pin.txt", lambda: f"{secrets.randbelow(10**6):06d}"),
                     token=_phone_secret("phone_token.txt", lambda: secrets.token_hex(24)))
        print(f"📱 폰(같은 와이파이)에서: {PHONE['url']}  ·  PIN {PHONE['pin']}")
        print("   처음 켤 때 윈도우 방화벽 창이 뜨면 '개인 네트워크' 허용을 눌러 주세요.")
    elif public_ip:
        print(f"📱 이 PC 주소({public_ip})가 인터넷에 공개된 주소라서, 안전을 위해 폰 접속은 꺼 두었어요 (PC 에서는 정상 사용).")
        print("   폰으로 쓰려면 공유기(와이파이)에 연결하거나 Tailscale 을 쓰세요.")
    elif phone:
        print("📱 와이파이 주소를 못 찾아서 폰 접속은 꺼 두었어요 (PC 에서만 사용 가능)")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    im.boot_check(log, _restart)   # 자동 개선 직후면 자체 점검 → 실패면 되돌리고 다시 켜기
    at._restart_fn[0] = _restart
    at.start(log)
    from . import notify
    notify.start(log)
    _keep_awake()
    threading.Thread(target=im.auto_worker, args=(log, _restart), daemon=True, name="improve-auto").start()
    from . import gitsync
    threading.Thread(target=gitsync.worker, args=(log, _restart), daemon=True, name="git-sync").start()
    if gitsync.enabled():
        log("🔄 GitHub 동기화 켜짐 — 1시간마다·자동 개선 적용 때 코드만 올리고 받아요")
    if im.auto_on():
        log("🤖🛠️ 자동 개선 켜짐 — 팀이 제안하면 고치고 검사 통과 시 바로 적용 (키·계좌 관련 파일은 잠금)")
    if at.config()["mode"] != "off":
        print(f"🤖 자동매매 켜져 있음: {at.config()['mode']} (끄려면 화면의 🤖 자동매매 → 긴급 정지)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n종료 — 기록 저장 중...")
        try:   # 그림자 매매는 3분마다만 쓰므로 끌 때 남은 것 저장
            from . import shadow, store
            shadow.flush()
            left = store.flush()
            if left:
                print("⚠️ 못 쓴 기록:", ", ".join(left))
        except Exception as e:
            print(f"⚠️ 기록 저장 실패: {type(e).__name__}: {e}")
    finally:
        server.server_close()
