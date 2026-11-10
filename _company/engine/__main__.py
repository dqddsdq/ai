"""1인 기업 AI 엔진 — 실행 방법:

  cd _company
  python -m engine chat                     # 대화 (CEO가 알아서 담당자 배정)
  python -m engine chat --agent writer      # 특정 팀과 대화
  python -m engine ask "이번 주 영상 아이디어 3개"   # 한 번만 묻기
  python -m engine models                   # 사용 가능한 AI 확인
  python -m engine selftest                 # 자체 점검 (매매·안전 규칙)
  python -m engine set-model writer-2 gemini:gemini-2.5-pro
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import agents as ag
from . import knowledge as kn
from . import learn as ln
from . import providers as pv
from . import training as tr

try:
    from dotenv import load_dotenv

    load_dotenv(ag.COMPANY_DIR / ".env")
except ImportError:
    pass


USE_KNOWLEDGE = True


def resolve_model(agent_id: str, override: str | None) -> pv.ModelSpec:
    return pv.parse_model(override or ag.Agent(agent_id).model_spec())


def pick_agent(text: str, requested: str, override: str | None) -> tuple[str, str]:
    if requested != "auto":
        return requested, ""
    from . import team as tm

    reply = pv.chat(tm.fast_spec(override), ag.route_prompt(), [{"role": "user", "content": text}], effort="low")
    quick = ag.parse_quick(reply)
    if quick:  # 인사·잡담: 팀 없이 바로
        print(f"\n🧭 {quick}\n")
        return "", ""
    return ag.parse_route(reply)


def answer(agent_id: str, history: list[dict], override: str | None, effort: str) -> str:
    agent = ag.Agent(agent_id)
    spec = resolve_model(agent_id, override)
    system = agent.system_prompt()
    refs = kn.context_for(history[-1]["content"]) if USE_KNOWLEDGE else ""
    if refs:
        system += "\n\n" + refs
    print(f"\n🤖 {agent_id} ({spec}){'  📚 자료 참고' if refs else ''}\n", flush=True)
    text = pv.chat(spec, system, history,
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
    if not agent_id:  # 인사·잡담이라 이미 답함
        return
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
                picked, reason = pick_agent(text, "auto", override)
                if not picked:  # 인사·잡담이라 이미 답함 → 다음 질문 때 다시 배정
                    continue
                current = picked
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
    print("\n에이전트별 모델 (_shared/agent_models.json):")
    models = ag.load_models()
    for a in ag.list_agents():
        print(f"  {a:12} {models.get(a, '(기본값)')}")


def cmd_search(args) -> None:
    hits = kn.search(" ".join(args.query), k=5)
    if not hits:
        print("관련 자료를 못 찾았어요. 00_Raw/ 에 .md 파일을 넣어 보세요.")
    for score, c in hits:
        print(f"\n[{score:.0%}] {c.source}\n{c.text[:300]}")


def cmd_export(args) -> None:
    out, n = tr.export(include_system=not args.no_system)
    print(f"학습 데이터 {n}개 → {out}")
    if n:
        print("다음 단계: engine/README.md 의 '내 컴퓨터에서 학습시키기' 참고")


def cmd_learn(args) -> None:
    spec = resolve_model("researcher", args.model)
    if args.auto:
        topics = ln.pick_topics(spec, args.rounds)
        if not topics:
            sys.exit("새로 공부할 주제를 못 정했어요. 주제를 직접 적어 주세요.")
        print("🧭 공부할 주제: " + " / ".join(topics))
    elif args.topic:
        topics = [" ".join(args.topic)]
    else:
        sys.exit('주제를 적거나 --auto 를 쓰세요. 예: python -m engine learn "쇼츠 수익화"')
    for t in topics:
        print(f"\n📖 공부 시작: {t}  ({spec})")
        try:
            path = ln.learn(t, spec, max_pages=args.pages)
            print(f"✅ 노트 저장: {path.relative_to(ag.COMPANY_DIR.parent)}  (상태: 미검토)")
        except pv.ProviderError as e:
            print(f"⚠️  {e}")


def cmd_simulate(args) -> None:
    from . import simulate as sm

    spec = resolve_model(args.agent, args.model)
    goal = " ".join(args.goal)
    model = None
    try:
        if args.stock:
            if args.target is None:
                sys.exit("--target 수익률(%)을 적어 주세요. 예: --target 20")
            prices = sm.load_prices(args.stock, args.csv)
            result = sm.run_stock(prices, args.years, args.target, runs=args.runs)
            goal = goal or f"{args.stock} {args.years}년 +{args.target}%"
            sm.print_stock_result(args.stock, result)
        else:
            if args.scenario:
                model = json.loads(Path(args.scenario).read_text(encoding="utf-8"))
                goal = goal or model.get("title", args.scenario)
            elif goal:
                print(f"🧠 시나리오 설계 중... ({spec})")
                model = sm.build_model(goal, spec)
            else:
                sys.exit('목표를 적어 주세요. 예: python -m engine simulate "블로그로 월 1000만원"')
            result = sm.run_model(model, runs=args.runs)
            sm.print_model_result(model, result)
    except sm.FormulaError as e:
        sys.exit(f"⚠️  {e}")

    print("\n💬 해석\n")
    explanation = sm.explain(spec, goal, model, result, on_text=lambda t: print(t, end="", flush=True))
    path = sm.save_report(goal, model, result, explanation)
    print(f"\n\n📝 보고서: {path.relative_to(ag.COMPANY_DIR)}")
    if model:
        scen = path.with_suffix(".json")
        scen.write_text(json.dumps(model, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"🔧 가정을 바꿔 다시 돌리려면: {scen.relative_to(ag.COMPANY_DIR)} 수정 후 "
              f"python -m engine simulate --scenario {scen.relative_to(ag.COMPANY_DIR)}")


def cmd_doctor(args) -> None:
    from . import doctor

    doctor.run()


def cmd_web(args) -> None:
    from . import web

    web.serve(port=args.port, open_browser=not args.no_browser, phone=args.phone)


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
    common.add_argument("--no-knowledge", action="store_true", help="내 자료(00_Raw) 참고 끄기")
    common.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"],
                        help="생각 깊이 (Claude 전용)")

    s = sub.add_parser("chat", parents=[common], help="대화 모드")
    s.set_defaults(func=cmd_chat)
    s = sub.add_parser("ask", parents=[common], help="한 번 질문")
    s.add_argument("question", nargs="+")
    s.set_defaults(func=cmd_ask)
    s = sub.add_parser("search", help="내 자료에서 검색")
    s.add_argument("query", nargs="+")
    s.set_defaults(func=cmd_search)
    s = sub.add_parser("export-training", help="대화 기록 → 학습 데이터(JSONL)")
    s.add_argument("--no-system", action="store_true", help="system 메시지 빼기")
    s.set_defaults(func=cmd_export)
    s = sub.add_parser("learn", help="인터넷 검색으로 스스로 공부 → 10_Wiki 노트")
    s.add_argument("topic", nargs="*")
    s.add_argument("--auto", action="store_true", help="goals.md 를 보고 주제 스스로 고르기")
    s.add_argument("--rounds", type=int, default=3, help="--auto 일 때 주제 개수 (기본 3)")
    s.add_argument("--pages", type=int, default=5, help="주제당 읽을 페이지 수")
    s.add_argument("--model", help="제공자:모델 (기본: researcher 에이전트 모델)")
    s.set_defaults(func=cmd_learn)
    s = sub.add_parser("simulate", help="목표 달성 확률 시뮬레이션 (사업·주식 등)")
    s.add_argument("goal", nargs="*", help='원하는 것. 예: "블로그로 월 1000만원"')
    s.add_argument("--scenario", help="저장된 시나리오 JSON 다시 실행")
    s.add_argument("--stock", help="주식 티커 (예: AAPL, 005930.KS)")
    s.add_argument("--csv", help="주가 CSV 파일 (Close/종가 열)")
    s.add_argument("--years", type=float, default=1.0, help="주식: 몇 년 후 (기본 1)")
    s.add_argument("--target", type=float, help="주식: 목표 수익률 %%")
    s.add_argument("--runs", type=int, default=10000, help="시뮬레이션 횟수")
    s.add_argument("--agent", default="startup-money", help="시나리오를 설계할 에이전트 (기본 startup-money)")
    s.add_argument("--model", help="제공자:모델")
    s.set_defaults(func=cmd_simulate)
    s = sub.add_parser("selftest", help="자체 점검 (매매·안전 규칙, 인터넷 없이)")
    s.set_defaults(func=lambda a: sys.exit(__import__("engine.selftest", fromlist=["main"]).main()))
    s = sub.add_parser("doctor", help="문제 진단 (파이썬·패키지·Claude Code 연결 검사)")
    s.set_defaults(func=cmd_doctor)
    s = sub.add_parser("web", help="브라우저 화면 열기 (Claude 앱 스타일)")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-browser", action="store_true")
    s.add_argument("--phone", action="store_true", help="같은 와이파이의 폰에서도 접속 (PIN 필요)")
    s.set_defaults(func=cmd_web)
    s = sub.add_parser("models", help="AI 제공자 상태")
    s.set_defaults(func=cmd_models)
    s = sub.add_parser("set-model", help="에이전트 기본 모델 지정")
    s.add_argument("agent")
    s.add_argument("spec")
    s.set_defaults(func=cmd_set_model)

    args = p.parse_args(argv)
    global USE_KNOWLEDGE
    USE_KNOWLEDGE = not getattr(args, "no_knowledge", False)
    try:
        args.func(args)
    except pv.ProviderError as e:
        sys.exit(f"⚠️  {e}")


if __name__ == "__main__":
    main()
