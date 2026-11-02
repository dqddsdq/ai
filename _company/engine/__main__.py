"""1인 기업 AI 엔진 — 실행 방법:

  cd _company
  python -m engine chat                     # 대화 (CEO가 알아서 담당자 배정)
  python -m engine chat --agent youtube     # 특정 에이전트와 대화
  python -m engine ask "이번 주 영상 아이디어 3개"   # 한 번만 묻기
  python -m engine models                   # 사용 가능한 AI 확인
  python -m engine set-model youtube gemini:gemini-2.5-pro
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


def resolve_model(agent_id: str, override: str | None) -> pv.ModelSpec:
    return pv.parse_model(override or ag.Agent(agent_id).model_spec())


def pick_agent(text: str, requested: str, override: str | None) -> tuple[str, str]:
    if requested != "auto":
        return requested, ""
    spec = resolve_model("ceo", override)
    reply = pv.chat(spec, ag.route_prompt(), [{"role": "user", "content": text}], effort="low")
    return ag.parse_route(reply)


def answer(agent_id: str, history: list[dict], override: str | None, effort: str) -> str:
    agent = ag.Agent(agent_id)
    spec = resolve_model(agent_id, override)
    print(f"\n🤖 {agent_id} ({spec})\n", flush=True)
    text = pv.chat(spec, agent.system_prompt(), history,
                   on_text=lambda t: print(t, end="", flush=True), effort=effort)
    print("\n")
    return text


def check_agent(agent_id: str) -> None:
    if agent_id != "auto" and not ag.Agent(agent_id).exists():
        sys.exit(f"없는 에이전트예요: {agent_id}. 목록: {', '.join(ag.list_agents())}")


def cmd_ask(args) -> None:
    check_agent(args.agent)
    question = " ".join(args.question)
    agent_id, reason = pick_agent(question, args.agent, args.model)
    if reason:
        print(f"🧭 CEO → {agent_id} ({reason})")
    log = ag.SessionLog()
    log.write("user", question)
    reply = answer(agent_id, [{"role": "user", "content": question}], args.model, args.effort)
    log.write(agent_id, reply)


HELP = """명령어:
  /agent <이름|auto>   담당 에이전트 바꾸기 (auto = CEO가 자동 배정)
  /model <제공자:모델>  이번 대화에서 쓸 AI 바꾸기 (예: openai:gpt-5, /model off = 기본값)
  /remember <내용>     현재 에이전트 memory.md 에 기억 추가
  /agents  /models     목록 보기
  /new                 대화 새로 시작
  /quit                종료"""


def cmd_chat(args) -> None:
    check_agent(args.agent)
    mode, override, effort = args.agent, args.model, args.effort
    history: list[dict] = []
    current = "ceo" if mode == "auto" else mode
    log = ag.SessionLog()
    print("💬 1인 기업 AI — /help 로 명령어 보기, /quit 로 종료\n")

    while True:
        try:
            text = input(f"[{mode if mode != 'auto' else 'auto→' + current}] 나> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text.startswith("/"):
            cmd, _, arg = text.partition(" ")
            arg = arg.strip()
            if cmd in ("/quit", "/exit"):
                break
            elif cmd == "/help":
                print(HELP)
            elif cmd == "/agents":
                for a in ag.list_agents():
                    print(f"  {a:12} {ag.agent_summary(a)[:80]}")
            elif cmd == "/models":
                cmd_models(None)
            elif cmd == "/agent" and arg:
                if arg != "auto" and not ag.Agent(arg).exists():
                    print(f"없는 에이전트: {arg}")
                    continue
                mode, history = arg, []
                current = "ceo" if arg == "auto" else arg
                print(f"→ {arg} (대화 새로 시작)")
            elif cmd == "/model" and arg:
                try:
                    override = None if arg == "off" else str(pv.parse_model(arg))
                    print(f"→ 모델: {override or '에이전트 기본값'}")
                except pv.ProviderError as e:
                    print(e)
            elif cmd == "/remember" and arg:
                ag.Agent(current).remember(arg)
                print(f"→ {current} 의 memory.md 에 저장했어요")
            elif cmd == "/new":
                history = []
                print("→ 새 대화")
            else:
                print(HELP)
            continue

        try:
            if mode == "auto" and not history:
                current, reason = pick_agent(text, "auto", override)
                print(f"🧭 CEO → {current} ({reason})")
            history.append({"role": "user", "content": text})
            log.write("user", text)
            reply = answer(current, history, override, effort)
            history.append({"role": "assistant", "content": reply})
            log.write(current, reply)
        except pv.ProviderError as e:
            history = history[:-1] if history and history[-1]["role"] == "user" else history
            print(f"\n⚠️  {e}\n")


def cmd_models(_args) -> None:
    print("제공자     기본 모델                    키")
    for name, model, ready in pv.provider_status():
        mark = "✅" if ready else "—  (.env 에 키 필요)"
        if name == "ollama":
            mark = "✅ (로컬 Ollama 실행 필요)"
        print(f"  {name:8} {model:28} {mark}")
    print("\n에이전트별 모델 (_shared/agent_models.json):")
    models = ag.load_models()
    for a in ag.list_agents():
        print(f"  {a:12} {models.get(a, '(기본값)')}")


def cmd_set_model(args) -> None:
    check_agent(args.agent)
    spec = pv.parse_model(args.spec)
    ag.save_model(args.agent, str(spec))
    print(f"{args.agent} → {spec}")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="python -m engine", description="1인 기업 AI 엔진")
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--agent", default="auto", help="에이전트 이름 또는 auto (기본)")
    common.add_argument("--model", help="제공자:모델 (예: claude:claude-opus-5-5)")
    common.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"],
                        help="생각 깊이 (Claude 전용)")

    s = sub.add_parser("chat", parents=[common], help="대화 모드")
    s.set_defaults(func=cmd_chat)
    s = sub.add_parser("ask", parents=[common], help="한 번 질문")
    s.add_argument("question", nargs="+")
    s.set_defaults(func=cmd_ask)
    s = sub.add_parser("models", help="AI 제공자 상태")
    s.set_defaults(func=cmd_models)
    s = sub.add_parser("set-model", help="에이전트 기본 모델 지정")
    s.add_argument("agent")
    s.add_argument("spec")
    s.set_defaults(func=cmd_set_model)

    args = p.parse_args(argv)
    try:
        args.func(args)
    except pv.ProviderError as e:
        sys.exit(f"⚠️  {e}")


if __name__ == "__main__":
    main()
