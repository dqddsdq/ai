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
        payload = json.dumps(data, ensure_ascii=False)
        with self._send_lock:
            self.wfile.write(f"event: {event}\ndata: {payload}\n\n".encode())
            self.wfile.flush()

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
        if path == "/api/autotrade/action":
            return self._json(at.apply_action(body.get("action") or {}))
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
            qm = dict(parse_qsl(urlparse(self.path).query)).get("market")
            with at.in_market(qm):
                return self._json(at.overview() if path == "/api/autotrade" else {"items": at.auto_charts()})
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
        if path == "/api/conversations":
            return self._json(list_convs())
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
            return self._json({"jobs": im.history()[:50], "auto": im.auto_on()})
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
                routes[path](body)
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
        if path == "/api/improve/auto":
            return self._json(im.set_auto(bool(body.get("on"))))
        if path in ("/api/improve/apply", "/api/improve/rollback", "/api/improve/discard"):
            fn = {"apply": im.apply, "rollback": im.rollback, "discard": im.discard}[path.rsplit("/", 1)[1]]
            res = fn(str(body.get("id", "")))
            log(f"🛠️ 개선 {path.rsplit('/', 1)[1]}: {res.get('message', res)}")
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
        conv["messages"].append({"role": "user", "content": text, "images": image_ids})
        save_conv(conv)
        self._send("meta", {"conversation_id": conv["id"], "title": conv["title"], "agent": agent_id,
                            "model": str(spec), "reason": "", "sources": [], "team": 1, "router_model": "", "member_model": ""})
        self._charts = []
        extra = (tl.guide_for_agents(agent_id) or "") + "\n\n" + im.IMPROVE_GUIDE
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
        save_conv(conv)
        log("  ✅ 답변 완료")
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


def _restart() -> None:
    """자동 개선을 적용한 뒤 앱 다시 켜기 ('AI 시작.bat' 이 종료 코드 3을 보면 바로 다시 켬)"""
    if os.environ.get("CAI_SUPERVISED") != "1":
        log("🛠️ 코드가 바뀌었어요 — 'AI 시작.bat' 으로 켜야 자동으로 다시 켜져요. 지금은 직접 다시 켜 주세요")
        return
    log("🔄 자동 개선 적용 → 자동매매 바퀴가 끝나면 앱을 다시 켜요")
    at.pause_for_restart()
    try:   # '자동 개선 적용' 같은 알림이 텔레그램으로 다 나간 뒤에 끄기
        from . import notify
        notify.flush(15)
    except Exception:
        pass
    time.sleep(1)
    os._exit(3)


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
    if not _single_instance():   # 앱이 두 개 켜지면 자동매매도 두 개가 돌아서 같은 기록 파일을 동시에 씀 → 막기
        print(f"이미 다른 창에서 켜져 있어요. 브라우저로 {url} 을 엽니다.")
        if open_browser:
            webbrowser.open(url)
        return
    try:
        server = ThreadingHTTPServer(("0.0.0.0" if ip else "127.0.0.1", port), Handler)
    except OSError:  # 이미 켜져 있음 (AI 시작.bat 두 번 실행 등)
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
    at.start(log)
    from . import notify
    notify.start(log)
    _keep_awake()
    threading.Thread(target=im.auto_worker, args=(log, _restart), daemon=True, name="improve-auto").start()
    if im.auto_on():
        log("🤖🛠️ 자동 개선 켜짐 — 팀이 제안하면 고치고 검사 통과 시 바로 적용 (키·계좌 관련 파일은 잠금)")
    if at.config()["mode"] != "off":
        print(f"🤖 자동매매 켜져 있음: {at.config()['mode']} (끄려면 화면의 🤖 자동매매 → 긴급 정지)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n종료")
    finally:
        server.server_close()
