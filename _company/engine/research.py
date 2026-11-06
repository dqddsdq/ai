"""공용 자료수집: 리서치 팀 3명이 동시에 홈페이지·공식 자료를 찾아 '자료 묶음'을 만들고,
배정된 팀(초안·검토·승인 AI 전원)이 그 자료를 받아 일합니다.

  - 1번: 공식 출처 (정부·기관·기업 홈페이지, 공시, 법령, 보도자료)
  - 2번: 최신 뉴스·업계
  - 3번: 데이터·통계·반대 근거
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from . import agents as ag
from . import providers as pv
from . import team as tm

COLLECT_PROMPT = """[자료수집 단계]
<question> 은 사장님이 다른 팀({team})에 한 질문입니다. 그 팀이 정확하게 답할 수 있도록, 당신의 관점에 맞는 자료를 찾으세요.
- 결론·조언은 쓰지 마세요. 자료만.
- 검색하고, 중요한 숫자·문장은 페이지를 직접 열어 원문으로 확인하세요.
- 최대 4개, 각 자료는 아래 형식 그대로:

### 자료 N
- 제목:
- 출처: URL (사이트 이름)
- 날짜:
- 핵심 내용: (원문 숫자·문장 그대로, 2~3줄)
- 신뢰도: 높음/중간/낮음 — 이유 한 줄

찾지 못했으면 "자료를 찾지 못했어요: (무엇을 찾았는데 없었는지)" 라고만 쓰세요."""

MAX_PACK_CHARS = 12000


def wanted(use_team: bool) -> bool:
    mode = (tm.settings().get("shared_research") or "auto").lower()
    return mode == "always" or (mode == "auto" and use_team)


def run_research(
    question: str,
    team: str,
    override: str | None = None,
    on_step_start: Callable | None = None,
    on_step_done: Callable | None = None,
    on_step_text: Callable | None = None,
    on_step_activity: Callable | None = None,
) -> tuple[str, list]:
    """(자료 묶음 텍스트, 단계 기록). 리서치 팀이 없거나 모두 실패하면 ("", 단계)."""
    members = ag.team_members("researcher")
    if not members or team == "researcher":
        return "", []
    cfg = tm.team_config("researcher")
    spec = pv.parse_model(override or cfg.get("member_model") or "claudecode:sonnet")
    team_name = ag.agent_summary(team).split("`")[0].strip() or team
    labels = {1: "🌐 자료수집 · 공식 출처", 2: "🌐 자료수집 · 최신 뉴스", 3: "🌐 자료수집 · 데이터·반대 근거"}
    steps = [tm.Step(f"res{i}", labels.get(i, f"🌐 자료수집 {i}"), m, ag.member_view(m),
                     reads=["question"], kind="collect", model=str(spec)) for i, m in enumerate(members[:3], 1)]
    for st in steps:
        if on_step_start:
            on_step_start(st)

    def work(st):
        system = ag.Agent(st.member).system_prompt() + "\n\n" + COLLECT_PROMPT.format(team=team_name)
        try:
            st.text = pv.chat(spec, system, [{"role": "user", "content": f"<question>\n{question}\n</question>"}],
                              web=True, effort="medium",
                              on_text=(lambda t: on_step_text(st, t)) if on_step_text else None,
                              on_activity=(lambda a: on_step_activity(st, a)) if on_step_activity else None)
        except Exception as e:  # 한 명이 실패해도 나머지 자료로 진행
            st.error = str(e) if isinstance(e, pv.ProviderError) else f"{type(e).__name__}: {e}"
        return st

    max_parallel = max(1, int(tm.settings().get("max_parallel") or 3))
    with ThreadPoolExecutor(max_workers=min(len(steps), max_parallel)) as pool:
        for f in as_completed([pool.submit(work, st) for st in steps]):
            if on_step_done:
                on_step_done(f.result())

    good = [st for st in steps if st.text.strip()]
    if not good:
        return "", steps
    body = "\n\n".join(f"<collected by=\"{st.title}\">\n{st.text.strip()}\n</collected>" for st in good)
    if len(body) > MAX_PACK_CHARS:
        body = body[:MAX_PACK_CHARS] + "\n…(길어서 일부 생략)"
    pack = ("<research_pack>\n공용 자료수집 AI 들이 방금 홈페이지·공식 자료에서 찾은 자료입니다. "
            "근거로 쓸 때는 출처(사이트·날짜)를 밝히고, 자료에 없는 숫자는 지어내지 마세요. "
            "자료끼리 다르면 다르다고 말하고, 신뢰도 낮은 자료는 조심해서 쓰세요.\n\n" + body + "\n</research_pack>")
    return pack, steps


def count_items(steps: list) -> int:
    return sum(len(re.findall(r"^###\s*자료", st.text or "", re.M)) for st in steps)
