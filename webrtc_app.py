"""
WebRTC P2P 애플리케이션 레이어 (GUI 비의존). P2P 전용 — 릴레이 서버는 신호(SDP) 교환용으로만
쓰고, 실제 화면/입력 데이터는 P2P 로만 오간다. P2P 연결 자체가 안 되면(대칭 NAT, 막힌 방화벽
등) 릴레이로 우회하지 않고 연결 실패로 처리한다.

전송 방식: 선명한 변경영역 타일(JPEG)을 P2P 데이터채널로 보냄.
 - 화면(host->viewer): MSG_TILES, 입력(viewer->host): MSG_INPUT
 - 클립보드(텍스트/파일)와 파일 전송은 **양방향** (MSG_CLIPBOARD / MSG_FILE_*)

GUI(host.py/viewer.py)는 콜백/큐/bridge 로만 연결:
 - apply_input(ev), frame_cb(pil), set_status(str), out_q((type,payload)), bridge{"send_file": fn}
"""
import asyncio
import concurrent.futures
import json
import os
import queue
import struct

from PIL import Image

import clipboardx
from common import (MSG_INPUT, MSG_TILES,
                    MSG_CLIPBOARD, MSG_FILE_START, MSG_FILE_CHUNK, MSG_FILE_END,
                    MSG_DIAG_REQUEST, MSG_DIAG_REPORT,
                    MSG_CMD_REQUEST, MSG_CMD_RESULT, MSG_CLIENT_INFO,
                    pack_msg, unpack_msg,
                    encode_tiles, chunk_tiles, composite_tiles)
from webrtc_core import (relay_connect, host_auth, viewer_auth,
                         host_negotiate, viewer_negotiate,
                         wait_connected, make_pc)

CAP_FPS = 20
CAP_MAX_WIDTH = 1920
TILE_QUALITY = 75
P2P_TIMEOUT = 12
DC_BUFFER_CAP = 1_500_000   # 데이터채널 송신버퍼 상한(백프레셔)
FILE_CHUNK = 65536
DOWNLOAD_DIR = "downloads"


class ScreenGrabber:
    """mss 화면 캡처 -> PIL 이미지 (max_width 로 축소). mss 는 한 스레드에 고정."""

    def __init__(self, monitor_index=1, max_width=CAP_MAX_WIDTH):
        self.monitor_index = monitor_index
        self.max_width = max_width
        self._sct = None
        self._monitor = None

    def grab_pil(self):
        if self._sct is None:
            import mss
            self._sct = mss.mss()
            self._monitor = self._sct.monitors[self.monitor_index]
        shot = self._sct.grab(self._monitor)
        img = Image.frombytes("RGB", shot.size, shot.rgb)
        if img.width > self.max_width:
            ratio = self.max_width / img.width
            img = img.resize((self.max_width, int(img.height * ratio)))
        return img

    def close(self):
        if self._sct is not None:
            try:
                self._sct.close()
            except Exception:
                pass
            self._sct = None


async def _safe_close(obj):
    try:
        await obj.close()
    except Exception:
        pass


def _to_bytes(m):
    return m if isinstance(m, (bytes, bytearray)) else str(m).encode("utf-8")


# ---------------- 원격 PC 진단 ----------------
def _build_diag_summary(data, extra_fn):
    """collect_all() 결과에서 AS 기록용 요약 필드를 뽑아낸다. 각 항목은 개별 try 로 방어."""
    summary = {}
    try:
        s = data.get("system", {})
        summary["hostname"] = s.get("hostname")
        summary["os"] = s.get("edition") or s.get("os")
        summary["build"] = s.get("build")
    except Exception:
        pass
    try:
        vols = (data.get("disks") or {}).get("volumes") or []
        summary["disks"] = [{"device": v.get("device"), "percent": v.get("percent"),
                             "free_gb": v.get("free_gb")} for v in vols]
    except Exception:
        pass
    try:
        import analysis
        summary["hardware_suspicion"] = analysis.hardware_suspicion(data)
    except Exception:
        pass
    try:
        import diagnostic
        summary["security"] = diagnostic.collect_security2()
    except Exception:
        pass
    if extra_fn:
        try:
            extra = extra_fn()
            if extra:
                summary.update(extra)
        except Exception:
            pass
    return summary


