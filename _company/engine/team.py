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
- 최종 답 길이는 질문 크기에 맞추세요. 팀원 답보다 길어지면 안 됩니다.
- 팀원 답에 [[...]] 실행 태그가 있었고 여전히 필요하면, 최종 답 마지막 줄에 태그 하나만 남기세요.
- 팀원들이 사장님께 화면 캡처(📸)나 추가 정보를 요청했다면, 겹치는 요청을 합쳐서 최종 답 끝에 한 번만 구체적으로 요청하세요."""


def team_config(team: str) -> dict:
    """_agents/<팀>/team.json — 예: {"web": true, "market_data": true}"""
    try:
        return json.loads((ag.AGENTS_DIR / team / "team.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def settings() -> dict:
    """_shared/settings.json — max_parallel, team_mode"""
    try:
        return json.loads((ag.SHARED_DIR / "settings.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


DEEP_WORDS = ("분석", "어때", "될까", "돼", "가능", "계획", "추천", "비교", "전략", "어떻게", "왜", "판단", "검토",
              "리스크", "전망", "살까", "사도", "팔까", "팔아", "매수", "매도", "들어가", "손절", "익절", "비중",
              "계약", "소송", "고소", "세금", "창업", "시뮬", "괜찮", "해야")


def wants_team(text: str, has_images: bool = False, mode: str | None = None) -> bool:
    """팀 비교를 돌릴지: on=항상, off=안 함, auto=분석·판단이 필요한 질문만."""
    mode = (mode or settings().get("team_mode") or "auto").lower()
    if mode in ("on", "always", "true"):
        return True
    if mode in ("off", "false"):
        return False
    if has_images:
        return True
    t = text.strip()
    is_question = "?" in t or t.endswith(("까", "요", "냐", "니"))
    return len(t) >= 30 or any(w in t for w in DEEP_WORDS) or (is_question and len(t) >= 12)


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

    cfg = team_config(team)
    lead_spec = pv.parse_model(override or ag.Agent(members[0]).model_spec())   # 심사·단독 답변: 큰 모델
    member_spec_str = override or cfg.get("member_model")                       # 팀원 초안: 가벼운 모델
    web = bool(cfg.get("web"))
    if len(members) == 1:
        text = pv.chat(lead_spec, system_for(members[0]), history, on_text=on_text, effort=effort,
                       web=web, images=images)
        return text, [Draft(members[0], ag.member_view(members[0]), text)], str(lead_spec)

    drafts = [Draft(m, ag.member_view(m)) for m in members]
    for i, d in enumerate(drafts, 1):
        if on_draft_start:
            on_draft_start(i, d)

    def work(d: Draft) -> Draft:
        spec = pv.parse_model(member_spec_str or ag.Agent(d.member).model_spec())
        try:
            d.text = pv.chat(spec, system_for(d.member), history, effort=effort, web=web, images=images)
        except pv.ProviderError as e:
            d.error = str(e)
        return d

    max_parallel = max(1, int(settings().get("max_parallel") or len(drafts)))
    with ThreadPoolExecutor(max_workers=min(len(drafts), max_parallel)) as pool:
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


# ═══ 검토 파이프라인: 초안 1 → 검토 2 (근거 / 숫자·논리) → 수정 → 최종 승인 3 → 마지막 수정 ═══
VERIFY_EVIDENCE = """[검토 단계 — 근거·출처 확인 담당]
아래 <draft> 는 같은 팀 동료가 사장님 질문(<question>)에 쓴 초안입니다. 당신은 '근거·출처'를 확인합니다.
- 초안의 사실 주장(가격·수치·뉴스·법 조항·판례번호·제도·날짜)을 하나씩 확인하세요.
  시스템 자료(<market_data>, 참고자료), 첨부 화면, 웹 검색(가능하면)으로 실제 근거를 찾으세요.
