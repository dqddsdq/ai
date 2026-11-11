# 🤖 자동매매 AI — 폴더 안내

## 폴더 구조
- `_shared/` — 사장님이 정한 원칙·목표 (자동매매 팀이 매번 읽음)
  - `goals.md` — 목표 · `decisions.md` — 사장님이 확정한 원칙 (가장 우선)
  - `settings.json` — 모델 설정 · `tools_catalog.json` — 도구·키 허용 목록 (🔒)
  - `autotrade.json` — 자동매매 설정 (🔒 화면에서만 바꿈)
- `_agents/trader/` — 자동매매 팀 (prompt.md 지시문 · goal.md · memory.md 기억)
- `engine/` — 엔진 코드 (README.md 에 전체 설명)
- `_cache/` — 계좌 기록·토큰·학습 일지 (git 제외 · 백업은 _cache/backup/)

## 팀
- 🤖 **자동매매** `trader` (1명): 자동매매 엔진과 대화 — 오늘 뭐 샀는지·왜 샀는지/팔았는지/안 샀는지, 성적·복기·학습 제안, 설정 바꾸기·종목 제외·멈추기 요청, 코드 개선 제안
