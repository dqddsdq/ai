@echo off
chcp 65001 > nul
cd /d "%~dp0.."
title GitHub 충돌 해결
echo ============================================================
echo  GitHub 충돌 해결 (한 번만)
echo  - PC 자동 개선 변경은 GitHub 백업 브랜치에 먼저 보관해요 (Claude 가 나중에 합침)
echo  - 코드 폴더(engine, tests, 팀 지시문)만 GitHub 최신으로 바꿔요
echo  - 키(.env) / 학습 기록(_cache) / 설정 / 팀 기억 / 대화 기록은 안 건드려요
echo ============================================================
echo.
echo [먼저] 자동매매 앱 창을 닫아 주세요. 닫았으면 아무 키나 누르세요.
pause > nul

set "B=claude/build-ai-qnubm7"
git rebase --abort > nul 2>&1
git merge --abort > nul 2>&1

echo [1/4] GitHub 최신 받는 중...
git fetch sync %B%
if errorlevel 1 (
  echo [오류] 받기 실패. 인터넷과 GitHub 로그인을 확인하세요.
  pause
  exit /b 1
)

echo [2/4] PC 변경 보관 (커밋 후 GitHub 백업 브랜치로)
git add -A -- _company/engine _company/tests _company/_agents/trader/prompt.md _company/_agents/trader/goal.md _company/_agents/trader/team.json _company/requirements.txt
git reset -q HEAD -- _company/.env > nul 2>&1
git commit -q -m "PC 변경 (충돌 해결 전 보관)" > nul 2>&1
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmm"') do set "TS=%%i"
git branch -f pc-backup-%TS% HEAD
git push -q sync HEAD:refs/heads/pc-backup/%TS%
if errorlevel 1 (
  echo [오류] 백업 올리기 실패 - 아무것도 안 바꾸고 멈춰요. GitHub 로그인을 확인하세요.
  pause
  exit /b 1
)
echo        보관: GitHub 'pc-backup/%TS%' + PC 'pc-backup-%TS%'

echo [3/4] 코드 폴더만 GitHub 최신으로
git reset -q --mixed sync/%B%
git checkout sync/%B% -- _company/engine _company/tests _company/_agents/trader/prompt.md _company/_agents/trader/goal.md _company/_agents/trader/team.json _company/requirements.txt

echo [4/4] 자체 점검
cd _company
set "PY=python"
py -3 --version > nul 2>&1 && set "PY=py -3"
%PY% -B -m engine.selftest
%PY% -B -m tests.scenarios
echo.
echo [완료] 'AI 시작.bat' 으로 앱을 다시 켜세요.
echo        Claude 에게 "백업 pc-backup/%TS% 합쳐줘" 라고 하면 PC 자동 개선 내용을 합쳐 올려요.
pause