async def _run_and_send_diag(send, set_status, extra_fn=None, auto=False):
    """호스트: 진단 실행(느림 → executor) 후 {html, summary, auto} 를 JSON 으로 뷰어에 전송.
    auto=True 면 세션 장기화에 따른 백그라운드 자동 재진단(AS 기록 갱신용) — 뷰어가 브라우저를 띄우지 않는다."""
    set_status("PC 진단 실행 중...")
    loop = asyncio.get_event_loop()
    try:
        import diagnostic
        data = await loop.run_in_executor(None, diagnostic.collect_all)
        html = await loop.run_in_executor(None, diagnostic.report_html, data)
        summary = await loop.run_in_executor(None, _build_diag_summary, data, extra_fn)
        payload = json.dumps({"html": html, "summary": summary, "auto": bool(auto)},
                             ensure_ascii=False).encode("utf-8")
        await send(MSG_DIAG_REPORT, payload)
        set_status("진단 리포트 전송 완료")
    except Exception:
        set_status("진단 실행 오류")


def _open_diag_report(payload, set_status, bridge=None):
    """뷰어: 받은 {html,summary,auto} 를 임시파일로 저장. 기사가 직접 요청한 경우(auto=False)만
    브라우저로 즉시 열고, 백그라운드 자동 재진단(auto=True)은 조용히 저장만 한다(AS 기록에서 나중에 열람).
    bridge 있으면 앱에도 전달(고객 AS 기록 자동 연동용). 구버전(순수 HTML 바이트) 도 호환."""
    html_text, summary, auto = None, {}, False
    try:
        obj = json.loads(payload.decode("utf-8", "ignore"))
        html_text = obj.get("html", "")
        summary = obj.get("summary", {}) or {}
        auto = bool(obj.get("auto", False))
    except Exception:
        html_text = payload.decode("utf-8", "ignore")
    if bridge is not None:
        try:
            bridge["diag_html"] = html_text
            bridge["diag_summary"] = summary
            bridge["diag_ver"] = bridge.get("diag_ver", 0) + 1
        except Exception:
            pass
    try:
        import os
        import tempfile
        p = os.path.join(tempfile.gettempdir(), "원격진단_리포트.html")
        with open(p, "w", encoding="utf-8") as f:
            f.write(html_text)
        if auto:
            set_status("진단 리포트 자동 갱신 완료 (AS 기록에 저장됨)")
        else:
            set_status("진단 리포트 도착 — 브라우저로 엽니다")
            try:
                os.startfile(p)
            except Exception:
                pass
    except Exception:
        pass


# ---------------- 원격 명령(CMD / PowerShell) ----------------
def _exec_command(shell, cmd):
    """호스트: 원격 명령 실행. cmd 또는 powershell. 안전: 타임아웃 + 콘솔창 숨김."""
    import subprocess
    if not (cmd or "").strip():
        return "(빈 명령)"
    try:
        if shell == "powershell":
            args = ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd]
        else:
            args = ["cmd", "/c", cmd]
        r = subprocess.run(args, capture_output=True, text=True, errors="ignore",
                           timeout=60, creationflags=0x08000000)
        out = (r.stdout or "") + (r.stderr or "")
        return out.strip() or ("(출력 없음 · 종료코드 %s)" % r.returncode)
    except subprocess.TimeoutExpired:
        return "(시간 초과 — 60초 넘어 중단)"
    except Exception as e:
        return "실행 오류: %s" % e


