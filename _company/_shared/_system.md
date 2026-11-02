# 🧬 1인 기업 OS — 자가 매뉴얼

## 이 폴더는 무엇인가요?
당신의 1인 기업의 두뇌입니다. 7명의 AI 에이전트가 여기서 일합니다.

## 폴더 구조
- `_shared/` — 모든 에이전트가 매번 읽는 공동 메모리
  - `identity.md` — 회사 정체성 (이름, 톤, 가치)
  - `goals.md` — 목표
  - `decisions.md` — 의사결정 로그 (자가학습이 자동 누적)
  - `_system.md` — 이 파일
- `_agents/<id>/` — 각 에이전트 개인 공간
  - `memory.md` — 자가학습 (자동, append-only)
  - `prompt.md` — 페르소나 디테일 (사용자가 편집)
  - `config.md` — API 키·시크릿 (`.gitignore`로 보호)
- `sessions/<ts>/` — 세션별 산출물 (자동)
- `_cache/` — API 응답 캐시 (sync 제외)

## 메모리 위계 (충돌 시 우선순위)
1. `decisions.md` — 가장 강한 신뢰
2. `identity.md`
3. `goals.md`
4. 개인 메모리
5. 지식 베이스 (`10_Wiki/`)

## 다른 PC로 옮길 때
1. 새 PC에 Connect AI 설치
2. 👔 모드 ON → "📥 다른 PC에서 가져오기" 선택
3. GitHub URL 입력 → 자동 clone
4. 끝.

## 동기화 정책
- `_shared/`, `_agents/*/memory.md`, `_agents/*/prompt.md`, `sessions/` → git sync ✅
- `_agents/*/config.md`, `_cache/` → git sync ❌ (시크릿·캐시)

## 5개 팀 · 12명의 에이전트
같은 팀 팀원들은 같은 질문에 각자 관점으로 답하고, 심사 단계에서 비교해 가장 좋은 답 하나로 정리합니다.

- 🧭 **CEO** `ceo` (2명: 기회·성장 / 리스크·현실): 오케스트레이션, 작업 분해, 종합 판단, 다음 액션 결정, 업무 배정
- 💰 **Business** `business` (3명: 안정 수익 / 공격적 성장 / 숫자·실행): 수익화 모델, 가격 전략, 시장·경쟁 분석, ROI/KPI, 시뮬레이션
- 🔍 **Researcher** `researcher` (3명: 트렌드 / 데이터 검증 / 반대 의견): 트렌드 리서치, 경쟁사 분석, 데이터 수집·요약, 사실 확인
- 💻 **Developer** `developer` (2명: 빠른 구현 / 안정성): 코드, 자동화, API 통합, 웹사이트·홈페이지, 디버깅
- ✍️ **Writer** `writer` (2명: 스토리·감성 / SEO·정보): 블로그 글, 카피, 영상 대본, SNS 캡션, 후크
