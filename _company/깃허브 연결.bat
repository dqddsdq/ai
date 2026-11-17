@echo off
chcp 65001 > nul
cd /d "%~dp0.."
title GitHub 연결
rem 이 PC 의 ai 폴더를 GitHub 저장소(dqddsdq/ai)와 연결합니다. 코드만 동기화 — 키·학습 기록·개인 자료는 안 올라가요.

where git > nul 2>&1
if errorlevel 1 (
  echo [필요] Git 이 없어요. 아래 중 하나로 설치한 뒤 이 파일을 다시 실행하세요.
  echo   1. PowerShell 에서:  winget install --id Git.Git -e
  echo   2. 또는 https://git-scm.com/download/win 에서 받아 설치 ^(옵션은 기본값 그대로^)
  pause
  exit /b 1
)

if not exist ".git" git init -q
git remote get-url sync > nul 2>&1
if errorlevel 1 git remote add sync https://github.com/dqddsdq/ai.git
git config user.name > nul 2>&1 || git config user.name "CompanyAI PC"
git config user.email > nul 2>&1 || git config user.email "companyai-pc@users.noreply.github.com"
git config core.autocrlf true

echo [1/3] GitHub 에서 최신 코드 정보 받는 중... (처음이면 브라우저로 GitHub 로그인 창이 떠요)
git fetch sync claude/build-ai-qnubm7
if errorlevel 1 (
  echo [오류] 받기 실패. 인터넷과 GitHub 로그인을 확인하세요.
  pause
  exit /b 1
)

echo [2/3] 이 폴더를 동기화 브랜치에 연결 (지금 있는 파일은 하나도 안 지워요)
git symbolic-ref HEAD refs/heads/claude/build-ai-qnubm7
git reset -q --mixed sync/claude/build-ai-qnubm7
git branch -q --set-upstream-to=sync/claude/build-ai-qnubm7 2> nul

echo [3/3] PC 코드를 GitHub 에 올리기 (코드 폴더만)
cd _company
set "PY=python"
py -3 --version > nul 2>&1 && set "PY=py -3"
%PY% -c "from engine import gitsync as g; r = g.push('PC 처음 연결'); print(r['message'])"

echo.
echo [완료] 이제 앱이 1시간마다, 그리고 자동 개선이 적용될 때마다 GitHub 와 자동으로 맞춰요.
echo        🛠️ 개선 화면의 'GitHub 동기화' 버튼으로 바로 할 수도 있어요.
pause
