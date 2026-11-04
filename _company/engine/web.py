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
from urllib.parse import unquote, urlparse

from . import agents as ag
from . import knowledge as kn
from . import learn as ln
from . import providers as pv
from . import team as tm

STATIC_DIR = Path(__file__).resolve().parent / "web"
UPLOAD_DIR = ag.COMPANY_DIR / "_cache" / "uploads"   # git 에 안 올라감
IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024
UPLOAD_RE = re.compile(r"^[\w\-]+\.(png|jpg|webp|gif)$")
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


# ── 채팅 중 자동 실행 (시뮬레이션 / 공부) ─────────────────────────────────
ACTION_GUIDE = """[자동 실행 기능]
답변 맨 마지막 줄에 아래 태그 중 하나를 쓰면, 시스템이 실제로 실행하고 결과를 이 대화에 바로 붙여줍니다.
- [[SIMULATE: 목표 한 문장]] — 사용자가 "~할 수 있을까?", "가능해?", 달성 확률·기간·예상 수익을 물을 때.
  예: [[SIMULATE: 애드센스 블로그로 2년 안에 월 순수익 1000만원]]
- [[STOCK: 티커 | 년수 | 목표수익률%]] — 특정 주식/ETF 의 목표 수익률 달성 확률을 물을 때 (한국 주식은 005930.KS 형식).
  예: [[STOCK: 005930.KS | 1 | 20]]
- [[LEARN: 주제]] — 참고자료에 없는 최신 정보나 사실 조사가 꼭 필요할 때 (웹을 직접 검색해 노트로 저장).
규칙: 한 답변에 태그는 최대 1개. 태그를 쓸 때는 본문을 2~4줄로 짧게 — 무엇을 왜 돌리는지만 말하고, 숫자 예측은 결과에 맡기세요.
필요 없으면 태그를 쓰지 마세요. 사용자가 그냥 대화나 글쓰기를 원하면 평소처럼 답하세요."""

ACTION_RE = re.compile(r"\[\[(SIMULATE|STOCK|LEARN):\s*(.+?)\]\]", re.S)


def find_action(reply: str) -> tuple[str, str] | None:
    m = ACTION_RE.search(reply)
    return (m.group(1), m.group(2).strip()) if m else None


def chat_history(conv: dict) -> list[dict]:
    """저장된 메시지 → 모델용 history. 연속된 assistant 메시지는 하나로 합칩니다."""
    out: list[dict] = []
    for m in conv["messages"]:
        content = m["content"]
        if m.get("kind") == "tool":
            content = f"[자동 실행 결과: {m.get('tool', '')}]\n{content}"
        if out and out[-1]["role"] == m["role"]:
            out[-1]["content"] += "\n\n" + content
        else:
            out.append({"role": m["role"], "content": content})
    return out