async def _run_and_send_cmd(payload, send, set_status):
    """호스트: 원격 명령 실행(블로킹 → executor) 후 결과를 뷰어로 전송."""
    set_status("원격 명령 실행 중...")
    loop = asyncio.get_event_loop()
    try:
        req = json.loads(payload)
        out = await loop.run_in_executor(
            None, _exec_command, req.get("shell", "cmd"), req.get("cmd", ""))
        await send(MSG_CMD_RESULT, out.encode("utf-8"))
        set_status("원격 명령 완료")
    except Exception as e:
        try:
            await send(MSG_CMD_RESULT, ("실행 오류: %s" % e).encode("utf-8"))
        except Exception:
            pass


def _deliver_cmd_result(payload, bridge, set_status):
    """뷰어: 명령 결과를 bridge 에 저장(스튜디오가 폴링). bridge 없으면 상태만."""
    text = payload.decode("utf-8", "ignore") if isinstance(payload, (bytes, bytearray)) else str(payload)
    if bridge is not None:
        bridge["cmd_result"] = text
        bridge["cmd_ver"] = bridge.get("cmd_ver", 0) + 1
    set_status("원격 명령 결과 도착")


# ---------------- 연결 직후 자동 정보 동기화(고객정보+PC사양) ----------------
async def _send_client_info(send, client_info_fn, set_status):
    """호스트: 연결 성사 시 1회, 고객정보+PC사양 요약을 뷰어로 자동 전송."""
    try:
        info = client_info_fn()
        if info:
            await send(MSG_CLIENT_INFO, json.dumps(info, ensure_ascii=False).encode("utf-8"))
    except Exception:
        pass


def _receive_client_info(payload, bridge):
    """뷰어: 자동 수신된 고객정보+PC사양을 bridge 에 저장(스튜디오가 폴링)."""
    if bridge is None:
        return
    try:
        bridge["client_info"] = json.loads(payload.decode("utf-8", "ignore"))
        bridge["client_info_ver"] = bridge.get("client_info_ver", 0) + 1
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 클립보드(텍스트/파일) + 파일 전송 — 양방향 공통 로직
# ---------------------------------------------------------------------------
class Transfer:
    """한 연결에 대한 클립보드 동기화 + 파일 송수신. send 는 async fn(mtype, payload)."""

    def __init__(self, send, set_status, download_dir=DOWNLOAD_DIR):
        self.send = send
        self.set_status = set_status or (lambda s: None)
        self.download_dir = download_dir
        self.last_seq = clipboardx.sequence_number()
        self.suppress_seq = None
        self.last_text = clipboardx.get_text() or ""
        self._send_lock = asyncio.Lock()
        self._rf = None            # 수신 중 파일: {name,size,written,f,path}
        self._batch = []           # 최근 수신 파일 경로들(클립보드에 올릴 배치)
        self._batch_task = None

    # ---------------- 송신 ----------------
    async def send_file(self, path):
        async with self._send_lock:   # 파일끼리 겹치지 않게 직렬화
            loop = asyncio.get_event_loop()
            try:
                name = os.path.basename(path)
                nb = name.encode("utf-8")
                size = os.path.getsize(path)
            except Exception:
                return
            self.set_status("파일 보내는 중: %s" % name)
            await self.send(MSG_FILE_START,
                            struct.pack(">I", len(nb)) + nb + struct.pack(">Q", size))
            try:
                f = await loop.run_in_executor(None, open, path, "rb")
            except Exception:
                return
            try:
                while True:
                    chunk = await loop.run_in_executor(None, f.read, FILE_CHUNK)
                    if not chunk:
                        break
                    await self.send(MSG_FILE_CHUNK, chunk)
            finally:
                await loop.run_in_executor(None, f.close)
            await self.send(MSG_FILE_END, b"")
            self.set_status("파일 보냄: %s" % name)

    async def clip_monitor(self, should_run):
        while should_run():
            try:
                seq = clipboardx.sequence_number()
                if seq is not None and seq != self.last_seq:
                    self.last_seq = seq
                    if seq != self.suppress_seq:
                        await self._on_clip_change()
            except Exception:
                pass
            await asyncio.sleep(0.4)

    async def _on_clip_change(self):
        files = clipboardx.get_files()
        if files:
            for p in files:               # 복사된 파일 → 상대로 전송
                await self.send_file(p)
            return
        text = clipboardx.get_text()
        if text and text != self.last_text:
            self.last_text = text
            await self.send(MSG_CLIPBOARD, text.encode("utf-8", "ignore"))

    # ---------------- 수신 ----------------
    def on_message(self, mtype, payload):
        if mtype == MSG_CLIPBOARD:
            text = payload.decode("utf-8", "ignore")
            self.last_text = text
            clipboardx.set_text(text)
            self.suppress_seq = clipboardx.sequence_number()   # 에코 방지
        elif mtype == MSG_FILE_START:
            try:
                nlen = struct.unpack(">I", payload[:4])[0]
                name = os.path.basename(payload[4:4 + nlen].decode("utf-8", "ignore"))
                size = struct.unpack(">Q", payload[4 + nlen:12 + nlen])[0]
                os.makedirs(self.download_dir, exist_ok=True)
                dest = os.path.join(self.download_dir, name)
                self._rf = {"name": name, "size": size, "written": 0,
                            "f": open(dest, "wb"), "path": os.path.abspath(dest)}
                self.set_status("파일 받는 중: %s" % name)
            except Exception:
                self._rf = None
        elif mtype == MSG_FILE_CHUNK:
            if self._rf:
                try:
                    self._rf["f"].write(payload)
                    self._rf["written"] += len(payload)
                except Exception:
                    pass
        elif mtype == MSG_FILE_END:
            if self._rf:
                try:
                    self._rf["f"].close()
                    self._batch.append(self._rf["path"])
                    self.set_status("받음: %s/%s" % (self.download_dir, self._rf["name"]))
                except Exception:
                    pass
                self._rf = None
                self._schedule_batch()

    def _schedule_batch(self):
        if self._batch_task and not self._batch_task.done():
            self._batch_task.cancel()
        self._batch_task = asyncio.ensure_future(self._flush_batch())

    async def _flush_batch(self):
        try:
            await asyncio.sleep(0.6)      # 마지막 파일 후 잠깐 기다렸다 한 번에 클립보드로
        except asyncio.CancelledError:
            return
        paths = list(self._batch)
        self._batch = []
        if paths and clipboardx.set_files(paths):
            self.suppress_seq = clipboardx.sequence_number()   # 에코 방지

    def close(self):
        if self._rf:
            try:
                self._rf["f"].close()
            except Exception:
                pass
            self._rf = None


def _make_dsend(dcref):
    """데이터채널용 async send (백프레셔). dcref: () -> dc 또는 dc 객체."""
    def _dc():
        return dcref() if callable(dcref) else dcref

    async def dsend(mtype, payload):
        dc = _dc()
        while dc is not None and dc.readyState == "open" and dc.bufferedAmount > DC_BUFFER_CAP:
            await asyncio.sleep(0.01)
            dc = _dc()
        dc = _dc()
        if dc is not None and dc.readyState == "open":
            dc.send(pack_msg(mtype, payload))
    return dsend


# ---------------------------------------------------------------------------
# HOST
# ---------------------------------------------------------------------------
async def host_run(relay_url, code, apply_input, set_status, should_run,
                   monitor_index=1, bridge=None, client_info_fn=None, extra_fn=None,
                   touch_fn=None):
    # P2P 전용: 릴레이 서버는 신호(SDP) 교환용으로만 쓰고, 실제 화면 데이터는 P2P 로만 보낸다.
    # P2P 연결 자체가 안 되면(대칭 NAT, 막힌 방화벽 등) 릴레이로 우회하지 않고 실패로 처리한다.
    try:
        ws = await relay_connect(relay_url, code, "host")
    except Exception:
        set_status("릴레이 접속 실패")
        return
    try:
        await host_auth(ws, code)
    except Exception:
        set_status("접속 코드 불일치로 거부됨")
        await _safe_close(ws)
        return

    set_status("연결 협상 중...")
    pc = make_pc()
    dc = pc.createDataChannel("data")
    transfer = Transfer(_make_dsend(dc), set_status)
    loop = asyncio.get_event_loop()

    @dc.on("message")
    def _on_msg(m):
        try:
            mtype, payload = unpack_msg(_to_bytes(m))
        except Exception:
            return
        if mtype == MSG_INPUT:
            try:
                apply_input(json.loads(payload))
            except Exception:
                pass
        elif mtype == MSG_DIAG_REQUEST:
            asyncio.ensure_future(_run_and_send_diag(transfer.send, set_status, extra_fn,
                                                      auto=(payload == b"auto")))
        elif mtype == MSG_CMD_REQUEST:
            asyncio.ensure_future(_run_and_send_cmd(payload, transfer.send, set_status))
        else:
            transfer.on_message(mtype, payload)

    connected = False
    try:
        await host_negotiate(ws, pc, timeout=P2P_TIMEOUT + 6)
        connected = await wait_connected(pc, timeout=P2P_TIMEOUT)
    except Exception:
        connected = False

    if connected:
        set_status("P2P 직접 연결 · 원격 제어 중")
        _bind_bridge(bridge, transfer, loop)
        if client_info_fn:
            async def _auto_sync(_dc=dc, _pc=pc):
                # 데이터채널이 실제로 open 되기 전에 보내면 조용히 유실되므로(_stream_tiles_dc 와 동일 조건) 대기
                while should_run() and _dc.readyState != "open" and _pc.connectionState == "connected":
                    await asyncio.sleep(0.05)
                if _dc.readyState == "open":
                    await _send_client_info(transfer.send, client_info_fn, set_status)
                    await _run_and_send_diag(transfer.send, set_status, extra_fn)
            asyncio.ensure_future(_auto_sync())
        clip_task = asyncio.ensure_future(
            transfer.clip_monitor(lambda: should_run() and dc.readyState == "open"))
        try:
            await _stream_tiles_dc(dc, should_run, monitor_index, pc, touch_fn=touch_fn)
        finally:
            clip_task.cancel()
            _unbind_bridge(bridge)
            if should_run() and bridge is not None:
                bridge["last_p2p_state"] = pc.connectionState
    else:
        set_status("P2P 연결 실패 — 네트워크 환경을 확인하세요")

    transfer.close()
    await _safe_close(pc)
    await _safe_close(ws)


async def _stream_tiles_dc(dc, should_run, monitor_index, pc, touch_fn=None):
    loop = asyncio.get_event_loop()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    grab = ScreenGrabber(monitor_index)
    prev = {}
    try:
        while (should_run() and dc.readyState != "open"
               and pc.connectionState == "connected"):
            await asyncio.sleep(0.05)
        interval = 1.0 / CAP_FPS
        while (should_run() and dc.readyState == "open"
               and pc.connectionState == "connected"):
            t0 = loop.time()
            # 화면 캡처/인코딩 부하로 로컬 UI 폴링이 밀려도(느린 PC 등) 워치독이
            # "창이 닫혔다"고 오판해 세션을 죽이지 않도록, 실제 스트리밍 중엔 여기서도 활동을 알린다.
            if touch_fn:
                touch_fn()
            if dc.bufferedAmount < DC_BUFFER_CAP:
                img = await loop.run_in_executor(executor, grab.grab_pil)
                w, h = img.size
                changed = encode_tiles(img, prev, quality=TILE_QUALITY)
                for payload in chunk_tiles(w, h, changed):
                    try:
                        dc.send(pack_msg(MSG_TILES, payload))
                    except Exception:
                        break
            dt = loop.time() - t0
            if dt < interval:
                await asyncio.sleep(interval - dt)
    finally:
        executor.shutdown(wait=False)
        grab.close()


