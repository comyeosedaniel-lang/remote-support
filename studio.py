"""
AS Studio — 기사용 통합 대문(셸).
왼쪽 사이드바로 [대시보드 / 원격 지원 / PC 진단 / 이력 / 설정] 전환,
지금까지 만든 기능(원격 P2P, 진단, 복구, 이력)을 한 화면에 묶는다.
고객 Agent = host.py, 이 Studio = viewer(기사) 역할.
"""
import asyncio
import io
import json
import os
import queue
import struct
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext

from PIL import Image, ImageTk

from common import (MSG_INPUT, MSG_DIAG_REQUEST, composite_tiles)
import webrtc_app
import diagnostic
import analysis
import history
import repair
from viewer import SPECIAL_MAP, MODIFIERS, SYMBOL_MAP, RELAY_URL

BG = "#1e1e2e"
BG2 = "#181825"
FG = "#cdd6f4"
ACC = "#89b4fa"


# ---------------------------------------------------------------------------
# 원격 패널 (셸 안에 임베드되는 원격 화면 + 입력)
# ---------------------------------------------------------------------------
class RemotePanel(tk.Frame):
    def __init__(self, parent, relay_url):
        super().__init__(parent, bg="#11111b")
        self.relay_url = relay_url
        self.running = False
        self.frame_w = self.frame_h = 0
        self.screen_img = None
        self._img_lock = threading.Lock()
        self._dirty = False
        self.out_q = queue.Queue()
        self.bridge = {}
        self.held_specials = set()
        self.held_base = {}
        self._photo = None
        self._img_id = None
        self._status = ""

        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x")
        tk.Label(bar, text="접속 코드", fg=FG, bg=BG).pack(side="left", padx=(10, 4))
        self.code_e = tk.Entry(bar, width=10, justify="center")
        self.code_e.pack(side="left", pady=6)
        self.btn_conn = tk.Button(bar, text="연결", command=self.connect,
                                  bg=ACC, fg="#11111b", relief="flat", padx=12)
        self.btn_conn.pack(side="left", padx=6)
        self.btn_file = tk.Button(bar, text="파일 보내기", command=self.send_file,
                                  bg="#94e2d5", fg="#11111b", relief="flat",
                                  padx=8, state="disabled")
        self.btn_file.pack(side="left", padx=2)
        self.btn_diag = tk.Button(bar, text="PC 진단", command=self.req_diag,
                                  bg="#cba6f7", fg="#11111b", relief="flat",
                                  padx=8, state="disabled")
        self.btn_diag.pack(side="left", padx=2)
        self.btn_stop = tk.Button(bar, text="연결 끊기", command=self.stop,
                                  bg="#f38ba8", fg="#11111b", relief="flat",
                                  padx=8, state="disabled")
        self.btn_stop.pack(side="left", padx=2)
        self.lbl = tk.Label(bar, text="대기", fg="#f9e2af", bg=BG)
        self.lbl.pack(side="left", padx=10)

        self.canvas = tk.Canvas(self, bg="black", highlightthickness=0, cursor="dotbox")
        self.canvas.pack(fill="both", expand=True)
        c = self.canvas
        c.bind("<Motion>", self.on_motion)
        c.bind("<ButtonPress-1>", lambda e: self.on_button(e, "left", True))
        c.bind("<ButtonRelease-1>", lambda e: self.on_button(e, "left", False))
        c.bind("<ButtonPress-3>", lambda e: self.on_button(e, "right", True))
        c.bind("<ButtonRelease-3>", lambda e: self.on_button(e, "right", False))
        c.bind("<MouseWheel>", self.on_wheel)
        c.bind("<KeyPress>", self.on_key_press)
        c.bind("<KeyRelease>", self.on_key_release)

    # ---- 연결 ----
    def connect(self):
        code = self.code_e.get().strip()
        if not code:
            self._set_status("접속 코드를 입력하세요")
            return
        if self.running:
            return
        self.running = True
        self.btn_conn.config(state="disabled")
        for b in (self.btn_file, self.btn_diag, self.btn_stop):
            b.config(state="normal")
        self.canvas.focus_set()
        threading.Thread(target=self._loop, args=(code,), daemon=True).start()
        self._render()

    def _loop(self, code):
        async def run():
            while self.running:
                try:
                    await webrtc_app.viewer_run(self.relay_url, code, self._on_frame,
                                                self.out_q, self._set_status,
                                                lambda: self.running, bridge=self.bridge)
                except Exception:
                    pass
                if self.running:
                    self._set_status("재연결 중...")
                    await asyncio.sleep(1)
        try:
            asyncio.run(run())
        except Exception:
            pass

    def stop(self):
        self.running = False
        self.btn_conn.config(state="normal")
        for b in (self.btn_file, self.btn_diag, self.btn_stop):
            b.config(state="disabled")
        self._set_status("연결 종료")

    # ---- 화면 ----
    def _on_frame(self, pil_img):
        with self._img_lock:
            self.screen_img = pil_img
            self.frame_w, self.frame_h = pil_img.size
            self._dirty = True

    def _render(self):
        with self._img_lock:
            if self._dirty and self.screen_img is not None:
                try:
                    self._photo = ImageTk.PhotoImage(self.screen_img)
                    self._dirty = False
                    if self._img_id is None:
                        self._img_id = self.canvas.create_image(0, 0, anchor="nw",
                                                                image=self._photo)
                    else:
                        self.canvas.itemconfig(self._img_id, image=self._photo)
                except Exception:
                    pass
        self.after(15, self._render)

    def _set_status(self, text):
        self._status = text
        try:
            self.after(0, self.lbl.config, {"text": text})
        except Exception:
            pass

    # ---- 입력 ----
    def _send(self, obj):
        self.out_q.put((MSG_INPUT, json.dumps(obj).encode("utf-8")))

    def _norm(self, x, y):
        if self.frame_w and self.frame_h:
            return (x / self.frame_w, y / self.frame_h)
        return (0.0, 0.0)

    def on_motion(self, e):
        nx, ny = self._norm(e.x, e.y)
        self._send({"t": "move", "x": nx, "y": ny})

    def on_button(self, e, button, pressed):
        self.canvas.focus_set()
        nx, ny = self._norm(e.x, e.y)
        self._send({"t": "button", "x": nx, "y": ny, "button": button, "pressed": pressed})

    def on_wheel(self, e):
        self._send({"t": "scroll", "dy": 1 if e.delta > 0 else -1})

    def on_key_press(self, e):
        ks = e.keysym
        if ks in SPECIAL_MAP:
            canon = SPECIAL_MAP[ks]
            self._send({"t": "key", "action": "down", "key": canon})
            if canon in MODIFIERS:
                self.held_specials.add(canon)
            return
        if self.held_specials:
            base = self._base_key(e)
            if base:
                self._send({"t": "key", "action": "down", "key": base})
                self.held_base[ks] = base
            return
        ch = e.char
        if ch and ch.isprintable():
            self._send({"t": "type", "text": ch})

    def on_key_release(self, e):
        ks = e.keysym
        if ks in SPECIAL_MAP:
            self._send({"t": "key", "action": "up", "key": SPECIAL_MAP[ks]})
            self.held_specials.discard(SPECIAL_MAP[ks])
            return
        if ks in self.held_base:
            self._send({"t": "key", "action": "up", "key": self.held_base.pop(ks)})

    def _base_key(self, e):
        ks = e.keysym
        if len(ks) == 1:
            return ks.lower()
        if ks in SYMBOL_MAP:
            return SYMBOL_MAP[ks]
        if e.char and len(e.char) == 1 and e.char.isprintable():
            return e.char
        return None

    # ---- 파일 / 진단 ----
    def send_file(self):
        path = filedialog.askopenfilename(title="보낼 파일")
        if not path:
            return
        fn = self.bridge.get("send_file")
        if fn:
            fn(path)
            self._set_status("파일 전송 중...")
        else:
            self._set_status("아직 연결 안 됨")

    def req_diag(self):
        self.out_q.put((MSG_DIAG_REQUEST, b""))
        self._set_status("원격 진단 요청 — 리포트가 곧 열립니다")


