@echo off
chcp 65001 > nul
cd /d "%~dp0"
title 1인 기업 AI - 진단
set "PY="
py -3 --version > nul 2>&1 && set "PY=py -3"
if not defined PY python --version > nul 2>&1 && set "PY=python"
if not defined PY goto NOPY
%PY% -m engine doctor
echo.
pause
exit /b 0
:NOPY
echo [오류] 파이썬을 찾을 수 없어요. https://www.python.org/downloads/ 에서 설치하고 "Add python.exe to PATH" 를 체크하세요.
pause
exit /b 1
