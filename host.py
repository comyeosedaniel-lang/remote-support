"""
원격 지원 - 호스트 (제어를 받는 쪽 / 고객 PC)

화면을 실시간 전송하고, 기사 PC로부터 받은 마우스/키보드 입력을 실행합니다.
- 접속 코드(6자리)를 아는 사람만 연결할 수 있습니다.
- 항상 위에 뜨는 창으로 연결 상태가 보이며, '연결 끊기'로 언제든 종료할 수 있습니다.

두 가지 연결 방식:
    python host.py            # LAN (같은 네트워크) — IP + 접속 코드
    python host.py --relay    # 인터넷 (릴레이 서버 경유) — 접속 코드만
"""
import io
import json
import os
import random
import socket
import struct
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import filedialog

import mss
from PIL import Image
try:
    import dxcam
except ImportError:
    dxcam = None
try:
    import pyperclip
except ImportError:
    pyperclip = None
from pynput.mouse import Controller as MouseController, Button
from pynput.keyboard import Controller as KeyboardController, Key

from common import (
    MSG_AUTH, MSG_AUTH_OK, MSG_AUTH_FAIL, MSG_INPUT, MSG_BYE, MSG_TILES,
    MSG_CLIPBOARD, MSG_FILE_START, MSG_FILE_CHUNK, MSG_FILE_END,
    send_msg, TCPChannel, WSChannel,
)

# 디스플레이 배율(125%/150% 등)이 100%가 아니면, DPI-비인식 프로세스의 마우스 좌표(SetCursorPos)는
# 논리 픽셀로 해석되어 mss 의 실제(물리) 픽셀 캡처와 어긋난다 → 원격 클릭 위치가 밀림.
# 창 생성 전에 프로세스를 모니터별 DPI 인식으로 선언해 물리 픽셀 기준으로 맞춘다.
try:
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

TILE = 128  # 델타 전송 타일 크기(px)

PORT = 5900
FPS = 20
JPEG_QUALITY = 75
MAX_WIDTH = 1920  # 전송 전 화면을 이 폭으로 축소 (대역폭 절약)

# 인터넷 릴레이 주소. 공개 코드에 하드코딩하지 않는다:
#   환경변수 RELAY_URL  >  로컬 config.py(비공개)  >  빈 값
def _load_relay_url():
    v = os.environ.get("RELAY_URL")
    if v:
        return v
    try:
        from config import RELAY_URL as _u
        return _u
    except Exception:
        return ""


RELAY_URL = _load_relay_url()

# 뷰어가 보내는 특수키 이름 -> pynput Key
CANON_KEYS = {
    "enter": Key.enter, "backspace": Key.backspace, "tab": Key.tab,
    "esc": Key.esc, "space": Key.space,
    "shift": Key.shift, "shift_r": Key.shift_r,
    "ctrl": Key.ctrl, "ctrl_r": Key.ctrl_r,
    "alt": Key.alt, "alt_gr": Key.alt_gr, "cmd": Key.cmd,
    "up": Key.up, "down": Key.down, "left": Key.left, "right": Key.right,
    "delete": Key.delete, "home": Key.home, "end": Key.end,
    "page_up": Key.page_up, "page_down": Key.page_down,
    "insert": Key.insert, "caps_lock": Key.caps_lock,
}
for _i in range(1, 13):
    CANON_KEYS["f%d" % _i] = getattr(Key, "f%d" % _i)

BUTTONS = {"left": Button.left, "right": Button.right, "middle": Button.middle}


def encode_tiles(img, prev, quality=JPEG_QUALITY):
    """이미지를 타일로 나눠 직전과 달라진 타일만 JPEG 인코딩.
    prev 딕셔너리를 갱신하고, 변경된 [(col, row, jpeg_bytes), ...] 를 반환."""
    W, H = img.size
    cols = (W + TILE - 1) // TILE
    rows = (H + TILE - 1) // TILE
    changed = []
    for r in range(rows):
        for c in range(cols):
            box = (c * TILE, r * TILE,
                   min((c + 1) * TILE, W), min((r + 1) * TILE, H))
            tile = img.crop(box)
            raw = tile.tobytes()
            if prev.get((c, r)) != raw:
                prev[(c, r)] = raw
                tb = io.BytesIO()
                tile.save(tb, format="JPEG", quality=quality)
                changed.append((c, r, tb.getvalue()))
    return changed


