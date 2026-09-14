"""
원격 지원 - 뷰어 (제어하는 쪽 / 기사 PC)

고객 화면을 보고 마우스/키보드로 원격 제어합니다.

두 가지 연결 방식:
    python viewer.py                      # 접속 창 (LAN / 인터넷 선택)
    python viewer.py <IP> <접속코드>       # LAN 바로 연결
    python viewer.py --relay <접속코드>    # 인터넷(릴레이 서버) 바로 연결
"""
import io
import json
import os
import queue
import socket
import struct
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, filedialog
try:
    import pyperclip
except ImportError:
    pyperclip = None

from PIL import Image, ImageTk

from common import (
    MSG_AUTH, MSG_AUTH_OK, MSG_FRAME, MSG_INPUT, MSG_BYE, MSG_TILES,
    MSG_CLIPBOARD, MSG_FILE_START, MSG_FILE_CHUNK, MSG_FILE_END,
    MSG_DIAG_REQUEST,
    TCPChannel, WSChannel,
)

PORT = 5900


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

# Tkinter keysym -> 특수키 이름 (host 의 CANON_KEYS 와 짝을 이룸)
SPECIAL_MAP = {
    "Return": "enter", "KP_Enter": "enter",
    "BackSpace": "backspace", "Tab": "tab", "Escape": "esc",
    "space": "space",
    "Shift_L": "shift", "Shift_R": "shift_r",
    "Control_L": "ctrl", "Control_R": "ctrl_r",
    "Alt_L": "alt", "Alt_R": "alt_gr",
    "Super_L": "cmd", "Super_R": "cmd", "Win_L": "cmd", "Win_R": "cmd",
    "Up": "up", "Down": "down", "Left": "left", "Right": "right",
    "Delete": "delete", "Home": "home", "End": "end",
    "Prior": "page_up", "Next": "page_down",
    "Insert": "insert", "Caps_Lock": "caps_lock",
}
for _i in range(1, 13):
    SPECIAL_MAP["F%d" % _i] = "f%d" % _i

MODIFIERS = {"ctrl", "ctrl_r", "alt", "alt_gr", "cmd"}

SYMBOL_MAP = {
    "minus": "-", "equal": "=", "bracketleft": "[", "bracketright": "]",
    "backslash": "\\", "semicolon": ";", "apostrophe": "'", "comma": ",",
    "period": ".", "slash": "/", "grave": "`",
}


# ---------------------------------------------------------------------------
# 연결 + 인증 (채널 반환) — 실패 시 예외
# ---------------------------------------------------------------------------
def _auth(ch, code):
    ch.send(MSG_AUTH, code)
    mtype, _ = ch.recv()
    if mtype != MSG_AUTH_OK:
        ch.close()
        raise ConnectionError("접속 코드가 틀리거나 연결이 거부되었습니다.")
    return ch


def connect_lan(ip, code):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(8)
    sock.connect((ip, PORT))
    sock.settimeout(None)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except Exception:
        pass
    return _auth(TCPChannel(sock), code)


def connect_relay(code, url=RELAY_URL):
    from websockets.sync.client import connect as ws_connect
    full = "%s?code=%s&role=viewer" % (url, code)
    ws = ws_connect(full, max_size=8 * 1024 * 1024, open_timeout=10)
    return _auth(WSChannel(ws), code)


