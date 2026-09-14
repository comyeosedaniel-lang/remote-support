"""
Windows 클립보드 헬퍼 — 텍스트 + 파일(CF_HDROP) 읽기/쓰기, 변경 감지.

- 텍스트: pyperclip 사용
- 파일: pywin32(win32clipboard)로 CF_HDROP 처리 (Ctrl+C 한 파일 목록 읽기 / 붙여넣기 가능하게 쓰기)
- pywin32 가 없으면 파일 기능은 조용히 비활성화(텍스트만 동작).
"""
import struct

try:
    import win32clipboard as _wcb
    import win32con as _wc
    HAS_FILE_CLIPBOARD = True
except Exception:
    HAS_FILE_CLIPBOARD = False

try:
    import pyperclip
except Exception:
    pyperclip = None


def sequence_number():
    """클립보드가 바뀔 때마다 증가하는 번호. 변경 감지에 사용. (없으면 None)"""
    if HAS_FILE_CLIPBOARD:
        try:
            return _wcb.GetClipboardSequenceNumber()
        except Exception:
            return None
    return None


# ---------------- 텍스트 ----------------
def get_text():
    if pyperclip:
        try:
            return pyperclip.paste()
        except Exception:
            return None
    return None


def set_text(text):
    if pyperclip:
        try:
            pyperclip.copy(text)
        except Exception:
            pass


# ---------------- 파일 (CF_HDROP) ----------------
def get_files():
    """클립보드에 복사된 파일 경로 목록(tuple). 없으면 ()."""
    if not HAS_FILE_CLIPBOARD:
        return ()
    try:
        _wcb.OpenClipboard()
        try:
            if _wcb.IsClipboardFormatAvailable(_wc.CF_HDROP):
                data = _wcb.GetClipboardData(_wc.CF_HDROP)
                return tuple(data) if data else ()
        finally:
            _wcb.CloseClipboard()
    except Exception:
        pass
    return ()


def set_files(paths):
    """파일 경로 목록을 클립보드에 올림 → 탐색기에서 Ctrl+V 로 붙여넣기 가능. 성공 시 True."""
    if not HAS_FILE_CLIPBOARD or not paths:
        return False
    try:
        # DROPFILES 구조체: pFiles=20, POINT(0,0), fNC=0, fWide=1  (총 20바이트)
        # 이어서 UTF-16LE 파일목록(널 구분, 이중 널 종료)
        joined = "\0".join(paths) + "\0\0"
        blob = struct.pack("<IiiII", 20, 0, 0, 0, 1) + joined.encode("utf-16-le")
        _wcb.OpenClipboard()
        try:
            _wcb.EmptyClipboard()
            _wcb.SetClipboardData(_wc.CF_HDROP, blob)
        finally:
            _wcb.CloseClipboard()
        return True
    except Exception:
        return False
