"""
AS 도구 모음 — 컴퓨터 AS 에 필요한 드라이버/유틸리티/커뮤니티/쇼핑몰 등 사이트 링크를
검색해서 찾아 쓰는 로컬 페이지. ERP 고객관리와 같은 패턴(SQLite + 로컬 웹서버 + Edge 앱창) —
링크가 바뀌면 코드 수정 없이 화면에서 바로 수정한다(하드코딩 금지).
"""
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


def _res(rel):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


# 최초 실행 시 한 번만 채우는 기본 데이터. 이후로는 전부 DB(화면에서 수정)로 관리한다.
_SEED = [
    ("드라이버", "3DP Chip", "https://www.3dpchip.com", "하드웨어 자동 인식 후 필요한 드라이버 검색"),
    ("드라이버", "Windows Update 카탈로그", "https://www.catalog.update.microsoft.com", "MS 공식 드라이버/업데이트 카탈로그"),
    ("드라이버", "NVIDIA 드라이버", "https://www.nvidia.com", "지포스 그래픽 드라이버 공식 다운로드"),
    ("드라이버", "AMD 드라이버", "https://www.amd.com", "라이젠/라데온 공식 드라이버"),
    ("드라이버", "Intel 드라이버", "https://www.intel.com", "인텔 CPU/칩셋 공식 드라이버"),
    ("드라이버", "Realtek", "https://www.realtek.com", "오디오/랜카드 드라이버 제조사"),

    ("진단·점검", "CrystalDiskInfo", "https://crystalmark.info", "HDD/SSD 상태·수명 점검"),
    ("진단·점검", "HWiNFO", "https://www.hwinfo.com", "하드웨어 센서·상태 모니터링"),
    ("진단·점검", "CPU-Z / GPU-Z", "https://www.cpuid.com", "CPU/GPU 상세 정보 확인"),
    ("진단·점검", "MemTest86", "https://www.memtest86.com", "메모리(RAM) 불량 테스트"),
    ("진단·점검", "Sysinternals (Autoruns 등)", "https://learn.microsoft.com/sysinternals", "시작프로그램/프로세스 분석 도구 모음(MS 공식)"),
    ("진단·점검", "NirSoft (BlueScreenView 등)", "https://www.nirsoft.net", "블루스크린 분석 등 소형 진단 유틸 모음"),

    ("복구·백업", "Rufus", "https://rufus.ie", "윈도우 설치 USB 제작"),
    ("복구·백업", "EaseUS", "https://www.easeus.com", "파티션 관리·데이터 복구"),
    ("복구·백업", "MiniTool", "https://www.minitool.com", "파티션 관리·데이터 복구"),
    ("복구·백업", "Macrium Reflect", "https://www.macrium.com", "디스크 이미지 백업"),
    ("복구·백업", "7-Zip", "https://www.7-zip.org", "압축·해제"),

    ("최적화·정리", "CCleaner", "https://www.ccleaner.com", "임시파일·레지스트리 정리"),
    ("최적화·정리", "Everything (voidtools)", "https://www.voidtools.com", "파일명 즉시 검색"),
    ("최적화·정리", "FastCopy", "https://fastcopy.jp", "초고속 파일 복사"),

    ("보안", "Malwarebytes", "https://www.malwarebytes.com", "악성코드·애드웨어 검사"),
    ("보안", "AhnLab V3", "https://www.ahnlab.com", "국내 백신(오진 신고도 여기서)"),
    ("보안", "알약(ESRC/이스트시큐리티)", "https://www.estsecurity.com", "국내 무료 백신"),

    ("커뮤니티", "클리앙", "https://www.clien.net", "IT/하드웨어 정보 커뮤니티"),
    ("커뮤니티", "퀘이사존", "https://quasarzone.com", "하드웨어 리뷰·정보 커뮤니티"),
    ("커뮤니티", "쿨엔조이", "https://coolenjoy.net", "하드웨어 커뮤니티"),
    ("커뮤니티", "뽐뿌", "https://www.ppomppu.co.kr", "컴퓨터 게시판·핫딜"),
    ("커뮤니티", "디시인사이드 컴퓨터 갤러리", "https://gall.dcinside.com", "컴퓨터 관련 갤러리"),

    ("쇼핑몰", "다나와", "https://www.danawa.com", "부품·완제품 가격비교"),
    ("쇼핑몰", "컴퓨존", "https://www.compuzone.co.kr", "PC 부품 쇼핑몰"),

    ("공식 지원", "삼성서비스", "https://www.samsungsvc.co.kr", "삼성전자 A/S 접수·조회"),
    ("공식 지원", "LG전자 서비스", "https://www.lge.co.kr", "LG전자 A/S 접수·조회"),
]