class ViewerApp:
    def __init__(self, channel=None, reconnect=None,
                 webrtc=False, relay_url=None, code=None):
        self.channel = channel
        self.reconnect = reconnect     # () -> 새 인증된 채널 (자동 재연결용)
        self.webrtc = webrtc           # 인터넷(P2P) 모드 여부
        self.relay_url = relay_url
        self.code = code
        self.out_q = queue.Queue() if webrtc else None   # (msg_type, payload) 송신 큐
        self.bridge = {}               # webrtc_app 이 send_file 함수를 넣어줌
        self.running = True
        self._closing = False          # 사용자가 직접 종료한 경우
        self.frame_w = 0
        self.frame_h = 0
        self.screen_img = None         # 합성된 전체 화면 (PIL Image)
        self._img_lock = threading.Lock()
        self._dirty = False
        self._status = ""              # 화면 상단 오버레이 문구
        self.held_specials = set()
        self.held_base = {}            # keysym -> 전송한 base 키
        self.sending_file = False      # 파일 전송 중 플래그
        self.last_clipboard = ""
        self._build_gui()

    def _build_gui(self):
        self.root = tk.Tk()
        self.root.title("원격 지원 - 원격 제어 중 (뷰어)")
        
        # 상단 툴바 프레임 신설
        toolbar = tk.Frame(self.root, bg="#1e1e2e", height=40)
        toolbar.pack(fill="x", side="top")
        
        # 파일 전송 버튼
        self.btn_send_file = tk.Button(
            toolbar, text="파일 전송", command=self.send_file_action,
            bg="#89b4fa", fg="#11111b", relief="flat", padx=10, pady=2
        )
        self.btn_send_file.pack(side="left", padx=10, pady=5)

        # PC 진단 버튼 (원격 진단 요청)
        self.btn_diag = tk.Button(
            toolbar, text="PC 진단", command=self.request_diagnostic,
            bg="#cba6f7", fg="#11111b", relief="flat", padx=10, pady=2
        )
        self.btn_diag.pack(side="left", padx=(0, 6), pady=5)
        
        # 전송 진행률 레이블
        self.lbl_file_status = tk.Label(
            toolbar, text="상태: 대기 중", fg="#a6adc8", bg="#1e1e2e"
        )
        self.lbl_file_status.pack(side="left", padx=10)

        # 캔버스 패킹 (툴바 아래에 위치)
        self.canvas = tk.Canvas(self.root, bg="black", highlightthickness=0,
                                width=960, height=540, cursor="dotbox")
        self.canvas.pack(side="bottom")
        self.canvas.focus_set()
        self._img_id = None
        self._photo = None
        self._status_id = None

        c = self.canvas
        c.bind("<Motion>", self.on_motion)
        c.bind("<ButtonPress-1>", lambda e: self.on_button(e, "left", True))
        c.bind("<ButtonRelease-1>", lambda e: self.on_button(e, "left", False))
        c.bind("<ButtonPress-2>", lambda e: self.on_button(e, "middle", True))
        c.bind("<ButtonRelease-2>", lambda e: self.on_button(e, "middle", False))
        c.bind("<ButtonPress-3>", lambda e: self.on_button(e, "right", True))
        c.bind("<ButtonRelease-3>", lambda e: self.on_button(e, "right", False))
        c.bind("<MouseWheel>", self.on_wheel)
        self.root.bind("<KeyPress>", self.on_key_press)
        self.root.bind("<KeyRelease>", self.on_key_release)
        self.root.protocol("WM_DELETE_WINDOW", self.shutdown)

    def start(self):
        if self.webrtc:
            # 인터넷(P2P): 화면·입력·클립보드·파일 모두 데이터채널로 (webrtc_app 이 처리)
            try:
                self.lbl_file_status.config(text="상태: 인터넷(P2P)")
            except Exception:
                pass
            threading.Thread(target=self._webrtc_viewer_loop, daemon=True).start()
        else:
            if pyperclip:
                try:
                    self.last_clipboard = pyperclip.paste()
                except Exception:
                    self.last_clipboard = ""
                threading.Thread(target=self._clipboard_loop, args=(self.channel,), daemon=True).start()
            threading.Thread(target=self._recv_loop, daemon=True).start()
        self._render()
        self.root.mainloop()

    # ---------------- 인터넷 (WebRTC P2P + 릴레이 폴백) ----------------
    def _webrtc_viewer_loop(self):
        import asyncio
        import webrtc_app

        async def runner():
            while self.running:
                try:
                    await webrtc_app.viewer_run(
                        self.relay_url, self.code, self._on_webrtc_frame,
                        self.out_q, self._set_status, lambda: self.running,
                        bridge=self.bridge)
                except Exception:
                    pass
                if self.running and not self._closing:
                    self._set_status("재연결 중...")
                    await asyncio.sleep(1)

        try:
            asyncio.run(runner())
        except Exception:
            pass
        self.running = False

    def _on_webrtc_frame(self, pil_img):
        with self._img_lock:
            self.screen_img = pil_img
            self.frame_w, self.frame_h = pil_img.size
            self._dirty = True

    # ---------------- 수신 ----------------
    def _recv_loop(self):
        while self.running:
            try:
                mtype, payload = self.channel.recv()
            except Exception:
                mtype, payload = None, None
            if mtype is None:                       # 연결 끊김
                if self._closing or not self.reconnect:
                    break
                if not self._reconnect_now():       # 자동 재연결 시도
                    break
                continue
            if mtype == MSG_TILES:
                self._apply_tiles(payload)
            elif mtype == MSG_FRAME:
                self._apply_frame(payload)
            elif mtype == MSG_CLIPBOARD:
                try:
                    text = payload.decode("utf-8", "ignore")
                    self.last_clipboard = text
                    if pyperclip:
                        pyperclip.copy(text)
                except Exception:
                    pass
            elif mtype == MSG_BYE:
                break
        self.running = False

    # ---------------- 원격 PC 진단 요청 ----------------
    def request_diagnostic(self):
        if self.webrtc and self.out_q is not None:
            self.out_q.put((MSG_DIAG_REQUEST, b""))
            self.lbl_file_status.config(text="상태: 진단 요청함 — 리포트가 곧 열립니다")
        else:
            self.lbl_file_status.config(text="상태: 인터넷(P2P) 모드에서만 가능")

    # ---------------- 파일 전송 ----------------
    def send_file_action(self):
        path = filedialog.askopenfilename(title="전송할 파일 선택")
        if not path:
            return
        if self.webrtc:          # 인터넷(P2P): 데이터채널로 전송 (webrtc_app bridge)
            fn = self.bridge.get("send_file")
            if fn:
                fn(path)
                self.lbl_file_status.config(text="상태: 파일 전송 중...")
            else:
                self.lbl_file_status.config(text="상태: 아직 연결 안 됨")
            return
        if self.sending_file:
            return
        threading.Thread(target=self._send_file, args=(path,), daemon=True).start()

    def _send_file(self, path):
        self.sending_file = True
        self.root.after(0, lambda: self.btn_send_file.config(state="disabled"))
        CHUNK = 65536  # 64KB
        try:
            file_name = os.path.basename(path).encode("utf-8")
            file_size = os.path.getsize(path)
            # MSG_FILE_START : [4B 파일명길이][파일명][8B 파일크기]
            header = struct.pack(">I", len(file_name)) + file_name + struct.pack(">Q", file_size)
            self.channel.send(MSG_FILE_START, header)
            sent = 0
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(CHUNK)
                    if not chunk:
                        break
                    self.channel.send(MSG_FILE_CHUNK, chunk)
                    sent += len(chunk)
                    pct = int(sent / file_size * 100) if file_size else 100
                    self.root.after(0, lambda p=pct: self.lbl_file_status.config(
                        text=f"전송 중... {p}%"))
            self.channel.send(MSG_FILE_END)
            self.root.after(0, lambda: self.lbl_file_status.config(text="전송 완료 ✓"))
        except Exception as e:
            self.root.after(0, lambda: self.lbl_file_status.config(text=f"전송 실패: {e}"))
        finally:
            self.sending_file = False
            self.root.after(0, lambda: self.btn_send_file.config(state="normal"))

    # ---------------- 클립보드 동기화 ----------------
    def _clipboard_loop(self, ch):
        if not pyperclip:
            return
        while self.running:
            try:
                curr = pyperclip.paste()
                if curr and curr != self.last_clipboard:
                    self.last_clipboard = curr
                    self.channel.send(MSG_CLIPBOARD, curr.encode("utf-8", "ignore"))
            except Exception:
                pass
            time.sleep(1.0)


    def _reconnect_now(self):
        """끊긴 연결을 같은 코드로 자동 재접속. 성공 True."""
        self._set_status("재연결 중...")
        for _ in range(30):                         # 최대 ~60초
            if self._closing:
                return False
            try:
                self.channel = self.reconnect()
                self._set_status("")
                return True
            except Exception:
                time.sleep(2)
        self._set_status("재연결 실패")
        return False

    def _apply_tiles(self, payload):
        try:
            w, h, tile, count = struct.unpack(">IIHH", payload[:12])
        except Exception:
            return
        off = 12
        with self._img_lock:
            if self.screen_img is None or self.screen_img.size != (w, h):
                self.screen_img = Image.new("RGB", (w, h))
            for _ in range(count):
                if off + 8 > len(payload):
                    break
                c, r, ln = struct.unpack(">HHI", payload[off:off + 8])
                off += 8
                jp = payload[off:off + ln]
                off += ln
                try:
                    self.screen_img.paste(Image.open(io.BytesIO(jp)),
                                          (c * tile, r * tile))
                except Exception:
                    pass
            self.frame_w, self.frame_h = w, h
            self._dirty = True

    def _apply_frame(self, payload):
        try:
            w, h = struct.unpack(">II", payload[:8])
            img = Image.open(io.BytesIO(payload[8:]))
            img.load()
        except Exception:
            return
        with self._img_lock:
            self.screen_img = img.convert("RGB")
            self.frame_w, self.frame_h = w, h
            self._dirty = True

    def _set_status(self, text):
        self._status = text

    def _render(self):
        if not self.running:
            try:
                self.root.destroy()
            except Exception:
                pass
            return
        with self._img_lock:
            if self._dirty and self.screen_img is not None:
                self._photo = ImageTk.PhotoImage(self.screen_img)
                self._dirty = False
                if self._img_id is None:
                    self.canvas.config(width=self.frame_w, height=self.frame_h)
                    self._img_id = self.canvas.create_image(
                        0, 0, anchor="nw", image=self._photo)
                else:
                    self.canvas.itemconfig(self._img_id, image=self._photo)
        self._draw_status()
        self.root.after(15, self._render)

    def _draw_status(self):
        if self._status:
            if self._status_id is None:
                self._status_id = self.canvas.create_text(
                    12, 12, anchor="nw", fill="#f9e2af",
                    font=("", 14, "bold"), text=self._status)
            else:
                self.canvas.itemconfig(self._status_id, text=self._status)
            self.canvas.tag_raise(self._status_id)
        elif self._status_id is not None:
            self.canvas.delete(self._status_id)
            self._status_id = None

    # ---------------- 전송 도우미 ----------------
    def _send_input(self, obj):
        # 인터넷(P2P) 모드는 (타입, 페이로드) 로 송신 큐에 적재
        if self.webrtc:
            if self.out_q is not None:
                self.out_q.put((MSG_INPUT, json.dumps(obj).encode("utf-8")))
            return
        # 전송 실패는 무시 — 끊김은 수신 루프가 감지해 자동 재연결한다.
        try:
            self.channel.send(MSG_INPUT, json.dumps(obj))
        except Exception:
            pass

    def _norm(self, x, y):
        if self.frame_w and self.frame_h:
            return (x / self.frame_w, y / self.frame_h)
        return (0.0, 0.0)

    # ---------------- 마우스 ----------------
    def on_motion(self, e):
        nx, ny = self._norm(e.x, e.y)
        self._send_input({"t": "move", "x": nx, "y": ny})

    def on_button(self, e, button, pressed):
        self.canvas.focus_set()
        nx, ny = self._norm(e.x, e.y)
        self._send_input({"t": "button", "x": nx, "y": ny,
                          "button": button, "pressed": pressed})

    def on_wheel(self, e):
        self._send_input({"t": "scroll", "dy": 1 if e.delta > 0 else -1})

    # ---------------- 키보드 ----------------
    def on_key_press(self, e):
        ks = e.keysym
        if ks in SPECIAL_MAP:
            canon = SPECIAL_MAP[ks]
            self._send_input({"t": "key", "action": "down", "key": canon})
            if canon in MODIFIERS:
                self.held_specials.add(canon)
            return
        if self.held_specials:
            base = self._base_key(e)
            if base:
                self._send_input({"t": "key", "action": "down", "key": base})
                self.held_base[ks] = base
            return
        ch = e.char
        if ch and ch.isprintable():
            self._send_input({"t": "type", "text": ch})

    def on_key_release(self, e):
        ks = e.keysym
        if ks in SPECIAL_MAP:
            canon = SPECIAL_MAP[ks]
            self._send_input({"t": "key", "action": "up", "key": canon})
            self.held_specials.discard(canon)
            return
        if ks in self.held_base:
            base = self.held_base.pop(ks)
            self._send_input({"t": "key", "action": "up", "key": base})

    def _base_key(self, e):
        ks = e.keysym
        if len(ks) == 1:
            return ks.lower()
        if ks in SYMBOL_MAP:
            return SYMBOL_MAP[ks]
        if e.char and len(e.char) == 1 and e.char.isprintable():
            return e.char
        return None

    def shutdown(self):
        self._closing = True
        self.running = False
        if self.channel is not None:      # 인터넷(P2P) 모드는 채널이 없음
            try:
                self.channel.send(MSG_BYE)
            except Exception:
                pass
            self.channel.close()
        try:
            self.root.destroy()
        except Exception:
            pass


