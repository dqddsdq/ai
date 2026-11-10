@echo off
chcp 65001 > nul
cd /d "%~dp0"
title 1인 기업 AI

rem ---- 1. 파이썬 찾기 ----
set "PY="
py -3 --version > nul 2>&1 && set "PY=py -3"
if not defined PY python --version > nul 2>&1 && set "PY=python"
if not defined PY goto NOPY
echo [1/3] 파이썬 확인:
%PY% --version

rem ---- 2. 필요한 패키지 확인, 없으면 자동 설치 ----
%PY% -c "import numpy, requests, dotenv, anthropic, openai" > nul 2>&1
if not errorlevel 1 goto PKGOK
echo [2/3] 필요한 패키지를 설치하는 중입니다. 처음 한 번만, 1~3분 걸려요...
%PY% -m pip install -r requirements.txt
if errorlevel 1 goto PIPFAIL
:PKGOK
echo [2/3] 패키지 확인 완료

rem ---- 3. Claude Code 확인 ----
where claude > nul 2>&1
if errorlevel 1 echo [안내] Claude Code - claude 명령 - 를 못 찾았어요. 화면은 열리지만 AI 답변을 받으려면 Claude Code 를 설치하고 로그인하세요.

echo [3/3] 시작합니다. 브라우저에서 http://127.0.0.1:8765 가 열려요.
echo        폰은 같은 와이파이에서 - 화면 왼쪽 아래 📱 버튼에 주소와 PIN 이 있어요.
echo        처음에 윈도우 방화벽 창이 뜨면 '허용'을 눌러 주세요.
echo        끄려면 이 창을 닫으세요. (앱이 오류로 꺼지면 10초 뒤 자동으로 다시 켜요)
echo.
set "OPENBR="
if /i "%~1"=="auto" set "OPENBR=--no-browser"

:RUN
%PY% -m engine web --phone %OPENBR%
if not errorlevel 1 goto DONE
echo.
echo [다시 켜기] 앱이 오류로 꺼졌어요. 10초 뒤 자동으로 다시 켭니다. (멈추려면 이 창을 닫으세요)
timeout /t 10 /nobreak > nul
set "OPENBR=--no-browser"
goto RUN

:DONE
echo.
echo [종료됨] 위에 빨간 오류 메시지가 있으면 이 창을 캡처해서 보여주세요.
if /i not "%~1"=="auto" pause
exit /b 0

:NOPY
echo [오류] 파이썬을 찾을 수 없어요.
echo  1. https://www.python.org/downloads/ 에서 파이썬을 설치하세요.
echo  2. 설치 첫 화면에서 "Add python.exe to PATH" 를 꼭 체크하세요.
echo  3. 설치 후 이 파일을 다시 더블클릭하세요.
pause
exit /b 1

:PIPFAIL
echo.
echo [오류] 패키지 설치에 실패했어요. 위 메시지를 캡처해서 보여주세요.
pause
exit /b 1
