"""
WebRTC P2P 애플리케이션 레이어 (GUI 비의존).

전송 방식: **선명한 변경영역 타일(JPEG)을 P2P 데이터채널로** 보냄.
 - H.264 영상 코덱은 정적 화면(글자)에서 흐릿하고 CPU를 많이 먹어 원격지원에 부적합 →
   이전의 델타 타일 방식을 그대로 P2P 데이터채널에 실어 "P2P + 선명함"을 함께 얻는다.
 - P2P 실패(대칭 NAT 등) 시 같은 타일 스트림을 릴레이 서버로 폴백.

역할: host = 화면 송신(offerer, 데이터채널 생성), viewer = 화면 수신 + 입력 송신(answerer).
GUI(host.py/viewer.py)는 콜백/큐로만 연결: apply_input, frame_cb, set_status, input_q.
"""
import asyncio
import concurrent.futures
import json
import queue

from PIL import Image

from common import (MSG_INPUT, MSG_TILES, MSG_BYE,
                    pack_msg, unpack_msg,
                    encode_tiles, pack_tiles, chunk_tiles, composite_tiles)
from webrtc_core import (relay_connect, host_auth, viewer_auth,
                         host_negotiate, viewer_negotiate,
                         wait_connected, ws_send, ws_recv, make_pc)

CAP_FPS = 20
CAP_MAX_WIDTH = 1920
TILE_QUALITY = 75
P2P_TIMEOUT = 12          # 이 시간 안에 P2P 연결 안 되면 릴레이 폴백
DC_BUFFER_CAP = 1_500_000  # 데이터채널 송신버퍼가 이보다 크면 프레임 스킵(백프레셔)


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


# ---------------------------------------------------------------------------
# HOST : 화면 타일을 데이터채널로 송신, 입력은 데이터채널로 수신
# ---------------------------------------------------------------------------
async def host_run(relay_url, code, apply_input, set_status, should_run,
                   monitor_index=1):
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

    @dc.on("message")
    def _on_msg(m):
        try:
            mtype, payload = unpack_msg(_to_bytes(m))
            if mtype == MSG_INPUT:
                apply_input(json.loads(payload))
        except Exception:
            pass

    connected = False
    try:
        await host_negotiate(ws, pc, timeout=P2P_TIMEOUT + 6)
        connected = await wait_connected(pc, timeout=P2P_TIMEOUT)
    except Exception:
        connected = False

    if connected:
        set_status("P2P 직접 연결 · 원격 제어 중")
        await _stream_tiles_dc(dc, should_run, monitor_index, pc)
    else:
        set_status("릴레이 경유 · 원격 제어 중")
        await _host_fallback(ws, apply_input, should_run, monitor_index)

    await _safe_close(pc)
    await _safe_close(ws)


async def _stream_tiles_dc(dc, should_run, monitor_index, pc):
    loop = asyncio.get_event_loop()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    grab = ScreenGrabber(monitor_index)
    prev = {}
    try:
        # 데이터채널 열릴 때까지 대기
        while should_run() and dc.readyState != "open" and pc.connectionState == "connected":
            await asyncio.sleep(0.05)
        interval = 1.0 / CAP_FPS
        while (should_run() and dc.readyState == "open"
               and pc.connectionState == "connected"):
            t0 = loop.time()
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


async def _host_fallback(ws, apply_input, should_run, monitor_index):
    loop = asyncio.get_event_loop()

    async def rx():
        while should_run():
            mtype, payload = await ws_recv(ws)
            if mtype is None or mtype == MSG_BYE:
                break
            if mtype == MSG_INPUT:
                try:
                    apply_input(json.loads(payload))
                except Exception:
                    pass

    rx_task = asyncio.ensure_future(rx())
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    grab = ScreenGrabber(monitor_index)
    prev = {}
    try:
        interval = 1.0 / CAP_FPS
        while should_run():
            t0 = loop.time()
            img = await loop.run_in_executor(executor, grab.grab_pil)
            w, h = img.size
            changed = encode_tiles(img, prev, quality=TILE_QUALITY)
            if changed:
                try:
                    await ws_send(ws, MSG_TILES, pack_tiles(w, h, changed))
                except Exception:
                    break
            dt = loop.time() - t0
            if dt < interval:
                await asyncio.sleep(interval - dt)
    finally:
        rx_task.cancel()
        executor.shutdown(wait=False)
        grab.close()


# ---------------------------------------------------------------------------
# VIEWER : 데이터채널로 타일 수신 -> 합성 -> frame_cb, 입력은 데이터채널로 송신
# ---------------------------------------------------------------------------
async def viewer_run(relay_url, code, frame_cb, input_q, set_status, should_run):
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

    @pc.on("datachannel")
    def _on_dc(dc):
        dcbox["dc"] = dc

        @dc.on("message")
        def _on_msg(m):
            try:
                mtype, payload = unpack_msg(_to_bytes(m))
                if mtype == MSG_TILES:
                    screen["img"] = composite_tiles(screen["img"], payload)
                    if screen["img"] is not None:
                        frame_cb(screen["img"].copy())
            except Exception:
                pass

    connected = False
    try:
        await viewer_negotiate(ws, pc, timeout=P2P_TIMEOUT + 6)
        connected = await wait_connected(pc, timeout=P2P_TIMEOUT)
    except Exception:
        connected = False

    if connected:
        set_status("P2P 직접 연결 · 원격 제어 중")
        await _viewer_dc_loop(dcbox, input_q, should_run, pc)
    else:
        set_status("릴레이 경유 · 원격 제어 중")
        await _viewer_fallback(ws, frame_cb, input_q, should_run)

    await _safe_close(pc)
    await _safe_close(ws)


async def _drain_input(input_q):
    out = []
    while True:
        try:
            out.append(input_q.get_nowait())
        except queue.Empty:
            break
    return out


async def _viewer_dc_loop(dcbox, input_q, should_run, pc):
    while should_run() and pc.connectionState == "connected":
        dc = dcbox.get("dc")
        if dc is not None and dc.readyState == "open":
            for ev in await _drain_input(input_q):
                try:
                    dc.send(pack_msg(MSG_INPUT, json.dumps(ev)))
                except Exception:
                    pass
        await asyncio.sleep(0.008)


async def _viewer_fallback(ws, frame_cb, input_q, should_run):
    screen = {"img": None}

    async def rx():
        while should_run():
            mtype, payload = await ws_recv(ws)
            if mtype is None or mtype == MSG_BYE:
                break
            if mtype == MSG_TILES:
                screen["img"] = composite_tiles(screen["img"], payload)
                if screen["img"] is not None:
                    frame_cb(screen["img"].copy())

    rx_task = asyncio.ensure_future(rx())
    try:
        while should_run():
            for ev in await _drain_input(input_q):
                try:
                    await ws_send(ws, MSG_INPUT, json.dumps(ev))
                except Exception:
                    pass
            await asyncio.sleep(0.008)
    finally:
        rx_task.cancel()