def _prompt_and_run():
    login = tk.Tk()
    login.title("원격 지원 - 접속")
    login.geometry("320x310")
    login.configure(bg="#1e1e2e")

    mode = tk.StringVar(value="relay")
    tk.Label(login, text="연결 방식", fg="#cdd6f4", bg="#1e1e2e").pack(pady=(18, 4))
    row = tk.Frame(login, bg="#1e1e2e")
    row.pack()
    tk.Radiobutton(row, text="인터넷", variable=mode, value="relay",
                   fg="#cdd6f4", bg="#1e1e2e", selectcolor="#313244",
                   activebackground="#1e1e2e").pack(side="left", padx=6)
    tk.Radiobutton(row, text="LAN(같은 네트워크)", variable=mode, value="lan",
                   fg="#cdd6f4", bg="#1e1e2e", selectcolor="#313244",
                   activebackground="#1e1e2e").pack(side="left", padx=6)

    # LAN 전용 IP 필드 (모드에 따라 표시/숨김)
    lan_frame = tk.Frame(login, bg="#1e1e2e")
    tk.Label(lan_frame, text="고객 PC IP 주소", fg="#cdd6f4",
             bg="#1e1e2e").pack(pady=(12, 2))
    ip_e = tk.Entry(lan_frame, justify="center")
    ip_e.pack()

    tk.Label(login, text="접속 코드", fg="#cdd6f4", bg="#1e1e2e").pack(pady=(12, 2))
    pin_e = tk.Entry(login, justify="center")
    pin_e.pack()

    def toggle(*_):
        if mode.get() == "lan":
            lan_frame.pack(after=row)
        else:
            lan_frame.pack_forget()

    mode.trace_add("write", toggle)
    toggle()

    state = {}

    def do_connect():
        state["mode"] = mode.get()
        state["ip"] = ip_e.get().strip()
        state["pin"] = pin_e.get().strip()
        login.destroy()

    pin_e.bind("<Return>", lambda e: do_connect())
    tk.Button(login, text="연결", command=do_connect, bg="#89b4fa",
              fg="#11111b", relief="flat", padx=16, pady=6).pack(pady=18)
    pin_e.focus_set()
    login.mainloop()

    if not state:
        return
    pin = state["pin"]
    if state["mode"] == "lan":
        ip = state["ip"]
        try:
            if not ip:
                raise ConnectionError("LAN 연결은 IP 주소가 필요합니다.")
            ch = connect_lan(ip, pin)
        except Exception as e:
            err = tk.Tk()
            err.withdraw()
            messagebox.showerror("연결 실패", str(e))
            err.destroy()
            return
        ViewerApp(ch, lambda: connect_lan(ip, pin)).start()   # noqa: E731
    else:
        # 인터넷 = WebRTC P2P + 릴레이 폴백 (연결/인증은 백그라운드에서 진행)
        ViewerApp(webrtc=True, relay_url=RELAY_URL, code=pin).start()


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--relay" in argv:
        rest = [a for a in argv if a != "--relay"]
        code = rest[0] if rest else ""
        url = rest[1] if len(rest) > 1 else RELAY_URL
        ViewerApp(webrtc=True, relay_url=url, code=code).start()
    elif len(argv) >= 2:
        ip, code = argv[0], argv[1]
        ViewerApp(connect_lan(ip, code),
                  lambda: connect_lan(ip, code)).start()
    else:
        _prompt_and_run()
