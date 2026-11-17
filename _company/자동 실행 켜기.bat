@echo off
chcp 65001 > nul
cd /d "%~dp0"
rem 윈도우가 켜지면 'AI 시작.bat' 이 자동으로 (작게) 실행되도록 시작 프로그램에 바로가기를 만듭니다.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup') + '\CompanyAI.lnk'); $s.TargetPath='%~dp0autostart.cmd'; $s.WorkingDirectory='%~dp0'; $s.WindowStyle=7; $s.Save()"
if errorlevel 1 (
  echo [오류] 바로가기를 못 만들었어요. 이 창을 캡처해서 보여주세요.
) else (
  echo [완료] 이제 PC 를 켜면 1인 기업 AI 가 자동으로 켜져요. (작업 표시줄에 작게)
  echo        끄려면 '자동 실행 끄기.bat' 을 실행하세요.
  echo.
  echo [참고] 앱이 켜져 있는 동안에는 PC 가 절전 모드로 들어가지 않게 앱이 막아요. (화면은 꺼져도 돼요)
)
pause