class Api:
    def __init__(self):
        self.last_activity = time.monotonic()
        self.port = None
        self._seed_if_empty()

    def touch(self):
        self.last_activity = time.monotonic()

    def _db(self):
        import sqlite3
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        d = os.path.join(base, "ASStudio")
        os.makedirs(d, exist_ok=True)
        conn = sqlite3.connect(os.path.join(d, "tools.db"), timeout=10)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
        except Exception:
            pass
        conn.execute("""CREATE TABLE IF NOT EXISTS tools(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT, name TEXT, url TEXT, desc TEXT, created_at TEXT)""")
        return conn

    def _seed_if_empty(self):
        conn = self._db()
        try:
            n = conn.execute("SELECT COUNT(*) FROM tools").fetchone()[0]
            if n == 0:
                ts = time.strftime("%Y-%m-%d %H:%M:%S")
                conn.executemany(
                    "INSERT INTO tools(category,name,url,desc,created_at) VALUES(?,?,?,?,?)",
                    [(c, n, u, d, ts) for c, n, u, d in _SEED])
                conn.commit()
        finally:
            conn.close()

    def list_tools(self, q="", category=""):
        q = (q or "").strip()
        category = (category or "").strip()
        conn = self._db()
        try:
            sql = "SELECT id, category, name, url, desc FROM tools WHERE 1=1"
            args = []
            if category:
                sql += " AND category=?"
                args.append(category)
            if q:
                sql += " AND (name LIKE ? OR desc LIKE ? OR category LIKE ? OR url LIKE ?)"
                like = "%" + q + "%"
                args += [like, like, like, like]
            sql += " ORDER BY category, name"
            cur = conn.execute(sql, args)
            return [{"id": r[0], "category": r[1] or "", "name": r[2] or "",
                    "url": r[3] or "", "desc": r[4] or ""} for r in cur.fetchall()]
        finally:
            conn.close()

    def categories(self):
        conn = self._db()
        try:
            cur = conn.execute(
                "SELECT category, COUNT(*) FROM tools GROUP BY category ORDER BY category")
            return [{"name": r[0] or "", "count": r[1]} for r in cur.fetchall()]
        finally:
            conn.close()

    def add_tool(self, category, name, url="", desc=""):
        name = (name or "").strip()
        if not name:
            return {"error": "이름을 입력하세요."}
        conn = self._db()
        try:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            cur = conn.execute(
                "INSERT INTO tools(category,name,url,desc,created_at) VALUES(?,?,?,?,?)",
                ((category or "").strip() or "기타", name, (url or "").strip(), (desc or "").strip(), ts))
            conn.commit()
            return {"ok": True, "id": cur.lastrowid}
        finally:
            conn.close()

    def update_tool(self, id, category, name, url="", desc=""):
        name = (name or "").strip()
        if not name:
            return {"error": "이름을 입력하세요."}
        conn = self._db()
        try:
            conn.execute(
                "UPDATE tools SET category=?, name=?, url=?, desc=? WHERE id=?",
                ((category or "").strip() or "기타", name, (url or "").strip(), (desc or "").strip(), id))
            conn.commit()
            return {"ok": True}
        finally:
            conn.close()

    def delete_tool(self, id):
        conn = self._db()
        try:
            conn.execute("DELETE FROM tools WHERE id=?", (id,))
            conn.commit()
            return {"ok": True}
        finally:
            conn.close()

    def open_url(self, url):
        url = (url or "").strip()
        if not url:
            return {"error": "주소가 없습니다."}
        if not (url.startswith("http://") or url.startswith("https://")):
            url = "https://" + url
        try:
            os.startfile(url)
            return {"ok": True}
        except Exception as e:
            return {"error": str(e)}


# ============================ HTTP 서버 ============================
_API = None
_HTML = b""


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
    while True:
        time.sleep(3.0)
        if time.monotonic() - api.last_activity > timeout:
            os._exit(0)


def main():
    global _API, _HTML
    _API = Api()
    _HTML = open(_res(os.path.join("webui", "toolsdb.html")), "rb").read()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    _API.port = port
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    url = "http://127.0.0.1:%d/" % port
    browser = _find_chromium()
    profile = os.path.join(os.environ.get("TEMP", "."), "asstudio_tools_edge_%d" % port)
    if browser:
        subprocess.Popen([browser, "--app=" + url,
                          "--user-data-dir=" + profile,
                          "--window-size=1100,760",
                          "--no-first-run", "--no-default-browser-check"])
    else:
        import webbrowser
        webbrowser.open(url)

    time.sleep(4)
    _API.touch()
    _watchdog(_API)


if __name__ == "__main__":
    main()
