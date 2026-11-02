"""브라우저 화면 (Claude 앱 스타일) — 내 PC에서만 열리는 로컬 서버.

  python -m engine web           # 자동으로 브라우저가 열립니다 (http://127.0.0.1:8765)
  python -m engine web --port 9000 --no-browser

추가 설치 없이 파이썬 기본 기능만 사용합니다.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import agents as ag
from . import knowledge as kn
from . import learn as ln
from . import providers as pv

STATIC_DIR = Path(__file__).resolve().parent / "web"
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
            lines.append(f"\n### {who}\n\n{m['content'].strip()}\n")
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

    def _send(self, event: str, data):
        payload = json.dumps(data, ensure_ascii=False)
        self.wfile.write(f"event: {event}\ndata: {payload}\n\n".encode())
        self.wfile.flush()

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def _local_only(self) -> bool:
        """다른 웹사이트가 이 서버로 요청을 보내지 못하도록:
        POST 는 커스텀 헤더(X-Company-AI)가 있어야 함 → 브라우저가 교차 출처 요청을 막음."""
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            self._json({"error": "로컬 접속만 허용"}, 403)
            return False
        if self.command == "POST" and self.headers.get("X-Company-AI") != "1":
            self._json({"error": "잘못된 요청"}, 403)
            return False
        return True

    # GET
    def do_GET(self):
        if not self._local_only():
            return
        path = urlparse(self.path).path
        if path == "/" or path == "/index.html":
            return self._static("index.html", "text/html; charset=utf-8")
        if path == "/api/meta":
            models = ag.load_models()
            return self._json({
                "agents": [{"id": a, "summary": ag.agent_summary(a).replace("**", ""), "model": models.get(a)}
                           for a in ag.list_agents()],
                "models": [{"label": l, "value": v} for l, v in MODEL_CHOICES],
                "company": _company_name(),
            })
        if path == "/api/conversations":
            return self._json(list_convs())
        m = re.match(r"^/api/conversations/([\w\-]+)$", path)
        if m:
            try:
                return self._json(load_conv(m.group(1)))
            except (FileNotFoundError, ValueError):
                return self._json({"error": "없는 대화"}, 404)
        if path == "/api/wiki":
            return self._json(_wiki_list())
        m = re.match(r"^/api/wiki/(.+)$", path)
        if m:
            return self._wiki_file(unquote(m.group(1)))
        self._json({"error": "not found"}, 404)

    def _static(self, name: str, ctype: str):
        body = (STATIC_DIR / name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _wiki_file(self, rel: str):
        path = (ln.WIKI_DIR / rel).resolve()
        if not str(path).startswith(str(ln.WIKI_DIR.resolve())) or path.suffix != ".md" or not path.is_file():
            return self._json({"error": "없는 노트"}, 404)
        return self._json({"path": rel, "content": path.read_text(encoding="utf-8")})

    # POST
    def do_POST(self):
        if not self._local_only():
            return
        path = urlparse(self.path).path
        try:
            body = self._body()
        except json.JSONDecodeError:
            return self._json({"error": "JSON 형식 오류"}, 400)
        routes = {"/api/chat": self._chat, "/api/learn": self._learn, "/api/simulate": self._simulate}
        if path in routes:
            self._sse_start()
            try:
                routes[path](body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # 사용자가 중지 버튼을 누름
            except Exception as e:
                with contextlib.suppress(Exception):
                    self._send("error", {"message": str(e)})
            return
        m = re.match(r"^/api/conversations/([\w\-]+)/delete$", path)
        if m:
            import shutil

            with contextlib.suppress(ValueError, FileNotFoundError):
                shutil.rmtree(conv_dir(m.group(1)))
            return self._json({"ok": True})
        self._json({"error": "not found"}, 404)

    # 채팅
    def _chat(self, body: dict):
        text = (body.get("message") or "").strip()
        if not text:
            return self._send("error", {"message": "메시지가 비어 있어요"})
        cid = body.get("conversation_id")
        conv = load_conv(cid) if cid else new_conv(text)
        requested = body.get("agent") or "auto"
        override = body.get("model") or None

        agent_id = requested if requested != "auto" else conv.get("agent")
        reason = ""
        if not agent_id:
            spec = pv.parse_model(override or ag.Agent("ceo").model_spec())
            reply = pv.chat(spec, ag.route_prompt(), [{"role": "user", "content": text}], effort="low")
            agent_id, reason = ag.parse_route(reply)
        if not ag.Agent(agent_id).exists():
            return self._send("error", {"message": f"없는 에이전트: {agent_id}"})
        conv["agent"] = agent_id

        spec = pv.parse_model(override or ag.Agent(agent_id).model_spec())
        history = [{"role": m["role"], "content": m["content"]} for m in conv["messages"]
                   if m.get("kind") in (None, "chat")]
        history.append({"role": "user", "content": text})
        system = ag.Agent(agent_id).system_prompt()
        refs = kn.context_for(text) if body.get("knowledge", True) else ""
        sources = sorted({h.source for _, h in kn.search(text)}) if refs else []
        if refs:
            system += "\n\n" + refs

        conv["messages"].append({"role": "user", "content": text})
        save_conv(conv)
        self._send("meta", {"conversation_id": conv["id"], "title": conv["title"], "agent": agent_id,
                            "model": str(spec), "reason": reason, "sources": sources})
        reply = pv.chat(spec, system, history, on_text=lambda t: self._send("delta", {"text": t}),
                        effort=body.get("effort") or "medium")
        conv["messages"].append({"role": "assistant", "content": reply, "agent": agent_id,
                                 "model": str(spec), "sources": sources})
        save_conv(conv)
        self._send("done", {"conversation_id": conv["id"]})

    # 공부
    def _learn(self, body: dict):
        spec = pv.parse_model(body.get("model") or ag.Agent("researcher").model_spec())
        topic = (body.get("topic") or "").strip()
        topics = [topic] if topic else ln.pick_topics(spec, int(body.get("rounds") or 1))
        if not topics:
            return self._send("error", {"message": "공부할 주제를 못 정했어요"})
        for t in topics:
            self._send("log", {"text": f"📖 공부 시작: {t} ({spec})"})
            path = ln.learn(t, spec, log=lambda s: self._send("log", {"text": s}))
            rel = str(path.relative_to(ln.WIKI_DIR))
            self._send("note", {"path": rel, "content": path.read_text(encoding="utf-8")})
        self._send("done", {})

    # 시뮬레이션
    def _simulate(self, body: dict):
        from . import simulate as sm

        goal = (body.get("goal") or "").strip()
        spec = pv.parse_model(body.get("model") or ag.Agent("business").model_spec())
        model = None
        buf = io.StringIO()
        if body.get("stock"):
            ticker, target = body["stock"].strip(), float(body.get("target") or 10)
            years = float(body.get("years") or 1)
            self._send("log", {"text": f"📈 {ticker} 가격 데이터 불러오는 중..."})
            result = sm.run_stock(sm.load_prices(ticker, None), years, target)
            goal = goal or f"{ticker} {years:g}년 +{target:g}%"
            with contextlib.redirect_stdout(buf):
                sm.print_stock_result(ticker, result)
        else:
            if not goal:
                return self._send("error", {"message": "목표를 적어 주세요"})
            self._send("log", {"text": f"🧠 시나리오 설계 중... ({spec})"})
            model = sm.build_model(goal, spec)
            self._send("log", {"text": "🎲 1만 번 시뮬레이션 중..."})
            result = sm.run_model(model)
            with contextlib.redirect_stdout(buf):
                sm.print_model_result(model, result)
        self._send("result", {"text": buf.getvalue().strip(), "result": result, "model": model})
        explanation = sm.explain(spec, goal, model, result,
                                 on_text=lambda t: self._send("delta", {"text": t}))
        path = sm.save_report(goal, model, result, explanation)
        if model:
            path.with_suffix(".json").write_text(json.dumps(model, ensure_ascii=False, indent=2), encoding="utf-8")
        self._send("done", {"report": str(path.relative_to(ag.COMPANY_DIR))})


def _company_name() -> str:
    m = re.search(r"회사 이름:\*\*\s*(.+)", ag._read(ag.SHARED_DIR / "identity.md"))
    return m.group(1).strip() if m else "1인 기업"


def _wiki_list() -> list[dict]:
    if not ln.WIKI_DIR.is_dir():
        return []
    out = []
    for p in sorted(ln.WIKI_DIR.rglob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True):
        text = p.read_text(encoding="utf-8", errors="replace")
        topic = re.search(r"^topic:\s*(.+)$", text, re.M)
        status = re.search(r"^status:\s*(.+)$", text, re.M)
        out.append({"path": str(p.relative_to(ln.WIKI_DIR)), "topic": topic.group(1) if topic else p.stem,
                    "status": status.group(1).strip() if status else ""})
    return out


def serve(port: int = 8765, open_browser: bool = True) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    url = f"http://127.0.0.1:{port}"
    print(f"🌐 {url} 에서 열렸어요. 끄려면 이 창에서 Ctrl+C")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n종료")
    finally:
        server.server_close()
