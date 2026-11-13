"""도구 요청 → 사장님 승인.

AI 가 답 끝에 [[TOOL_REQUEST: 도구ID | 이유]] 를 쓰면 요청 카드가 뜹니다.
- 허용 목록(_shared/tools_catalog.json)에 있는 도구만 설치·연결할 수 있어요.
- pip 도구: 승인하면 공식 저장소(PyPI)에서 목록에 적힌 패키지 이름 그대로 설치 (AI 가 쓴 글자는 설치 명령에 안 들어감)
- api_key 도구: 승인하면 가입 방법 안내 → 사장님이 🧰 도구 화면에서 키 입력 → .env 에 저장
- 목록에 없는 도구: 설치하지 않고 요청 기록만 남김 (나중에 검토)
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime

from .agents import COMPANY_DIR, SHARED_DIR

CATALOG_FILE = SHARED_DIR / "tools_catalog.json"
REQUESTS_FILE = SHARED_DIR / "tool_requests.json"
ENV_FILE = COMPANY_DIR / ".env"
TOOL_RE = re.compile(r"\[\[TOOL_REQUEST:\s*([^|\]]+?)\s*(?:\|\s*([^\]]*?))?\s*\]\]")
_lock = threading.Lock()


def catalog() -> list[dict]:
    try:
        return json.loads(CATALOG_FILE.read_text(encoding="utf-8")).get("tools", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def find(tool: str) -> dict | None:
    key = tool.strip().lower()
    for t in catalog():
        if key in (t["id"].lower(), t["name"].lower(), t.get("package", "").lower()):
            return t
    return None


def status(t: dict) -> str:
    """installed / missing / key_set / key_needed"""
    if t["type"] == "pip":
        mod = {"pykrx": "pykrx", "ddgs": "ddgs", "yfinance": "yfinance"}.get(t["package"], t["package"].replace("-", "_"))
        return "installed" if importlib.util.find_spec(mod) else "missing"
    return "key_set" if os.getenv(t.get("env", "")) else "key_needed"


def guide_for_agents(team: str | None = None) -> str:
    """AI 에게 알려줄 요청 가능 도구 목록 (팀에 맞는 것만)."""
    rows = [t for t in catalog() if not team or team in t.get("teams", [])]
    if not rows:
        return ""
    lines = [f"- {t['id']}: {t['name']} — {t['desc']} ({'사용 가능' if status(t) in ('installed', 'key_set') else '아직 없음'})"
             for t in rows]
    return ("[도구 요청]\n답에 꼭 필요한데 '아직 없음'인 도구가 있으면, 답 맨 마지막 줄에 [[TOOL_REQUEST: 도구ID | 왜 필요한지 한 줄]] 을 쓰세요. "
            "사장님이 승인하면 연결됩니다. 한 번에 하나만, 정말 필요할 때만. 목록에 없는 도구도 요청할 수 있지만 자동 설치는 안 돼요.\n"
            + "\n".join(lines))


def find_requests(reply: str) -> list[tuple[str, str]]:
    return [(m.group(1).strip(), (m.group(2) or "").strip()) for m in TOOL_RE.finditer(reply)][:2]


# ── 요청 기록 ─────────────────────────────────────────────────────────────
def _load() -> list[dict]:
    try:
        return json.loads(REQUESTS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save(rows: list[dict]) -> None:
    REQUESTS_FILE.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")


def add_request(tool: str, why: str, team: str) -> dict:
    t = find(tool)
    with _lock:
        rows = _load()
        # 같은 도구의 대기 중 요청이 있으면 새로 만들지 않음
        for r in rows:
            if r["status"] == "pending" and r["tool_id"] == (t["id"] if t else tool.lower()):
                return r
        req = {"id": datetime.now().strftime("%Y%m%d%H%M%S") + os.urandom(2).hex(),
               "tool_id": t["id"] if t else tool.lower(), "name": t["name"] if t else tool,
               "in_catalog": bool(t), "type": t["type"] if t else "unknown", "why": why[:300], "team": team,
               "status": "pending" if t else "noted", "time": datetime.now().isoformat(timespec="seconds")}
        rows.append(req)
        _save(rows[-200:])
    return req


def requests() -> list[dict]:
    return list(reversed(_load()))


def set_status(req_id: str, new: str, note: str = "") -> dict | None:
    with _lock:
        rows = _load()
        for r in rows:
            if r["id"] == req_id:
                r["status"], r["note"] = new, note
                r["decided"] = datetime.now().isoformat(timespec="seconds")
                _save(rows)
                return r
    return None


def approve(req_id: str) -> dict:
    req = next((r for r in _load() if r["id"] == req_id), None)
    if not req:
        return {"ok": False, "message": "없는 요청이에요"}
    t = find(req["tool_id"])
    if not t:  # 허용 목록에 없음 → 설치 안 함
        set_status(req_id, "noted", "허용 목록에 없어 자동 설치하지 않음")
        return {"ok": False, "message": "허용 목록에 없는 도구라 자동으로 설치하지 않았어요. 기록만 남겼어요."}
    if t["type"] == "pip":
        if status(t) == "installed":
            set_status(req_id, "done", "이미 설치됨")
            return {"ok": True, "message": f"{t['name']} 은(는) 이미 설치돼 있어요."}
        # 패키지 이름은 허용 목록 값만 사용 (AI 가 쓴 글자는 명령에 안 들어감)
        pkg = t["package"]
        if not re.fullmatch(r"[A-Za-z0-9._\-]+", pkg):
            return {"ok": False, "message": "목록의 패키지 이름이 이상해요"}
        r = subprocess.run([sys.executable, "-m", "pip", "install", pkg], capture_output=True, text=True,
                           timeout=600, encoding="utf-8", errors="replace")
        if r.returncode == 0:
            importlib.invalidate_caches()
            set_status(req_id, "done", "설치 완료")
            return {"ok": True, "message": f"{t['name']} 설치 완료"}
        set_status(req_id, "failed", (r.stderr or r.stdout)[-300:])
        return {"ok": False, "message": "설치 실패: " + (r.stderr or r.stdout)[-300:]}
    # api_key
    set_status(req_id, "approved", "키 입력 대기")
    return {"ok": True, "message": f"{t['name']} 승인됨 — 가입 후 🧰 도구 화면에서 키를 입력하세요.",
            "signup": t.get("signup"), "steps": t.get("steps", [])}


def save_key(tool_id: str, value: str) -> dict:
    t = find(tool_id)
    if not t or t["type"] != "api_key":
        return {"ok": False, "message": "키를 넣을 수 없는 도구예요"}
    value = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9._@:\-]{4,200}", value):
        return {"ok": False, "message": "키 형식이 이상해요 (영문·숫자만, 공백 없이)"}
    env = t["env"]
    with _lock:
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
        lines = [l for l in lines if not l.startswith(env + "=")] + [f"{env}={value}"]
        ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[env] = value
    for r in _load():
        if r["tool_id"] == t["id"] and r["status"] in ("pending", "approved"):
            set_status(r["id"], "done", "키 입력 완료")
    return {"ok": True, "message": f"{t['name']} 키를 저장했어요 (.env 의 {env})"}


def overview() -> dict:
    return {"tools": [{**{k: v for k, v in t.items() if k != "env"}, "status": status(t)} for t in catalog()],
            "requests": requests()[:50]}
