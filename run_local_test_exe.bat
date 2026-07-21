@echo off
chcp 65001 > nul
echo [원격 지원] 빌드된 실행 파일로 로컬 LAN 테스트를 시작합니다...
start "원격 지원 - 호스트" dist\RemoteSupport-Host.exe --lan
start "원격 지원 - 뷰어" dist\RemoteSupport-Viewer.exe
pause
