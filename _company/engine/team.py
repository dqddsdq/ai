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


def fast_spec(override: str | None = None):
    """팀 배정·종목 찾기처럼 짧은 판단용 빠른 모델 (CEO 팀 member_model, 기본 Haiku). 사용자가 모델을 고르면 그걸 씀."""
    return pv.parse_model(override or team_config("ceo").get("member_model") or "claudecode:haiku")


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

VERIFY_RISK = """[검토 단계 — 반대·리스크 확인 담당]
아래 <draft> 는 같은 팀 동료가 사장님 질문(<question>)에 쓴 초안입니다. 당신은 '이 답이 틀렸다면 왜일까'를 찾습니다.
- 빠진 위험, 반대 시나리오, 사장님에게 불리한 조건·예외, 지나친 낙관, 놓친 비용·세금·기한을 찾으세요.
- 근거 없는 트집은 잡지 마세요. 실제로 결론을 바꿀 수 있는 것만. 각 항목: ✅ / ❌(보완안) / ⚠️ 로 짧게.
- 마지막 줄은 반드시 둘 중 하나: `판정: 통과` 또는 `판정: 수정 필요`"""

# 중간 검토 기본 구성: Claude 3 (항상) + 다른 AI 3 (무료 사용량이 있을 때만 참여, 안 되면 빠짐)
# models: 앞에서부터 쓸 수 있는 첫 번째 AI 를 씀. "freellm:#1" = .env FREELLM_MODELS 의 1번째 모델
ROLE_PROMPTS = {"evidence": None, "logic": None, "risk": None}   # 아래에서 채움
ROLE_NAMES = {"evidence": ("🔍", "근거·출처"), "logic": ("🧮", "숫자·논리"), "risk": ("😈", "반대·리스크")}
DEFAULT_REVIEWERS = [
    {"role": "evidence", "models": [""], "label": "Claude"},
    {"role": "logic", "models": [""], "label": "Claude"},
    {"role": "risk", "models": [""], "label": "Claude"},
    {"role": "evidence", "models": ["freellm:#1", "gemini"], "label": "무료 AI 1", "optional": True},
    {"role": "logic", "models": ["freellm:#2", "openai"], "label": "무료 AI 2", "optional": True},
    {"role": "risk", "models": ["freellm:#3", "mistral"], "label": "무료 AI 3", "optional": True},
]
# 최종 승인 기본 구성: Claude 3
DEFAULT_APPROVERS = [{"models": [""], "label": "Claude"} for _ in range(3)]


def resolve_seat(seat: dict, default_spec) -> tuple | None:
    """자리 → (모델, 이름). 쓸 수 있는 AI 가 없으면: 선택 자리는 None(빠짐), 필수 자리는 Claude."""
    import os

    candidates = seat.get("models") or [seat.get("model", "")]
    for m in candidates:
        if not m:
            return default_spec, seat.get("label") or "Claude"
        spec = pv.parse_model(m)
        if spec.provider == "freellm" and spec.model.startswith("#"):
            names = [x.strip() for x in os.getenv("FREELLM_MODELS", "").split(",") if x.strip()]
            idx = int(spec.model[1:] or 1) - 1
            spec = pv.ModelSpec("freellm", names[idx] if idx < len(names) else os.getenv("FREELLM_MODEL", "auto"))
        if pv.is_ready(spec):
            name = "FreeLLMAPI" if spec.provider == "freellm" else spec.provider.capitalize()
            return spec, f"{name} · {spec.model}"
    if seat.get("optional"):
        return None
    return default_spec, (seat.get("label") or "Claude") + " (대체)"