# ---------------------------------------------------------------------------
# 진단 패널 (로컬 진단 + 분석 + 복구 + 이력)
# ---------------------------------------------------------------------------
class DiagnosticPanel(tk.Frame):
    def __init__(self, parent):
        super().__init__(parent, bg=BG)
        self.data = None
        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", pady=(8, 2), padx=8)
        self._btns = {}

        def add(bar, key, text, cmd, color, state="normal"):
            b = tk.Button(bar, text=text, command=cmd, bg=color, fg="#11111b",
                          relief="flat", padx=8, pady=6, state=state)
            b.pack(side="left", padx=3)
            self._btns[key] = b
            return b

        add(bar, "run", "🔍 진단 시작", self.run, ACC)
        add(bar, "autofix", "⚡ 자동 복구", self.auto_repair, "#f38ba8", "disabled")
        add(bar, "save", "📄 저장", self.save_html, "#a6e3a1", "disabled")
        add(bar, "hist", "📋 이력", self.show_history, "#b4befe")
        bar2 = tk.Frame(self, bg=BG)
        bar2.pack(fill="x", pady=(0, 6), padx=8)
        add(bar2, "net", "🌐 인터넷 복구", self.repair_net, "#f9e2af")
        add(bar2, "win", "🛠 Windows 복구", self.win_repair, "#fab387")
        add(bar2, "inst", "📦 설치 프로그램", self.show_installed, "#94e2d5")

        self.text = scrolledtext.ScrolledText(self, bg="#11111b", fg=FG, relief="flat",
                                              font=("Consolas", 10), wrap="none")
        self.text.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self._set("‘진단 시작’을 누르면 이 PC(또는 로컬)를 분석합니다.\n"
                  "관리자로 실행하면 CPU 온도·시스템 복구까지 가능합니다.")

    def _set(self, s):
        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", s)
        self.text.config(state="disabled")

    def _append(self, s):
        self.text.config(state="normal")
        self.text.insert("end", "\n" + s)
        self.text.see("end")
        self.text.config(state="disabled")

    def run(self):
        self._btns["run"].config(state="disabled", text="분석 중...")
        self._set("분석 중... (몇 초)")
        threading.Thread(target=self._run_worker, daemon=True).start()

    def _run_worker(self):
        try:
            d = diagnostic.collect_all()
            rep = diagnostic.report_text(d)
        except Exception as e:
            d, rep = None, "오류: %s" % e
        self.after(0, self._run_done, d, rep)

    def _run_done(self, d, rep):
        self.data = d
        self._set(rep)
        self._btns["run"].config(state="normal", text="🔍 다시 진단")
        for k in ("autofix", "save"):
            self._btns[k].config(state="normal" if d else "disabled")
        if d:
            try:
                history.save(d, html=diagnostic.report_html(d))
            except Exception:
                pass

    def save_html(self):
        if not self.data:
            return
        p = filedialog.asksaveasfilename(defaultextension=".html",
                                         initialfile="PC진단리포트.html",
                                         filetypes=[("HTML", "*.html")])
        if not p:
            return
        try:
            with open(p, "w", encoding="utf-8") as f:
                f.write(diagnostic.report_html(self.data))
            os.startfile(p)
        except Exception as e:
            messagebox.showerror("저장 실패", str(e))

    def auto_repair(self):
        if not self.data:
            return
        a = analysis.analyze(self.data)
        admin = diagnostic.is_admin()
        actions = analysis.recommended_actions(a, admin=admin)
        if not actions:
            messagebox.showinfo("자동 복구", "자동 실행할 복구 항목이 없습니다.")
            return
        names = {"flush_dns": "DNS 초기화", "renew_ip": "IP 갱신",
                 "reset_winsock": "Winsock 초기화", "reset_tcpip": "TCP/IP 초기화",
                 "run_sfc": "시스템 파일 복구(SFC)", "run_dism": "이미지 복구(DISM)"}
        if not messagebox.askyesno("자동 복구", "다음을 실행합니다:\n\n%s\n\n진행할까요?"
                                   % "\n".join("• " + names.get(k, k) for k in actions)):
            return
        self._btns["autofix"].config(state="disabled", text="복구 중...")
        threading.Thread(target=self._autofix_worker, args=(actions,), daemon=True).start()

    def _autofix_worker(self, actions):
        self.after(0, self._set, "[자동 복구] 진행 중...\n")
        fnmap = {"flush_dns": repair.flush_dns, "renew_ip": repair.renew_ip,
                 "reset_winsock": repair.reset_winsock, "reset_tcpip": repair.reset_tcpip,
                 "run_sfc": repair.run_sfc, "run_dism": repair.run_dism}
        for k in actions:
            fn = fnmap.get(k)
            if not fn:
                continue
            self.after(0, self._append, "  → %s ..." % k)
            ok, _ = fn()
            self.after(0, self._append, "    %s" % ("완료" if ok else "실패"))
        self.after(0, self._append, "\n자동 복구 완료.")
        self.after(0, lambda: self._btns["autofix"].config(state="normal", text="⚡ 자동 복구"))

    def repair_net(self):
        admin = diagnostic.is_admin()
        if not messagebox.askyesno("인터넷 복구",
                                   "DNS 초기화·IP 갱신%s 을 실행합니다. 진행할까요?"
                                   % (" + Winsock/TCP-IP 초기화(재부팅)" if admin else "")):
            return
        threading.Thread(target=self._net_worker, args=(admin,), daemon=True).start()

    def _net_worker(self, admin):
        self.after(0, self._set, "[인터넷 복구] 진행 중...\n")
        res = repair.repair_internet(admin=admin, include_reset=admin,
                                     on_step=lambda l, ok, s: self.after(0, self._append,
                                                                         "  %s : %s" % (l, s)))
        if res["reboot_needed"]:
            self.after(0, self._append, "\n⚠️ 재부팅해야 적용됩니다.")

    def win_repair(self):
        if not diagnostic.is_admin():
            messagebox.showwarning("Windows 복구", "관리자 권한으로 실행하세요.")
            return
        if not messagebox.askyesno("Windows 복구", "SFC /scannow (수 분). 진행할까요?"):
            return
        self._btns["win"].config(state="disabled", text="복구 중...")
        threading.Thread(target=self._win_worker, daemon=True).start()

    def _win_worker(self):
        self.after(0, self._set, "[Windows 복구] SFC 실행 중... 창을 닫지 마세요.\n")
        ok, msg = repair.run_sfc()
        self.after(0, self._append, (msg or "")[-1200:])
        self.after(0, self._append, "\n" + ("완료" if ok else "오류/일부 실패"))
        self.after(0, lambda: self._btns["win"].config(state="normal", text="🛠 Windows 복구"))

    def show_installed(self):
        self._set("[설치 프로그램] 불러오는 중...")
        threading.Thread(target=self._inst_worker, daemon=True).start()

    def _inst_worker(self):
        items = diagnostic.collect_installed()
        lines = ["[설치 프로그램] 총 %d개\n" % len(items)]
        lines += ["  %s %s" % (it["name"], it["version"]) for it in items]
        self.after(0, self._set, "\n".join(lines))

    def show_history(self):
        self._set(history.format_list(history.list_history()))


