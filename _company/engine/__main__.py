"""자동매매 AI 엔진 — 실행 방법:

  cd _company
  python -m engine web                      # 화면 열기 (🤖 자동매매 · 📊 차트 · 🧰 도구 · 🛠️ 개선 · 자동매매 팀과 대화)
  python -m engine selftest                 # 자체 점검 (매매·안전 규칙, 인터넷 없이)
  python -m engine doctor                   # 문제 진단 (파이썬·패키지·Claude Code·토스 연결)
  python -m engine models                   # 사용 가능한 AI 확인
"""

from __future__ import annotations

import argparse
import sys

from . import agents as ag
from . import providers as pv

try:
    from dotenv import load_dotenv

    load_dotenv(ag.COMPANY_DIR / ".env")
except ImportError:
    pass


def cmd_models(_args) -> None:
    print("💻 내 PC 로컬 실행기")
    for name in pv.LOCAL_SERVERS:
        models = pv.probe_local(name)
        state = "꺼짐" if models is None else ("켜짐 · " + (", ".join(models[:4]) or "로드된 모델 없음"))
        print(f"  {name:9} {pv.local_url(name):28} {state}")
    print("\n☁️  클라우드")
    for name, model, ready in pv.provider_status():
        if name in pv.LOCAL_SERVERS or name in ("local", "echo"):
            continue
        print(f"  {name:9} {model:28} {'✅' if ready else '—  (.env 에 키 필요)'}")


def cmd_doctor(_args) -> None:
    from . import doctor

    doctor.run()


def cmd_web(args) -> None:
    from . import web

    web.serve(port=args.port, open_browser=not args.no_browser, phone=args.phone)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="python -m engine", description="자동매매 AI 엔진")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("web", help="브라우저 화면 열기")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-browser", action="store_true")
    s.add_argument("--phone", action="store_true", help="같은 와이파이의 폰에서도 접속 (PIN 필요)")
    s.set_defaults(func=cmd_web)
    s = sub.add_parser("selftest", help="자체 점검 (매매·안전 규칙, 인터넷 없이)")
    s.set_defaults(func=lambda a: sys.exit(__import__("engine.selftest", fromlist=["main"]).main()))
    s = sub.add_parser("doctor", help="문제 진단")
    s.set_defaults(func=cmd_doctor)
    s = sub.add_parser("models", help="AI 제공자 상태")
    s.set_defaults(func=cmd_models)

    args = p.parse_args(argv)
    try:
        args.func(args)
    except pv.ProviderError as e:
        sys.exit(f"⚠️  {e}")


if __name__ == "__main__":
    main()
