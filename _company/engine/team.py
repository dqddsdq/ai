"""팀 토론: 같은 팀 팀원들이 각자 관점으로 답하고 → 심사로 비교해 → 가장 좋은 최종 답 하나.

  팀원 답변은 동시에(병렬) 만들어서 시간은 '가장 느린 팀원 + 심사' 만큼만 걸립니다.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from dataclasses import dataclass
from typing import Callable

from . import agents as ag
from . import providers as pv

JUDGE_PROMPT = """[심사 단계]
당신은 지금 '{team}팀'의 심사위원입니다. 같은 질문에 팀원 {n}명이 각자 관점으로 쓴 답변(<draft>)을 비교하세요.

심사 기준: ① 정확성과 근거 ② 구체성·바로 실행할 수 있는지 ③ 회사 목표와의 연관성 ④ 질문에 딱 맞는지
- 가장 좋은 답을 뼈대로 삼고, 다른 답의 좋은 점만 합쳐서 최종 답 하나를 쓰세요.
- 틀렸거나 근거 없는 주장, 팀원끼리 충돌하는 부분은 더 근거 있는 쪽을 택하고 나머지는 버리세요.
- 첫 줄은 반드시 이 형식: `🏆 채택: 팀원N (관점) — 한 줄 이유` (다른 팀원 내용을 반영했으면 "+ 팀원M의 ○○ 반영")
- 그다음 빈 줄, 그리고 사용자에게 줄 최종 답만. '팀원', '초안' 같은 말은 최종 답 본문에 쓰지 마세요.
- 팀원 답에 [[...]] 실행 태그가 있었고 여전히 필요하면, 최종 답 마지막 줄에 태그 하나만 남기세요.
- 팀원들이 사장님께 화면 캡처(📸)나 추가 정보를 요청했다면, 겹치는 요청을 합쳐서 최종 답 끝에 한 번만 구체적으로 요청하세요."""


def team_config(team: str) -> dict:
    """_agents/<팀>/team.json — 예: {"web": true, "market_data": true}"""
    try:
        return json.loads((ag.AGENTS_DIR / team / "team.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


@dataclass
class Draft:
    member: str
    view: str
    text: str = ""
    error: str = ""


def run_team(
    team: str,
    history: list[dict],
    extra_system: str = "",
    override: str | None = None,
    effort: str = "medium",
    on_draft_start: Callable[[int, Draft], None] | None = None,
    on_draft_done: Callable[[int, Draft], None] | None = None,
    on_judge_start: Callable[[], None] | None = None,
    on_text: Callable[[str], None] | None = None,
    images: list[str] | None = None,
) -> tuple[str, list[Draft], str]:
    """(최종 답, 팀원 초안들, 심사 모델) — 팀원이 1명이면 심사 없이 그 답 그대로."""
    members = ag.team_members(team)
    if not members:
        raise pv.ProviderError(f"없는 팀: {team}")

    def system_for(member: str) -> str:
        return ag.Agent(member).system_prompt() + ("\n\n" + extra_system if extra_system else "")

    lead_spec = pv.parse_model(override or ag.Agent(members[0]).model_spec())
    web = bool(team_config(team).get("web"))
    if len(members) == 1:
        text = pv.chat(lead_spec, system_for(members[0]), history, on_text=on_text, effort=effort,
                       web=web, images=images)
        return text, [Draft(members[0], ag.member_view(members[0]), text)], str(lead_spec)

    drafts = [Draft(m, ag.member_view(m)) for m in members]
    for i, d in enumerate(drafts, 1):
        if on_draft_start:
            on_draft_start(i, d)

    def work(d: Draft) -> Draft:
        spec = pv.parse_model(override or ag.Agent(d.member).model_spec())
        try:
            d.text = pv.chat(spec, system_for(d.member), history, effort=effort, web=web, images=images)
        except pv.ProviderError as e:
            d.error = str(e)
        return d

    with ThreadPoolExecutor(max_workers=len(drafts)) as pool:
        futures = {pool.submit(work, d): i for i, d in enumerate(drafts, 1)}
        for f in as_completed(futures):  # 완료 알림은 호출한 스레드에서 보냄
            if on_draft_done:
                on_draft_done(futures[f], f.result())

    good = [(i, d) for i, d in enumerate(drafts, 1) if d.text.strip()]
    if not good:
        raise pv.ProviderError("팀원 모두 답을 못 만들었어요: " + " / ".join(d.error for d in drafts if d.error))
    if len(good) == 1:
        i, d = good[0]
        text = f"🏆 채택: 팀원{i} ({d.view}) — 다른 팀원 응답 실패\n\n{d.text}"  # noqa: E501
        if on_text:
            on_text(text)
        return text, drafts, str(lead_spec)

    if on_judge_start:
        on_judge_start()
    question = history[-1]["content"]
    blocks = "\n\n".join(f'<draft member="팀원{i}" view="{d.view}">\n{d.text}\n</draft>' for i, d in good)
    judge_msgs = history[:-1] + [{"role": "user", "content": f"<question>\n{question}\n</question>\n\n{blocks}"}]
    display = ag.agent_summary(team).split("`")[0].strip().lstrip("🧭💰🔍💻✍️ ").strip() or team
    judge_system = system_for(members[0]) + "\n\n" + JUDGE_PROMPT.format(team=display, n=len(good))
    final = pv.chat(lead_spec, judge_system, judge_msgs, on_text=on_text, effort=effort)
    return final, drafts, str(lead_spec)
