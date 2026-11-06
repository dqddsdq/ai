"""대화 기록(sessions/*/chat.md)을 학습용 데이터(JSONL)로 바꿉니다.

만든 파일은 내 컴퓨터에서 로컬 모델을 파인튜닝할 때 씁니다
(Unsloth, MLX-LM, Axolotl 등이 이 'messages' 형식을 그대로 읽습니다).
좋았던 답변만 남기도록 sessions 폴더를 정리한 뒤 실행하면 품질이 좋아집니다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .agents import COMPANY_DIR, SESSIONS_DIR, Agent

TRAINING_DIR = COMPANY_DIR / "training"


def parse_chat(path: Path) -> list[tuple[str, str]]:
    """chat.md → [(누가, 내용), ...]"""
    text = path.read_text(encoding="utf-8")
    turns = []
    for block in re.split(r"\n### ", "\n" + text)[1:]:
        who, _, body = block.partition("\n")
        if body.strip():
            turns.append((who.strip(), body.strip()))
    return turns


def build_examples(include_system: bool = True) -> list[dict]:
    examples = []
    for chat in sorted(SESSIONS_DIR.glob("*/chat.md")):
        turns = parse_chat(chat)
        messages: list[dict] = []
        agent_id = None
        for who, body in turns:
            if who == "user":
                messages.append({"role": "user", "content": body})
            elif messages and messages[-1]["role"] == "user":
                agent_id = agent_id or who
                messages.append({"role": "assistant", "content": body})
        if len(messages) < 2:
            continue
        if messages[-1]["role"] == "user":
            messages.pop()
        if include_system and agent_id and Agent(agent_id).exists():
            # 에이전트 페르소나만 짧게 (공동 메모리 전체를 넣으면 너무 길어짐)
            persona = f"당신은 1인 기업의 AI 에이전트 '{agent_id}' 입니다. 한국어로 답하세요."
            messages.insert(0, {"role": "system", "content": persona})
        examples.append({"messages": messages, "agent": agent_id or "unknown", "source": str(chat.parent.name)})
    return examples


def export(include_system: bool = True) -> tuple[Path, int]:
    TRAINING_DIR.mkdir(exist_ok=True)
    out = TRAINING_DIR / "dataset.jsonl"
    examples = build_examples(include_system)
    with out.open("w", encoding="utf-8") as f:
        for ex in examples:
            f.write(json.dumps(ex, ensure_ascii=False) + "\n")
    return out, len(examples)
