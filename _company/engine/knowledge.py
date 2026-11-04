"""내 자료(00_Raw, 10_Wiki)에서 질문과 관련된 부분을 찾아 에이전트에게 붙여줍니다.

추가 설치 없이 동작하는 간단한 검색(RAG)입니다. 모델을 다시 학습시키지 않아도
자료 폴더에 .md 파일을 넣기만 하면 바로 답변에 반영됩니다.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

from .agents import COMPANY_DIR

REPO_DIR = COMPANY_DIR.parent
KNOWLEDGE_DIRS = (REPO_DIR / "00_Raw", REPO_DIR / "10_Wiki", COMPANY_DIR / "10_Wiki")
CHUNK_CHARS = 1200
TOP_K = 3
MAX_CONTEXT_CHARS = 4000


@dataclass
class Chunk:
    source: str
    text: str
    grams: frozenset


def _grams(text: str) -> frozenset:
    """글자 2개씩 묶음 — 한국어는 띄어쓰기/조사가 달라도 잘 맞습니다."""
    s = re.sub(r"[^\w]", "", text.lower())
    return frozenset(s[i:i + 2] for i in range(len(s) - 1))


def _split(text: str) -> list[str]:
    sections = re.split(r"\n(?=#{1,3} )", text)
    out = []
    for sec in sections:
        while len(sec) > CHUNK_CHARS:
            cut = sec.rfind("\n", 0, CHUNK_CHARS)
            cut = cut if cut > CHUNK_CHARS // 2 else CHUNK_CHARS
            out.append(sec[:cut])
            sec = sec[cut:]
        if sec.strip():
            out.append(sec)
    return out


def load_chunks() -> list[Chunk]:
    chunks = []
    for base in KNOWLEDGE_DIRS:
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            rel = str(path.relative_to(REPO_DIR))
            for part in _split(text):
                chunks.append(Chunk(rel, part.strip(), _grams(part)))
    return chunks


def search(query: str, chunks: list[Chunk] | None = None, k: int = TOP_K) -> list[tuple[float, Chunk]]:
    chunks = load_chunks() if chunks is None else chunks
    q = _grams(query)
    if not q:
        return []
    scored = []
    for c in chunks:
        overlap = len(q & c.grams)
        if overlap >= 2:
            # 코사인 유사도: 긴 글이 무조건 이기지 않도록 길이로 나눔
            scored.append((overlap / math.sqrt(len(q) * len(c.grams)), c))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [s for s in scored[:k] if s[0] >= 0.09]  # 0.05 는 관계없는 자료가 붙었음


def context_for(query: str) -> str:
    """시스템 프롬프트에 붙일 참고자료 블록 (없으면 빈 문자열)."""
    hits = search(query)
    if not hits:
        return ""
    parts, used = [], 0
    for _, c in hits:
        if used + len(c.text) > MAX_CONTEXT_CHARS:
            break
        parts.append(f"<reference source=\"{c.source}\">\n{c.text}\n</reference>")
        used += len(c.text)
    return (
        "아래는 사용자 자료에서 찾은 참고자료입니다. 관련 있으면 활용하고, "
        "근거로 쓸 때는 출처 파일명을 밝히세요.\n\n" + "\n\n".join(parts)
    )
