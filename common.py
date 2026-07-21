"""
공통 통신 프로토콜.

메시지 형식: [1바이트 타입][4바이트 길이(big-endian)][페이로드]
host.py 와 viewer.py 가 함께 사용합니다.
"""
import struct

# 메시지 타입
MSG_AUTH = 1       # viewer -> host : 접속 코드(PIN) 전송
MSG_AUTH_OK = 2    # host -> viewer : 인증 성공
MSG_AUTH_FAIL = 3  # host -> viewer : 인증 실패/거부
MSG_FRAME = 4      # host -> viewer : [4B width][4B height][JPEG bytes] (전체 프레임)
MSG_INPUT = 5      # viewer -> host : 입력 이벤트(JSON)
MSG_BYE = 6        # 양방향 : 정상 종료 통보
MSG_TILES = 7      # host -> viewer : 변경된 타일만 (델타 전송)
#   페이로드: [4B W][4B H][2B tile][2B count] + count 개의 [2B col][2B row][4B len][JPEG]
MSG_CLIPBOARD = 8  # 양방향 : 클립보드 텍스트 동기화 (UTF-8 텍스트)
MSG_FILE_START = 9 # 파일 전송 시작 : [4B 파일명길이][파일명(UTF-8)][8B 파일크기]
MSG_FILE_CHUNK = 10# 파일 전송 데이터 청크 : [데이터바이트]
MSG_FILE_END = 11  # 파일 전송 완료
MSG_SIGNAL = 12    # 양방향 : WebRTC 시그널링(JSON) — {"kind":"offer"/"answer", "sdp":...}


_HEADER = struct.Struct(">BI")  # 타입(1) + 길이(4)


def send_msg(sock, msg_type, payload=b""):
    """메시지 한 개를 소켓으로 전송."""
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    sock.sendall(_HEADER.pack(msg_type, len(payload)) + payload)


def recv_exact(sock, n):
    """정확히 n 바이트를 수신. 연결이 끊기면 None."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def recv_msg(sock):
    """메시지 한 개를 수신. (타입, 페이로드) 반환. 끊기면 (None, None)."""
    header = recv_exact(sock, _HEADER.size)
    if header is None:
        return None, None
    msg_type, length = _HEADER.unpack(header)
    payload = b""
    if length:
        payload = recv_exact(sock, length)
        if payload is None:
            return None, None
    return msg_type, payload


def pack_msg(msg_type, payload=b""):
    """메시지 한 개를 바이트로 직렬화 (WebSocket 바이너리 프레임용)."""
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return _HEADER.pack(msg_type, len(payload)) + payload


def unpack_msg(data):
    """pack_msg 로 만든 바이트를 (타입, 페이로드) 로 복원."""
    if data is None or len(data) < _HEADER.size:
        return None, None
    msg_type, length = _HEADER.unpack(data[:_HEADER.size])
    payload = data[_HEADER.size:_HEADER.size + length]
    return msg_type, payload


TILE = 128  # 델타 전송 타일 크기(px)


def encode_tiles(img, prev, quality=45):
    """이미지를 타일로 나눠 직전과 달라진 타일만 JPEG 인코딩.
    prev 딕셔너리를 갱신하고, 변경된 [(col, row, jpeg_bytes), ...] 를 반환."""
    import io as _io
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
                tb = _io.BytesIO()
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


def chunk_tiles(W, H, changed, cap=48000):
    """변경 타일 목록을 바이트 상한(cap) 이하의 여러 MSG_TILES 페이로드로 분할.
    WebRTC 데이터채널 메시지 크기 제한을 넘지 않도록 프레임을 쪼갠다."""
    batch = []
    size = 0
    for t in changed:
        tsz = len(t[2]) + 8
        if batch and size + tsz > cap:
            yield pack_tiles(W, H, batch)
            batch = []
            size = 0
        batch.append(t)
        size += tsz
    if batch:
        yield pack_tiles(W, H, batch)


def composite_tiles(screen_img, payload):
    """MSG_TILES 페이로드를 받아 screen_img(PIL) 에 변경 타일을 합성해 반환.
    screen_img 가 None 이거나 크기가 다르면 새로 만든다. (PIL/Image 는 지연 임포트)"""
    import io as _io
    import struct as _struct
    from PIL import Image as _Image
    if payload is None or len(payload) < 12:
        return screen_img
    w, h, tile, count = _struct.unpack(">IIHH", payload[:12])
    off = 12
    if screen_img is None or screen_img.size != (w, h):
        screen_img = _Image.new("RGB", (w, h))
    for _ in range(count):
        if off + 8 > len(payload):
            break
        c, r, ln = _struct.unpack(">HHI", payload[off:off + 8])
        off += 8
        jp = payload[off:off + ln]
        off += ln
        try:
            screen_img.paste(_Image.open(_io.BytesIO(jp)), (c * tile, r * tile))
        except Exception:
            pass
    return screen_img


# ---------------------------------------------------------------------------
# 전송 계층 추상화 : LAN(raw TCP) 과 인터넷(WebSocket 릴레이)을 같은 코드로 사용
# 인터페이스:  send(type, payload) / recv() -> (type, payload) / close()
# ---------------------------------------------------------------------------
class TCPChannel:
    """raw TCP 소켓 채널 (LAN 직접 연결)."""

    def __init__(self, sock):
        self.sock = sock

    def send(self, msg_type, payload=b""):
        send_msg(self.sock, msg_type, payload)

    def recv(self):
        return recv_msg(self.sock)

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


class WSChannel:
    """WebSocket 채널 (websockets.sync ClientConnection 래퍼, 릴레이 경유)."""

    def __init__(self, ws):
        self.ws = ws

    def send(self, msg_type, payload=b""):
        self.ws.send(pack_msg(msg_type, payload))

    def recv(self):
        try:
            data = self.ws.recv()
        except Exception:
            return None, None
        if isinstance(data, str):
            data = data.encode("utf-8")
        return unpack_msg(data)

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass
