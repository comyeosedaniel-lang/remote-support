"""
AS Studio 클라이언트 (고객 PC / 서비스 대상 PC) — 로컬 웹서버 + Edge 앱창 방식.

두 가지 역할을 동시에 수행한다.
  1) 호스트 역할: 시작과 동시에 화면을 WebRTC(릴레이 경유)로 내보내 기사 PC 가
     접속 코드(6자리)로 원격 접속·제어할 수 있게 한다. (host.py 와 동일한 host_run 사용)
  2) 로컬 진단/복구/도구 UI: webui/client.html 을 로컬 HTTP 로 서빙하고 Edge 를
     앱 모드로 띄운다. 고객 또는 원격 접속한 기사가 이 PC 에서 직접 진단·복구를 실행한다.

studio_web.py 와 같은 아키텍처(ThreadingHTTPServer + msedge --app + fetch('/api/<method>'))이며,
진단/복구/도구/모니터 로직은 그대로 재사용한다. 원격 지원(뷰어)·모바일·세션녹화 기능은 없다.
"""
import datetime
import json
import os
import random
import subprocess
import sys
import threading
import time
from html import escape as _esc
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

try:
    import psutil
except Exception:
    psutil = None

from pynput.mouse import Controller as MouseController, Button
from pynput.keyboard import Controller as KeyboardController, Key

import webrtc_app
import diagnostic
import analysis
import history
import repair

# 디스플레이 배율(125%/150% 등)이 100%가 아니면, DPI-비인식 프로세스의 마우스 좌표(SetCursorPos)는
# 논리 픽셀로 해석되어 화면 캡처의 실제(물리) 픽셀과 어긋난다 → 원격 클릭 위치가 밀림.
# Edge 창/캡처 시작 전에 프로세스를 모니터별 DPI 인식으로 선언해 물리 픽셀 기준으로 맞춘다.
try:
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _res(rel):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


# Cloudflare 가 Python-urllib UA 를 봇으로 차단(403)하므로 일반 UA 사용
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AS-Studio/1.0"


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


# 뷰어(기사)가 보내는 특수키 이름 -> pynput Key
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


def _decode_out(b):
    """콘솔 출력 바이트 디코드. sfc/dism 은 UTF-16LE, ipconfig/netsh 등은 mbcs(cp949)."""
    if not b:
        return ""
    # 널바이트가 있으면 UTF-16LE(sfc/dism) 가능성 → 시도해서 결과에 널문자 없으면 채택
    if b"\x00" in b:
        try:
            s = b.decode("utf-16-le", "ignore").lstrip("﻿")
            if "\x00" not in s:
                return s
        except Exception:
            pass
    for enc in ("utf-8", "mbcs"):
        try:
            s = b.decode(enc)
            if "�" not in s and "\x00" not in s:
                return s
        except Exception:
            pass
    return b.decode("mbcs", "ignore").replace("\x00", "")


# ============================ 실시간 모니터 ============================
class LiveMon:
    """LHM 핸들을 1회만 열고 백그라운드에서 1초마다 CPU/RAM/GPU/온도/네트워크를 샘플링.
    사양(정적)은 스레드 시작 시 1회 계산해 캐시. get_live/get_specs 는 캐시만 즉시 반환."""

    def __init__(self):
        self._run = False
        self._snap = {}
        self._specs = None
        self._computer = None
        self._net_prev = None
        self._thread = None

    def start(self):
        if self._run:
            return
        self._run = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def snapshot(self):
        return self._snap

    def specs(self):
        return self._specs

    def _compute_specs(self):
        d = {}
        for k, fn, default in (
            ("system", diagnostic.collect_system, {}),
            ("cpu", diagnostic.collect_cpu, {}),
            ("mem", diagnostic.collect_memory, {}),
            ("disks", diagnostic.collect_disks, {"volumes": [], "physical": []}),
            ("gpu", diagnostic.collect_gpu, []),
        ):
            try:
                d[k] = fn()
            except Exception:
                d[k] = default
        self._specs = d

    def _open_lhm(self):
        try:
            from HardwareMonitor.Hardware import Computer
            c = Computer()
            # 관리자 권한(+PawnIO 드라이버) 없이 CPU 센서에 접근하면 pythonnet/.NET 레벨에서
            # access violation(네이티브 크래시)이 나 Python try/except로도 못 막는다 → 비관리자면 아예 끈다.
            try:
                admin = diagnostic.is_admin()
            except Exception:
                admin = False
            c.IsCpuEnabled = bool(admin)
            c.IsGpuEnabled = True
            c.IsMemoryEnabled = True
            c.IsMotherboardEnabled = True
            c.IsStorageEnabled = True
            c.Open()
            self._computer = c
        except Exception:
            self._computer = None

    def _read_lhm(self):
        c = self._computer
        if not c:
            return {}, {}
        temps, gpu, cpu_cand = {}, {}, []
        try:
            for hw in c.Hardware:
                hw.Update()
                for sub in hw.SubHardware:
                    sub.Update()
                htype = str(hw.HardwareType)
                is_cpu = "Cpu" in htype
                is_gpu = "Gpu" in htype
                is_sto = "Storage" in htype
                for s in hw.Sensors:
                    if s.Value is None:
                        continue
                    st = str(s.SensorType)
                    name = s.Name or ""
                    if st == "Temperature":
                        if is_cpu:
                            cpu_cand.append((name, float(s.Value)))
                        elif is_gpu and "Hot Spot" in name:
                            temps["GPU 핫스팟"] = round(s.Value, 1)
                        elif is_gpu and "Core" in name:
                            temps["GPU"] = round(s.Value, 1)
                            gpu["temp"] = round(s.Value, 1)
                        elif is_sto and "Critical" not in name:
                            temps.setdefault("디스크", round(s.Value, 1))
                    elif is_gpu and st == "SmallData":
                        if name == "GPU Memory Total":
                            gpu["vram_total_mb"] = round(s.Value)
                        elif name == "GPU Memory Used":
                            gpu["vram_used_mb"] = round(s.Value)
                    elif is_gpu and st == "Load" and name == "GPU Core":
                        gpu["load"] = round(s.Value, 1)
                    elif is_gpu and st == "Power":
                        gpu["power_w"] = round(s.Value, 1)
                    elif is_gpu and st == "Voltage" and "Core" in name:
                        gpu["voltage_v"] = round(s.Value, 3)
                    elif is_gpu and st == "Clock":
                        if name == "GPU Core":
                            gpu["core_mhz"] = round(s.Value)
                        elif name == "GPU Memory":
                            gpu["mem_mhz"] = round(s.Value)
            if cpu_cand:
                pkg = [v for n, v in cpu_cand if "Package" in n or "CPU" in n]
                temps["CPU"] = round(pkg[0] if pkg else max(v for _, v in cpu_cand), 1)
        except Exception:
            pass
        return temps, gpu

    def _loop(self):
        try:
            import pythoncom
            pythoncom.CoInitialize()
        except Exception:
            pass
        self._open_lhm()
        try:
            self._compute_specs()
        except Exception:
            pass
        if psutil:
            try:
                psutil.cpu_percent(percpu=True)
            except Exception:
                pass
        while self._run:
            snap = {}
            if psutil:
                try:
                    cores = psutil.cpu_percent(percpu=True)
                    snap["cores"] = [round(x) for x in cores]
                    snap["cpu"] = round(sum(cores) / len(cores)) if cores else 0
                except Exception:
                    pass
                try:
                    vm = psutil.virtual_memory()
                    snap["mem"] = {"percent": round(vm.percent),
                                   "used_gb": diagnostic._gb(vm.used),
                                   "total_gb": diagnostic._gb(vm.total)}
                except Exception:
                    pass
                try:
                    io = psutil.net_io_counters()
                    now = time.monotonic()
                    if self._net_prev:
                        dt = max(0.2, now - self._net_prev[2])
                        snap["net"] = {
                            "up_kbs": max(0, round((io.bytes_sent - self._net_prev[0]) / 1024 / dt)),
                            "down_kbs": max(0, round((io.bytes_recv - self._net_prev[1]) / 1024 / dt))}
                    self._net_prev = (io.bytes_sent, io.bytes_recv, now)
                except Exception:
                    pass
            temps, gpu = self._read_lhm()
            snap["temps"] = temps
            snap["gpu"] = gpu
            try:
                gpu_w = float(gpu.get("power_w") or 0)
                cpu_est = 20 + (snap.get("cpu") or 0)          # CPU 유휴 20W + 부하 비례(추정)
                ndisk = len((self._specs or {}).get("disks", {}).get("physical") or []) or 1
                snap["power_est"] = round(cpu_est + gpu_w + 40 + ndisk * 6)
            except Exception:
                pass
            self._snap = snap
            time.sleep(1.0)


# ============================ API ============================
class Api:
    def __init__(self):
        self.relay = RELAY_URL
        self.data = None
        self.mon = LiveMon()
        self.last_activity = time.monotonic()
        self.timeline = []          # 세션 기록 [(HH:MM:SS, 이벤트)]
        self.port = None            # 이 인스턴스 HTTP 포트(옛 Edge 창 닫기용)
        self.repair_log = []        # 복구 실시간 진행 로그
        self.repair_ver = 0
        self.repair_busy = False

        # ---- 호스트(원격 접속 대기) 상태 ----
        _env_code = os.environ.get("REMOTE_CODE", "")
        self.pin = (_env_code if (len(_env_code) == 6 and _env_code.isdigit())
                    else "%06d" % random.randint(0, 999999))
        self.mode = "relay"                 # 인터넷 릴레이 경유(WebRTC)
        self.host_running = False
        self.bridge = {}                    # webrtc_app 이 send_file 함수를 넣어줌
        self._connected = False
        self._hstatus = "준비 중"
        self.mouse = MouseController()
        self.keyboard = KeyboardController()
        self.screen_w, self.screen_h = self._screen_size()
        self.customer = self._customer_load()

    def touch(self):
        self.last_activity = time.monotonic()

    # ---- 고객 정보(대시보드 상단 동의서) ----
    def _customer_path(self):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        d = os.path.join(base, "ASStudio")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "customer.json")

    def _customer_load(self):
        try:
            with open(self._customer_path(), "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def customer_get(self):
        return self.customer

    def customer_save(self, phone, media_consent=False, name="", address=""):
        phone = (phone or "").strip()
        name = (name or "").strip()
        address = (address or "").strip()
        digits = "".join(c for c in phone if c.isdigit())
        if len(digits) < 9:
            return {"error": "전화번호를 정확히 입력해 주세요."}
        rec = {"phone": phone, "name": name, "address": address, "agreed": True,
               "media_consent": bool(media_consent),
               "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        try:
            with open(self._customer_path(), "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False)
        except Exception:
            pass
        self.customer = rec
        self._log("고객 정보 확인 (전화번호 동의)")
        # 서버에 감사 기록 남기기 (베스트에포트 — 실패해도 로컬 저장은 유지)
        try:
            import urllib.request
            body = json.dumps({"phone": phone, "name": name, "address": address, "agreed": True,
                               "media_consent": bool(media_consent)}).encode("utf-8")
            req = urllib.request.Request(self._http_base() + "/consent", data=body, method="POST",
                                         headers={"User-Agent": _UA, "Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=8)
        except Exception:
            pass
        return {"ok": True}

    def _http_base(self):
        b = (self.relay or "").replace("wss://", "https://").replace("ws://", "http://")
        if b.endswith("/relay"):
            b = b[:-len("/relay")]
        return b or "https://mylineal.com"

    # ---- 세션 타임라인 ----
    def _log(self, event):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self.timeline.append((ts, event))
        if len(self.timeline) > 300:
            self.timeline = self.timeline[-300:]

    def timeline_text(self):
        if not self.timeline:
            return "아직 기록이 없습니다.\n원격 연결·진단·복구를 실행하면 여기에 시간순으로 기록됩니다."
        return "\n".join("%s   %s" % (t, e) for t, e in self.timeline)

    def timeline_clear(self):
        self.timeline = []
        self._log("세션 기록 초기화")
        return True

    # ---- 창 생존 신호 ----
    def ping(self):
        return True

    # ---- 설정/공통 ----
    def get_settings(self):
        return {"relay": self.relay, "admin": diagnostic.is_admin()}

    # ---- 빠른 도구: 구글 검색 + 윈도우 기본 도구 ----
    def web_search(self, q):
        import webbrowser
        import urllib.parse
        q = (q or "").strip()
        if not q:
            return False
        try:
            webbrowser.open("https://www.google.com/search?q=" + urllib.parse.quote(q))
        except Exception:
            return False
        self._log("웹 검색: " + q[:60])
        return True

    def open_tool(self, key):
        import subprocess
        tools = {
            "control": "control", "admintools": "control admintools",
            "update": "ms-settings:windowsupdate", "devmgmt": "devmgmt.msc",
            "diskmgmt": "diskmgmt.msc", "services": "services.msc",
            "msinfo": "msinfo32", "taskmgr": "taskmgr", "eventvwr": "eventvwr.msc",
            "regedit": "regedit", "cleanmgr": "cleanmgr", "sysprop": "sysdm.cpl",
        }
        arg = tools.get(key)
        if not arg:
            return False
        try:
            subprocess.Popen('start "" ' + arg, shell=True)   # shell start: .msc/.cpl/ms-settings/exe 모두 열림
            self._log("Windows 도구 실행: " + key)
            return True
        except Exception:
            return False

    def open_url(self, url):
        import webbrowser
        if not (url or "").startswith("http"):
            return False
        try:
            webbrowser.open(url)
        except Exception:
            return False
        self._log("링크 열기: " + url[:80])
        return True

    def set_relay(self, url):
        if url:
            self.relay = url
        return True

    def history_text(self):
        try:
            return history.format_list(history.list_history(limit=20))
        except Exception:
            return "이력 없음"

    # ---- 대시보드 모니터 ----
    def get_specs(self):
        return self.mon.specs()

    def _client_info(self):
        """연결 성사 시 host_run 이 자동 전송 — 고객정보(동의서)+PC사양 요약."""
        try:
            return {"customer": self.customer, "specs": self.mon.specs()}
        except Exception:
            return {"customer": self.customer, "specs": None}

    def _extra_diag_info(self):
        """진단 리포트 전송 시 함께 실어보내는 이번 세션 작업 로그(복구 실행 등 포함)."""
        try:
            return {"session_log": self.timeline[-30:]}
        except Exception:
            return {}

    def get_live(self):
        return self.mon.snapshot()

    # ---- 진단/복구 ----
    def run_diagnostic(self):
        self._log("전체 진단 실행")
        try:
            self.data = diagnostic.collect_all()
            html = diagnostic.report_html(self.data)
            try:
                history.save(self.data, html=html)
            except Exception:
                pass
            return html
        except Exception as e:
            return "<h2>진단 오류</h2><pre>%s</pre>" % e

    def save_report(self):
        if not self.data:
            return "먼저 진단하세요."
        try:
            path = _save_dialog("PC진단리포트.html")
            if not path:
                return "취소됨"
            with open(path, "w", encoding="utf-8") as f:
                f.write(diagnostic.report_html(self.data))
            try:
                os.startfile(path)
            except Exception:
                pass
            return "저장됨: " + path
        except Exception as e:
            return "저장 실패: %s" % e

    def auto_repair(self):
        self._log("복구: 자동 복구")
        if not self.data:
            return "먼저 진단하세요."
        a = analysis.analyze(self.data)
        admin = diagnostic.is_admin()
        actions = analysis.recommended_actions(a, admin=admin)
        if not actions:
            return "자동 실행할 복구 항목이 없습니다."
        fnmap = {"flush_dns": repair.flush_dns, "renew_ip": repair.renew_ip,
                 "reset_winsock": repair.reset_winsock, "reset_tcpip": repair.reset_tcpip,
                 "run_sfc": repair.run_sfc, "run_dism": repair.run_dism}
        out = ["[자동 복구]"]
        for k in actions:
            fn = fnmap.get(k)
            if not fn:
                continue
            ok, _ = fn()
            out.append("  %s : %s" % (k, "완료" if ok else "실패"))
        return "\n".join(out)

    def repair_net(self):
        self._log("복구: 인터넷 복구")
        admin = diagnostic.is_admin()
        res = repair.repair_internet(admin=admin, include_reset=admin)
        lines = ["[인터넷 복구]"] + ["  %s : %s" % (l, s) for l, _, s in res["results"]]
        if res["reboot_needed"]:
            lines.append("⚠️ 재부팅해야 적용됩니다.")
        return "\n".join(lines)

    def repair_win(self):
        self._log("복구: Windows 복구(SFC)")
        if not diagnostic.is_admin():
            return "관리자 권한으로 실행하세요."
        ok, msg = repair.run_sfc()
        return "[Windows 복구]\n" + (msg or "")[-1500:] + "\n" + ("완료" if ok else "오류/일부 실패")

    def installed_text(self):
        items = diagnostic.collect_installed()
        return "[설치 프로그램] 총 %d개\n" % len(items) + "\n".join(
            "  %s %s" % (it["name"], it["version"]) for it in items)

    # ---- 진단: 세부 섹션 (CPU/그래픽/메모리/저장/네트워크/시스템) ----
    def _ensure_data(self):
        if self.data is None:
            try:
                self.data = diagnostic.collect_all()
            except Exception:
                self.data = {}
        return self.data

    def diag_section(self, name):
        return _section_html(name, self._ensure_data())

    def diag_refresh(self):
        self.data = None
        return True

    # ---- 복구(수정) 전용 ----
    def repair_power(self):
        self._log("복구: 최대 성능 모드")
        fn = getattr(repair, "power_high_performance", None)
        if not fn:
            return "준비되지 않음"
        ok, msg = fn()
        return "[전원] " + ("최대 성능 모드로 전환됨" if ok else "실패") + (("\n" + msg) if msg else "")

    def repair_faststartup(self):
        self._log("복구: 빠른 시작 끄기")
        fn = getattr(repair, "disable_fast_startup", None)
        if not fn:
            return "준비되지 않음"
        ok, msg = fn()
        return "[빠른 시작 끄기] " + (msg or ("완료" if ok else "실패"))

    def backup_wifi(self):
        self._log("복구: Wi-Fi 프로파일 백업")
        fn = getattr(repair, "backup_wifi_profiles", None)
        if not fn:
            return "준비되지 않음"
        ok, msg = fn()
        return "[Wi-Fi 백업] " + (msg or ("완료" if ok else "실패"))

    def repair_store(self):
        self._log("복구: Store 앱 재등록")
        fn = getattr(repair, "reregister_store_apps", None)
        if not fn:
            return "준비되지 않음"
        ok, msg = fn()
        return "[Store 앱 재등록] " + (msg or ("완료" if ok else "실패"))

    def repair_restore_point(self):
        self._log("복구: 시스템 복원 지점 생성")
        fn = getattr(repair, "create_restore_point", None)
        if not fn:
            return "준비되지 않음"
        ok, msg = fn()
        return "[복원 지점] " + (msg or ("완료" if ok else "실패"))

    def repair_cleartemp(self):
        self._log("복구: 임시 파일 정리")
        fn = getattr(repair, "clear_temp", None)
        if not fn:
            return "준비되지 않음"
        ok, msg = fn()
        return "[임시 파일] " + (msg or ("완료" if ok else "실패"))

    def repair_updatecache(self):
        self._log("복구: Windows Update 캐시 정리")
        fn = getattr(repair, "clear_update_cache", None)
        if not fn:
            return "준비되지 않음"
        ok, msg = fn()
        return "[업데이트 캐시] " + (msg or ("완료" if ok else "실패"))

    # ==== 복구: 실시간 진행 로그 (명령 → 실제 출력 → 결과) ====
    def _rlog(self, line):
        self.repair_log.append(str(line))
        self.repair_ver += 1

    def _run_cmd(self, cmd, timeout=60):
        import subprocess
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout,
                               creationflags=0x08000000)
            out = _decode_out((r.stdout or b"") + (r.stderr or b"")).strip()
            return r.returncode == 0, out
        except subprocess.TimeoutExpired:
            return False, "(시간 초과)"
        except Exception as e:
            return False, "실행 오류: %s" % e

    def _step(self, label, cmd, timeout=60):
        self._rlog("")
        self._rlog("▶ " + label)
        self._rlog("  $ " + " ".join(cmd))
        ok, out = self._run_cmd(cmd, timeout)
        lines = [l for l in (out or "").splitlines() if l.strip()]
        for l in lines[:15]:
            self._rlog("  " + l)
        if len(lines) > 15:
            self._rlog("  … (%d줄 더 있음)" % (len(lines) - 15))
        if not lines:
            self._rlog("  (출력 없음)")
        self._rlog("  → " + ("✅ 성공" if ok else "❌ 실패"))
        return ok

    def run_repair(self, kind):
        if self.repair_busy:
            return False
        self.repair_busy = True
        self.repair_log = []
        self.repair_ver = 0
        self._log("복구 실행: " + str(kind))
        threading.Thread(target=self._do_repair, args=(str(kind),), daemon=True).start()
        return True

    def repair_progress(self, seen):
        try:
            seen = int(seen)
        except Exception:
            seen = 0
        if self.repair_ver <= seen and self.repair_log:
            return {"ver": self.repair_ver, "text": None, "busy": self.repair_busy}
        return {"ver": self.repair_ver, "text": "\n".join(self.repair_log), "busy": self.repair_busy}

    def _do_repair(self, kind):
        fnmap = {"net": self._repair_net, "win": self._repair_win, "auto": self._repair_auto,
                 "power": self._repair_power, "faststartup": self._repair_faststartup,
                 "wifi": self._repair_wifi, "store": self._repair_store,
                 "restore": self._repair_restore, "temp": self._repair_temp,
                 "updatecache": self._repair_updatecache_log, "outlook": self._repair_outlook,
                 "stress": self._repair_stress, "speedtest": self._repair_speedtest}
        fn = fnmap.get(kind)
        try:
            if fn:
                fn()
            else:
                self._rlog("알 수 없는 복구: " + kind)
        except Exception as e:
            self._rlog("오류: %s" % e)
        finally:
            self._rlog("")
            self._rlog("──────── 작업 종료 ────────")
            self.repair_busy = False

    def _repair_net(self):
        admin = diagnostic.is_admin()
        self._rlog("🌐 인터넷 복구  ·  관리자 권한: %s" % ("있음" if admin else "없음(초기화류 건너뜀)"))
        before = diagnostic.collect_network()
        self._rlog("  [현재] 인터넷 %s / DNS %s"
                   % ("정상" if before.get("internet") else "실패", "정상" if before.get("dns") else "실패"))
        self._step("DNS 캐시 초기화", ["ipconfig", "/flushdns"], 30)
        self._step("IP 주소 반납", ["ipconfig", "/release"], 30)
        self._step("IP 주소 재할당", ["ipconfig", "/renew"], 50)
        if admin:
            self._step("Winsock 초기화", ["netsh", "winsock", "reset"], 30)
            self._step("TCP/IP 초기화", ["netsh", "int", "ip", "reset"], 30)
            self._rlog("")
            self._rlog("⚠️ Winsock/TCP-IP 초기화는 재부팅해야 완전히 적용됩니다.")
        self._rlog("")
        self._rlog("▶ 복구 후 연결 재확인")
        after = diagnostic.collect_network()
        self._rlog("  인터넷: %s → %s"
                   % ("정상" if before.get("internet") else "실패", "정상" if after.get("internet") else "실패"))
        self._rlog("  DNS   : %s → %s"
                   % ("정상" if before.get("dns") else "실패", "정상" if after.get("dns") else "실패"))

    def _repair_win(self):
        if not diagnostic.is_admin():
            self._rlog("❌ 관리자 권한이 필요합니다. [🔺 관리자 권한으로 재시작] 후 실행하세요.")
            return
        self._rlog("🛠 Windows 시스템 파일 검사·복구")
        self._rlog("")
        self._rlog("▶ sfc /scannow   (수 분 소요 — 진행 중, 창을 닫지 마세요)")
        self._rlog("  $ sfc /scannow")
        ok, out = self._run_cmd(["sfc", "/scannow"], 1200)
        for l in (out or "").splitlines():
            if l.strip():
                self._rlog("  " + l.strip())
        o = (out or "").lower()
        if ("찾지 못했습니다" in out) or ("did not find" in o) or ("no integrity" in o):
            self._rlog("  → ✅ 무결성 위반 없음 — 시스템 파일 정상")
        elif ("복구했습니다" in out) or ("successfully repaired" in o):
            self._rlog("  → ✅ 손상된 시스템 파일을 복구했습니다")
        elif ("복구할 수 없" in out) or ("unable to fix" in o) or ("could not perform" in o):
            self._rlog("  → ⚠️ 일부 손상을 복구하지 못함 — [자동 복구](DISM)를 이어서 실행 권장")
        else:
            self._rlog("  → 검사 완료 (위 결과 메시지 확인)")

    def _repair_auto(self):
        if not self.data:
            self._rlog("먼저 [PC 진단]을 실행하세요. 분석 결과를 기반으로 필요한 복구만 실행합니다.")
            return
        a = analysis.analyze(self.data)
        admin = diagnostic.is_admin()
        actions = analysis.recommended_actions(a, admin=admin)
        self._rlog("⚡ 분석 기반 자동 복구")
        if not actions:
            self._rlog("  ✅ 자동 실행할 복구 항목이 없습니다. (분석상 조치 필요 없음)")
            return
        cmap = {"flush_dns": (["ipconfig", "/flushdns"], "DNS 캐시 초기화"),
                "renew_ip": (["ipconfig", "/renew"], "IP 주소 재할당"),
                "reset_winsock": (["netsh", "winsock", "reset"], "Winsock 초기화"),
                "reset_tcpip": (["netsh", "int", "ip", "reset"], "TCP/IP 초기화"),
                "run_sfc": (["sfc", "/scannow"], "시스템 파일 복구(SFC)"),
                "run_dism": (["DISM", "/Online", "/Cleanup-Image", "/RestoreHealth"], "Windows 이미지 복구(DISM)")}
        self._rlog("  실행 예정: " + ", ".join(cmap.get(k, ([], k))[1] for k in actions))
        for k in actions:
            cmd, label = cmap.get(k, (None, k))
            if cmd:
                self._step(label, cmd, 1800)

    def _repair_power(self):
        self._rlog("🔋 전원 계획을 고성능으로 변경")
        self._step("고성능 전원 계획 활성화",
                   ["powercfg", "/setactive", "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"], 15)
        ok, out = self._run_cmd(["powercfg", "/getactivescheme"], 15)
        self._rlog("  현재 활성 계획: " + (out or "?"))

    def _repair_faststartup(self):
        self._rlog("⚡ 빠른 시작(Fast Startup) 끄기")
        fn = getattr(repair, "disable_fast_startup", None)
        ok, msg = fn() if fn else (False, "준비 안됨")
        self._rlog("  레지스트리 HiberbootEnabled = 0 설정")
        self._rlog("  → " + ("✅ " if ok else "❌ ") + (msg or ""))

    def _repair_wifi(self):
        self._rlog("📶 저장된 Wi-Fi 프로파일 백업 (비밀번호 포함)")
        self._rlog("  $ netsh wlan export profile key=clear folder=바탕화면\\WiFi백업")
        fn = getattr(repair, "backup_wifi_profiles", None)
        ok, msg = fn() if fn else (False, "준비 안됨")
        self._rlog("  → " + ("✅ " if ok else "❌ ") + (msg or ""))

    def _repair_store(self):
        if not diagnostic.is_admin():
            self._rlog("❌ 관리자 권한이 필요합니다. [🔺 관리자 권한으로 재시작] 후 실행하세요.")
            return
        self._rlog("🧩 Microsoft Store 앱 재등록   (수 분 소요 — 진행 중)")
        self._rlog("  $ Get-AppxPackage -AllUsers | Add-AppxPackage -Register …")
        fn = getattr(repair, "reregister_store_apps", None)
        ok, msg = fn() if fn else (False, "준비 안됨")
        self._rlog("  → " + ("✅ " if ok else "❌ ") + (msg or ""))

    def _repair_restore(self):
        if not diagnostic.is_admin():
            self._rlog("❌ 관리자 권한이 필요합니다. [🔺 관리자 권한으로 재시작] 후 실행하세요.")
            return
        self._rlog("💾 시스템 복원 지점 생성")
        self._rlog("")
        # 1) 시스템 보호(복원) 켜기 — 꺼져 있으면 Checkpoint 가 실패/지연되므로 먼저 활성화
        self._rlog("▶ 1단계 · 시스템 보호 활성화")
        self._rlog("  $ Enable-ComputerRestore -Drive \"C:\\\"")
        ok1, out1 = self._run_cmd(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                   "Enable-ComputerRestore -Drive 'C:\\'"], 60)
        self._rlog("  → " + ("✅ 시스템 보호 활성화됨" if ok1 else ("⚠️ " + (out1[:150] if out1 else "확인 불가"))))
        # 2) 복원 지점 생성 (진행 중 표시 → 오래 걸려도 멈춘 게 아님)
        self._rlog("")
        self._rlog("▶ 2단계 · 복원 지점 생성  (최대 1~2분, 진행 중…)")
        self._rlog("  $ Checkpoint-Computer -Description 'AS Studio 복구 지점' -RestorePointType MODIFY_SETTINGS")
        ok2, out2 = self._run_cmd(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                   "Checkpoint-Computer -Description 'AS Studio 복구 지점' "
                                   "-RestorePointType MODIFY_SETTINGS"], 150)
        if ok2:
            self._rlog("  → ✅ 복원 지점을 생성했습니다.")
        else:
            self._rlog("  → ❌ 생성 실패: " + (out2[:250] if out2 else ""))
            self._rlog("     · Windows 정책상 24시간에 한 번만 생성됩니다(이미 있으면 실패).")
            self._rlog("     · 그래도 안 되면 제어판 > 시스템 > 시스템 보호에서 켜져 있는지 확인하세요.")

    def _repair_temp(self):
        self._rlog("🧹 임시 파일(%TEMP%) 정리   (진행 중)")
        fn = getattr(repair, "clear_temp", None)
        ok, msg = fn() if fn else (False, "준비 안됨")
        self._rlog("  → " + ("✅ " if ok else "❌ ") + (msg or ""))

    def _repair_updatecache_log(self):
        if not diagnostic.is_admin():
            self._rlog("❌ 관리자 권한이 필요합니다. [🔺 관리자 권한으로 재시작] 후 실행하세요.")
            return
        self._rlog("🔄 Windows Update 캐시 정리")
        self._rlog("  wuauserv·bits 서비스 중지 → SoftwareDistribution\\Download 삭제 → 서비스 재시작")
        fn = getattr(repair, "clear_update_cache", None)
        ok, msg = fn() if fn else (False, "준비 안됨")
        self._rlog("  → " + ("✅ " if ok else "❌ ") + (msg or ""))

    def _find_scanpst(self):
        import glob
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pfx = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        for base in (pf, pfx):
            for pat in (os.path.join(base, "Microsoft Office", "root", "Office*", "SCANPST.EXE"),
                        os.path.join(base, "Microsoft Office", "Office*", "SCANPST.EXE")):
                hits = glob.glob(pat)
                if hits:
                    return hits[0]
        return None

    def _find_pst(self):
        import glob
        dirs = [os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Outlook"),
                os.path.join(os.environ.get("USERPROFILE", ""), "Documents", "Outlook Files")]
        out = []
        for dd in dirs:
            for ext in ("*.pst", "*.ost"):
                out += glob.glob(os.path.join(dd, ext))
        return out

    def _repair_outlook(self):
        import subprocess
        self._rlog("📧 Outlook 데이터파일(PST/OST) 복구")
        sp = self._find_scanpst()
        if not sp:
            self._rlog("  ❌ scanpst.exe(받은편지함 복구 도구)를 찾지 못했습니다.")
            self._rlog("     Microsoft Office(Outlook)가 설치돼 있어야 합니다.")
            return
        self._rlog("  복구 도구: " + sp)
        psts = self._find_pst()
        if psts:
            self._rlog("  발견된 데이터 파일:")
            for p in psts[:10]:
                try:
                    sz = "%d MB" % (os.path.getsize(p) // (1024 * 1024))
                except Exception:
                    sz = "?"
                self._rlog("    · %s  (%s)" % (p, sz))
        else:
            self._rlog("  (기본 위치에서 PST/OST 를 못 찾음 — 도구에서 [찾아보기]로 직접 선택하세요)")
        try:
            subprocess.Popen([sp])
            self._rlog("")
            self._rlog("  ▶ 복구 도구 창을 열었습니다.")
            self._rlog("    ① [찾아보기]로 위 파일 선택 → ② [시작] → ③ 오류 발견 시 [복구]")
            self._rlog("    (복구 전 원본 백업을 만들도록 기본 체크되어 있습니다)")
        except Exception as e:
            self._rlog("  ❌ 실행 오류: %s" % e)

    def _repair_stress(self):
        import threading as _th
        import hashlib
        dur = 30
        ncpu = os.cpu_count() or 4
        self._rlog("🔥 CPU 스트레스 테스트  ·  %d초 · %d 스레드(전체 코어 부하)" % (dur, ncpu))
        self._rlog("  ⚠️ 발열/불안정 확인용. 노트북은 충전 연결 권장. 온도 90°C 도달 시 자동 중단.")
        self._rlog("")
        stop = {"v": False}

        def worker():
            data = b"x" * 8192
            while not stop["v"]:
                for _ in range(500):
                    hashlib.sha256(data).digest()   # C 연산(GIL 해제) → 실제 멀티코어 부하

        threads = [_th.Thread(target=worker, daemon=True) for _ in range(ncpu)]
        for th in threads:
            th.start()
        t0 = time.time()
        maxcpu = 0
        maxtemp = 0
        try:
            while time.time() - t0 < dur:
                time.sleep(3)
                snap = self.mon.snapshot()
                cpu = snap.get("cpu") or 0
                temp = (snap.get("temps") or {}).get("CPU")
                maxcpu = max(maxcpu, cpu)
                if temp:
                    maxtemp = max(maxtemp, temp)
                el = int(time.time() - t0)
                self._rlog("  %2d초 · CPU %d%%%s" % (el, cpu, ("  ·  %d°C" % temp) if temp else ""))
                if temp and temp >= 90:
                    self._rlog("  ⚠️ CPU 온도 90°C 도달 — 안전을 위해 중단합니다.")
                    break
        finally:
            stop["v"] = True
            time.sleep(0.5)
        self._rlog("")
        self._rlog("  결과: 최대 CPU %d%%%s" % (maxcpu, ("  ·  최대 온도 %d°C" % maxtemp) if maxtemp
                                              else "  ·  (CPU 온도는 관리자+PawnIO 필요)"))
        self._rlog("  → ✅ 크래시·멈춤 없이 완료 — 안정적입니다.")

    def _repair_speedtest(self):
        import urllib.request
        self._rlog("🚀 인터넷 속도 측정  (Cloudflare)")
        self._rlog("")
        self._rlog("▶ 지연시간(핑)")
        lat = []
        for _ in range(4):
            try:
                t = time.time()
                urllib.request.urlopen(urllib.request.Request(
                    "https://speed.cloudflare.com/__down?bytes=1000",
                    headers={"User-Agent": _UA}), timeout=10).read()
                lat.append((time.time() - t) * 1000)
            except Exception:
                pass
        if not lat:
            self._rlog("  ❌ 연결 실패 — 인터넷 연결을 확인하세요.")
            return
        self._rlog("  최소 %.0f ms · 평균 %.0f ms" % (min(lat), sum(lat) / len(lat)))
        self._rlog("")
        self._rlog("▶ 다운로드 (약 25MB 받아 측정 · 잠시 대기)")
        try:
            t = time.time()
            data = urllib.request.urlopen(urllib.request.Request(
                "https://speed.cloudflare.com/__down?bytes=25000000",
                headers={"User-Agent": _UA}), timeout=60).read()
            dt = max(0.05, time.time() - t)
            self._rlog("  ↓ %.1f Mbps   (%.1f MB / %.1f초)"
                       % ((len(data) * 8) / dt / 1e6, len(data) / 1e6, dt))
        except Exception as e:
            self._rlog("  ❌ 다운로드 측정 실패: %s" % e)
        self._rlog("")
        self._rlog("▶ 업로드 (약 10MB 전송하여 측정)")
        try:
            payload = b"0" * 10000000
            t = time.time()
            urllib.request.urlopen(urllib.request.Request(
                "https://speed.cloudflare.com/__up", data=payload,
                headers={"User-Agent": _UA, "Content-Type": "application/octet-stream"}), timeout=60).read()
            dt = max(0.05, time.time() - t)
            self._rlog("  ↑ %.1f Mbps   (%.1f MB / %.1f초)"
                       % ((len(payload) * 8) / dt / 1e6, len(payload) / 1e6, dt))
        except Exception as e:
            self._rlog("  ❌ 업로드 측정 실패: %s" % e)
        self._rlog("")
        self._rlog("  → ✅ 측정 완료")

    # ---- 관리자 권한으로 자기 자신을 재시작 (UAC 승격) ----
    def restart_as_admin(self):
        import subprocess
        if diagnostic.is_admin():
            return "이미 관리자 권한으로 실행 중입니다."
        self._log("관리자 권한으로 재시작 요청")
        try:
            # 재시작해도 접속 코드가 바뀌지 않도록 현재 코드를 환경변수로 넘긴다.
            os.environ["REMOTE_CODE"] = self.pin
            if getattr(sys, "frozen", False):
                target = sys.executable.replace("'", "''")
                ps = "Start-Process -FilePath '%s' -Verb RunAs" % target
            else:
                target = sys.executable.replace("'", "''")
                script = os.path.abspath(sys.argv[0]).replace("'", "''")
                ps = "Start-Process -FilePath '%s' -ArgumentList '%s' -Verb RunAs" % (target, script)
            subprocess.Popen(["powershell", "-NoProfile", "-Command", ps],
                             creationflags=0x08000000)
        except Exception as e:
            return "재시작 실패: %s" % e

        def _bye():
            time.sleep(2.5)                 # 응답 전송 + UAC 승격 요청이 걸릴 여유
            self._close_my_window()         # 이 인스턴스의 Edge 창(옛 창)을 닫음
            os._exit(0)
        threading.Thread(target=_bye, daemon=True).start()
        return "관리자 권한으로 재시작합니다. UAC 창에서 '예'를 누르면 이 창은 닫히고 관리자 창이 열립니다."

    def _close_my_window(self):
        """이 인스턴스가 띄운 Edge 앱창만 종료 (명령줄에 내 포트가 있음)."""
        import subprocess
        if not self.port:
            return
        try:
            ps = ("Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
                  "Where-Object { $_.CommandLine -match '127.0.0.1:%d' } | "
                  "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" % self.port)
            subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           timeout=8, creationflags=0x08000000)
        except Exception:
            pass

    # ---- 고급 센서(PawnIO) : CPU 온도·전압·전력 활성화 ----
    def enable_advanced_sensors(self):
        # 출력을 캡처/디코딩하면 winget(UTF-8) 이 깨지므로, 설치 창을 그대로 띄운다.
        import subprocess
        self._log("고급 센서 활성화(PawnIO 설치) 시도")
        manual = "수동 설치: https://github.com/namazso/PawnIO.Setup/releases 에서 PawnIO_setup.exe 다운로드 후 실행"
        try:
            if diagnostic.is_admin():
                # 관리자 → winget 설치 창을 띄움(창이 뜨고 진행됨)
                subprocess.Popen(
                    ["winget", "install", "--id", "namazso.PawnIO", "-e",
                     "--accept-package-agreements", "--accept-source-agreements"])
            else:
                # 비관리자 → UAC 승인받아 관리자 winget 창에서 설치
                ps = ("Start-Process winget -Verb RunAs -ArgumentList "
                      "'install --id namazso.PawnIO -e --accept-package-agreements --accept-source-agreements'")
                subprocess.Popen(["powershell", "-NoProfile", "-Command", ps],
                                 creationflags=0x08000000)
        except Exception as e:
            return "[고급 센서] 자동 설치 실행 실패: %s\n%s" % (e, manual)
        return ("[고급 센서] 설치 창을 띄웠습니다.\n"
                "① UAC 창이 뜨면 '예', winget(검정 창)에서 설치가 끝날 때까지 기다리세요.\n"
                "② 설치 후 이 프로그램을 종료하고 '관리자 권한으로 실행'하면\n"
                "   CPU 온도·전압, 그리고 (메인보드가 지원하면) 레일 전압이 표시됩니다.\n"
                "· 자동 설치가 안 되면 → " + manual)

    # ---- 호스트(원격 접속 대기) ----
    def _screen_size(self):
        """주 모니터 해상도(논리 픽셀). host.py 의 tk winfo_screen* 대체."""
        try:
            import ctypes
            u = ctypes.windll.user32
            return u.GetSystemMetrics(0), u.GetSystemMetrics(1)
        except Exception:
            return 1920, 1080

    def start_host(self):
        """시작 시 1회 호출 — 호스트 역할 WebRTC 세션을 백그라운드에서 자동 시작."""
        if self.host_running:
            return
        self.host_running = True
        threading.Thread(target=self._host_loop, daemon=True).start()
        self._log("원격 접속 대기 시작 (코드 %s)" % self.pin)

    def _host_loop(self):
        # host.py 의 _webrtc_host_loop 과 동일 — 자체 asyncio 루프를 가진 스레드에서
        # webrtc_app.host_run 을 반복 호출한다.
        import asyncio
        self._set_host_status("대기 중 - 접속 코드 %s" % self.pin)

        async def runner():
            while self.host_running:
                connected_this_round = [False]

                def tracking_status(s, _box=connected_this_round):
                    if "제어 중" in s:
                        _box[0] = True
                    self._set_host_status(s)

                try:
                    await webrtc_app.host_run(
                        self.relay, self.pin, self._apply_input,
                        tracking_status, lambda: self.host_running,
                        bridge=self.bridge, client_info_fn=self._client_info,
                        extra_fn=self._extra_diag_info, touch_fn=self.touch)
                except Exception as e:
                    self._set_host_status("연결 오류, 재시도...")
                    self._log("원격 연결 오류: %s: %s" % (type(e).__name__, e))

                if connected_this_round[0]:
                    reason = self.bridge.pop("last_p2p_state", None)
                    if reason:
                        self._log("P2P 연결 끊김 (상태: %s)" % reason)

                if self.host_running:
                    self._set_host_status("대기 중 - 접속 코드 %s" % self.pin)
                    await asyncio.sleep(1)

        try:
            asyncio.run(runner())
        except Exception:
            pass

    def _set_host_status(self, s):
        self._hstatus = s
        if "원격 제어 중" in s or "연결됨" in s:
            self._connected = True
        elif ("대기" in s) or ("실패" in s) or ("거부" in s) or ("오류" in s) or ("협상" in s):
            self._connected = False

    def host_status(self):
        return {"code": self.pin, "connected": self._connected,
                "mode": self.mode, "status": self._hstatus}

    # ---- 원격 입력 실행 (기사 PC 가 보낸 마우스/키보드) — host.py 와 동일 ----
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


# ---- 파일 저장 대화상자 (tkinter — pythonnet 아님, 안전) ----
def _save_dialog(default_name):
    try:
        import tkinter
        from tkinter import filedialog
        r = tkinter.Tk()
        r.withdraw()
        r.attributes("-topmost", True)
        p = filedialog.asksaveasfilename(parent=r, title="리포트 저장",
                                         initialfile=default_name,
                                         defaultextension=".html",
                                         filetypes=[("HTML", "*.html")])
        r.destroy()
        return p or None
    except Exception:
        return None


# ---- 세부 진단 섹션 HTML ----
def _row(k, v):
    return ('<div class="drow"><span class="dk">%s</span><span class="dv">%s</span></div>'
            % (_esc(str(k)), _esc(str(v))))


def _card(title, rows_html, icon="", cls=""):
    head = ((icon + " ") if icon else "") + _esc(str(title))
    return ('<div class="dcard%s"><h3>%s</h3><div class="dbody">%s</div></div>'
            % ((" " + cls) if cls else "", head, rows_html))


def _fmt_kb(kb):
    try:
        kb = int(kb)
    except Exception:
        return str(kb)
    return "%d MB" % (kb // 1024) if kb >= 1024 else "%d KB" % kb


def _bar(pct, kind="load"):
    try:
        p = max(0.0, min(100.0, float(pct)))
    except Exception:
        p = 0.0
    if kind == "temp":
        col = "#c67c5f" if p >= 80 else ("#d3a457" if p >= 65 else "#8a9c6a")
    elif kind == "disk":
        col = "#c67c5f" if p >= 90 else ("#d3a457" if p >= 75 else "#8a9c6a")
    else:
        col = "#c67c5f" if p >= 90 else ("#d3a457" if p >= 70 else "#bd8f5d")
    return '<span class="rbtrack"><i style="width:%d%%;background:%s"></i></span>' % (int(p), col)


def _rowbar(k, pct, text, kind="load"):
    return ('<div class="drow rb"><span class="dk">%s</span>'
            '<span class="rbwrap">%s<span class="dv">%s</span></span></div>'
            % (_esc(str(k)), _bar(pct, kind), _esc(str(text))))


def _badge(text, ok=True):
    return '<span class="badge %s">%s</span>' % ("bok" if ok else "bbad", _esc(str(text)))


def _rowbadge(k, text, ok=True):
    return ('<div class="drow"><span class="dk">%s</span><span class="dv">%s</span></div>'
            % (_esc(str(k)), _badge(text, ok)))


def _cleanlbl(s):
    s = str(s)
    for a in ("GPU GPU", "CPU CPU", "MB MB"):
        s = s.replace(a, a.split()[0])
    return s


def _psu_rails(volts):
    """메인보드 전압 센서에서 PSU 레일(+12V/+5V/+3.3V) 추출 → {name:(값, 하한, 상한)}."""
    rails = {}
    for k, v in (volts or {}).items():
        kl = str(k).lower().replace(" ", "").replace("+", "")
        try:
            fv = float(v)
        except Exception:
            continue
        if "12v" in kl:
            rails.setdefault("+12V", (fv, 11.4, 12.6))
        elif "3.3v" in kl or "3v3" in kl or "3vcc" in kl:
            rails.setdefault("+3.3V", (fv, 3.14, 3.47))
        elif "5v" in kl:
            rails.setdefault("+5V", (fv, 4.75, 5.25))
    return rails


def _diag_card(findings):
    """세부 탭 상단 진단 결과 카드. findings: [(level, text)], level in ('warn','info')."""
    warns = [x for x in findings if x[0] == "warn"]
    infos = [x for x in findings if x[0] == "info"]
    if warns:
        status = _badge("경고 %d건" % len(warns), False)
    elif infos:
        status = '<span class="badge binfo">주의 %d건</span>' % len(infos)
    else:
        status = _badge("정상", True)
    body = ('<div class="drow"><span class="dk">종합 판정</span><span class="dv">%s</span></div>' % status)
    if findings:
        for lv, tx in findings:
            ic = "⛔" if lv == "warn" else "⚠️"
            body += '<div class="fitem %s">%s %s</div>' % (lv, ic, _esc(str(tx)))
    else:
        body += '<div class="fitem ok">✅ 특이사항 없음 — 정상 범위입니다.</div>'
    return _card("진단 결과", body, "🩺")


def _section_html(name, d):
    try:
        if name == "cpu":
            c = d.get("cpu", {})
            t = d.get("temps", {})
            pw = d.get("powers", {})
            r = _row("모델", c.get("name", "?"))
            if c.get("manufacturer"):
                r += _row("제조사", c["manufacturer"])
            if c.get("socket"):
                r += _row("소켓", c["socket"])
            r += _row("코어 / 스레드", "%s코어 · %s스레드"
                      % (c.get("cores_physical", "?"), c.get("cores_logical", "?")))
            out = _card("프로세서", r, "🧠")
            r2 = ""
            if c.get("cur_clock_mhz"):
                r2 += _row("현재 클럭", "%s MHz" % c["cur_clock_mhz"])
            if c.get("max_clock_mhz"):
                r2 += _row("최대 클럭", "%s MHz" % c["max_clock_mhz"])
            if not (c.get("cur_clock_mhz") or c.get("max_clock_mhz")) and c.get("freq_mhz"):
                r2 += _row("클럭", "%s MHz" % c["freq_mhz"])
            if c.get("l2_kb"):
                r2 += _row("L2 캐시", _fmt_kb(c["l2_kb"]))
            if c.get("l3_kb"):
                r2 += _row("L3 캐시", _fmt_kb(c["l3_kb"]))
            if r2:
                out += _card("클럭 · 캐시", r2, "⏱️")
            r3 = ""
            if c.get("usage_percent") is not None:
                r3 += _rowbar("사용률", c["usage_percent"], "%s%%" % c["usage_percent"])
            if t.get("CPU") is not None:
                r3 += _rowbar("온도", t["CPU"], "%s°C" % t["CPU"], "temp")
            else:
                r3 += _row("온도", "관리자 + PawnIO 필요")
            r3 += _row("전압", ("%.3f V" % c["voltage_v"]) if c.get("voltage_v") is not None else "관리자 + PawnIO 필요")
            cpupw = pw.get("CPU Package") or pw.get("CPU Cores")
            r3 += _row("전력", ("%s W" % cpupw) if cpupw else "관리자 + PawnIO 필요")
            out += _card("상태 · 전압 · 전력", r3, "⚡")
            out += ('<div class="dcard"><h3>🔬 코어별 실시간 로드</h3><div class="dbody">'
                    '<div class="corewrap" id="cpucores"><div class="muted">측정 중…</div></div>'
                    '</div></div>')
            if t.get("CPU") is None or c.get("voltage_v") is None:
                step2 = ('' if d.get("admin")
                         else '<button class="btn pri" onclick="restartAdmin()">🔺 관리자 권한으로 재시작</button>')
                out += ('<div class="dcard notice"><h3>🔧 CPU 온도·전압 활성화</h3><div class="dbody">'
                        '<div class="ninfo">Intel CPU의 온도·전압·전력은 <b>PawnIO 드라이버 + 관리자 권한</b>이 있어야 읽힙니다. '
                        '① PawnIO 설치 → ② 관리자 권한으로 재시작. (전원 탭에서도 가능)</div>'
                        '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:12px">'
                        '<button class="btn pri" onclick="enableSensors()">PawnIO 설치</button>' + step2 + '</div>'
                        '<div id="sensout" style="margin-top:10px;white-space:pre-wrap;font-size:12.5px;color:var(--sub)"></div>'
                        '</div></div>')
            f = []
            if t.get("CPU") is not None:
                if t["CPU"] >= 90:
                    f.append(("warn", "CPU 온도 매우 높음 (%d°C) — 쿨링·먼지 점검 필요" % t["CPU"]))
                elif t["CPU"] >= 80:
                    f.append(("info", "CPU 온도 다소 높음 (%d°C)" % t["CPU"]))
            if c.get("usage_percent") is not None and c["usage_percent"] >= 90:
                f.append(("info", "CPU 사용률 높음 (%s%%) — 백그라운드 프로그램 확인" % c["usage_percent"]))
            return _diag_card(f) + out

        if name == "gpu":
            cards = ""
            for g in d.get("gpu", []):
                r = _row("모델", g.get("name", "?"))
                if g.get("driver"):
                    r += _row("드라이버", g["driver"])
                if g.get("resolution"):
                    r += _row("해상도", g["resolution"])
                if "vram_total_gb" in g:
                    r += _row("VRAM", "%s / %s GB" % (g.get("vram_used_gb", "?"), g["vram_total_gb"]))
                elif g.get("adapter_ram_gb"):
                    r += _row("VRAM", "%s GB" % g["adapter_ram_gb"])
                cards += _card(g.get("name", "그래픽"), r, "🎮")
                r2 = ""
                if g.get("core_clock_mhz"):
                    r2 += _row("코어 클럭", "%s MHz" % g["core_clock_mhz"])
                if g.get("mem_clock_mhz"):
                    r2 += _row("메모리 클럭", "%s MHz" % g["mem_clock_mhz"])
                if g.get("power_w") is not None:
                    r2 += _row("전력", "%s W" % g["power_w"])
                if g.get("voltage_v") is not None:
                    r2 += _row("전압", "%s V" % g["voltage_v"])
                if r2:
                    cards += _card("클럭 · 전력 · 전압", r2, "⚡")
                r3 = ""
                if "temp" in g:
                    r3 += _rowbar("온도", g["temp"], "%s°C" % g["temp"], "temp")
                if "load_percent" in g:
                    r3 += _rowbar("로드", g["load_percent"], "%s%%" % g["load_percent"])
                if r3:
                    cards += _card("상태", r3, "📊")
            gf = []
            for g in d.get("gpu", []):
                tv = g.get("temp")
                if tv is not None:
                    if tv >= 90:
                        gf.append(("warn", "GPU 온도 매우 높음 (%d°C)" % tv))
                    elif tv >= 83:
                        gf.append(("info", "GPU 온도 다소 높음 (%d°C)" % tv))
            return _diag_card(gf) + (cards or _card("그래픽", '<div class="drow">정보 없음</div>'))

        if name == "mem":
            m = d.get("memory", {})
            mm = d.get("memory_modules", {}) or {}
            r = _row("총 용량", "%s GB" % m.get("total_gb", "?"))
            if mm.get("type"):
                r += _row("타입", mm["type"])
            if mm.get("modules"):
                r += _row("장착 슬롯", "%d개" % len(mm["modules"]))
            if m.get("percent") is not None:
                r += _rowbar("사용률", m["percent"], "%s GB · %s%%" % (m.get("used_gb", "?"), m["percent"]))
            r += _row("여유", "%s GB" % m.get("available_gb", "?"))
            out = _card("메모리 개요", r, "🎛️")
            for mod in mm.get("modules", []):
                rr = _row("용량", "%s GB" % mod.get("size_gb", "?"))
                sp = mod.get("configured_mhz") or mod.get("speed_mhz")
                if sp:
                    rr += _row("속도", "%s MHz" % sp)
                if mod.get("type"):
                    rr += _row("타입", mod["type"])
                if mod.get("manufacturer"):
                    rr += _row("제조사", mod["manufacturer"])
                if mod.get("part"):
                    rr += _row("파트넘버", mod["part"])
                out += _card("슬롯 " + str(mod.get("slot", "?")), rr, "💠")
            sw = d.get("swap") or {}
            if sw.get("total_gb"):
                sr = _rowbar("사용률", sw.get("percent", 0),
                             "%s / %s GB" % (sw.get("used_gb", "?"), sw["total_gb"]))
                out += _card("가상 메모리 (페이지 파일)", sr, "📄")
            f = []
            if m.get("percent") is not None:
                if m["percent"] >= 90:
                    f.append(("warn", "메모리 부족 (%d%% 사용) — 프로그램 정리·증설 권장" % m["percent"]))
                elif m["percent"] >= 80:
                    f.append(("info", "메모리 사용률 높음 (%d%%)" % m["percent"]))
            return _diag_card(f) + out

        if name == "disk":
            dd = d.get("disks", {})
            cards = ""
            for v in dd.get("volumes", []):
                r = _rowbar("사용률", v.get("percent", 0), "%s%%" % v.get("percent", "?"), "disk")
                r += _row("용량", "%s GB" % v.get("total_gb", "?"))
                r += _row("사용", "%s GB" % v.get("used_gb", "?"))
                r += _row("여유", "%s GB" % v.get("free_gb", "?"))
                if v.get("fstype"):
                    r += _row("파일시스템", v["fstype"])
                cards += _card("드라이브 " + str(v.get("device", "")), r, "💾")
            for p in dd.get("physical", []):
                r = _row("종류", p.get("type", "?"))
                if p.get("bus"):
                    r += _row("인터페이스", p["bus"])
                r += _row("용량", "%s GB" % p.get("size_gb", "?"))
                health = str(p.get("health", "?"))
                r += _rowbadge("SMART 상태", health, health in ("정상", "OK", "Healthy"))
                cards += _card(p.get("model", "디스크"), r, "🗄️")
            for sm in d.get("smart", []):
                sr = ""
                if sm.get("temp_c") is not None:
                    sr += _rowbar("온도", sm["temp_c"], "%s°C" % sm["temp_c"], "temp")
                if sm.get("power_on_hours") is not None:
                    sr += _row("전원 인가 시간", "%s 시간" % sm["power_on_hours"])
                if sm.get("data_written_tb") is not None:
                    sr += _row("총 기록량", "%s TB" % sm["data_written_tb"])
                if sm.get("data_read_tb") is not None:
                    sr += _row("총 읽기량", "%s TB" % sm["data_read_tb"])
                if sm.get("wear_pct") is not None:
                    sr += _rowbar("남은 수명", sm["wear_pct"], "%s%%" % sm["wear_pct"])
                if sr:
                    cards += _card("SMART · " + str(sm.get("model", "디스크")), sr, "🩹")
            f = []
            for v in dd.get("volumes", []):
                pc = v.get("percent")
                if pc is not None:
                    if pc >= 95:
                        f.append(("warn", "%s 공간 매우 부족 (%s%%) — 정리 필요" % (v.get("device", ""), pc)))
                    elif pc >= 90:
                        f.append(("info", "%s 공간 부족 (%s%%)" % (v.get("device", ""), pc)))
            for p in dd.get("physical", []):
                h = str(p.get("health", "정상"))
                if h not in ("정상", "OK", "Healthy", "?"):
                    f.append(("warn", "%s 상태 이상 (%s) — 백업 권장" % (p.get("model", "디스크"), h)))
            for sm in d.get("smart", []):
                w = sm.get("wear_pct")
                if w is not None and w <= 20:
                    f.append(("warn", "%s 수명 임박 (남은 수명 %s%%) — 교체 준비" % (sm.get("model", "SSD"), w)))
                elif w is not None and w <= 40:
                    f.append(("info", "%s 남은 수명 %s%%" % (sm.get("model", "SSD"), w)))
            return _diag_card(f) + (cards or _card("저장장치", '<div class="drow">정보 없음</div>'))

        if name == "net":
            n = d.get("network", {})
            out = ""
            for a in n.get("adapters", []):
                ar = _row("IP 주소", a.get("ip", "?"))
                if a.get("speed_mbps"):
                    ar += _row("속도", "%s Mbps" % a["speed_mbps"])
                if a.get("mac"):
                    ar += _row("MAC 주소", a["mac"])
                if a.get("dhcp") is not None:
                    ar += _row("IP 할당", "DHCP(자동)" if a["dhcp"] else "고정 IP")
                if a.get("subnet"):
                    ar += _row("서브넷", a["subnet"])
                if a.get("ipv6"):
                    ar += _row("IPv6", a["ipv6"])
                if a.get("dns_servers"):
                    ar += _row("DNS 서버", ", ".join(a["dns_servers"][:3]))
                out += _card(a.get("name", "어댑터"), ar, "🔌")
            cr = ""
            if n.get("gateway"):
                cr += _row("게이트웨이", n["gateway"])
            if n.get("public_ip"):
                cr += _row("공인 IP", n["public_ip"])
            if n.get("dns_servers"):
                cr += _row("DNS 서버", ", ".join(n["dns_servers"][:3]))
            cr += _rowbadge("인터넷 연결", "정상" if n.get("internet") else "실패", bool(n.get("internet")))
            cr += _rowbadge("DNS 조회", "정상" if n.get("dns") else "실패", bool(n.get("dns")))
            out += _card("연결 상태", cr, "🌐")
            wifi = d.get("wifi")
            if wifi:
                wr = _row("저장된 Wi-Fi", "%s개" % wifi.get("count", 0))
                if wifi.get("profiles"):
                    wr += _row("목록", ", ".join(wifi["profiles"][:12]))
                out += _card("Wi-Fi", wr, "📶")
            f = []
            if not n.get("internet"):
                f.append(("warn", "인터넷 연결 안 됨 — [복구·수정 → 인터넷 복구] 시도"))
            elif not n.get("dns"):
                f.append(("info", "DNS 조회 실패 — DNS 설정 확인 권장"))
            return _diag_card(f) + (out or _card("네트워크", '<div class="drow">정보 없음</div>'))

        if name == "power":
            volts = d.get("volts") or {}
            pw = d.get("powers") or {}
            rails = _psu_rails(volts)
            RSPEC = [("+12V", 12.0, 11.4, 12.6), ("+5V", 5.0, 4.75, 5.25), ("+3.3V", 3.3, 3.14, 3.47)]
            # 1) 전원 레일 전압 (핵심 — 5V/12V/3.3V 가 정상 범위인지)
            if rails:
                rr = ""
                for nm, nominal, lo, hi in RSPEC:
                    if nm in rails:
                        v = rails[nm][0]
                        okr = lo <= v <= hi
                        rr += _rowbadge("%s  (정격 %.1fV · 정상 %.2f~%.2f)" % (nm, nominal, lo, hi),
                                        ("%.3f V · 정상" % v) if okr else ("%.3f V · 범위 벗어남" % v), okr)
                out = _card("전원 레일 전압", rr, "🔌")
            else:
                out = _card("전원 레일 전압",
                            '<div class="fitem warn">⛔ +12V · +5V · +3.3V 레일 전압이 측정되지 않습니다.</div>'
                            '<div style="padding-top:8px;color:var(--sub);font-size:13px">메인보드 전압 센서를 읽으려면 '
                            '<b>PawnIO 드라이버 + 관리자 권한</b>이 필요합니다. 아래 버튼으로 설치·재시작하세요.<br>'
                            '※ 일부 보급형 메인보드(예: H610)는 드라이버가 있어도 레일 전압 센서를 제공하지 않을 수 있습니다.</div>', "🔌")
            # 2) 감지된 전압 센서 전체 (보드가 실제로 내주는 값 — 숨김 없이)
            if volts:
                vr = "".join(_row(_cleanlbl(k), "%s V" % v) for k, v in list(volts.items())[:20])
                out += _card("감지된 전압 센서 (전체)", vr, "⚡")
            # 3) 실측 전력 (참고용)
            wr = "".join(_row(_cleanlbl(k), "%s W" % v) for k, v in list(pw.items())[:10] if v)
            if wr:
                out += _card("전력 센서 (실측)", wr, "🔋")
            # 4) 레일이 안 보이면 활성화 안내
            if not rails:
                if d.get("admin"):
                    admline = '현재 <b>관리자 모드 ✓</b> — PawnIO 설치 후 [새로고침]하면 값이 나옵니다.'
                    step2 = ('<button class="btn" onclick="api(\'diag_refresh\').then(function(){loadSection(\'power\')})">'
                             '↻ 새로고침</button>')
                else:
                    admline = '순서: <b>① PawnIO 설치 → ② 관리자 권한으로 재시작</b> (아래 버튼으로 한 번에)'
                    step2 = '<button class="btn pri" onclick="restartAdmin()">🔺 관리자 권한으로 재시작</button>'
                out += ('<div class="dcard notice"><h3>🔧 레일 전압 센서 활성화</h3><div class="dbody">'
                        '<div class="ninfo">' + admline + '</div>'
                        '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:12px">'
                        '<button class="btn pri" onclick="enableSensors()">PawnIO 설치</button>' + step2 + '</div>'
                        '<div id="sensout" style="margin-top:10px;white-space:pre-wrap;font-size:12.5px;color:var(--sub)"></div>'
                        '</div></div>')
            # 진단 판정
            f = []
            if not rails:
                f.append(("info", "레일 전압(+12V·+5V·+3.3V) 측정 불가 — PawnIO+관리자 필요, 또는 메인보드 미지원"))
            else:
                for nm, nominal, lo, hi in RSPEC:
                    if nm in rails and not (lo <= rails[nm][0] <= hi):
                        f.append(("warn", "%s 전압 이상 (%.3fV, 정상 %.2f~%.2f) — PSU 점검 권장"
                                  % (nm, rails[nm][0], lo, hi)))
            return _diag_card(f) + out

        if name == "system":
            s = d.get("system", {})
            r = _row("OS", "%s (빌드 %s)" % (s.get("edition", s.get("os", "?")), s.get("build", "?")))
            r += _row("호스트명", s.get("hostname", "?"))
            if s.get("owner"):
                r += _row("등록 사용자", s["owner"])
            if s.get("install_date"):
                r += _row("설치일", s["install_date"])
            if s.get("last_boot"):
                r += _row("마지막 부팅", s["last_boot"])
            if s.get("uptime_hours") is not None:
                r += _row("가동시간", "%s 시간" % s["uptime_hours"])
            if s.get("timezone"):
                r += _row("시간대", s["timezone"])
            out = _card("운영체제", r, "🪟")
            hs = analysis.hardware_suspicion(d)
            if hs:
                hr = ""
                for it in hs:
                    lv = it.get("level", "중간")
                    hr += ('<div class="fitem %s">%s <b>%s</b> · %s</div>'
                           % ("warn" if lv == "높음" else "info",
                              "⛔" if lv == "높음" else "⚠️",
                              _esc(it.get("part", "")), _esc(it.get("reason", ""))))
                out += _card("하드웨어 고장 의심 (100%% 확정 아님)".replace("%%", "%"), hr, "🔬")
            else:
                out += _card("하드웨어 고장 의심",
                             '<div class="fitem ok">✅ 하드웨어 고장 의심 신호 없음 (수집 데이터 기준)</div>', "🔬")
            mb = d.get("mainboard")
            if mb:
                mr = ""
                bd = ("%s %s" % (mb.get("board_mfr", ""), mb.get("board_model", ""))).strip()
                if bd:
                    mr += _row("메인보드", bd)
                if mb.get("bios_version"):
                    bios = ("%s %s" % (mb.get("bios_vendor", ""), mb["bios_version"])).strip()
                    if mb.get("bios_date"):
                        bios += " (%s)" % mb["bios_date"]
                    mr += _row("BIOS", bios)
                if mr:
                    out += _card("메인보드 · BIOS", mr, "🧩")
            disp = d.get("displays") or []
            if disp:
                dr = ""
                for mn in disp:
                    val = mn.get("resolution", "?")
                    if mn.get("refresh_hz"):
                        val += " @%sHz" % mn["refresh_hz"]
                    dr += _row(mn.get("manufacturer") or mn.get("name", "모니터"), val)
                out += _card("디스플레이", dr, "🖥️")
            secu = d.get("security") or {}
            if secu:
                xr = ""
                if secu.get("tpm"):
                    xr += _row("TPM", secu["tpm"])
                if secu.get("secure_boot") is not None:
                    xr += _rowbadge("보안 부팅", "켜짐" if secu["secure_boot"] else "꺼짐", bool(secu["secure_boot"]))
                if secu.get("activation"):
                    xr += _rowbadge("Windows 정품", secu["activation"], bool(secu.get("activated")))
                if xr:
                    out += _card("보안 (TPM · 부팅 · 정품)", xr, "🔐")
            bat = d.get("battery") or {}
            if isinstance(bat, dict) and bat.get("percent") is not None:
                br = _rowbar("잔량", bat["percent"], "%s%%%s" % (bat["percent"], " · 충전 중" if bat.get("plugged") else ""))
                if bat.get("health_pct") is not None:
                    br += _row("배터리 수명", "%s%%" % bat["health_pct"])
                if bat.get("cycle_count") is not None:
                    br += _row("충전 사이클", "%s회" % bat["cycle_count"])
                if bat.get("full_wh") and bat.get("design_wh"):
                    br += _row("용량(현재/설계)", "%s / %s Wh" % (bat["full_wh"], bat["design_wh"]))
                out += _card("배터리", br, "🔋")
            comp = ""
            dn = d.get("dotnet")
            if dn:
                comp += _row(".NET Framework", dn.get("framework", "?"))
                if dn.get("runtimes") is not None:
                    comp += _row(".NET 런타임", "%d개" % len(dn.get("runtimes", [])))
            vc = d.get("vcredist")
            if vc is not None:
                comp += _row("VC++ 재배포", "%d개" % len(vc))
            dx = d.get("directx")
            if dx:
                comp += _row("DirectX", dx.get("version", "?"))
            wh = d.get("wmi_health")
            if wh:
                st = wh.get("status", "?")
                comp += _rowbadge("WMI 저장소", st, st == "정상")
            pw = d.get("power")
            if pw:
                fs = pw.get("fast_startup")
                comp += _row("전원 플랜", pw.get("plan", "?"))
                comp += _row("빠른 시작", "켜짐" if fs else ("꺼짐" if fs is not None else "?"))
            rc = d.get("recovery")
            if rc:
                comp += _row("복구환경(WinRE)", rc.get("winre", "?"))
            if comp:
                out += _card("시스템 구성 요소", comp, "⚙️")
            bs = d.get("bluescreen") or {}
            mds = bs.get("minidumps") or []
            evs = bs.get("events") or []
            nbsod = max(len(mds), len(evs))
            if nbsod:
                br = _rowbadge("블루스크린 기록", "%d회" % nbsod, False)
                for e in evs[:5]:
                    br += _row(e.get("time", "?"), "%s  %s" % (e.get("code", ""), e.get("name", "")))
                if not evs:
                    for m in mds[:5]:
                        br += _row(m.get("date", "?"), "덤프: " + m.get("file", ""))
                out += _card("블루스크린(BSOD) 이력", br, "💥")
            else:
                out += _card("블루스크린(BSOD) 이력", _rowbadge("최근 기록", "없음 (정상)", True), "💥")
            accts = d.get("accounts")
            if accts:
                ar = ""
                for a in accts[:12]:
                    stx = "활성" if a.get("enabled") else "비활성"
                    if a.get("lockout"):
                        stx += " · 잠김"
                    if a.get("admin"):
                        stx += " · 관리자"
                    ar += _row(a.get("name", "?"), stx)
                out += _card("로컬 계정", ar, "👤")
            sec = ""
            df = d.get("defender")
            if df:
                sec += _row("백신", df.get("antivirus", "?"))
                rt = df.get("realtime")
                sec += _rowbadge("실시간 보호", "켜짐" if rt else ("꺼짐" if rt is not None else "?"), bool(rt))
            rdp = d.get("rdp")
            if rdp:
                e = rdp.get("enabled")
                sec += _row("원격 데스크톱(RDP)", "사용" if e else ("사용 안 함" if e is not None else "?"))
            rst = d.get("restore")
            if rst:
                sec += _row("시스템 복원",
                            ("사용 · 지점 %d개" % rst.get("count", 0)) if rst.get("enabled") else "사용 안 함/없음")
            db = d.get("default_browser")
            if db:
                sec += _row("기본 브라우저", db.get("browser", "?"))
            sh = d.get("shares")
            if sh is not None:
                sec += _row("공유 폴더", "%d개" % len(sh))
            s2 = d.get("security2") or {}
            if s2.get("firewall"):
                sec += _rowbadge("방화벽", s2["firewall"], s2["firewall"] == "켜짐")
            if s2.get("uac"):
                sec += _rowbadge("UAC(사용자 계정 컨트롤)", s2["uac"], s2["uac"] == "켜짐")
            if s2.get("bitlocker"):
                sec += _row("BitLocker(C:)", s2["bitlocker"])
            if s2.get("smb1"):
                sec += _rowbadge("SMBv1", s2["smb1"], "비활성" in s2["smb1"])
            if s2.get("guest"):
                sec += _rowbadge("게스트 계정", s2["guest"], "비활성" in s2["guest"])
            if "autologin" in s2:
                sec += _rowbadge("자동 로그인", "켜짐(위험)" if s2["autologin"] else "꺼짐", not s2["autologin"])
            if s2.get("hosts_entries") is not None:
                he = s2["hosts_entries"]
                sec += _rowbadge("hosts 파일", ("변조 의심 (%d개 항목)" % he) if he > 0 else "정상(기본)", he == 0)
            if sec:
                out += _card("보안 · 공유", sec, "🛡️")
            f = []
            pdv = d.get("problem_devices") or []
            if pdv:
                f.append(("warn", "장치 문제 %d건 — 드라이버 확인 필요" % len(pdv)))
            if (d.get("wmi_health") or {}).get("status") == "손상":
                f.append(("warn", "WMI 저장소 손상 — 복구 필요"))
            if (d.get("defender") or {}).get("realtime") is False:
                f.append(("info", "실시간 보호 꺼짐 — 보안 점검 권장"))
            if (d.get("recovery") or {}).get("winre") == "사용안함":
                f.append(("info", "복구환경(WinRE) 비활성 — 시스템 복구 제한"))
            ssv = d.get("services_stopped") or []
            if isinstance(ssv, list) and len(ssv) >= 5:
                f.append(("info", "자동 시작 서비스 %d개 중지됨" % len(ssv)))
            bat2 = d.get("battery") or {}
            if isinstance(bat2, dict) and bat2.get("health_pct") is not None and bat2["health_pct"] < 70:
                f.append(("info", "배터리 노후 (수명 %s%%) — 교체 고려" % bat2["health_pct"]))
            se2 = d.get("security") or {}
            if se2.get("secure_boot") is False:
                f.append(("info", "보안 부팅 꺼짐"))
            if se2.get("activated") is False:
                f.append(("info", "Windows 정품 미인증 상태"))
            s2 = d.get("security2") or {}
            if s2.get("firewall") == "꺼짐":
                f.append(("warn", "Windows 방화벽 꺼짐 — 보안 위험"))
            if (s2.get("uac") or "").startswith("꺼짐"):
                f.append(("warn", "UAC 꺼짐 — 악성코드 권한 상승 위험"))
            if "활성" in (s2.get("smb1") or ""):
                f.append(("warn", "SMBv1 활성 — 랜섬웨어(워너크라이) 취약, 비활성 권장"))
            if "활성" in (s2.get("guest") or ""):
                f.append(("info", "게스트 계정 활성 — 비활성 권장"))
            if s2.get("autologin"):
                f.append(("warn", "자동 로그인 켜짐 — 도난·무단접근 위험"))
            if (s2.get("hosts_entries") or 0) > 0:
                f.append(("info", "hosts 파일에 %d개 항목 — 변조/광고차단 여부 확인" % s2["hosts_entries"]))
            bs2 = d.get("bluescreen") or {}
            nb = max(len(bs2.get("minidumps") or []), len(bs2.get("events") or []))
            if nb:
                f.append(("warn", "블루스크린 %d회 기록 — 시스템 탭 BSOD 이력에서 원인 코드 확인" % nb))
                for e in (bs2.get("events") or []):
                    nm = e.get("name", "")
                    if ("하드웨어" in nm) or ("메모리 불량" in nm) or ("CPU" in nm):
                        f.append(("warn", "BSOD %s: %s → 하드웨어 점검 권장" % (e.get("code"), nm)))
                        break
            for hsit in (hs or []):
                if hsit.get("level") == "높음":
                    f.append(("warn", "하드웨어 의심: %s" % hsit.get("reason", "")))
            return _diag_card(f) + out

        return _card("정보", '<div class="drow">해당 섹션 없음</div>')
    except Exception as e:
        return _card("오류", '<div class="drow">%s</div>' % _esc(str(e)))


# ============================ HTTP 서버 ============================
_API = None
_HTML = b""
_ICON = b""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, (bytes, bytearray)) else str(body).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except Exception:
            pass

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, _HTML, "text/html; charset=utf-8")
        elif path in ("/favicon.ico", "/icon.png"):
            if _ICON:
                self._send(200, _ICON, "image/x-icon")
            else:
                self._send(404, b"", "text/plain")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            self._send(404, b"nf", "text/plain")
            return
        name = path[len("/api/"):]
        try:
            ln = int(self.headers.get("Content-Length") or 0)
        except Exception:
            ln = 0
        raw = self.rfile.read(ln) if ln else b"[]"
        try:
            args = json.loads(raw.decode("utf-8") or "[]")
        except Exception:
            args = []
        if not isinstance(args, list):
            args = [args]
        _API.touch()
        fn = None if name.startswith("_") else getattr(_API, name, None)
        if not callable(fn):
            self._send(404, json.dumps({"result": None}))
            return
        try:
            res = fn(*args)
        except Exception:
            res = None
        self._send(200, json.dumps({"result": res}))


def _app_path(exe):
    """레지스트리 App Paths 에서 실행파일 경로 조회."""
    try:
        import winreg
    except Exception:
        return None
    sub = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\\" + exe
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            k = winreg.OpenKey(hive, sub)
            v, _ = winreg.QueryValueEx(k, None)
            winreg.CloseKey(k)
            if v and os.path.exists(v):
                return v
        except Exception:
            pass
    return None


def _find_chromium():
    """Edge → Chrome 순으로 크로미엄 브라우저 경로를 찾는다. (앱 모드 지원)"""
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pfx = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    cands = [
        _app_path("msedge.exe"),
        os.path.join(pfx, r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(pf, r"Microsoft\Edge\Application\msedge.exe"),
        _app_path("chrome.exe"),
        os.path.join(pf, r"Google\Chrome\Application\chrome.exe"),
        os.path.join(pfx, r"Google\Chrome\Application\chrome.exe"),
        os.path.join(local, r"Google\Chrome\Application\chrome.exe") if local else None,
    ]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None


def _watchdog(api, timeout=30.0):
    # Edge 창을 닫으면 ping 이 끊긴다 → 일정 시간 활동 없으면 프로세스 종료.
    # (능동적 창 감지는 오탐으로 정상 실행 중인 앱까지 죽이는 문제가 있어 제거 — ping 기반 안전망만 사용)
    while True:
        time.sleep(3.0)
        if time.monotonic() - api.last_activity > timeout:
            os._exit(0)


def main():
    global _API, _HTML, _ICON
    _API = Api()
    _HTML = open(_res(os.path.join("webui", "client.html")), "rb").read()
    try:
        _ICON = open(_res(os.path.join("webui", "icon.ico")), "rb").read()
    except Exception:
        _ICON = b""

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    _API.port = port          # 이 인스턴스의 Edge 창을 식별(재시작 시 닫기)용
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    _API.mon.start()
    _API.start_host()          # 호스트 역할 WebRTC 세션 자동 시작(기사 원격 접속 대기)

    # 진단 정적 캐시 워밍(백그라운드) → 사용자가 [진단] 누를 즈음엔 캐시가 준비돼 첫 진단도 빠름
    def _warm():
        try:
            diagnostic.collect_all()
        except Exception:
            pass
    threading.Thread(target=_warm, daemon=True).start()

    url = "http://127.0.0.1:%d/" % port
    browser = _find_chromium()
    # 인스턴스별 고유 프로필 → 재시작 시 옛 창이 새 창에 붙지 않고 독립적으로 닫힘
    profile = os.path.join(os.environ.get("TEMP", "."), "asclient_edge_%d" % port)
    if browser:
        subprocess.Popen([browser, "--app=" + url,
                          "--user-data-dir=" + profile,
                          "--window-size=1180,780",
                          "--no-first-run", "--no-default-browser-check"])
    else:
        import webbrowser        # 크로미엄 없으면 기본 브라우저 일반 탭으로
        webbrowser.open(url)

    # 창이 닫히면 종료 (감시). 창 뜰 시간 여유를 주고 시작.
    time.sleep(4)
    _API.touch()
    _watchdog(_API)


if __name__ == "__main__":
    main()
