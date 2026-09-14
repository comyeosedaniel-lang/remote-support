# REMOTE — 원격 AS 지원 도구 (AS Studio)

숨고 원격 AS용 **동의형 원격 지원 + PC 진단·복구** 도구. Python, AGPLv3 공개, 무료 배포 목표(판매 아님).

## 플랫폼 주의
대상 OS는 **Windows 전용**이다 (pywin32, WMI, win32evtlog, LibreHardwareMonitor/pythonnet, msedge --app, CREATE_NO_WINDOW).
macOS 에서는 코드 편집·정적 검사(`python -m py_compile`, `node --check`)까지만 가능하고 실행/exe 빌드는 불가.
exe 는 Windows PC 또는 GitHub Actions(`.github/workflows/build.yml`, windows-latest)로 빌드한다.

## 구조 (현재 주력 = Server/Client 2앱)
두 앱 모두 `ThreadingHTTPServer` + Edge 앱창(`msedge --app`) + JS `fetch('/api/<method>')` → Python `Api` 메서드 패턴.

| 파일 | 역할 |
|---|---|
| `studio_server.py` + `webui/server.html` | **기사 PC** (RemoteSupport-Server.exe). WebRTC 뷰어(원격 제어), 세션 녹화(PyAV MP4), QR 모바일 사진/채팅/음성통화, 원격 진단 요청, 원격 CMD/PowerShell, 파일 보내기, SQLite 고객·AS 이력 DB |
| `studio_client.py` + `webui/client.html` | **고객 PC** (RemoteSupport-Client.exe). 시작 시 WebRTC 호스트(6자리 코드) + 로컬 진단/복구/도구 UI, 연결 시 MSG_CLIENT_INFO 자동 전송 |
| `toolsdb.py` + `webui/toolsdb.html` | AS 도구 링크 모음 (SQLite, 화면에서 편집 — 하드코딩 금지). 아직 exe 미빌드 |
| `diagnostic.py` | 수집: 사양/SMART/온도(LHM)/이벤트로그/네트워크/보안/BSOD 등 |
| `analysis.py` | 규칙 기반 건강점수·원인 순위·하드웨어 고장 의심 |
| `repair.py` | 복구 명령 (DNS/IP/winsock/tcpip/sfc/dism) |
| `history.py` | 진단 이력 저장 (%LOCALAPPDATA%\PCDiagnostic\history) |
| `common.py` | 프레임 프로토콜 `[1B type][4B len][payload]`, MSG_* 번호(1–17), 타일 인코딩 |
| `webrtc_core.py` / `webrtc_app.py` | WebRTC P2P. 릴레이는 **시그널링 전용**, P2P 실패 시 폴백 없이 실패 처리. 화면 = 변경 타일 JPEG over datachannel |
| `clipboardx.py` | 클립보드 텍스트+파일(CF_HDROP) |
| `web/support.html` | 고객용 웹 대문(동의서), mylineal.com/remote/ 에 배포 |

레거시(유지만): `host.py`/`viewer.py`(초기 원격), `studio.py`(tkinter), `studio_web.py`+`webui/index.html`(분리 전 통합판), `diagnostic_gui.py`(독립 진단).

## 서버 쪽
릴레이·QR 모바일·동의서·exe 다운로드는 **별도 저장소**인 mylineal.com Cloudflare Worker(`worker.js`, Durable Object `RelaySession`, KV `SHARE`, R2 `PHOTOS`)에 있다. 이 저장소에는 없음.
릴레이 주소: 환경변수 `RELAY_URL` > `config.py`(gitignore, `config.example.py` 복사) > 빈 값.

## 빌드
- Server/Client: `pyinstaller RemoteSupport-Server.spec` / `RemoteSupport-Client.spec`
- 레거시 exe: `build.ps1` (Server/Client 는 여기에 없음)

## 알려진 함정
- `py-cpuinfo` 금지 (frozen exe 에서 fork-bomb). CPU 이름은 WMI.
- mss 는 스레드 종속 → 캡처는 전용 단일 스레드 executor.
- datachannel `open` 전에는 입력 큐를 비우지 말 것.
- `diagnostic.report_html` 템플릿은 %-포맷 → 리터럴 `%` 는 `%%`.
- winsock/tcpip 리셋은 원격 세션을 끊는다 → 원격 트리거 시 DNS flush 만.
- pywebview 는 이 환경에서 접근성 무한재귀로 멈춰서 제거함 (Edge 앱창 방식 사용).
