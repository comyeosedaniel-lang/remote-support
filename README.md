# 원격 지원 프로그램

접속 코드 기반 **동의형(consent-based)** 화면 공유 + 원격 제어 도구.
LAN(같은 네트워크)과 인터넷(WebRTC P2P) 모두 지원합니다.

- **host.py** — 제어를 *받는* 쪽 (고객 PC). 화면을 전송하고 입력을 실행.
- **viewer.py** — 제어를 *하는* 쪽 (기사 PC). 화면을 보고 마우스·키보드로 조작.
- **common.py** — 공유 통신 프로토콜(타일 델타 등).
- **webrtc_core.py / webrtc_app.py** — 인터넷 P2P(WebRTC) 연결과 화면·입력 채널.

## 설치

두 PC 모두 Python 3.9 이상 설치 후:

```bash
pip install -r requirements.txt
```

> `tkinter` 는 Windows용 Python에 기본 포함되어 있습니다.

## 사용법 (LAN)

두 PC가 **같은 공유기/네트워크**에 있어야 합니다.

**고객 PC (제어받는 쪽)**
```bash
python host.py --lan
```
창에 표시되는 **IP 주소**와 **접속 코드(6자리)** 를 기사에게 알려줍니다.
(Windows 방화벽이 처음 뜨면 **액세스 허용**을 눌러야 합니다.)

**기사 PC (제어하는 쪽)**
```bash
python viewer.py               # 접속 창에서 'LAN' 선택 후 IP·코드 입력
# 또는 바로:
python viewer.py 192.168.0.15 123456
```

## 사용법 (인터넷 — WebRTC P2P)

서로 다른 네트워크에서 접속할 때. IP 없이 **접속 코드만** 공유합니다.
가능하면 **P2P로 직접 연결**(선명한 델타 타일을 데이터채널로 전송)하고,
직접 연결이 안 되는 환경(대칭 NAT 등)에서는 **릴레이 서버로 자동 폴백**합니다.

> 먼저 아래 **릴레이 설정**이 되어 있어야 합니다.

**고객 PC**
```bash
python host.py --relay
```
표시되는 **접속 코드(6자리)** 를 기사에게 알려줍니다.

**기사 PC**
```bash
python viewer.py                 # 접속 창에서 '인터넷' 선택 후 코드 입력
# 또는:
python viewer.py --relay 123456
```

## 릴레이 설정

인터넷 모드는 **시그널링 + 폴백**용 릴레이(WebSocket) 서버가 필요합니다.
`config.example.py` 를 `config.py` 로 복사하고 본인 릴레이 주소를 넣으세요:

```python
# config.py  (공개 저장소에 올리지 않음 — .gitignore 처리됨)
RELAY_URL = "wss://YOUR-DOMAIN/relay"
```

- 환경변수 `RELAY_URL` 로도 지정할 수 있습니다 (config.py보다 우선).
- 릴레이는 같은 6자리 code 의 host/viewer 를 짝지어 바이트를 중계하면 됩니다.
  Cloudflare Workers + Durable Object(무료 플랜) 등으로 직접 운영하세요.
  (P2P 시그널링 메시지와, P2P 실패 시 화면 타일 폴백이 이 릴레이를 통해 흐릅니다.)

## 지원 기능

- 실시간 화면 전송 — **변경영역만 전송(델타 타일, JPEG)**: 128px 타일로 나눠 바뀐 칸만 → 대역폭 절약, 선명
- **인터넷은 WebRTC P2P**(데이터채널) 우선, 실패 시 릴레이 폴백
- 마우스 이동 / 좌·중·우 클릭 / 휠 스크롤
- 키보드 입력 (한글·영문 타이핑, Ctrl+C/V 등 단축키, 방향키·기능키)
- 접속 코드(PIN) 인증, 동시 1명 연결
- **LAN 직접 연결** + **인터넷 P2P** 두 방식
- 자동 재연결, 고객 측 상시 표시 창 + **연결 끊기** 버튼

## 빌드 (단일 실행파일)

```powershell
./build.ps1   # dist\RemoteSupport-Host.exe, RemoteSupport-Viewer.exe 생성
```

## 안전 / 동의 원칙

이 프로그램은 **고객이 접속 코드를 직접 알려주고, 화면에 항상 상태 창이 떠 있는**
동의 기반 원격 지원용입니다. 몰래 실행되거나 백신을 우회하는 형태로 만들지 않습니다.
