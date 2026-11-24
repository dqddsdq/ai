"""🔄 GitHub 동기화 (잠긴 파일) — PC 코드와 GitHub 저장소를 같게 유지.

- 올리기: 자동 개선이 적용되면 바로 + 1시간마다 → **코드만** (engine 폴더 · 자동매매 팀 지시문) 커밋·푸시
- 받기: 1시간마다 GitHub 에 새 코드가 있으면 받고 → 자체 점검 → 통과하면 앱 다시 켜기, 실패하면 받기 전으로 되돌림
- 절대 안 올라가는 것: .env(키) · _cache(학습·계좌 기록·봇 장부) · autotrade.json(설정) · 대화 기록 · 옛 공부 노트 등
  (올릴 폴더를 아래 SYNC_PATHS 로 정해 놓고 그것만 add — 다른 파일은 손대지 않음)
- 준비: '깃허브 연결.bat' 한 번 (Git for Windows 필요, 처음 올릴 때 브라우저로 GitHub 로그인)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime

from .agents import COMPANY_DIR

ROOT = COMPANY_DIR.parent                      # 저장소 맨 위 (ai 폴더)
BRANCH = "claude/build-ai-qnubm7"
REMOTE = "sync"                                # '깃허브 연결.bat' 이 만든 원격 이름 (원래 있던 origin 은 안 건드림)
URL = "https://github.com/dqddsdq/ai.git"
SYNC_PATHS = ["_company/engine", "_company/tests", "_company/_agents/trader/prompt.md", "_company/_agents/trader/goal.md",
              "_company/_agents/trader/team.json", "_company/requirements.txt"]
NEVER = (".env", "_cache/", "autotrade.json", "sessions/", "improvements.json", "tool_requests.json", "memory.md")
_lock = threading.Lock()
STATUS = {"last": "", "msg": "", "ok": None}


def _git(*args, timeout: int = 120) -> tuple[int, str]:
    exe = shutil.which("git")
    if not exe:
        return 127, "git 이 없어요 (Git for Windows 설치 필요)"
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0" if not sys.stdin or not sys.stdin.isatty() else "1"}
    try:
        r = subprocess.run([exe, *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, "시간 초과"


def enabled() -> bool:
    return (ROOT / ".git").exists() and bool(shutil.which("git")) and _git("remote", "get-url", REMOTE)[0] == 0


def _set(ok: bool, msg: str) -> dict:
    STATUS.update(last=datetime.now().strftime("%m-%d %H:%M"), msg=msg, ok=ok)
    return {"ok": ok, "message": msg}


def _staged_safe() -> list[str]:
    """올라갈 파일 중 키·기록이 섞였는지 마지막 확인 (섞였으면 그 파일은 빼고 경고)"""
    _, out = _git("diff", "--cached", "--name-only")
    bad = [f for f in out.splitlines() if any(n in f for n in NEVER)]
    for f in bad:
        _git("reset", "-q", "HEAD", "--", f)
    return bad


def push(reason: str = "PC 자동 개선") -> dict:
    """코드만 커밋 → GitHub 최신을 위에 받기(rebase) → 올리기"""
    if not enabled():
        return _set(False, "GitHub 연결 안 됨 — '깃허브 연결.bat' 을 한 번 실행해 주세요")
    with _lock:
        _git("add", "-A", "--", *[p for p in SYNC_PATHS if (ROOT / p).exists()])
        bad = _staged_safe()
        rc, out = _git("diff", "--cached", "--quiet")
        if rc == 1:   # 올릴 변경이 있음
            rc, out = _git("commit", "-q", "-m", f"{reason} ({datetime.now():%Y-%m-%d %H:%M})")
            if rc:
                return _set(False, f"커밋 실패: {out[-200:]}")
        rc, out = _git("pull", "-q", "--rebase", "--autostash", REMOTE, BRANCH, timeout=180)
        if rc:
            _git("rebase", "--abort")
            return _set(False, "GitHub 쪽과 같은 곳이 바뀌어서 자동으로 못 합쳤어요 — 그대로 두면 다음에 다시 시도해요. 계속되면 Claude 에게 알려 주세요")
        rc, out = _git("push", "-q", REMOTE, f"HEAD:{BRANCH}", timeout=180)
        if rc:
            return _set(False, f"올리기 실패 (GitHub 로그인 필요할 수 있어요): {out[-200:]}")
        return _set(True, "GitHub 에 올렸어요" + (f" (키·기록 파일 {len(bad)}개는 빼고)" if bad else ""))


def _engine_rev() -> str:
    _, out = _git("rev-parse", "HEAD")
    return out.strip()


def pull_and_check(log=print, restart=None) -> dict:
    """GitHub 에 새 코드가 있으면 받기 → 자체 점검 → 실패면 되돌림 · 성공하고 코드가 바뀌었으면 다시 켜기"""
    if not enabled():
        return _set(False, "GitHub 연결 안 됨")
    with _lock:
        rc, out = _git("fetch", "-q", REMOTE, BRANCH, timeout=180)
        if rc:
            return _set(False, f"받기 실패: {out[-200:]}")
        _, behind = _git("rev-list", "--count", f"HEAD..{REMOTE}/{BRANCH}")
        if behind.strip() in ("", "0"):
            return _set(True, "이미 최신이에요")
        before = _engine_rev()
        _git("add", "-A", "--", *[p for p in SYNC_PATHS if (ROOT / p).exists()])   # 내 쪽 변경은 먼저 커밋해서 안 잃게
        _staged_safe()
        if _git("diff", "--cached", "--quiet")[0] == 1:
            _git("commit", "-q", "-m", f"PC 변경 (받기 전 보관, {datetime.now():%Y-%m-%d %H:%M})")
        rc, out = _git("pull", "-q", "--rebase", "--autostash", REMOTE, BRANCH, timeout=180)
        if rc:
            _git("rebase", "--abort")
            return _set(False, "새 코드와 PC 코드가 같은 곳이 달라서 자동으로 못 합쳤어요 — 지금 코드 그대로 계속 돌아요")
        _, changed = _git("diff", "--name-only", before, "HEAD")
        code = any(f.startswith("_company/engine/") for f in changed.splitlines())
        r = subprocess.run([sys.executable, "-B", "-m", "engine.selftest"], cwd=COMPANY_DIR, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=300)
        if not r.returncode and (COMPANY_DIR / "tests" / "scenarios.py").exists():   # 🧪 매매 흐름 시나리오도
            r = subprocess.run([sys.executable, "-B", "-m", "tests.scenarios"], cwd=COMPANY_DIR, capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=600)
        if r.returncode:
            _git("reset", "-q", "--keep", before)
            tail = (r.stdout or "").strip().splitlines()[-1:] or [""]
            return _set(False, f"받은 코드가 자체 점검·시나리오 시험에 실패해서 되돌렸어요 ({tail[0][:80]})")
        n = len(changed.splitlines())
        msg = f"GitHub 새 코드 받음 ({n}개 파일)" + (" → 앱 다시 켜요" if code else "")
        _set(True, msg)
    log("🔄 " + msg)
    try:
        from . import notify
        notify.send("🔄 " + msg)
    except Exception:
        pass
    if code and restart:
        restart()
    return {"ok": True, "message": msg}


def sync_now(log=print, restart=None) -> dict:
    """화면 버튼: 올리고 → 받기"""
    p = push("PC 변경")
    q = pull_and_check(log, restart)
    return {"ok": p["ok"] and q["ok"], "message": f"올리기: {p['message']} · 받기: {q['message']}"}


def worker(log=print, restart=None, every: int = 3600) -> None:
    """1시간마다 올리기·받기 (절대 안 죽음)"""
    time.sleep(120)
    while True:
        try:
            if enabled():
                p = push("PC 변경 (1시간마다)")
                if not p["ok"]:
                    log("🔄 " + p["message"])
                pull_and_check(log, restart)
        except Exception as e:
            log(f"🔄 GitHub 동기화 오류: {type(e).__name__}: {e}")
        time.sleep(every)
