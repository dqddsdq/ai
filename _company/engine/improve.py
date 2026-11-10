"""🛠️ 자기 개선: AI 가 제안 → 개발 AI 가 '복사본'에서 고침 → 자동 검사 → 사장님 확인 → 적용(자동 백업, 되돌리기 가능)

안전 규칙
- 원본은 사장님이 [적용]을 누르기 전까지 절대 바뀌지 않아요. 수정은 _cache/improve/<id>/ 복사본에서만.
- 개발 AI 는 Read·Edit·Write·Grep·Glob 만 쓸 수 있어요 (명령 실행·인터넷 없음).
- 보호 파일(키·도구 허용 목록·도구 설치·이 파일)을 건드린 변경안은 자동 거부.
- 적용 전 원본을 _cache/backups/<id>/ 에 백업 → [되돌리기]로 복구.
- Git 이 없어도 동작 (파일 복사 방식).
"""

from __future__ import annotations

import difflib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from . import providers as pv
from .agents import COMPANY_DIR, SHARED_DIR

WORK_ROOT = COMPANY_DIR / "_cache" / "improve"
BACKUP_ROOT = COMPANY_DIR / "_cache" / "backups"
HISTORY = SHARED_DIR / "improvements.json"
EDITABLE_DIRS = ("engine", "_agents", "_shared")
PROTECTED = {".env", "_shared/tools_catalog.json", "engine/tools.py", "engine/improve.py", "engine/toss.py", "engine/autotrade.py", "engine/tradelearn.py", "engine/picker.py", "engine/shadow.py", "engine/backtest.py", "engine/notify.py", "engine/store.py", "_shared/autotrade.json",
             "_shared/improvements.json", "_shared/tool_requests.json"}
SKIP = {"__pycache__", "archive"}
RUNTIME = {"_shared/improvements.json", "_shared/tool_requests.json", "_shared/autotrade.json"}   # 실행 중 계속 바뀌는 기록 파일 (복사·비교 안 함)
TEXT_EXT = {".py", ".md", ".json", ".html", ".txt", ".js", ".css"}
TIMEOUT = 900
_lock = threading.Lock()

IMPROVE_GUIDE = """[자기 개선 제안]
당신의 지시문(prompt.md)·설정·이 앱의 코드 때문에 사장님 요청을 제대로 못 했다면, 답 맨 마지막 줄에
[[IMPROVE: 무엇을 어떻게 바꾸면 좋을지 | 왜 필요한지]] 를 쓸 수 있어요. 사장님이 확인하고 승인해야 적용돼요.
정말 필요할 때만, 한 번에 하나. 보안·키·도구 설치 규칙을 약하게 바꾸자는 제안은 하지 마세요."""

DEV_PROMPT = """너는 이 PC에서 돌아가는 '1인 기업 AI' 앱의 개발자다. 지금 작업 폴더는 앱의 복사본이다.
요청에 맞게 필요한 파일만 최소한으로 고쳐라. 원본이 아니니 안심하고 고치되, 앱이 계속 켜지도록 조심해라.

구조: engine/ (파이썬 엔진, web/index.html 은 화면), _agents/<팀>/ (prompt.md 지시문, team.json 팀 설정),
_shared/ (settings.json 설정, goals.md·decisions.md·identity.md 회사 메모리). engine/README.md 에 전체 설명이 있다.

규칙:
- 아래 파일은 절대 수정·삭제하지 마라: {protected}
- 보안 장치(로컬 접속 제한, 도구 허용 목록, 키 보관, 승인 절차)를 약하게 만들지 마라. 그런 요청이면 고치지 말고 이유만 써라.
- 새 외부 패키지·인터넷 주소를 추가해야 하면 고치지 말고 필요하다고만 써라.
- 주석은 한국어로 짧게.
- 다 고친 뒤 마지막에 사장님이 읽을 요약을 한국어로 3~6줄: 무엇을 왜 바꿨는지, 적용 후 앱을 다시 켜야 하는지(코드 수정이면 '다시 켜야 함', 지시문·설정만이면 '바로 적용').

요청:
{request}"""


