"""📒 노트 정리 회의 — AI 셋이 서로 의견을 주고받아 공유 노트를 정리하고, 다수가 동의한 것만 핵심 노트로.

- 참가: 📝 정리 담당(합치고 다듬음) · 🔍 검증 담당(거래 기록 숫자로 뒷받침되는지) · 📈 현장 담당(실제 매매에 바로 쓸 수 있는지)
- 순서: ① 정리 담당이 초안(핵심 노트 최대 MAX_NOTES 개 + 뺄 교훈) → ② 두 담당이 항목마다 찬성/반대·이유·고칠 문장
        → ③ 반대가 있으면 정리 담당이 반영해서 고침 → ②로 (최대 ROUNDS 번)
        → 마지막 투표에서 3명 중 2명 이상 찬성(정리 담당은 자기 초안에 찬성)한 항목만 핵심 노트
        → 둘 다 반대한 항목은 '의견 갈림' 으로 따로 보여 주고 핵심 노트엔 안 넣음
- 결과: AI 들이 판단할 때 읽는 공유 노트가 '최근 교훈 n개' → '핵심 노트 + 그 뒤 새 교훈' 으로 (tradelearn.lessons_text)
- 원본 교훈은 지우지 않음 · 채팅 '📒 노트 정리 회의' 에 대화 전체 + [되돌리기] 카드
- 언제: 주말 공부 끝(설정 notes_council) · 🤖 학습·기록 화면 [지금 노트 정리]
"""

from __future__ import annotations

import json
import re
import statistics as st
import threading
from datetime import datetime, timedelta, timezone

from . import tradelearn as tl

KST = timezone(timedelta(hours=9))
MAX_NOTES = 12
ROUNDS = 2          # 고치고 다시 투표하는 최대 횟수
MIN_LESSONS = 6     # 이보다 적으면 정리 안 함
_running = threading.Event()

ROLES = {
    "editor": ("📝 정리 담당", "너는 자동매매 공유 노트의 편집자다. 같은 뜻은 하나로 합치고, 매매 판단에 바로 쓸 수 있게 짧고 구체적으로 다듬는다. "
               "근거 숫자는 아래 자료에 있는 것만 쓴다. JSON 으로만 답한다."),
    "skeptic": ("🔍 검증 담당", "너는 깐깐한 데이터 검증가다. 교훈마다 아래 거래 기록 숫자로 뒷받침되는지 본다. 표본이 적거나(10건 미만) 숫자와 반대면 반대한다. "
                "말이 그럴듯해도 근거가 없으면 반대한다. JSON 으로만 답한다."),
    "trader": ("📈 현장 담당", "너는 급등주 단타 트레이더다. 교훈이 실제 매매 순간에 바로 적용할 수 있을 만큼 구체적인지(언제·무엇을·얼마나) 본다. "
               "애매하거나 다른 교훈과 부딪히면 반대한다. JSON 으로만 답한다."),
}


# ── 근거 자료 ──
def evidence(market: str, days: int = 60) -> str:
    """AI 들이 교훈을 검증할 거래 기록 요약 (실전+가상, 최근 days 일)"""
    since = (datetime.now(KST) - timedelta(days=days)).strftime("%Y-%m-%d")
    out = []
    try:
        with tl._lock:
            ts = [t for t in tl._load()["trades"] if t.get("closed") and tl._tm(t) == market and str(t.get("entry_time", "")) >= since]
        if ts:
            out.append(f"최근 {days}일 끝난 거래 전체: {json.dumps(tl._stats(ts), ensure_ascii=False)}")
            out.append("오늘 상승률 구간별: " + json.dumps(tl._bucket(ts, "change", tl.CHANGE_EDGES, "%"), ensure_ascii=False))
            out.append("거래량 배수 구간별: " + json.dumps(tl._bucket(ts, "ratio", tl.RATIO_EDGES, "배"), ensure_ascii=False))
            for name, key in (("프리장 여부", lambda t: "프리장" if (t.get("features") or {}).get("pre") else "정규장"),
                              ("판 이유별", lambda t: t.get("exit_kind") or "?"),
                              ("전략별", lambda t: tl._strategy(t)),
                              ("실전/가상", lambda t: t.get("mode", "?"))):
                g: dict[str, list] = {}
                for t in ts:
                    g.setdefault(str(key(t)), []).append(t)
                out.append(f"{name}: " + json.dumps({k: tl._stats(v) for k, v in g.items()}, ensure_ascii=False))
        else:
            out.append(f"최근 {days}일 끝난 거래 없음")
    except Exception as e:
        out.append(f"(거래 기록 요약 실패: {type(e).__name__})")
    try:   # 🏟️ 생존전 상황별 (참가자 전원 합침) · 대조군
        from . import arena
        d = arena.load(market)
        sits: dict[str, list] = {}
        for a in d["alive"]:
            for t in a["trades"]:
                if t.get("sit"):
                    sits.setdefault(t["sit"], []).append(t["ret"])
        if sits:
            out.append("생존전 상황별(가상, 전원): " + ", ".join(f"{k} {len(v)}건 평균 {st.fmean(v):+.2f}%" for k, v in sorted(sits.items(), key=lambda x: -len(x[1]))))
        ct = d["ctrl"]["trades"] if d.get("ctrl") else []
        if ct:
            out.append(f"🎲 무작위 대조군: {len(ct)}건 평균 {st.fmean(t['ret'] for t in ct):+.2f}%")
    except Exception:
        pass
    return "\n".join(out)[:7000]


