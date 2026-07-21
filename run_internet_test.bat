@echo off
chcp 65001 > nul
echo [원격 지원] 인터넷 릴레이 테스트를 시작합니다...
echo 1. 고객 PC(Host) 프로그램을 실행합니다 (인터넷 릴레이 모드)...
start "원격 지원 - 호스트" python host.py --relay

echo 2. 기사 PC(Viewer) 프로그램을 실행합니다...
start "원격 지원 - 뷰어" python viewer.py

echo.
echo 완료! 호스트 창에 표시되는 6자리 접속 코드를 뷰어 창에서 '인터넷' 선택 후 입력하여 연결하세요.
pause