# ---------------------------------------------------------------------------
# VIEWER
# ---------------------------------------------------------------------------
async def viewer_run(relay_url, code, frame_cb, out_q, set_status, should_run,
                     bridge=None, touch_fn=None):
    try:
        ws = await relay_connect(relay_url, code, "viewer")
    except Exception:
        set_status("릴레이 접속 실패")
        return
    try:
        await viewer_auth(ws, code)
    except Exception:
        set_status("접속 코드가 틀리거나 거부됨")
        await _safe_close(ws)
        return

    set_status("연결 협상 중...")
    pc = make_pc()
    dcbox = {"dc": None}
    screen = {"img": None}
    transfer = Transfer(_make_dsend(lambda: dcbox.get("dc")), set_status)
    loop = asyncio.get_event_loop()

    @pc.on("datachannel")
    def _on_dc(dc):
        dcbox["dc"] = dc

        @dc.on("message")
        def _on_msg(m):
            try:
                mtype, payload = unpack_msg(_to_bytes(m))
            except Exception:
                return
            if mtype == MSG_TILES:
                screen["img"] = composite_tiles(screen["img"], payload)
                if screen["img"] is not None:
                    frame_cb(screen["img"].copy())
            elif mtype == MSG_DIAG_REPORT:
                _open_diag_report(payload, set_status, bridge)
            elif mtype == MSG_CMD_RESULT:
                _deliver_cmd_result(payload, bridge, set_status)
            elif mtype == MSG_CLIENT_INFO:
                _receive_client_info(payload, bridge)
            else:
                transfer.on_message(mtype, payload)

    connected = False
    try:
        await viewer_negotiate(ws, pc, timeout=P2P_TIMEOUT + 6)
        connected = await wait_connected(pc, timeout=P2P_TIMEOUT)
    except Exception:
        connected = False

    if connected:
        set_status("P2P 직접 연결 · 원격 제어 중")
        _bind_bridge(bridge, transfer, loop)
        clip_task = asyncio.ensure_future(
            transfer.clip_monitor(lambda: should_run() and dcbox.get("dc") is not None))
        try:
            await _viewer_dc_loop(dcbox, out_q, should_run, pc, touch_fn=touch_fn)
        finally:
            clip_task.cancel()
            _unbind_bridge(bridge)
            if should_run() and bridge is not None:
                bridge["last_p2p_state"] = pc.connectionState
    else:
        set_status("P2P 연결 실패 — 네트워크 환경을 확인하세요")

    transfer.close()
    await _safe_close(pc)
    await _safe_close(ws)


async def _viewer_dc_loop(dcbox, out_q, should_run, pc, touch_fn=None):
    while should_run() and pc.connectionState == "connected":
        if touch_fn:
            touch_fn()
        dc = dcbox.get("dc")
        if dc is not None and dc.readyState == "open":
            while dc.bufferedAmount < DC_BUFFER_CAP:
                try:
                    mtype, payload = out_q.get_nowait()
                except queue.Empty:
                    break
                try:
                    dc.send(pack_msg(mtype, payload))
                except Exception:
                    break
        await asyncio.sleep(0.008)


# ---------------------------------------------------------------------------
# GUI 브리지 : Tk 스레드에서 파일 전송을 asyncio 루프로 넘김
# ---------------------------------------------------------------------------
def _bind_bridge(bridge, transfer, loop):
    if bridge is None:
        return

    def send_file(path):
        try:
            asyncio.run_coroutine_threadsafe(transfer.send_file(path), loop)
        except Exception:
            pass

    bridge["send_file"] = send_file


def _unbind_bridge(bridge):
    if bridge is not None:
        bridge.pop("send_file", None)
