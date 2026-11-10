"""문제 진단: python -m engine doctor  (또는 _company 의 '진단.bat' 더블클릭)

파이썬, 패키지, Claude Code 설치·로그인, 실제 Claude 호출, 포트를 하나씩 검사합니다.
"""

from __future__ import annotations

import importlib
import platform
import socket
import subprocess
import sys
import time

from . import providers as pv


def ok(msg):
    print(f"  ✅ {msg}", flush=True)


def bad(msg, fix=""):
    print(f"  ❌ {msg}", flush=True)
    if fix:
        print(f"     👉 {fix}", flush=True)


def run() -> int:
    problems = 0
    print("\n🩺 1인 기업 AI 진단\n")

    print("[1] 파이썬")
    v = sys.version_info
    if v >= (3, 10):
        ok(f"Python {v.major}.{v.minor}.{v.micro} ({platform.system()} {platform.release()})")
    else:
        problems += 1
        bad(f"Python {v.major}.{v.minor} — 너무 오래됐어요", "python.org 에서 최신 버전 설치")

    print("[2] 패키지")
    for mod in ("numpy", "requests", "dotenv", "anthropic", "openai", "yfinance", "PIL"):
        try:
            importlib.import_module(mod)
            ok(mod)
        except Exception as e:
            problems += 1
            bad(f"{mod} 없음 ({type(e).__name__})", "pip install -r requirements.txt")

    print("[3] Claude Code")
    exe = pv.claude_cli()
    if not exe:
        problems += 1
        bad("claude 명령을 찾을 수 없어요 (흔한 설치 위치도 모두 확인했어요)",
            "Claude Code 가 아직 설치 안 된 것 같아요. PowerShell 에서: irm https://claude.ai/install.ps1 | iex\n"
            "        설치 끝에 나오는 메시지를 캡처해 주세요. 위치를 알면 .env 에 CLAUDE_CLI=그위치 로 지정할 수 있어요.")
    else:
        ok(f"위치: {exe}")
        try:
            out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=60,
                                 encoding="utf-8", errors="replace")
            ok(f"버전: {(out.stdout or out.stderr).strip()[:80]}")
        except Exception as e:
            problems += 1
            bad(f"claude --version 실패: {e}")

        print("[4] Claude 실제 호출 (최대 2분)")
        t = time.time()
        try:
            reply = pv.chat(pv.parse_model("claudecode:haiku"), "짧게 답하세요.",
                            [{"role": "user", "content": "OK 라고만 답해"}], timeout=120)
            ok(f"응답 '{reply.strip()[:30]}' ({time.time() - t:.0f}초)")
        except pv.ProviderError as e:
            problems += 1
            msg = str(e)
            fix = "PowerShell 에서 claude 를 한 번 실행해서 로그인했는지 확인하세요"
            low = msg.lower()
            if "git-bash" in low or "git bash" in low or "bash" in low:
                fix = "윈도우용 Claude Code 는 Git 이 필요해요: https://git-scm.com/download/win 설치 후 다시 시도"
            elif "login" in low or "auth" in low or "로그인" in msg:
                fix = "PowerShell 에서 claude 를 실행해서 로그인하세요"
            bad(msg[:500], fix)

    print("[5] FreeLLMAPI (선택 — 무료 AI 검토자)")
    import os
    if not os.getenv("FREELLM_URL"):
        ok("설정 안 함 — 검토는 Claude만으로 진행 (정상)")
    else:
        url = os.getenv("FREELLM_URL")
        if url.rstrip("/").endswith("/v1"):
            bad(f"FREELLM_URL 이 /v1 로 끝나요: {url}", "루트 주소만 쓰세요 (예: http://localhost:3000) — 자동으로 빼고 쓰긴 해요")
        if not pv._freellm_alive():
            problems += 1
            bad(f"{url} 에 연결 안 됨", "FreeLLMAPI 가 켜져 있는지, 주소·포트가 맞는지 확인하세요")
        else:
            ok(f"{url} 켜져 있음")
            names = [x.strip() for x in os.getenv("FREELLM_MODELS", "").split(",") if x.strip()] or [os.getenv("FREELLM_MODEL", "auto")]
            for name in names[:3]:
                t = time.time()
                try:
                    reply = pv.chat(pv.ModelSpec("freellm", name), "짧게 답하세요.",
                                    [{"role": "user", "content": "OK 라고만 답해"}], timeout=60)
                    ok(f"모델 {name}: '{reply.strip()[:20]}' ({time.time() - t:.0f}초)")
                except pv.ProviderError as e:
                    problems += 1
                    bad(f"모델 {name}: {str(e)[:200]}",
                        "FREELLM_API_KEY(통합 키)와 모델 이름을 확인하세요. 계속 안 되면 .env 에 FREELLM_API=openai 로 바꿔 보세요")

    print("[6] 포트 8765")
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", 8765)) == 0:
            ok("이미 AI 화면이 켜져 있어요 (정상 — 다른 창에서 실행 중)")
        else:
            ok("사용 가능")

    print("[7] 토스증권 (선택 — 자동매매·실시간 시세)")
    from . import toss
    if not toss.enabled():
        ok("키 없음 — 🧰 도구 화면에서 클라이언트 ID·시크릿 키를 넣으면 연결돼요 (정상)")
    else:
        try:
            toss._token(force=True)
            ok("로그인 성공")
            toss.account_seq()
            ok("종합계좌 찾음")
            p = toss.prices(["AAPL", "005930"])
            ok("시세: " + ", ".join(f"{k} {v['price']:g}" for k, v in p.items()))
            usd = (toss.buying_power("USD") or {}).get("cashBuyingPower")
            krw = (toss.buying_power("KRW") or {}).get("cashBuyingPower")
            ok(f"주문 가능 현금: 달러 {usd} · 원화 {krw}")
            if not float(usd or 0):
                print("     ℹ️ 주문 가능 달러가 0 이에요. 미장 자동매매를 하려면 토스에서 달러로 환전해 두세요")
            ok(f"보유 종목 {len((toss.holdings() or {}).get('items') or [])}개 (조회만, 주문 안 함)")
        except toss.TossError as e:
            problems += 1
            bad(str(e), "토스 개발자 화면의 클라이언트 ID·시크릿 키(같이 발급된 한 쌍)를 🧰 도구에 다시 넣어 보세요")

    print("[8] 텔레그램 알림 (선택)")
    from . import notify
    if not notify.enabled():
        ok("토큰 없음 — 🧰 도구에서 넣으면 폰으로 알림이 와요 (정상)")
    else:
        r = notify.test()
        if r["ok"]:
            ok(r["message"])
        else:
            problems += 1
            bad(r["message"], "🧰 도구의 텔레그램 토큰을 확인하세요")

    print("[9] 자체 점검 (매매·안전 규칙)")
    from . import selftest
    n_ok, n_all, fails = selftest.run(verbose=False)
    if fails:
        problems += 1
        bad(f"{n_ok}/{n_all} 통과 — {fails[0][:120]}", "python -m engine selftest 로 자세히 보기")
    else:
        ok(f"{n_all}개 모두 통과")
    print()
    if problems:
        print(f"⚠️  문제 {problems}개. 이 창을 캡처해서 보여주세요.")
    else:
        print("🎉 모두 정상이에요!")
    return problems


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
