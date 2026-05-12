# 🧭 CEO (Chief Executive Agent) 개인 메모리

_CEO 에이전트만 읽고 쓰는 개인 노트. 학습·교훈·자주 쓰는 패턴이 누적됩니다._

## 학습 기록

- [2026-05-12] 지금 ezer ai에서 iunject 가안됨 ⚠️ 포트 충돌 가능성 연결 실패가 지속되고 있습니다. 다른 앱이 4825 포트를 점유하고 있을 수 있습니다. 터미널에서 `lsof -nP -iTCP:4825 -sTCP:LISTEN` 로 확인해주세요. 계속이것만뜸 → 보고서 sessions/2026-05-12T05-16/_report.md
- [2026-05-12] C:\Users\admin>lsof -nP -iTCP:4825 -sTCP:LISTEN 'lsof'은(는) 내부 또는 외부 명령, 실행할 수 있는 프로그램, 또는 배치 파일이 아닙니다. → 보고서 sessions/2026-05-12T05-17/_report.md
- [2026-05-12] 꼭 있어야하는거야? → 보고서 sessions/2026-05-12T05-18/_report.md
- [2026-05-12] 아니요. Windows 환경이라 lsof 설치는 진행하지 않겠습니다. 이미 netstat와 curl로 확인했으며, 4825 포트는 Antigravity.exe가 정상적으로 LISTENING 중이고 /ping 응답도 정상입니다. 포트 점유 문제가 아니라 UI 캐시 또는 프론트 상태 갱신 문제로 보입니다. → 보고서 sessions/2026-05-12T05-20/_report.md
- [2026-05-12] Windows라서 lsof는 설치하지 않겠습니다. netstat로 확인한 결과 4825 포트는 Antigravity.exe가 LISTENING 중입니다.  curl http://127.0.0.1:4825/ping 결과: {"status":"ok","msg":"Connect AI Bridge Ready"}  curl http://127.0.0.1:11434/api/tags 결과: gemma4:e2b 모델도 설치되어 있습니다.  따라서 포트 점유 문제가 아니라 UI 상태 갱신, 프론트 캐시, 또는 Connect AI Lab 내부 체크 
- [2026-05-12] 테스트 진행 → 보고서 sessions/2026-05-12T05-27/_report.md
- [2026-05-12] 추가 지시입니다.  lsof / Chocolatey 설치는 진행하지 마세요. 이미 4825 포트와 Ollama는 정상입니다.  다음 순서로 진행하세요.  1. 모델 실행 테스트 CMD에서 아래 명령어 실행:  curl http://127.0.0.1:11434/api/generate -d "{\"model\":\"gemma4:e2b\",\"prompt\":\"hello\",\"stream\":false}"  응답에 "response"가 나오는지 확인하세요.  2. Connect AI 웹 UI 캐시 초기화 안내 Chrome에서 Conn
- [2026-05-12] Developer: Toggle Developer Tools → 보고서 sessions/2026-05-12T05-29/_report.md
- [2026-05-12] workbench.desktop.main.js:4236  WARN SettingsEditor2: Settings not included in settingsLayout.ts: antigravity.enableAgentMode, extensions.requestTimeout, chat.agent.terminal.allowList, chat.agent.terminal.autoApprove, chat.agent.terminal.denyList, chat.tools.terminal.autoApprove, chat.tools.terminal
- [2026-05-12] [모닝 브리핑] 오늘 날짜는 2026-05-12입니다. 회사 목표(goals.md)와 지금까지의 의사결정 로그를 바탕으로 오늘 우리 회사가 우선순위로 처리해야 할 작업 3가지를 결정하고, 각 작업을 적절한 에이전트에게 분배하세요. → 보고서 sessions/2026-05-12T05-31/_report.md