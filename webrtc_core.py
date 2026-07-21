"""
WebRTC P2P 코어.

- 릴레이 서버(Cloudflare Durable Object 등)를 **시그널링 채널**로 사용
  (릴레이는 바이트만 중계하므로 Worker 수정 불필요 — 시그널 메시지도 그냥 바이트)
- non-trickle ICE: 후보를 모두 모은 뒤 SDP 에 담아 한 번에 교환 (구현 단순·견고)
- STUN 으로 직접 P2P 시도. 실패(대칭 NAT 등)하면 상위에서 기존 릴레이 타일 스트림으로 폴백.

역할: host = 화면 송신(offerer), viewer = 화면 수신 + 입력 송신(answerer).
"""
import asyncio
import json

from websockets.asyncio.client import connect as ws_connect_async
from aiortc import (RTCPeerConnection, RTCConfiguration, RTCIceServer,
                    RTCSessionDescription)

from common import (pack_msg, unpack_msg,
                    MSG_AUTH, MSG_AUTH_OK, MSG_AUTH_FAIL, MSG_SIGNAL)

# 무료 공개 STUN (공인 주소 파악용 — 데이터는 안 지나감)
ICE_SERVERS = [
    RTCIceServer(urls="stun:stun.l.google.com:19302"),
    RTCIceServer(urls="stun:stun1.l.google.com:19302"),
]


def make_pc():
    return RTCPeerConnection(RTCConfiguration(iceServers=ICE_SERVERS))


# ---------------- 릴레이(시그널링) 연결 ----------------
async def relay_connect(relay_url, code, role):
    url = "%s?code=%s&role=%s" % (relay_url, code, role)
    return await ws_connect_async(url, max_size=8 * 1024 * 1024, open_timeout=15)


async def ws_send(ws, mtype, payload=b""):
    await ws.send(pack_msg(mtype, payload))


async def ws_recv(ws):
    try:
        data = await ws.recv()
    except Exception:
        return None, None
    if isinstance(data, str):
        data = data.encode("utf-8")
    return unpack_msg(data)


async def send_signal(ws, kind, sdp=None):
    await ws_send(ws, MSG_SIGNAL, json.dumps({"kind": kind, "sdp": sdp}))


# ---------------- 인증 (기존 프로토콜 재사용) ----------------
async def viewer_auth(ws, code):
    await ws_send(ws, MSG_AUTH, code)
    mtype, _ = await ws_recv(ws)
    if mtype != MSG_AUTH_OK:
        raise ConnectionError("접속 코드가 틀리거나 연결이 거부되었습니다.")


async def host_auth(ws, code):
    mtype, payload = await ws_recv(ws)
    if mtype != MSG_AUTH or not payload or payload.decode("utf-8", "ignore") != code:
        await ws_send(ws, MSG_AUTH_FAIL)
        raise ConnectionError("접속 코드 불일치")
    await ws_send(ws, MSG_AUTH_OK)


# ---------------- ICE / 연결 상태 대기 ----------------
async def wait_ice_complete(pc, timeout=6):
    if pc.iceGatheringState == "complete":
        return
    ev = asyncio.Event()

    @pc.on("icegatheringstatechange")
    def _on():
        if pc.iceGatheringState == "complete":
            ev.set()

    try:
        await asyncio.wait_for(ev.wait(), timeout)
    except asyncio.TimeoutError:
        pass  # 그때까지 모은 후보로 진행


async def wait_connected(pc, timeout=15):
    if pc.connectionState == "connected":
        return True
    ev = asyncio.Event()

    @pc.on("connectionstatechange")
    def _on():
        if pc.connectionState in ("connected", "failed", "closed"):
            ev.set()

    try:
        await asyncio.wait_for(ev.wait(), timeout)
    except asyncio.TimeoutError:
        pass
    return pc.connectionState == "connected"


# ---------------- 협상 (non-trickle) ----------------
async def host_negotiate(ws, pc, timeout=20):
    """host = offerer. offer 송신 후 answer 수신."""
    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    await wait_ice_complete(pc)
    await send_signal(ws, "offer", pc.localDescription.sdp)

    async def _wait_answer():
        while True:
            mtype, payload = await ws_recv(ws)
            if mtype is None:
                raise ConnectionError("시그널링 연결 종료")
            if mtype == MSG_SIGNAL:
                msg = json.loads(payload)
                if msg.get("kind") == "answer":
                    await pc.setRemoteDescription(
                        RTCSessionDescription(sdp=msg["sdp"], type="answer"))
                    return
    await asyncio.wait_for(_wait_answer(), timeout)


async def viewer_negotiate(ws, pc, timeout=20):
    """viewer = answerer. offer 수신 후 answer 송신."""
    async def _wait_offer():
        while True:
            mtype, payload = await ws_recv(ws)
            if mtype is None:
                raise ConnectionError("시그널링 연결 종료")
            if mtype == MSG_SIGNAL:
                msg = json.loads(payload)
                if msg.get("kind") == "offer":
                    await pc.setRemoteDescription(
                        RTCSessionDescription(sdp=msg["sdp"], type="offer"))
                    answer = await pc.createAnswer()
                    await pc.setLocalDescription(answer)
                    await wait_ice_complete(pc)
                    await send_signal(ws, "answer", pc.localDescription.sdp)
                    return
    await asyncio.wait_for(_wait_offer(), timeout)


def negotiated_codec(sdp):
    if "H264" in sdp:
        return "H264"
    if "VP8" in sdp:
        return "VP8"
    return "?"