- 각 항목: ✅ 확인됨(근거) / ❌ 틀림(맞는 내용) / ⚠️ 확인 못 함 — 로 짧게 적으세요. 지어내지 마세요.
- 마지막 줄은 반드시 둘 중 하나: `판정: 통과` 또는 `판정: 수정 필요`"""

VERIFY_LOGIC = """[검토 단계 — 숫자·계산·논리 확인 담당]
아래 <draft> 는 같은 팀 동료가 사장님 질문(<question>)에 쓴 초안입니다. 당신은 '숫자·계산·논리'를 확인합니다.
- 모든 계산(수익률, 비율, 세금, 손익, 기간, 환산)을 직접 다시 계산하세요. 틀리면 맞는 값을 적으세요.
- 결론이 근거에서 정말 나오는지, 빠진 위험·조건·예외가 없는지, 질문에 실제로 답했는지 보세요.
- 각 항목: ✅ / ❌(수정안) / ⚠️ 로 짧게. 문제 없으면 "문제 없음".
- 마지막 줄은 반드시 둘 중 하나: `판정: 통과` 또는 `판정: 수정 필요`"""

REVISE_PROMPT = """[수정 단계]
아래 <draft>(초안)를 <review>(검토 의견)에 따라 고쳐서 사장님께 드릴 답을 다시 쓰세요.
- ❌ 지적은 반드시 반영하고, ⚠️ 확인 못 한 주장은 빼거나 "확인 필요"라고 표시하세요.
- 맞는 부분은 그대로 두세요. 검토 과정·'초안'·'검토자' 같은 말은 쓰지 말고 완성된 답만 쓰세요.
- 길이는 질문 크기에 맞게. 초안의 [[...]] 실행 태그가 여전히 필요하면 마지막 줄에 남기세요."""

APPROVE_PROMPT = """[최종 승인 단계 — 승인 담당 {i}/{n}]
아래 <answer> 는 사장님 질문(<question>)에 대한 최종 답 후보입니다. 내보내기 전 마지막 확인입니다.
- 읽고 ① 사실 오류 ② 계산 오류(직접 다시 계산) ③ 꼭 고쳐야 할 내용·빠진 위험 ④ 질문에 답했는지 를 점검하세요.
- 사소한 말투·취향은 지적하지 마세요. 고칠 게 있으면 무엇을 어떻게 고칠지 구체적으로 1~3개.
- 마지막 줄은 반드시 둘 중 하나: `승인` 또는 `반려`"""


@dataclass
class Step:
    id: str
    title: str
    member: str
    view: str = ""
    text: str = ""
    verdict: str = ""     # pass / fix / approve / reject / ""
    error: str = ""

    def as_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "member": self.member, "view": self.view,
                "text": self.text, "verdict": self.verdict, "error": self.error}


def _verdict(text: str, kind: str) -> str:
    tail = text.strip().splitlines()[-3:] if text.strip() else []
    tail = " ".join(tail)
    if kind == "verify":
        return "fix" if "수정 필요" in tail else "pass" if "통과" in tail else "fix"
    return "reject" if "반려" in tail else "approve" if "승인" in tail else "reject"


def run_review(
    team: str,
    history: list[dict],
    extra_system: str = "",
    override: str | None = None,
    effort: str = "medium",
    on_step_start: Callable[[Step], None] | None = None,
    on_step_done: Callable[[Step], None] | None = None,
    on_text: Callable[[str], None] | None = None,
    images: list[str] | None = None,
) -> tuple[str, list[Step], str]:
    """(최종 답, 단계 기록, 수정 모델)"""
    members = ag.team_members(team)
    if not members:
        raise pv.ProviderError(f"없는 팀: {team}")
    cfg = team_config(team)
    web = bool(cfg.get("web"))
    lead_spec = pv.parse_model(override or ag.Agent(members[0]).model_spec())   # 수정: 큰 모델
    member_spec = pv.parse_model(override or cfg.get("member_model") or ag.Agent(members[0]).model_spec())
    max_parallel = max(1, int(settings().get("max_parallel") or 3))
    question = history[-1]["content"]
    pick = lambda i: members[i % len(members)]  # noqa: E731 — 팀원이 2명이면 돌아가며 맡음

    def system_for(member: str, stage: str = "") -> str:
        s = ag.Agent(member).system_prompt() + ("\n\n" + extra_system if extra_system else "")
        return s + ("\n\n" + stage if stage else "")

    def call(step: Step, system: str, msgs: list[dict], spec, use_web: bool, kind: str = "") -> Step:
        try:
            step.text = pv.chat(spec, system, msgs, effort=effort, web=use_web, images=images)
            step.verdict = _verdict(step.text, kind) if kind else ""
        except pv.ProviderError as e:
            step.error = str(e)
        return step

    def run_parallel(steps_and_args):
        for st, *_ in steps_and_args:
            if on_step_start:
                on_step_start(st)
        with ThreadPoolExecutor(max_workers=min(len(steps_and_args), max_parallel)) as pool:
            futs = {pool.submit(call, *args): args[0] for args in steps_and_args}
            for f in as_completed(futs):
                if on_step_done:
                    on_step_done(f.result())
        return [args[0] for args in steps_and_args]

    steps: list[Step] = []

    # ① 초안
    draft = Step("draft", "✍️ 초안", pick(0), ag.member_view(pick(0)))
    run_parallel([(draft, system_for(pick(0)), history, member_spec, web)])
    steps.append(draft)
    if not draft.text.strip():
        raise pv.ProviderError(f"초안 작성 실패: {draft.error}")

    # ② 검토 2명 (근거·출처 / 숫자·논리)
    base = history[:-1]
    review_msg = [{"role": "user", "content": f"<question>\n{question}\n</question>\n\n<draft>\n{draft.text}\n</draft>"}]
    v1 = Step("verify1", "🔍 검토 · 근거·출처", pick(1), "자료·출처 대조")
    v2 = Step("verify2", "🧮 검토 · 숫자·논리", pick(2), "계산·논리 재확인")
    run_parallel([(v1, system_for(pick(1), VERIFY_EVIDENCE), base + review_msg, member_spec, web, "verify"),
                  (v2, system_for(pick(2), VERIFY_LOGIC), base + review_msg, member_spec, False, "verify")])
    steps += [v1, v2]

    answer = draft.text
    fixes = [v for v in (v1, v2) if v.verdict == "fix" and v.text]
    if fixes:
        rev = Step("revise1", "✏️ 검토 의견 반영", members[0], f"{len(fixes)}명 지적 반영")
        notes = "\n\n".join(f"<review by=\"{v.title}\">\n{v.text}\n</review>" for v in fixes)
        msg = [{"role": "user", "content": f"<question>\n{question}\n</question>\n\n<draft>\n{draft.text}\n</draft>\n\n{notes}"}]
        run_parallel([(rev, system_for(members[0], REVISE_PROMPT), base + msg, lead_spec, False)])
        steps.append(rev)
        if rev.text.strip():
            answer = rev.text

    # ③ 최종 승인 3명
    approve_msg = [{"role": "user", "content": f"<question>\n{question}\n</question>\n\n<answer>\n{answer}\n</answer>"}]
    approvers = [Step(f"approve{i}", f"✅ 최종 승인 {i}/3", pick(i - 1), ag.member_view(pick(i - 1))) for i in (1, 2, 3)]
    run_parallel([(a, system_for(a.member, APPROVE_PROMPT.format(i=i, n=3)), base + approve_msg, member_spec, False, "approve")
                  for i, a in enumerate(approvers, 1)])
    steps += approvers
    rejects = [a for a in approvers if a.verdict == "reject" and a.text]
    ok = sum(a.verdict == "approve" for a in approvers)

    # 요약 배지 (코드가 만듦 — 모델이 지어낼 수 없게)
    def mark(v: Step) -> str:
        return "⚠️ 오류" if v.error else ("✅" if v.verdict == "pass" else "✏️ 수정")
    header = (f"🏆 검토 완료 · 근거 {mark(v1)} · 숫자·논리 {mark(v2)} · 최종 승인 {ok}/3"
              + (f" (반려 {len(rejects)}건 반영)" if rejects else ""))

    if rejects:  # 마지막 수정 — 스트리밍으로 바로 보여줌
        if on_text:
            on_text(header + "\n\n")
        notes = "\n\n".join(f"<review by=\"{a.title}\">\n{a.text}\n</review>" for a in rejects)
        msg = [{"role": "user", "content": f"<question>\n{question}\n</question>\n\n<draft>\n{answer}\n</draft>\n\n{notes}"}]
        final = pv.chat(lead_spec, system_for(members[0], REVISE_PROMPT), base + msg, on_text=on_text, effort=effort)
        steps.append(Step("revise2", "✏️ 최종 수정", members[0], f"반려 {len(rejects)}건 반영", text=final))
        return header + "\n\n" + final, steps, str(lead_spec)

    if on_text:
        on_text(header + "\n\n" + answer)
    return header + "\n\n" + answer, steps, str(lead_spec)