# ---------------------------------------------------------------------------
# Studio 셸
# ---------------------------------------------------------------------------
class Studio:
    NAV = [("dash", "🏠  대시보드"), ("remote", "🖥  원격 지원"),
           ("diag", "🔍  PC 진단"), ("settings", "⚙  설정")]

    def __init__(self):
        self.relay_url = RELAY_URL
        self.root = tk.Tk()
        self.root.title("AS Studio — 원격 지원 + PC 진단")
        self.root.geometry("1060x680")
        self.root.configure(bg=BG)

        side = tk.Frame(self.root, bg=BG2, width=180)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        tk.Label(side, text="AS Studio", fg=ACC, bg=BG2,
                 font=("Malgun Gothic", 14, "bold")).pack(pady=(18, 20))
        self._navbtns = {}
        for key, label in self.NAV:
            b = tk.Button(side, text=label, anchor="w", command=lambda k=key: self.show(k),
                          bg=BG2, fg=FG, relief="flat", font=("Malgun Gothic", 11),
                          activebackground=ACC, padx=16, pady=10, bd=0)
            b.pack(fill="x")
            self._navbtns[key] = b

        self.content = tk.Frame(self.root, bg=BG)
        self.content.pack(side="left", fill="both", expand=True)

        self.panels = {}
        self.panels["remote"] = RemotePanel(self.content, self.relay_url)
        self.panels["diag"] = DiagnosticPanel(self.content)
        self.panels["dash"] = self._dashboard()
        self.panels["settings"] = self._settings()
        self.show("dash")
        self.root.protocol("WM_DELETE_WINDOW", self._quit)

    def _dashboard(self):
        f = tk.Frame(self.content, bg=BG)
        tk.Label(f, text="AS Studio", fg=FG, bg=BG,
                 font=("Malgun Gothic", 22, "bold")).pack(anchor="w", padx=24, pady=(28, 4))
        tk.Label(f, text="원격 지원과 PC 진단을 한 곳에서.", fg="#a6adc8",
                 bg=BG).pack(anchor="w", padx=24)
        card = tk.Frame(f, bg=BG2)
        card.pack(fill="x", padx=24, pady=20)
        tk.Label(card, text="빠른 시작", fg=FG, bg=BG2,
                 font=("Malgun Gothic", 12, "bold")).pack(anchor="w", padx=16, pady=(12, 6))
        row = tk.Frame(card, bg=BG2)
        row.pack(anchor="w", padx=16, pady=(0, 14))
        tk.Button(row, text="🖥  고객에게 원격 접속", command=lambda: self.show("remote"),
                  bg=ACC, fg="#11111b", relief="flat", padx=14, pady=8).pack(side="left")
        tk.Button(row, text="🔍  내 PC 진단", command=lambda: self.show("diag"),
                  bg="#cba6f7", fg="#11111b", relief="flat", padx=14, pady=8).pack(side="left", padx=8)
        tk.Label(f, text="최근 진단 이력", fg=FG, bg=BG,
                 font=("Malgun Gothic", 12, "bold")).pack(anchor="w", padx=24, pady=(6, 4))
        self.hist_box = tk.Text(f, height=10, bg="#11111b", fg=FG, relief="flat",
                                font=("Consolas", 10))
        self.hist_box.pack(fill="both", expand=True, padx=24, pady=(0, 20))
        return f

    def _settings(self):
        f = tk.Frame(self.content, bg=BG)
        tk.Label(f, text="설정", fg=FG, bg=BG,
                 font=("Malgun Gothic", 18, "bold")).pack(anchor="w", padx=24, pady=(28, 12))
        tk.Label(f, text="릴레이 서버 주소", fg="#a6adc8", bg=BG).pack(anchor="w", padx=24)
        self.relay_e = tk.Entry(f, width=46)
        self.relay_e.insert(0, self.relay_url)
        self.relay_e.pack(anchor="w", padx=24, pady=(2, 10))
        tk.Button(f, text="적용", command=self._apply_relay, bg=ACC, fg="#11111b",
                  relief="flat", padx=14, pady=6).pack(anchor="w", padx=24)
        tk.Label(f, text="관리자 권한: %s" % ("예" if diagnostic.is_admin() else "아니오 (CPU온도·복구 제한)"),
                 fg="#f9e2af", bg=BG).pack(anchor="w", padx=24, pady=16)
        return f

    def _apply_relay(self):
        url = self.relay_e.get().strip()
        if url:
            self.relay_url = url
            self.panels["remote"].relay_url = url
            messagebox.showinfo("설정", "릴레이 주소 적용됨:\n%s" % url)

    def show(self, key):
        for panel in self.panels.values():
            panel.pack_forget()
        self.panels[key].pack(fill="both", expand=True)
        for k, b in self._navbtns.items():
            b.config(bg=(ACC if k == key else BG2), fg=("#11111b" if k == key else FG))
        if key == "dash":
            self._refresh_history()

    def _refresh_history(self):
        try:
            txt = history.format_list(history.list_history(limit=15))
        except Exception:
            txt = "이력 없음"
        self.hist_box.config(state="normal")
        self.hist_box.delete("1.0", "end")
        self.hist_box.insert("1.0", txt)
        self.hist_box.config(state="disabled")

    def _quit(self):
        try:
            self.panels["remote"].stop()
        except Exception:
            pass
        self.root.destroy()

    def start(self):
        self.root.mainloop()


if __name__ == "__main__":
    Studio().start()