# ── AI 한 번 ──
def _ask(role: str, prompt: str) -> dict | None:
    from . import providers as pv
    from . import team as tm
    try:
        reply = pv.chat(tm.fast_spec("claudecode:sonnet"), ROLES[role][1], [{"role": "user", "content": prompt}], effort="low", timeout=180)
    except Exception:
        return None
    m = re.search(r"\{.*\}", reply or "", re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except json.JSONDecodeError:
        return None


def _notes_block(notes: list[dict]) -> str:
    return "\n".join(f"- {n['id']}: {n['text']} (근거: {n.get('evidence') or '없음'} · 출처 {', '.join(n.get('from') or [])})" for n in notes)


def _clean_notes(raw, src_ids: set) -> list[dict]:
    out = []
    for i, n in enumerate(raw or []):
        if not isinstance(n, dict) or not str(n.get("text") or "").strip():
            continue
        out.append({"id": str(n.get("id") or f"N{i + 1}")[:8], "text": str(n["text"]).strip()[:200],
                    "from": [x for x in (n.get("from") or []) if x in src_ids][:10], "evidence": str(n.get("evidence") or "").strip()[:160]})
    seen, uniq = set(), []
    for n in out:   # id 겹치면 새로 매김
        if n["id"] in seen:
            n["id"] = f"N{len(seen) + 1}x"
        seen.add(n["id"]); uniq.append(n)
    return uniq[:MAX_NOTES]


VOTE_FMT = '{"votes": {"N1": {"ok": true, "why": "한 줄", "fix": "이렇게 고치면 찬성 (없으면 빈칸)"}, ...}, "drop_ok": true, "comment": "전체 의견 한두 줄"}'


def run(c: dict, market: str, ai_fn=None, log=print) -> dict:
    """회의 한 번. 결과 + 대화 기록(turns). 합의된 게 있으면 핵심 노트로 저장"""
    ask = ai_fn or _ask
    src = [x for x in tl.lessons(market, 80)[::-1] if x.get("kind") != "owner_ref"]   # 오래된 → 최근 · 사장님 참고 의견은 핵심 노트로 안 올림
    if len(src) < MIN_LESSONS:
        return {"ok": False, "message": f"교훈이 {len(src)}개라 아직 정리할 만큼 안 쌓였어요 ({MIN_LESSONS}개부터)", "turns": []}
    ids = {f"L{i + 1}": x for i, x in enumerate(src)}
    lesson_block = "\n".join(f"- {k}: [{x.get('t', '')[:10]} · {x.get('by') or '?'}] {x['text']}" for k, x in ids.items())
    ev = evidence(market)
    prev = tl.core(market).get("items") or []
    base = (f"시장: {'미장' if market == 'US' else '국장'}\n[지금까지 쌓인 교훈 원본]\n{lesson_block}\n\n[거래 기록 요약 — 근거는 여기 숫자만]\n{ev}\n"
            + (f"\n[지난번 합의한 핵심 노트]\n" + "\n".join(f"- {x['text']}" for x in prev) + "\n" if prev else ""))
    turns: list[dict] = []

    def say(role: str, text: str, data=None):
        turns.append({"role": role, "name": ROLES[role][0], "text": text, "data": data})

    # ① 초안
    j = ask("editor", base + f"\n위 교훈을 정리해서 핵심 노트 초안을 만드세요 (최대 {MAX_NOTES}개, 중요한 순). 같은 뜻은 합치고, 근거 숫자가 있으면 evidence 에. "
                     "핵심 노트에 안 넣을 교훈은 drop 에 이유와 함께.\n"
                     '형식: {"notes": [{"id": "N1", "text": "...", "from": ["L3", "L7"], "evidence": "..."}], "drop": [{"from": ["L5"], "why": "..."}], "comment": "한두 줄"}')
    if not isinstance(j, dict):
        return {"ok": False, "message": "정리 담당 AI 가 답하지 않았어요", "turns": turns}
    notes = _clean_notes(j.get("notes"), set(ids))
    drop = [x for x in (j.get("drop") or []) if isinstance(x, dict)][:40]
    say("editor", str(j.get("comment") or "초안이에요."), {"notes": notes, "drop": drop})
    if not notes:
        return {"ok": False, "message": "초안이 비었어요", "turns": turns}

    votes: dict[str, dict] = {}
    asked: dict[str, str] = {}   # 지난번 정리 담당에게 넘긴 반대 항목 {id: 그때 문장}
    for rnd in range(ROUNDS + 1):
        # ② 두 담당 투표
        votes = {}
        for role in ("skeptic", "trader"):
            others = "".join(f"\n{t['name']} 의견: {t['text']}" for t in turns[-2:] if t["role"] != role and t["role"] != "editor")
            v = ask(role, base + f"\n[정리 담당의 핵심 노트 {'초안' if rnd == 0 else f'고친 안 ({rnd}번째)'}]\n{_notes_block(notes)}\n"
                              + (f"[뺄 교훈] {json.dumps(drop, ensure_ascii=False)[:1500]}\n" if drop else "") + others
                              + f"\n항목마다 찬성(ok true)/반대(ok false)와 이유. 반대면 fix 에 이렇게 고치면 찬성하겠다는 문장. 형식: {VOTE_FMT}")
            v = v if isinstance(v, dict) else {}
            votes[role] = {k: x for k, x in (v.get("votes") or {}).items() if isinstance(x, dict)}
            say(role, str(v.get("comment") or ("답이 없었어요 (반대로 칩니다)" if not v else "")), {"votes": votes[role], "drop_ok": v.get("drop_ok", True)})
        against = {n["id"]: [r for r in ("skeptic", "trader") if not (votes[r].get(n["id"]) or {}).get("ok")] for n in notes}
        if not any(against.values()) or rnd == ROUNDS:
            break
        now_contested = {n["id"]: n["text"] for n in notes if against[n["id"]]}
        if all(asked.get(k) == v for k, v in now_contested.items()):   # 정리 담당이 이미 안 고치기로 한 것만 남음 → 투표로 결정
            break
        asked = now_contested
        # ③ 정리 담당이 반대 의견 반영
        objections = "\n".join(f"- {nid} ({next(n['text'] for n in notes if n['id'] == nid)}): "
                               + " / ".join(f"{ROLES[r][0]} 반대 — {(votes[r].get(nid) or {}).get('why', '이유 없음')}"
                                            + (f" · 이렇게 고치면 찬성: {(votes[r].get(nid) or {}).get('fix')}" if (votes[r].get(nid) or {}).get("fix") else "")
                                            for r in rs) for nid, rs in against.items() if rs)
        j = ask("editor", base + f"\n[지금 안]\n{_notes_block(notes)}\n[반대 의견]\n{objections}\n"
                         "반대 의견을 받아들일 건 고치고, 근거가 없는 항목은 빼고, 받아들이지 않을 건 이유를 comment 에. 같은 id 를 유지하세요.\n"
                         '형식: {"notes": [{"id": "N1", "text": "...", "from": [...], "evidence": "..."}], "drop": [...], "comment": "무엇을 고쳤는지"}')
        if not isinstance(j, dict) or not _clean_notes(j.get("notes"), set(ids)):
            say("editor", "고친 안을 못 만들었어요. 지금 안으로 마지막 투표를 할게요.")
            break
        notes = _clean_notes(j.get("notes"), set(ids))
        drop = [x for x in (j.get("drop") or []) if isinstance(x, dict)][:40] or drop
        say("editor", str(j.get("comment") or "고쳤어요."), {"notes": notes, "drop": drop})

    # ④ 다수결: 정리 담당(찬성) + 두 담당 중 한 명 이상 찬성 → 채택
    agreed, split = [], []
    for n in notes:
        yes = [r for r in ("skeptic", "trader") if (votes.get(r, {}).get(n["id"]) or {}).get("ok")]
        item = {**n, "votes": f"{1 + len(yes)}/3", "yes": ["editor"] + yes}
        (agreed if yes else split).append(item)
        if not yes:
            item["why_not"] = " / ".join(f"{ROLES[r][0]}: {(votes.get(r, {}).get(n['id']) or {}).get('why', '답 없음')}" for r in ("skeptic", "trader"))
    if not agreed:
        return {"ok": False, "message": "합의된 노트가 없어서 지금 노트를 그대로 둬요", "turns": turns, "split": split}
    tl.set_core(market, agreed, split=split, drop=drop, n_src=len(src))
    return {"ok": True, "agreed": agreed, "split": split, "drop": drop, "turns": turns, "n_src": len(src),
            "message": f"교훈 {len(src)}개 → 핵심 노트 {len(agreed)}개 합의" + (f" · 의견 갈림 {len(split)}개" if split else "")
                       + (f" · 뺀 교훈 묶음 {len(drop)}개" if drop else "")}


# ── 채팅으로 ──
def _turn_text(t: dict) -> str:
    d = t.get("data") or {}
    lines = [f"**{t['name']}**", "", t["text"] or ""]
    if d.get("notes"):
        lines += ["", *[f"{i + 1}. `{n['id']}` {n['text']}" + (f" — _{n['evidence']}_" if n.get("evidence") else "") for i, n in enumerate(d["notes"])]]
    if d.get("drop"):
        lines += ["", "빼자는 교훈: " + " · ".join(f"{', '.join(x.get('from') or [])} ({str(x.get('why') or '')[:60]})" for x in d["drop"][:12])]
    if d.get("votes") is not None:
        vs = d["votes"]
        lines += ["", *[f"- `{k}` {'👍' if v.get('ok') else '👎'} {str(v.get('why') or '')[:120]}" + (f" → 고치면: {v['fix']}" if not v.get("ok") and v.get("fix") else "")
                        for k, v in vs.items()]] if vs else ["", "(투표 없음)"]
    return "\n".join(lines).strip()


def post(market: str, r: dict) -> str | None:
    """회의 대화 전체 + 결과 + [되돌리기] 카드를 채팅 새 대화로. 대화 id"""
    if not r.get("turns"):
        return None
    from . import agents as ag
    from . import web
    flag = "미장" if market == "US" else "국장"
    conv = web.new_conv(f"📒 노트 정리 회의 ({flag} {datetime.now(KST).strftime('%m-%d')})")
    conv["id"] += f"-notes{market}"
    conv["agent"] = ag.TRADER
    msg = lambda text, **kw: {"role": "assistant", "agent": ag.TRADER, "model": "", "sources": [], "drafts": [], "steps": [], "content": text, **kw}
    conv["messages"].append(msg("AI 셋이 공유 노트를 놓고 의견을 주고받았어요. 3명 중 2명 이상 찬성한 것만 핵심 노트가 돼요. 원본 교훈은 그대로 보관해요."))
    for t in r["turns"]:
        conv["messages"].append(msg(_turn_text(t)))
    res = [f"**결과: {r['message']}**"]
    if r.get("agreed"):
        res += ["", "✅ 핵심 노트 (이제 AI 들이 판단할 때 이걸 읽어요):", *[f"{i + 1}. {n['text']} _(찬성 {n['votes']})_" for i, n in enumerate(r["agreed"])]]
    if r.get("split"):
        res += ["", "⚖️ 의견 갈림 (핵심 노트엔 안 넣음):", *[f"- {n['text']} — {n.get('why_not', '')[:160]}" for n in r["split"]]]
    cards = [{"id": f"nu{market}{datetime.now(KST).strftime('%H%M%S')}", "type": "notes_undo", "market": market,
              "label": f"📒 이번 노트 정리 되돌리기 ({flag})", "why": "그 전 핵심 노트로 돌아가요 (원본 교훈은 그대로)"}] if r.get("ok") else []
    conv["messages"].append(msg("\n".join(res), **({"trade_actions": cards} if cards else {})))
    with web.CONV_LOCK:
        web.save_conv(conv)
    return conv["id"]


def run_and_post(c: dict, market: str, ai_fn=None, log=print) -> dict:
    try:
        r = run(c, market, ai_fn, log)
    except Exception as e:
        return {"ok": False, "message": f"노트 정리 실패: {type(e).__name__}: {e}"}
    try:
        r["conversation_id"] = post(market, r)
    except Exception as e:
        r["message"] += f" (채팅 저장 실패: {type(e).__name__})"
    try:
        from . import autotrade as at
        with at.in_market(market):
            at.event("노트 정리", f"📒 {r['message']}" + (" — 채팅 '📒 노트 정리 회의' 에서 대화 전체를 볼 수 있어요" if r.get("conversation_id") else ""))
    except Exception:
        pass
    return r


def run_bg(c: dict, markets: list[str]) -> dict:
    if _running.is_set():
        return {"ok": False, "message": "이미 노트 정리 회의 중이에요"}
    _running.set()

    def go():
        try:
            for m in markets:
                run_and_post(c, m)
        finally:
            _running.clear()
    threading.Thread(target=go, daemon=True, name="notes-council").start()
    return {"ok": True, "message": "노트 정리 회의를 시작했어요. 2~5분 뒤 채팅 '📒 노트 정리 회의' 에 대화 전체가 와요"}