def run_simulation(goal: str, stock: dict | None, spec, log, on_text) -> dict:
    """시뮬레이션 공통 실행 — 채팅과 시뮬레이션 화면이 함께 씁니다."""
    from . import simulate as sm

    model, buf = None, io.StringIO()
    if stock:
        ticker, years, target = stock["ticker"], float(stock.get("years") or 1), float(stock.get("target") or 10)
        log(f"📈 {ticker} 가격 데이터 불러오는 중...")
        result = sm.run_stock(sm.load_prices(ticker, None), years, target)
        goal = goal or f"{ticker} {years:g}년 +{target:g}%"
        with contextlib.redirect_stdout(buf):
            sm.print_stock_result(ticker, result)
    else:
        log(f"🧠 시나리오 설계 중... ({spec})")
        model = sm.build_model(goal, spec)
        log("🎲 1만 번 시뮬레이션 중...")
        result = sm.run_model(model)
        with contextlib.redirect_stdout(buf):
            sm.print_model_result(model, result)
    text = buf.getvalue().strip()
    explanation = sm.explain(spec, goal, model, result, on_text=on_text(text))
    path = sm.save_report(goal, model, result, explanation)
    if model:
        path.with_suffix(".json").write_text(json.dumps(model, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"text": text, "explanation": explanation, "report": str(path.relative_to(ag.COMPANY_DIR)),
            "result": result, "model": model}


def parse_stock_arg(arg: str) -> dict:
    parts = [x.strip().rstrip("%") for x in arg.split("|")]
    return {"ticker": parts[0], "years": parts[1] if len(parts) > 1 else 1, "target": parts[2] if len(parts) > 2 else 10}


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
                "agents": [{"id": t, "summary": ag.agent_summary(t), "model": models.get(t),
                            "members": [{"id": m, "view": ag.member_view(m)} for m in ag.team_members(t)]}
                           for t in ag.list_teams()],
                "models": [{"label": l, "value": v} for l, v in MODEL_CHOICES],
                "company": _company_name(),
                "team_mode": tm.settings().get("team_mode", "auto"),
            })
        if path == "/api/conversations":
            return self._json(list_convs())
        m = re.match(r"^/api/conversations/([\w\-]+)$", path)
        if m:
            try:
                return self._json(load_conv(m.group(1)))
            except (FileNotFoundError, ValueError):
                return self._json({"error": "없는 대화"}, 404)
        m = re.match(r"^/api/upload/([\w\-]+\.(?:png|jpg|webp|gif))$", path)
        if m and (UPLOAD_DIR / m.group(1)).is_file():
            ext = m.group(1).rsplit(".", 1)[1]
            body = (UPLOAD_DIR / m.group(1)).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", {"jpg": "image/jpeg"}.get(ext, f"image/{ext}"))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
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
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                log("  (사용자가 중지했거나 연결이 끊김)")
            except Exception as e:
                log(f"  ⚠️ 오류: {e}")
                traceback.print_exc()
                with contextlib.suppress(Exception):
                    self._send("error", {"message": str(e)})
            return
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

    def _market_context(self, agent_id: str, text: str, history: list[dict], spec) -> str:
        """주식 팀: 질문에서 종목을 찾아 무료 시세·차트 지표를 가져와 팀원들에게 붙여줍니다."""
        from concurrent.futures import ThreadPoolExecutor

        from . import market as mk

        if not tm.team_config(agent_id).get("market_data"):
            return ""
        self._send("status", {"text": "📈 종목 찾는 중..."})
        recent = "\n".join(re.sub(r"\[화면 캡처 \d+장 첨부\]", "", m["content"])[:500] for m in history[-3:])
        try:
            reply = pv.chat(spec, mk.TICKER_PROMPT, [{"role": "user", "content": recent}], effort="low")
            # 티커 모양만 통과 (6자리.KS/.KQ 또는 영문 티커) — 엉뚱한 글자는 버림
            tickers = [t.strip().upper() for t in ln._json_list(reply)
                       if re.fullmatch(r"\d{6}(\.(KS|KQ))?|[A-Za-z][A-Za-z.\-]{0,9}", t.strip())][:5]
        except pv.ProviderError:
            tickers = []
        if not tickers:
            self._send("status", {"text": ""})
            return ""
        self._send("status", {"text": "📈 " + ", ".join(tickers) + " 시세·차트 지표 가져오는 중..."})
        with ThreadPoolExecutor(max_workers=len(tickers)) as pool:
            snaps = list(pool.map(mk.snapshot, tickers))
        self._send("market", {"items": [s.brief() for s in snaps]})
        self._send("status", {"text": ""})
        return mk.format_for_prompt(snaps)

    # 채팅
    def _chat(self, body: dict):
        text = (body.get("message") or "").strip()
        image_ids = [i for i in (body.get("images") or []) if UPLOAD_RE.match(str(i)) and (UPLOAD_DIR / i).is_file()][:6]
        images = [str(UPLOAD_DIR / i) for i in image_ids]
        if not text and images:
            text = "첨부한 화면을 분석해 줘."
        if not text:
            return self._send("error", {"message": "메시지가 비어 있어요"})
        cid = body.get("conversation_id")
        conv = load_conv(cid) if cid else new_conv(text)
        requested = body.get("agent") or "auto"
        override = body.get("model") or None

        agent_id = ag.team_of(requested) if requested != "auto" else conv.get("agent")
        if agent_id and agent_id not in ag.list_teams():
            agent_id = ag.team_of(agent_id) if ag.team_of(agent_id) in ag.list_teams() else None
        reason = ""
        log(f"💬 질문: {text[:40]}")
        if not agent_id:
            spec = pv.parse_model(override or ag.Agent("ceo").model_spec())
            log(f"  🧭 담당 팀 고르는 중 ({spec})...")
            try:
                reply = pv.chat(spec, ag.route_prompt(), [{"role": "user", "content": text}], effort="low", timeout=120)
            except pv.ProviderError as e:
                log(f"  ⚠️ 배정 실패: {e}")
                raise
            quick = ag.parse_quick(reply) if not image_ids else None
            if quick:  # 인사·잡담: 팀 배정 없이 CEO 가 바로 짧게
                log("  → 가벼운 말이라 CEO가 바로 답함")
                conv["messages"] += [{"role": "user", "content": text},
                                     {"role": "assistant", "content": quick, "agent": "ceo", "model": str(spec)}]
                save_conv(conv)
                self._send("meta", {"conversation_id": conv["id"], "title": conv["title"], "agent": "ceo",
                                    "model": str(spec), "reason": "", "sources": [], "team": 1})
                self._send("delta", {"text": quick})
                return self._send("done", {"conversation_id": conv["id"]})
            agent_id, reason = ag.parse_route(reply)
            log(f"  → {agent_id} 팀")
        if agent_id not in ag.list_teams():
            return self._send("error", {"message": f"없는 팀: {agent_id}"})
        conv["agent"] = agent_id

        spec = pv.parse_model(override or ag.Agent(ag.team_members(agent_id)[0]).model_spec())
        history = chat_history(conv)
        if history and history[-1]["role"] == "user":
            history[-1]["content"] += "\n\n" + text
        else:
            history.append({"role": "user", "content": text})
        extra = ACTION_GUIDE if body.get("actions", True) else ""
        refs = kn.context_for(text) if body.get("knowledge", True) else ""
        sources = sorted({h.source for _, h in kn.search(text)}) if refs else []
        if refs:
            extra += "\n\n" + refs
        members = ag.team_members(agent_id)
        # 팀 비교: auto(분석·판단 질문만) / on / off — 화면 선택값, 없으면 settings.json
        mode = body.get("team")
        mode = {True: "on", False: "off"}.get(mode, mode)
        short_talk = len(text) <= 8 and not images and "?" not in text
        use_team = len(members) > 1 and not short_talk and tm.wants_team(text, bool(images), mode)

        if images:
            history[-1]["content"] += f"\n\n[화면 캡처 {len(images)}장 첨부]"
        conv["messages"].append({"role": "user", "content": text, "images": image_ids})
        save_conv(conv)
        self._send("meta", {"conversation_id": conv["id"], "title": conv["title"], "agent": agent_id,
                            "model": str(spec), "reason": reason, "sources": sources,
                            "team": len(members) if use_team else 1,
                            "member_model": (override or tm.team_config(agent_id).get("member_model") or "") if use_team else ""})
        delta = lambda t: self._send("delta", {"text": t})  # noqa: E731
        effort = body.get("effort") or "medium"
        market = self._market_context(agent_id, text, history, spec)
        if market:
            extra += "\n\n" + market
        drafts, steps = [], []
        style = tm.settings().get("team_style", "review")
        if use_team and style == "review":
            reply, steps, _ = tm.run_review(
                agent_id, history, extra, override, effort,
                on_step_start=lambda st: self._send("step_start", st.as_dict()),
                on_step_done=lambda st: self._send("step_done", st.as_dict()),
                on_text=delta, images=images,
            )
        elif use_team:
            reply, drafts, _ = tm.run_team(
                agent_id, history, extra, override, effort,
                on_draft_start=lambda i, d: self._send("draft_start", {"i": i, "member": d.member, "view": d.view}),
                on_draft_done=lambda i, d: self._send("draft_done", {"i": i, "member": d.member, "view": d.view,
                                                                      "text": d.text, "error": d.error}),
                on_judge_start=lambda: self._send("judge_start", {}),
                on_text=delta,
                images=images,
            )
        else:
            lead = members[0]
            system = ag.Agent(lead).system_prompt() + ("\n\n" + extra if extra else "")
            reply = pv.chat(spec, system, history, on_text=delta, effort=effort,
                            web=bool(tm.team_config(agent_id).get("web")), images=images)
        conv["messages"].append({"role": "assistant", "content": reply, "agent": agent_id,
                                 "model": str(spec), "sources": sources,
                                 "drafts": [{"member": d.member, "view": d.view, "text": d.text, "error": d.error}
                                            for d in drafts],
                                 "steps": [st.as_dict() for st in steps]})
        save_conv(conv)
        action = find_action(reply) if body.get("actions", True) else None
        if action:
            self._run_action(conv, *action, override)
        log("  ✅ 답변 완료")
        self._send("done", {"conversation_id": conv["id"]})

    def _run_action(self, conv: dict, kind: str, arg: str, override: str | None):
        label = {"SIMULATE": "🎲 시뮬레이션", "STOCK": "📈 주식 시뮬레이션", "LEARN": "📖 공부"}[kind]
        self._send("tool_start", {"kind": kind, "label": label, "arg": arg})
        log = lambda t: self._send("tool_log", {"text": t})  # noqa: E731
        try:
            if kind == "LEARN":
                spec = pv.parse_model(override or ag.Agent("researcher").model_spec())
                path = ln.learn(arg, spec, log=log)
                note = re.sub(r"^---.*?---\n", "", path.read_text(encoding="utf-8"), flags=re.S).strip()
                content = note
                footer = f"10_Wiki/{path.relative_to(ln.WIKI_DIR)}"
                self._send("tool_result", {"markdown": note})
                self._send("tool_done", {"footer": f"📝 노트 저장: {footer}"})
            else:
                spec = pv.parse_model(override or ag.Agent("startup-money").model_spec())
                stock = parse_stock_arg(arg) if kind == "STOCK" else None

                def on_text(result_text):
                    self._send("tool_result", {"text": result_text})
                    return lambda t: self._send("tool_delta", {"text": t})

                out = run_simulation("" if stock else arg, stock, spec, log, on_text)
                content = f"```\n{out['text']}\n```\n\n{out['explanation']}"
                footer = out["report"]
                self._send("tool_done", {"footer": f"📝 보고서: {footer}"})
        except Exception as e:  # 시뮬/공부 실패해도 대화는 유지
            self._send("tool_error", {"message": str(e)})
            content, footer = f"실행 실패: {e}", ""
        conv["messages"].append({"role": "assistant", "kind": "tool", "tool": f"{label}: {arg}",
                                 "content": content, "agent": label, "footer": footer})
        save_conv(conv)

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
        goal = (body.get("goal") or "").strip()
        spec = pv.parse_model(body.get("model") or ag.Agent("startup-money").model_spec())
        stock = None
        if body.get("stock"):
            stock = {"ticker": body["stock"].strip(), "years": body.get("years"), "target": body.get("target")}
        elif not goal:
            return self._send("error", {"message": "목표를 적어 주세요"})

        def on_text(result_text):
            self._send("result", {"text": result_text})
            return lambda t: self._send("delta", {"text": t})

        out = run_simulation(goal, stock, spec, lambda t: self._send("log", {"text": t}), on_text)
        self._send("done", {"report": out["report"]})


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