# ── 기록 ──────────────────────────────────────────────────────────────────
def _load() -> list[dict]:
    try:
        return json.loads(HISTORY.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save(rows: list[dict]) -> None:
    HISTORY.write_text(json.dumps(rows[-100:], ensure_ascii=False, indent=1), encoding="utf-8")


def _update(job_id: str, **fields) -> dict | None:
    with _lock:
        rows = _load()
        for r in rows:
            if r["id"] == job_id:
                r.update(fields)
                _save(rows)
                return r
    return None


def history() -> list[dict]:
    return list(reversed(_load()))


def get(job_id: str) -> dict | None:
    return next((r for r in _load() if r["id"] == job_id), None)


def create(request: str, why: str = "", team: str = "사장님") -> dict:
    job = {"id": datetime.now().strftime("%Y%m%d-%H%M%S-") + os.urandom(2).hex(), "request": request.strip()[:2000],
           "why": why.strip()[:500], "team": team, "status": "proposed", "time": datetime.now().isoformat(timespec="seconds")}
    with _lock:
        rows = _load()
        rows.append(job)
        _save(rows)
    return job


def find_proposals(reply: str) -> list[tuple[str, str]]:
    import re

    return [(m.group(1).strip(), (m.group(2) or "").strip())
            for m in re.finditer(r"\[\[IMPROVE:\s*([^|\]]+?)\s*(?:\|\s*([^\]]*?))?\s*\]\]", reply)][:1]


# ── 복사본 만들기 / 비교 ──────────────────────────────────────────────────
def _files(root: Path) -> dict[str, Path]:
    out = {}
    for d in EDITABLE_DIRS:
        base = root / d
        if not base.exists():
            continue
        for f in base.rglob("*"):
            rel = f.relative_to(root).as_posix()
            if f.is_file() and not (set(f.relative_to(root).parts) & SKIP) and f.suffix in TEXT_EXT and rel not in RUNTIME:
                out[rel] = f
    return out


def _make_sandbox(job_id: str) -> Path:
    """복사본(box) + 만든 순간의 스냅샷(base). 비교는 base 기준 → 개발 AI 가 실제로 바꾼 것만 '변경'으로 잡힘."""
    box, base = WORK_ROOT / job_id, WORK_ROOT / f"{job_id}.base"
    for d in (box, base):
        if d.exists():
            shutil.rmtree(d)
    for rel, src in _files(COMPANY_DIR).items():
        for root in (box, base):
            dst = root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    return box


def changes(job_id: str) -> list[dict]:
    box, base = WORK_ROOT / job_id, WORK_ROOT / f"{job_id}.base"
    orig, new = _files(base), _files(box)
    out = []
    for rel in sorted(set(orig) | set(new)):
        a = orig[rel].read_text(encoding="utf-8", errors="replace") if rel in orig else ""
        b = new[rel].read_text(encoding="utf-8", errors="replace") if rel in new else ""
        if a == b:
            continue
        diff = "".join(difflib.unified_diff(a.splitlines(True), b.splitlines(True), f"원본/{rel}", f"수정/{rel}", n=2))
        out.append({"path": rel, "status": "added" if rel not in orig else "deleted" if rel not in new else "modified",
                    "protected": rel in PROTECTED, "diff": diff[:20000],
                    "plus": sum(1 for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++")),
                    "minus": sum(1 for l in diff.splitlines() if l.startswith("-") and not l.startswith("---"))})
    return out


def _checks(box: Path, files: list[dict]) -> list[dict]:
    res = []
    if any(f["protected"] for f in files):
        res.append({"name": "보호 파일", "ok": False, "detail": "보호 파일을 건드려서 적용할 수 없어요: "
                    + ", ".join(f["path"] for f in files if f["protected"])})
    py = [f for f in files if f["path"].endswith(".py") and f["status"] != "deleted"]
    if py:
        bad = []
        for f in py:
            r = subprocess.run([sys.executable, "-m", "py_compile", str(box / f["path"])], capture_output=True, text=True)
            if r.returncode:
                bad.append(f"{f['path']}: {(r.stderr or '').strip()[-200:]}")
        res.append({"name": "파이썬 문법", "ok": not bad, "detail": "; ".join(bad) or f"{len(py)}개 파일 통과"})
        r = subprocess.run([sys.executable, "-c", "import engine.web, engine.team, engine.providers"],
                           cwd=box, capture_output=True, text=True, timeout=120)
        res.append({"name": "앱 불러오기", "ok": r.returncode == 0, "detail": "정상" if r.returncode == 0 else (r.stderr or "")[-300:]})
    for f in files:
        if f["path"].endswith(".json") and f["status"] != "deleted":
            try:
                json.loads((box / f["path"]).read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                res.append({"name": f"JSON 형식 ({f['path']})", "ok": False, "detail": str(e)})
    if any(f["path"].endswith(".html") for f in files) and shutil.which("node"):
        import re

        html = (box / "engine/web/index.html").read_text(encoding="utf-8")
        m = re.search(r"<script>([\s\S]*)</script>", html)
        if m:
            tmp = Path(tempfile.mkstemp(suffix=".js")[1])
            tmp.write_text(m.group(1), encoding="utf-8")
            r = subprocess.run(["node", "--check", str(tmp)], capture_output=True, text=True)
            tmp.unlink(missing_ok=True)
            res.append({"name": "화면 스크립트 문법", "ok": r.returncode == 0, "detail": "정상" if r.returncode == 0 else r.stderr[-300:]})
    if not files:
        res.append({"name": "변경 사항", "ok": False, "detail": "바뀐 파일이 없어요"})
    elif not any(not c["ok"] for c in res):
        res.insert(0, {"name": "보호 파일", "ok": True, "detail": "보호 파일은 건드리지 않음"})
    return res


# ── 개발 AI 실행 ──────────────────────────────────────────────────────────
def run(job_id: str, on_text=None, on_activity=None, model: str | None = None) -> dict:
    job = get(job_id)
    if not job:
        raise pv.ProviderError("없는 개선 요청이에요")
    exe = pv.claude_cli()
    if not exe:
        raise pv.ProviderError("Claude Code 가 없어서 변경안을 만들 수 없어요")
    _update(job_id, status="working")
    box = _make_sandbox(job_id)
    prompt = DEV_PROMPT.format(protected=", ".join(sorted(PROTECTED)), request=job["request"] + (f"\n(이유: {job['why']})" if job.get("why") else ""))
    cmd = [exe, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
           "--tools", "Read,Edit,Write,Grep,Glob", "--allowedTools", "Read,Edit,Write,Grep,Glob",
           "--permission-mode", "acceptEdits", "--no-session-persistence", "--strict-mcp-config"]
    if model and model.split(":", 1)[-1] not in ("", "default", "claudecode"):
        cmd += ["--model", model.split(":", 1)[-1]]
    err_file = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err_file, text=True,
                            encoding="utf-8", errors="replace", cwd=box,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    timer = threading.Timer(TIMEOUT, proc.kill)
    timer.daemon = True
    timer.start()
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
        streamed, final, is_error = pv._read_stream_json(proc.stdout, on_text, on_activity)
        proc.wait(timeout=30)
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
    err_file.seek(0)
    err = err_file.read()[-400:]
    if proc.returncode or is_error:
        _update(job_id, status="failed", summary=(final or err or "개발 AI 오류")[:1000])
        raise pv.ProviderError(f"개발 AI 오류: {(final or err)[:300]}")
    files = changes(job_id)
    checks = _checks(box, files)
    ok = all(c["ok"] for c in checks)
    _update(job_id, status="ready" if ok else "blocked", summary=(final or streamed).strip()[:3000],
            files=[{k: v for k, v in f.items() if k != "diff"} for f in files], checks=checks)
    return {**get(job_id), "files": files, "checks": checks, "ok": ok}


def apply(job_id: str) -> dict:
    job = get(job_id)
    if not job or job.get("status") != "ready":
        return {"ok": False, "message": "적용할 수 있는 상태가 아니에요 (검사 통과한 변경안만 적용돼요)"}
    files = changes(job_id)
    if any(f["protected"] for f in files):
        return {"ok": False, "message": "보호 파일이 포함돼 있어 적용하지 않았어요"}
    box, base, backup = WORK_ROOT / job_id, WORK_ROOT / f"{job_id}.base", BACKUP_ROOT / job_id
    # 충돌 확인: 변경안을 만드는 동안 원본도 바뀌었으면(예: AI 기억 추가) 덮어쓰지 않음
    conflicts = []
    for f in files:
        cur, snap = COMPANY_DIR / f["path"], base / f["path"]
        cur_txt = cur.read_text(encoding="utf-8", errors="replace") if cur.exists() else None
        snap_txt = snap.read_text(encoding="utf-8", errors="replace") if snap.exists() else None
        if cur_txt != snap_txt:
            conflicts.append(f["path"])
    if conflicts:
        _update(job_id, status="conflict")
        return {"ok": False, "message": "변경안을 만드는 동안 원본도 바뀌어서 덮어쓰지 않았어요: " + ", ".join(conflicts)
                + " — [버리기] 후 다시 만들어 주세요"}
    backup.mkdir(parents=True, exist_ok=True)
    manifest = []
    for f in files:
        src, dst = box / f["path"], COMPANY_DIR / f["path"]
        if dst.exists():
            (backup / f["path"]).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, backup / f["path"])
        manifest.append({"path": f["path"], "existed": dst.exists()})
        if f["status"] == "deleted":
            dst.unlink(missing_ok=True)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    (backup / "_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    code = any(f["path"].startswith("engine/") for f in files)
    _update(job_id, status="applied", applied=datetime.now().isoformat(timespec="seconds"), needs_restart=code)
    return {"ok": True, "needs_restart": code,
            "message": f"{len(files)}개 파일 적용 완료" + (" — 코드가 바뀌어서 'AI 시작.bat' 을 다시 켜야 해요" if code else " — 바로 적용돼요")}


def rollback(job_id: str) -> dict:
    backup = BACKUP_ROOT / job_id
    try:
        manifest = json.loads((backup / "_manifest.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"ok": False, "message": "백업이 없어서 되돌릴 수 없어요"}
    for m in manifest:
        dst = COMPANY_DIR / m["path"]
        if m["existed"]:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup / m["path"], dst)
        else:
            dst.unlink(missing_ok=True)
    code = any(m["path"].startswith("engine/") for m in manifest)
    _update(job_id, status="rolled_back", rolled_back=datetime.now().isoformat(timespec="seconds"))
    return {"ok": True, "message": "원래대로 되돌렸어요" + (" — 'AI 시작.bat' 을 다시 켜야 해요" if code else "")}


def discard(job_id: str) -> dict:
    shutil.rmtree(WORK_ROOT / job_id, ignore_errors=True)
    shutil.rmtree(WORK_ROOT / f"{job_id}.base", ignore_errors=True)
    _update(job_id, status="discarded")
    return {"ok": True}
