@echo off
chcp 65001 > nul
echo [원격 지원] 로컬 LAN 테스트를 시작합니다...
echo 1. 고객 PC(Host) 프로그램을 실행합니다...
start "원격 지원 - 호스트" python host.py --lan

echo 2. 기사 PC(Viewer) 프로그램을 실행합니다...
start "원격 지원 - 뷰어" python viewer.py

echo.
echo 완료! 호스트 창에 표시되는 IP 주소와 6자리 접속 코드를 뷰어 창에 입력하여 연결하세요.
pause