def pack_tiles(W, H, changed):
    """변경 타일 목록을 MSG_TILES 페이로드 바이트로 직렬화."""
    parts = [struct.pack(">IIHH", W, H, TILE, len(changed))]
    for c, r, jp in changed:
        parts.append(struct.pack(">HHI", c, r, len(jp)))
        parts.append(jp)
    return b"".join(parts)


def get_local_ip():
    """이 PC의 LAN IP 주소를 구합니다 (실제 패킷은 보내지 않음)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


class HostApp:
    def __init__(self, mode="lan", relay_url=RELAY_URL):
        self.mode = mode                       # "lan" 또는 "relay"
        self.relay_url = relay_url
        _env_code = os.environ.get("REMOTE_CODE", "")
        self.pin = (_env_code if (len(_env_code) == 6 and _env_code.isdigit())
                    else "%06d" % random.randint(0, 999999))
        self.local_ip = get_local_ip()
        self.mouse = MouseController()
        self.keyboard = KeyboardController()
        self.channel = None
        self.running = True
        self.bridge = {}               # webrtc_app 이 send_file 함수를 넣어줌
        self._build_gui()
        self.screen_w = self.root.winfo_screenwidth()
        self.screen_h = self.root.winfo_screenheight()
        self.file_dest = None
        self.file_size = 0
        self.file_written = 0
        self.file_name = ""
        self.last_clipboard = ""
        self.peer_name = ""

    # ---------------- GUI ----------------
    def _build_gui(self):
        self.root = tk.Tk()
        self.root.title("원격 지원 - 내 PC (호스트)")
        self.root.attributes("-topmost", True)
        self.root.geometry("360x330")
        self.root.configure(bg="#1e1e2e")

        title_f = tkfont.Font(size=13, weight="bold")
        big_f = tkfont.Font(size=22, weight="bold")

        head = "인터넷 원격 대기 중" if self.mode == "relay" else "원격 지원 대기 중"
        tk.Label(self.root, text=head, font=title_f,
                 fg="#cdd6f4", bg="#1e1e2e").pack(pady=(18, 2))
        tk.Label(self.root, text="아래 정보를 기사님께 알려주세요",
                 fg="#a6adc8", bg="#1e1e2e").pack()

        info = tk.Frame(self.root, bg="#313244")
        info.pack(padx=20, pady=14, fill="x")
        if self.mode == "lan":
            tk.Label(info, text="내 IP 주소", fg="#a6adc8", bg="#313244").pack(pady=(10, 0))
            tk.Label(info, text=self.local_ip, font=big_f, fg="#89b4fa",
                     bg="#313244").pack()
            tk.Label(info, text="접속 코드", fg="#a6adc8", bg="#313244").pack(pady=(8, 0))
        else:
            tk.Label(info, text="접속 코드", fg="#a6adc8", bg="#313244").pack(pady=(12, 0))
        tk.Label(info, text=self.pin, font=big_f, fg="#a6e3a1",
                 bg="#313244").pack(pady=(0, 12))

        self.status_var = tk.StringVar(value="상태: 준비 중")
        tk.Label(self.root, textvariable=self.status_var, fg="#f9e2af",
                 bg="#1e1e2e").pack(pady=4)

        if self.mode == "relay":       # 인터넷(P2P): 기사에게 파일 보내기
            tk.Button(self.root, text="파일 보내기", command=self.send_file_action,
                      bg="#89b4fa", fg="#11111b", relief="flat",
                      padx=10, pady=4).pack(pady=(0, 4))

        tk.Button(self.root, text="연결 끊기 / 종료", command=self.shutdown,
                  bg="#f38ba8", fg="#11111b", relief="flat",
                  padx=10, pady=6).pack(pady=6)

        self.root.protocol("WM_DELETE_WINDOW", self.shutdown)

    def send_file_action(self):
        path = filedialog.askopenfilename(title="보낼 파일 선택")
        if not path:
            return
        fn = self.bridge.get("send_file")
        if fn:
            fn(path)
            self.set_status("파일 전송 중...")
        else:
            self.set_status("아직 연결 안 됨")

    def set_status(self, text):
        try:
            self.root.after(0, self.status_var.set, "상태: " + text)
        except Exception:
            pass

    # ---------------- 시작 ----------------
    def start(self):
        print("[HOST] STARTING PIN:", self.pin, flush=True)
        if self.mode == "relay":
            threading.Thread(target=self._webrtc_host_loop, daemon=True).start()
        else:
            threading.Thread(target=self._accept_loop, daemon=True).start()
        self.root.mainloop()

    # ---------------- 인터넷 (WebRTC P2P + 릴레이 폴백) ----------------
    def _webrtc_host_loop(self):
        import asyncio
        import webrtc_app
        self.set_status("대기 중 - 접속 코드 %s" % self.pin)

        async def runner():
            while self.running:
                try:
                    await webrtc_app.host_run(
                        self.relay_url, self.pin, self._apply_input,
                        self.set_status, lambda: self.running,
                        bridge=self.bridge)
                except Exception:
                    self.set_status("연결 오류, 재시도...")
                if self.running:
                    self.set_status("대기 중 - 접속 코드 %s" % self.pin)
                    await asyncio.sleep(1)

        try:
            asyncio.run(runner())
        except Exception:
            pass

    # ---------------- LAN (raw TCP) ----------------
    def _accept_loop(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("0.0.0.0", PORT))
        except OSError as e:
            self.set_status("포트 %d 사용 불가: %s" % (PORT, e))
            return
        srv.listen(1)
        srv.settimeout(1.0)
        self.set_status("연결 대기 중 (LAN)")
        while self.running:
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if self.channel is not None:
                try:
                    send_msg(conn, MSG_AUTH_FAIL)
                    conn.close()
                except Exception:
                    pass
                continue
            try:
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            except Exception:
                pass
            ch = TCPChannel(conn)
            threading.Thread(target=self._serve_channel, args=(ch, addr[0]),
                             daemon=True).start()
        srv.close()

    # ---------------- 인터넷 (WebSocket 릴레이) ----------------
    def _relay_loop(self):
        try:
            from websockets.sync.client import connect as ws_connect
        except Exception:
            self.set_status("websockets 미설치: pip install websockets")
            return
        full = "%s?code=%s&role=host" % (self.relay_url, self.pin)
        while self.running:
            try:
                self.set_status("릴레이 접속 중...")
                with ws_connect(full, max_size=8 * 1024 * 1024,
                                open_timeout=10) as ws:
                    self.set_status("대기 중 - 접속 코드 %s" % self.pin)
                    self._serve_channel(WSChannel(ws), "인터넷")
            except Exception:
                if not self.running:
                    break
                self.set_status("릴레이 재연결 대기...")
                time.sleep(2)

    # ---------------- 세션 처리 (전송 방식 공통) ----------------
    def _serve_channel(self, ch, peer_name):
        # 1) 인증
        try:
            mtype, payload = ch.recv()
            if (mtype != MSG_AUTH or payload is None
                    or payload.decode("utf-8", "ignore") != self.pin):
                ch.send(MSG_AUTH_FAIL)
                ch.close()
                self.set_status("접속 코드 불일치로 거부됨")
                return
            ch.send(MSG_AUTH_OK)
        except Exception:
            ch.close()
            return

        # 2) 화면 전송 시작 + 입력 수신
        self.channel = ch
        self.peer_name = peer_name
        self.set_status("연결됨 - %s (원격 제어 중)" % peer_name)
        
        # 클립보드 초깃값 설정 및 동기화 스레드 구동
        if pyperclip:
            try:
                self.last_clipboard = pyperclip.paste()
            except Exception:
                self.last_clipboard = ""
            threading.Thread(target=self._clipboard_loop, args=(ch,), daemon=True).start()

        threading.Thread(target=self._stream_loop, args=(ch,),
                         daemon=True).start()
        try:
            while self.running and self.channel is ch:
                mtype, payload = ch.recv()
                if mtype is None:
                    break
                if mtype == MSG_INPUT:
                    try:
                        self._apply_input(json.loads(payload.decode("utf-8")))
                    except Exception:
                        pass
                elif mtype == MSG_CLIPBOARD:
                    try:
                        text = payload.decode("utf-8", "ignore")
                        self.last_clipboard = text
                        if pyperclip:
                            pyperclip.copy(text)
                    except Exception:
                        pass
                elif mtype == MSG_FILE_START:
                    try:
                        name_len = struct.unpack(">I", payload[:4])[0]
                        self.file_name = payload[4:4+name_len].decode("utf-8", "ignore")
                        self.file_size = struct.unpack(">Q", payload[4+name_len:12+name_len])[0]
                        self.file_written = 0
                        os.makedirs("downloads", exist_ok=True)
                        dest_path = os.path.join("downloads", self.file_name)
                        self.file_dest = open(dest_path, "wb")
                        self.set_status(f"파일 수신 시작: {self.file_name}")
                    except Exception:
                        pass
                elif mtype == MSG_FILE_CHUNK:
                    try:
                        if self.file_dest:
                            self.file_dest.write(payload)
                            self.file_written += len(payload)
                            pct = int(self.file_written / self.file_size * 100) if self.file_size else 0
                            self.set_status(f"파일 수신 중... {pct}% ({self.file_name})")
                    except Exception:
                        pass
                elif mtype == MSG_FILE_END:
                    try:
                        if self.file_dest:
                            self.file_dest.close()
                            self.file_dest = None
                            self.set_status(f"수신 완료: downloads/{self.file_name}")
                            
                            def restore():
                                time.sleep(3)
                                if self.channel is ch:
                                    self.set_status(f"연결됨 - {self.peer_name} (원격 제어 중)")
                            threading.Thread(target=restore, daemon=True).start()
                    except Exception:
                        pass
                elif mtype == MSG_BYE:
                    break
        finally:
            ch.close()
            if self.file_dest:
                try:
                    self.file_dest.close()
                except Exception:
                    pass
                self.file_dest = None
            if self.channel is ch:
                self.channel = None
            if self.running:
                self.set_status("연결 대기 중")

    def _clipboard_loop(self, ch):
        if not pyperclip:
            return
        while self.running and self.channel is ch:
            try:
                curr = pyperclip.paste()
                if curr and curr != self.last_clipboard:
                    self.last_clipboard = curr
                    ch.send(MSG_CLIPBOARD, curr.encode("utf-8", "ignore"))
            except Exception:
                pass
            time.sleep(1.0)

    def _stream_loop(self, ch):
        """화면을 타일로 나눠 바뀐 칸만 전송(델타). 첫 프레임은 전체가 전송됨."""
        use_dxcam = "--use-dxcam" in sys.argv
        camera = None
        if use_dxcam and dxcam is not None:
            try:
                camera = dxcam.create(device_idx=0, output_idx=0)
            except Exception:
                camera = None

        if camera is not None:
            try:
                self.screen_w = camera.width
                self.screen_h = camera.height
                interval = 1.0 / FPS
                prev = {}
                while self.running and self.channel is ch:
                    t0 = time.time()
                    frame = camera.grab()
                    if frame is None:
                        time.sleep(0.005)
                        if time.time() - t0 >= interval:
                            continue
                        continue
                    
                    img = Image.fromarray(frame)
                    if img.width > MAX_WIDTH:
                        ratio = MAX_WIDTH / img.width
                        img = img.resize((MAX_WIDTH, int(img.height * ratio)), Image.BOX)
                    W, H = img.size
                    cols = (W + TILE - 1) // TILE
                    rows = (H + TILE - 1) // TILE
                    total_tiles = cols * rows
                    
                    changed = encode_tiles(img, prev)
                    if changed:
                        try:
                            # 하이브리드 전송: 변경 영역이 30% 초과 시 전체 프레임 전송으로 전환 (CPU 병목 방지)
                            if len(changed) > total_tiles * 0.3:
                                tb = io.BytesIO()
                                img.save(tb, format="JPEG", quality=JPEG_QUALITY)
                                payload = struct.pack(">II", W, H) + tb.getvalue()
                                ch.send(MSG_FRAME, payload)
                                # 델타 갱신
                                for r in range(rows):
                                    for c in range(cols):
                                        box = (c * TILE, r * TILE, min((c + 1) * TILE, W), min((r + 1) * TILE, H))
                                        prev[(c, r)] = img.crop(box).tobytes()
                            else:
                                ch.send(MSG_TILES, pack_tiles(W, H, changed))
                        except Exception:
                            break
                    dt = time.time() - t0
                    if dt < interval:
                        time.sleep(interval - dt)
                return
            except Exception:
                camera = None

        # 기본 및 Fallback: mss 기반 캡처
        try:
            with mss.MSS() as sct:
                monitor = sct.monitors[1]  # 주 모니터
                self.screen_w = monitor["width"]
                self.screen_h = monitor["height"]
                interval = 1.0 / FPS
                prev = {}  # (col, row) -> 직전 타일의 raw 바이트
                while self.running and self.channel is ch:
                    t0 = time.time()
                    shot = sct.grab(monitor)
                    img = Image.frombytes("RGB", shot.size, shot.rgb)
                    if img.width > MAX_WIDTH:
                        ratio = MAX_WIDTH / img.width
                        img = img.resize((MAX_WIDTH, int(img.height * ratio)), Image.BOX)
                    W, H = img.size
                    cols = (W + TILE - 1) // TILE
                    rows = (H + TILE - 1) // TILE
                    total_tiles = cols * rows
                    
                    changed = encode_tiles(img, prev)
                    if changed:
                        try:
                            # 하이브리드 전송: 변경 영역이 30% 초과 시 전체 프레임 전송으로 전환 (CPU 병목 방지)
                            if len(changed) > total_tiles * 0.3:
                                tb = io.BytesIO()
                                img.save(tb, format="JPEG", quality=JPEG_QUALITY)
                                payload = struct.pack(">II", W, H) + tb.getvalue()
                                ch.send(MSG_FRAME, payload)
                                # 델타 갱신
                                for r in range(rows):
                                    for c in range(cols):
                                        box = (c * TILE, r * TILE, min((c + 1) * TILE, W), min((r + 1) * TILE, H))
                                        prev[(c, r)] = img.crop(box).tobytes()
                            else:
                                ch.send(MSG_TILES, pack_tiles(W, H, changed))
                        except Exception:
                            break
                    dt = time.time() - t0
                    if dt < interval:
                        time.sleep(interval - dt)
        except Exception:
            pass

    # ---------------- 입력 실행 ----------------
    def _apply_input(self, ev):
        t = ev.get("t")
        if t == "move":
            self.mouse.position = self._to_screen(ev["x"], ev["y"])
        elif t == "button":
            self.mouse.position = self._to_screen(ev["x"], ev["y"])
            btn = BUTTONS.get(ev.get("button"), Button.left)
            if ev.get("pressed"):
                self.mouse.press(btn)
            else:
                self.mouse.release(btn)
        elif t == "scroll":
            self.mouse.scroll(0, ev.get("dy", 0))
        elif t == "type":
            self.keyboard.type(ev.get("text", ""))
        elif t == "key":
            k = CANON_KEYS.get(ev.get("key"), ev.get("key"))
            try:
                if ev.get("action") == "down":
                    self.keyboard.press(k)
                else:
                    self.keyboard.release(k)
            except Exception:
                pass

    def _to_screen(self, nx, ny):
        x = int(max(0.0, min(1.0, nx)) * self.screen_w)
        y = int(max(0.0, min(1.0, ny)) * self.screen_h)
        return (x, y)

    def shutdown(self):
        self.running = False
        if self.channel is not None:
            self.channel.close()
            self.channel = None
        try:
            self.root.destroy()
        except Exception:
            pass


def _choose_mode():
    """시작 시 연결 방식(인터넷/LAN)을 고르는 작은 창. (mode, url) 반환."""
    win = tk.Tk()
    win.title("원격 지원 - 시작")
    win.geometry("300x210")
    win.configure(bg="#1e1e2e")
    tk.Label(win, text="원격 지원 시작", font=tkfont.Font(size=13, weight="bold"),
             fg="#cdd6f4", bg="#1e1e2e").pack(pady=(20, 6))
    tk.Label(win, text="연결 방식을 선택하세요", fg="#a6adc8",
             bg="#1e1e2e").pack()
    mode = tk.StringVar(value="relay")
    box = tk.Frame(win, bg="#1e1e2e")
    box.pack(pady=12)
    tk.Radiobutton(box, text="인터넷 (기사님이 원격 접속)", variable=mode,
                   value="relay", fg="#cdd6f4", bg="#1e1e2e",
                   selectcolor="#313244", activebackground="#1e1e2e",
                   anchor="w").pack(fill="x")
    tk.Radiobutton(box, text="LAN (같은 네트워크)", variable=mode,
                   value="lan", fg="#cdd6f4", bg="#1e1e2e",
                   selectcolor="#313244", activebackground="#1e1e2e",
                   anchor="w").pack(fill="x")
    result = {}

    def go():
        result["mode"] = mode.get()
        win.destroy()

    tk.Button(win, text="시작", command=go, bg="#89b4fa", fg="#11111b",
              relief="flat", padx=20, pady=6).pack(pady=8)
    win.mainloop()
    return result.get("mode"), RELAY_URL


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--relay" in argv:
        mode = "relay"
        rest = [a for a in argv if a != "--relay"]
        url = rest[0] if rest else RELAY_URL
    elif "--lan" in argv:
        mode, url = "lan", RELAY_URL
    else:
        mode, url = _choose_mode()   # 더블클릭 실행 시 GUI 로 선택 (기본: 인터넷)
    if mode:
        HostApp(mode=mode, relay_url=url).start()
