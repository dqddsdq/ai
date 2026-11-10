@echo off
chcp 65001 > nul
powershell -NoProfile -Command "Remove-Item -ErrorAction SilentlyContinue ([Environment]::GetFolderPath('Startup') + '\CompanyAI.lnk')"
echo [완료] 자동 실행을 껐어요. 이제 PC 를 켜도 1인 기업 AI 가 자동으로 켜지지 않아요.
pause