REVISE_PROMPT = """[수정 단계]
아래 <draft>(초안)를 <review>(검토 의견)에 따라 고쳐서 사장님께 드릴 답을 다시 쓰세요.
- 검토자는 여러 회사 AI 라서 의견이 엇갈리거나 틀릴 수 있어요. 근거 있는 ❌ 지적만 반영하고, 근거 없거나 틀린 지적은 무시하세요.
- ⚠️ 확인 못 한 주장은 빼거나 "확인 필요"라고 표시하세요.
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
    model: str = ""       # 이 단계를 맡은 AI (화면의 '담당 AI' 표시용)

    def as_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "member": self.member, "view": self.view,
                "text": self.text, "verdict": self.verdict, "error": self.error, "model": self.model}


def _verdict(text: str, kind: str) -> str:
    """마지막 '판정 줄'만 읽습니다. (본문의 "반려 사유 아님" 같은 말에 속지 않도록)"""
    import re

    good, bad = ("통과", "수정필요") if kind == "verify" else ("승인", "반려")
    lines = [l for l in text.strip().splitlines() if l.strip()]
    # 판정 줄 후보: 아래에서부터, 마크다운 기호를 빼고 짧은 줄 (예: "승인", "**판정: 통과**", "`반려`")
    for line in reversed(lines[-4:]):
        clean = re.sub(r"[\s*`_#>|]", "", line)
        head = re.sub(r"\(.*$", "", clean)              # "판정:통과(보완하면 더 좋음)" → "판정:통과"
        head = head.replace("판정:", "").replace("판정", "").strip(":.,!·-")
        head = re.sub(r"(합니다|입니다|함|해요|합니다요)$", "", head)  # "승인합니다" → "승인"
        if head == good or head.endswith(good) and bad not in head:
            return "pass" if kind == "verify" else "approve"
        if head == bad or head.endswith(bad) and good not in head:
            return "fix" if kind == "verify" else "reject"
    return "fix" if kind == "verify" else "reject"       # 판정 줄을 못 찾으면 안전하게 '다시 보기' 쪽


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
        for st, _sys, _msgs, spec, *_ in steps_and_args:
            st.model = str(spec)
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

    # ② 중간 검토: Claude 3 + 다른 AI 최대 3 (쓸 수 있을 때만)
    base = history[:-1]
    prompts = {"evidence": VERIFY_EVIDENCE, "logic": VERIFY_LOGIC, "risk": VERIFY_RISK}
    reviewer_seats = cfg.get("reviewers") or settings().get("reviewers") or DEFAULT_REVIEWERS

    def review_round(text: str, tag: str, must_fix: str = "") -> list[Step]:
        content = f"<question>\n{question}\n</question>\n\n<draft>\n{text}\n</draft>"
        if must_fix:
            content += ("\n\n<must_fix>\n최종 승인에서 반려된 사유입니다. 이 지적이 위 draft 에서 "
                        f"실제로 고쳐졌는지 반드시 확인하세요.\n{must_fix}\n</must_fix>")
        jobs = []
        for i, seat in enumerate(reviewer_seats):  # noqa: B007
            got = resolve_seat(seat, member_spec)
            if got is None:
                continue                                  # 무료 AI 사용 불가 → 이번엔 빠짐
            spec, label = got
            role = seat.get("role", "logic")
            icon, rname = ROLE_NAMES.get(role, ("🔍", role))
            member = pick(i)
            st = Step(f"{tag}_r{i + 1}", f"{icon} {'재검토 ' + tag + ' · ' if must_fix else '검토 · '}{rname}", member, label)
            use_web = web and role == "evidence" and spec.provider == "claudecode"
            jobs.append((st, system_for(member, prompts.get(role, VERIFY_LOGIC)), base + [{"role": "user", "content": content}],
                         spec, use_web, "verify"))
        run_parallel(jobs)
        done = [j[0] for j in jobs]
        for st in done:                                   # 다른 AI 오류(사용량 제한 등)는 판정에서 제외
            if st.error and st.view and "Claude" not in st.view:
                st.verdict, st.title = "", st.title + " (오류로 제외)"
        return done

    first = review_round(draft.text, "1")
    steps += first
    answer = draft.text
    fixes = [v for v in first if v.verdict == "fix" and v.text]
    if fixes:
        rev = Step("revise1", "✏️ 검토 의견 반영", members[0], f"{len(fixes)}명 지적 검토 후 반영")
        notes = "\n\n".join(f"<review by=\"{v.title} ({v.view})\">\n{v.text}\n</review>" for v in fixes)
        msg = [{"role": "user", "content": f"<question>\n{question}\n</question>\n\n<draft>\n{draft.text}\n</draft>\n\n{notes}"}]
        run_parallel([(rev, system_for(members[0], REVISE_PROMPT), base + msg, lead_spec, False)])
        steps.append(rev)
        if rev.text.strip():
            answer = rev.text

    # ③ 최종 승인 (기본 Claude 3)
    approve_msg = [{"role": "user", "content": f"<question>\n{question}\n</question>\n\n<answer>\n{answer}\n</answer>"}]
    seats = cfg.get("approvers") or settings().get("approvers") or DEFAULT_APPROVERS
    resolved = [(seat, resolve_seat(seat, member_spec)) for seat in seats]
    resolved = [(seat, got) for seat, got in resolved if got is not None]
    n = len(resolved)
    approvers, jobs = [], []
    for i, (_seat, (spec, label)) in enumerate(resolved, 1):
        a = Step(f"approve{i}", f"✅ 최종 승인 {i}/{n}", pick(i - 1), label)
        approvers.append(a)
        jobs.append((a, system_for(a.member, APPROVE_PROMPT.format(i=i, n=n)), base + approve_msg, spec, False, "approve"))
    run_parallel(jobs)
    retry = [(a, *job[1:3], member_spec, False, "approve") for a, job in zip(approvers, jobs)
             if a.error and job[3] is not member_spec]
    for a, *_ in retry:
        a.view += " → 오류로 Claude 대신"
        a.error = ""
    if retry:
        run_parallel(retry)
    steps += approvers
    rejects = [a for a in approvers if a.verdict == "reject" and a.text]
    ok = sum(a.verdict == "approve" for a in approvers)
    threshold = min(n, max(1, int(settings().get("reject_threshold") or 2)))
    minor_rejects = rejects if 0 < len(rejects) < threshold else []
    if minor_rejects:          # 기준 미만 반려는 참고로만
        rejects = []

    # ④ 반려가 기준 이상이면: 1번으로 돌아가지 않고 "수정 → 중간 검토 재확인" 루프 (모두 통과할 때까지, 최대 N바퀴)
    rounds_done, unresolved = 0, []
    if rejects:
        max_rounds = max(1, int(settings().get("max_review_rounds") or 3))
        notes = [f"<review by=\"{a.title}\">\n{a.text}\n</review>" for a in rejects]
        must_fix = "\n\n".join(notes)
        for r in range(1, max_rounds + 1):
            rounds_done = r
            rev = Step(f"loop{r}_revise", f"✏️ 재수정 {r}회차", members[0], "반려·검토 지적 반영")
            msg = [{"role": "user", "content": f"<question>\n{question}\n</question>\n\n<draft>\n{answer}\n</draft>\n\n" + "\n\n".join(notes)}]
            run_parallel([(rev, system_for(members[0], REVISE_PROMPT), base + msg, lead_spec, False)])
            steps.append(rev)
            if rev.text.strip():
                answer = rev.text
            checks = review_round(answer, f"{r}회차", must_fix)
            steps += checks
            unresolved = [c for c in checks if c.verdict == "fix" and c.text]
            if not unresolved:
                break
            notes = [f"<review by=\"{c.title} ({c.view})\">\n{c.text}\n</review>" for c in unresolved]

    # 요약 배지 (코드가 각 단계의 실제 판정을 모아서 만듦 — 모델이 지어낼 수 없게)
    counted = [v for v in first if v.verdict]
    others = sum(1 for v in counted if "Claude" not in v.view)
    header = (f"🏆 검토 완료 · 검토 {sum(v.verdict == 'pass' for v in counted)}/{len(counted)} 통과"
              + (f" (다른 AI {others}명 참여)" if others else " (Claude만)")
              + (f" · 지적 {len(fixes)}건 반영" if fixes else "")
              + f" · 최종 승인 {ok}/{n}")
    if minor_rejects:
        header += f" (반려 {len(minor_rejects)}건은 기준 {threshold}명 미만이라 참고만)"
    if rejects and not unresolved:
        header += f" → 반려 {len(rejects)}건 수정, 재검토 {rounds_done}회차에 통과"
    elif rejects:
        header = header.replace("🏆 검토 완료", "⚠️ 검토 미완료") + f" → 재검토 {rounds_done}회 후에도 지적 남음"
        answer += "\n\n---\n**⚠️ 확인 필요 (검토에서 해결되지 않은 지적)**\n" + "\n".join(
            "- " + (c.text.strip().splitlines()[0][:200] if c.text.strip() else c.title) for c in unresolved)

    if on_text:
        on_text(header + "\n\n" + answer)
    return header + "\n\n" + answer, steps, str(lead_spec)
