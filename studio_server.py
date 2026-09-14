"""
AS Studio 서버(기사용) — 로컬 웹서버 + Edge 앱창(msedge --app) 방식.

기사(기술자) PC 에서 실행되는 뷰어 역할: 고객 PC(studio_client.py 실행 측)에
WebRTC 뷰어로 접속하여 화면을 보며 원격 세션을 녹화하고, QR 코드로 고객
휴대폰과 사진/채팅/음성통화를 연결한다. 자체 PC 진단·복구·도구 기능은 없다
(해당 기능은 studio_client.py 측에 있다).
UI 는 webui/server.html 을 로컬 HTTP 로 서빙하고 Edge 를 앱 모드로 띄운다.
JS 는 fetch('/api/<method>') 로 Python 을 호출한다. 로직 모듈(webrtc/이력)은 그대로 재사용.
"""
import asyncio
import base64
import datetime
import io
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from html import escape as _esc
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from common import MSG_INPUT, MSG_DIAG_REQUEST, MSG_CMD_REQUEST
import webrtc_app
import diagnostic
import history
from viewer import RELAY_URL


def _res(rel):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


# Cloudflare 가 Python-urllib UA 를 봇으로 차단(403)하므로 일반 UA 사용
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AS-Studio/1.0"


# ============================ 원격 녹화 ============================
class Recorder:
    """수신된 원격 프레임(PIL)을 원본 해상도로 MP4(H.264) 녹화. 창 크기와 무관."""

    def __init__(self):
        self._c = None
        self._st = None
        self._t0 = None
        self._size = None
        self._lock = threading.Lock()
        self.path = None
        self.frames = 0

    def start(self, path, pil):
        import av
        w, h = pil.size
        w -= w % 2
        h -= h % 2                      # H.264 는 짝수 해상도 요구
        c = av.open(path, mode="w")
        st = c.add_stream("libx264", rate=15)
        st.width, st.height = w, h
        st.pix_fmt = "yuv420p"
        st.options = {"crf": "24", "preset": "veryfast"}
        self._c, self._st, self._size = c, st, (w, h)
        self._t0 = time.time()
        self.path, self.frames = path, 0

    def add(self, pil):
        import av
        with self._lock:
            if not self._c:
                return
            try:
                if pil.size != self._size:
                    pil = pil.resize(self._size)
                frame = av.VideoFrame.from_image(pil.convert("RGB"))
                for pkt in self._st.encode(frame):   # pts 는 인코더가 자동 할당(15fps)
                    self._c.mux(pkt)
                self.frames += 1
            except Exception:
                pass

    def stop(self):
        with self._lock:
            if not self._c:
                return None
            try:
                for pkt in self._st.encode(None):
                    self._c.mux(pkt)
                self._c.close()
            except Exception:
                pass
            p = self.path
            self._c = self._st = None
            return p


# ============================ API ============================
class Api:
    def __init__(self):
        self.relay = RELAY_URL
        self.data = None
        self.remote_running = False
        self.out_q = queue.Queue()
        self.bridge = {}
        self._frame = None
        self._frame_ver = 0
        self._frame_ts = 0.0         # 마지막 프레임 수신 시각 (무응답 워치독용)
        self._rstatus = "대기"
        self.last_activity = time.monotonic()
        self.timeline = []          # 세션 기록 [(HH:MM:SS, 이벤트)]
        self.port = None            # 이 인스턴스 HTTP 포트(옛 Edge 창 닫기용)
        self.repair_log = []        # 복구 실시간 진행 로그
        self.repair_ver = 0
        self.repair_busy = False
        self.recorder = None        # 원격 녹화
        self._recording = False
        self._rec_t0 = 0
        self._chat_files = {}       # 임시 채팅: 토큰 → 로컬 기록파일 경로(기사 PC 에만 저장)
        self._chat_logged = {}      # 토큰 → 이미 기록한 메시지 t 집합(중복 방지)
        self.company = self._settings_load().get("company", "AS Studio")
        self._diag_seen = 0         # bridge["diag_ver"] 폴링 위치(원격 진단 리포트 자동수신)
        self._client_info_seen = 0  # bridge["client_info_ver"] 폴링 위치(연결 시 자동수신 고객정보+사양)

    def touch(self):
        self.last_activity = time.monotonic()

    # ---- 앱 설정(업체명 등) — %APPDATA%\ASStudio\settings.json ----
    def _settings_path(self):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        d = os.path.join(base, "ASStudio")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "settings.json")

    def _settings_load(self):
        try:
            with open(self._settings_path(), "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def set_company(self, name):
        name = (name or "").strip() or "AS Studio"
        self.company = name
        try:
            d = self._settings_load()
            d["company"] = name
            with open(self._settings_path(), "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)
        except Exception:
            pass
        return {"ok": True, "company": name}

    # ---- 고객 관리(AS 이력) — SQLite, %APPDATA%\ASStudio\records.db ----
    def _db(self):
        import sqlite3
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        d = os.path.join(base, "ASStudio")
        os.makedirs(d, exist_ok=True)
        # 원격 세션이 백그라운드에서 주기적으로 기록을 갱신하는 동안 조회가 겹칠 수 있다.
        # WAL 모드는 읽기와 쓰기가 서로 막지 않게 하고, timeout 은 그래도 잠깐 걸리면 즉시 실패 대신 대기한다.
        conn = sqlite3.connect(os.path.join(d, "records.db"), timeout=10)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
        except Exception:
            pass
        conn.execute("""CREATE TABLE IF NOT EXISTS customers(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT UNIQUE, name TEXT, address TEXT, created_at TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS records(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER, ts TEXT,
            pc_summary TEXT, diag_summary TEXT, memo TEXT,
            cost TEXT, photos TEXT, video_path TEXT,
            FOREIGN KEY(customer_id) REFERENCES customers(id))""")
        # 기존 DB(구버전 스키마)에 새 컬럼 안전하게 추가
        for tbl, col, typ in (("customers", "address", "TEXT"),
                              ("records", "cost", "TEXT"),
                              ("records", "photos", "TEXT"),
                              ("records", "video_path", "TEXT"),
                              ("records", "report_path", "TEXT")):
            try:
                conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (tbl, col, typ))
            except Exception:
                pass
        return conn

    def customer_list(self, q=""):
        q = (q or "").strip()
        conn = self._db()
        try:
            cur = conn.cursor()
            sql = """SELECT c.id, c.phone, c.name,
                        COUNT(r.id) AS visits, MAX(r.ts) AS last_ts,
                        GROUP_CONCAT(r.cost, '|') AS costs
                      FROM customers c LEFT JOIN records r ON r.customer_id = c.id"""
            args = ()
            if q:
                sql += " WHERE c.phone LIKE ? OR c.name LIKE ?"
                args = ("%" + q + "%", "%" + q + "%")
            sql += " GROUP BY c.id ORDER BY last_ts DESC"
            cur.execute(sql, args)
            rows = cur.fetchall()
            out = []
            for r in rows:
                total = 0
                has_cost = False
                for part in (r[5] or "").split("|"):
                    digits = "".join(ch for ch in part if ch.isdigit())
                    if digits:
                        has_cost = True
                        total += int(digits)
                out.append({"id": r[0], "phone": r[1], "name": r[2] or "",
                           "visits": r[3], "last_ts": r[4] or "",
                           "total_cost": total if has_cost else None})
            return out
        finally:
            conn.close()

    def customer_detail(self, phone):
        phone = (phone or "").strip()
        conn = self._db()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id, phone, name, address FROM customers WHERE phone=?", (phone,))
            row = cur.fetchone()
            if not row:
                return None
            cid, phone, name, address = row
            cur.execute("""SELECT id, ts, pc_summary, diag_summary, memo, cost, photos, video_path, report_path
                           FROM records WHERE customer_id=? ORDER BY ts DESC""", (cid,))
            recs = []
            for r in cur.fetchall():
                try:
                    photos = json.loads(r[6]) if r[6] else []
                except Exception:
                    photos = []
                recs.append({"id": r[0], "ts": r[1], "pc_summary": r[2] or "",
                            "diag_summary": r[3] or "", "memo": r[4] or "",
                            "cost": r[5] or "", "photos": photos, "video_path": r[7] or "",
                            "report_path": r[8] or ""})
            return {"id": cid, "phone": phone, "name": name or "", "address": address or "",
                   "records": recs}
        finally:
            conn.close()

    def customer_update(self, phone, name="", address=""):
        phone = (phone or "").strip()
        if not phone:
            return {"error": "전화번호가 없습니다."}
        conn = self._db()
        try:
            conn.execute("UPDATE customers SET name=?, address=? WHERE phone=?",
                        (name, address, phone))
            conn.commit()
            return {"ok": True}
        finally:
            conn.close()

    def record_add(self, phone, name="", pc_summary="", diag_summary="", memo="",
                   address="", cost=""):
        phone = (phone or "").strip()
        if not phone:
            return {"error": "전화번호를 입력해 주세요."}
        conn = self._db()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM customers WHERE phone=?", (phone,))
            row = cur.fetchone()
            if row:
                cid = row[0]
                if name or address:
                    sets, args = [], []
                    if name: sets.append("name=?"); args.append(name)
                    if address: sets.append("address=?"); args.append(address)
                    args.append(cid)
                    cur.execute("UPDATE customers SET " + ",".join(sets) + " WHERE id=?", args)
            else:
                cur.execute("INSERT INTO customers(phone,name,address,created_at) VALUES(?,?,?,?)",
                           (phone, name, address, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                cid = cur.lastrowid
            cur.execute("""INSERT INTO records(customer_id,ts,pc_summary,diag_summary,memo,cost,photos)
                          VALUES(?,?,?,?,?,?,?)""",
                       (cid, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        pc_summary, diag_summary, memo, cost, "[]"))
            rid = cur.lastrowid
            conn.commit()
            self._log("고객 AS 기록 저장 (%s)" % phone)
            return {"ok": True, "id": rid, "phone": phone}
        finally:
            conn.close()

    def record_update(self, record_id, pc_summary=None, diag_summary=None, memo=None, cost=None):
        sets, args = [], []
        if pc_summary is not None:
            sets.append("pc_summary=?"); args.append(pc_summary)
        if diag_summary is not None:
            sets.append("diag_summary=?"); args.append(diag_summary)
        if memo is not None:
            sets.append("memo=?"); args.append(memo)
        if cost is not None:
            sets.append("cost=?"); args.append(cost)
        if not sets:
            return {"ok": True}
        args.append(record_id)
        conn = self._db()
        try:
            conn.execute("UPDATE records SET " + ",".join(sets) + " WHERE id=?", args)
            conn.commit()
            return {"ok": True}
        finally:
            conn.close()

    def record_add_photo(self, record_id, url):
        conn = self._db()
        try:
            cur = conn.cursor()
            cur.execute("SELECT photos FROM records WHERE id=?", (record_id,))
            row = cur.fetchone()
            if not row:
                return {"error": "기록을 찾을 수 없습니다."}
            try:
                photos = json.loads(row[0]) if row[0] else []
            except Exception:
                photos = []
            if url not in photos:
                photos.append(url)
            cur.execute("UPDATE records SET photos=? WHERE id=?",
                       (json.dumps(photos, ensure_ascii=False), record_id))
            conn.commit()
            return {"ok": True, "count": len(photos)}
        finally:
            conn.close()

    def record_set_video(self, record_id, path):
        conn = self._db()
        try:
            conn.execute("UPDATE records SET video_path=? WHERE id=?", (path, record_id))
            conn.commit()
            return {"ok": True}
        finally:
            conn.close()

    def record_open_video(self, path):
        if path and os.path.exists(path):
            try:
                os.startfile(path)
                return {"ok": True}
            except Exception as e:
                return {"error": str(e)}
        return {"error": "파일을 찾을 수 없습니다."}

    def record_set_report(self, record_id, path):
        conn = self._db()
        try:
            conn.execute("UPDATE records SET report_path=? WHERE id=?", (path, record_id))
            conn.commit()
            return {"ok": True}
        finally:
            conn.close()

    def record_open_report(self, path):
        if path and os.path.exists(path):
            try:
                os.startfile(path)
                return {"ok": True}
            except Exception as e:
                return {"error": str(e)}
        return {"error": "파일을 찾을 수 없습니다."}

    def record_delete(self, record_id):
        conn = self._db()
        try:
            conn.execute("DELETE FROM records WHERE id=?", (record_id,))
            conn.commit()
            return {"ok": True}
        finally:
            conn.close()

    def client_info_pending(self):
        """연결 성사 시 클라이언트가 자동 전송한 고객정보+PC사양이 새로 도착했으면 반환."""
        ver = self.bridge.get("client_info_ver", 0)
        if ver <= self._client_info_seen:
            return None
        self._client_info_seen = ver
        return self.bridge.get("client_info")

    def diag_report_pending(self):
        """원격 진단 리포트가 새로 도착했으면 요약+저장경로를 반환(AS 기록 자동 채우기용).
        건강 요약 + 호스트명/OS + 디스크 여유공간 + 하드웨어 의심 소견 + 보안 점검 + 이번 세션 작업 로그.
        전체 HTML 리포트는 고객별로 다시 열어볼 수 있도록 영구 폴더에 저장한다."""
        ver = self.bridge.get("diag_ver", 0)
        if ver <= self._diag_seen:
            return None
        self._diag_seen = ver
        html = self.bridge.get("diag_html", "") or ""
        summary = self.bridge.get("diag_summary", {}) or {}
        import re as _re
        text = _re.sub(r"<[^>]+>", " ", html)
        text = _re.sub(r"\s+", " ", text).strip()[:400]
        lines = [text] if text else []
        hn, os_, build = summary.get("hostname"), summary.get("os"), summary.get("build")
        if hn or os_:
            lines.append("PC: %s · %s%s" % (hn or "?", os_ or "?",
                         (" (빌드 %s)" % build) if build else ""))
        disks = summary.get("disks") or []
        if disks:
            lines.append("디스크: " + ", ".join(
                "%s %s%%사용/%s여유" % (d.get("device", "?"), d.get("percent", "?"), d.get("free_gb", "?"))
                for d in disks))
        hw = summary.get("hardware_suspicion") or []
        if hw:
            lines.append("하드웨어 의심: " + "; ".join(
                "%s(%s) %s" % (h.get("part", "?"), h.get("level", "?"), h.get("reason", "")) for h in hw))
        sec = summary.get("security") or {}
        sec_issues = [("%s: %s" % (k, v)) for k, v in sec.items() if "위험" in str(v)]
        if sec_issues:
            lines.append("보안 점검: " + ", ".join(sec_issues))
        log = summary.get("session_log") or []
        if log:
            lines.append("이번 세션 작업: " + "; ".join("%s %s" % (t, e) for t, e in log[-10:]))
        summary_text = "\n".join(lines) if lines else None

        report_path = ""
        if html:
            try:
                docs = os.path.join(os.path.expanduser("~"), "Documents", "AS진단리포트")
                os.makedirs(docs, exist_ok=True)
                safe_hn = _re.sub(r"[^0-9A-Za-z가-힣_-]+", "_", hn or "PC")[:40]
                fn = "진단_%s_%s.html" % (time.strftime("%Y%m%d_%H%M%S"), safe_hn)
                report_path = os.path.join(docs, fn)
                with open(report_path, "w", encoding="utf-8") as f:
                    f.write(html)
            except Exception:
                report_path = ""

        if not summary_text and not report_path:
            return None
        return {"summary": summary_text or "", "report_path": report_path}

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
        return {"relay": self.relay, "admin": diagnostic.is_admin(), "company": self.company}

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

    # ---- 모바일 지원 (QR → 고객 폰 사진 업로드) ----
    def _http_base(self):
        b = (self.relay or "").replace("wss://", "https://").replace("ws://", "http://")
        if b.endswith("/relay"):
            b = b[:-len("/relay")]
        return b or "https://mylineal.com"

    def mobile_new(self):
        import urllib.request
        base = self._http_base()
        try:
            req = urllib.request.Request(base + "/m/new", data=b"", method="POST",
                                         headers={"User-Agent": _UA})
            tok = json.loads(urllib.request.urlopen(req, timeout=10).read()).get("token")
        except Exception as e:
            return {"error": "서버 연결 실패: %s" % e}
        if not tok:
            return {"error": "토큰 발급 실패"}
        url = base + "/m/" + tok
        qr = ""
        try:
            import qrcode
            q = qrcode.QRCode(box_size=8, border=2)
            q.add_data(url)
            q.make(fit=True)
            img = q.make_image(fill_color="#3a2f26", back_color="white").convert("RGB")
            buf = io.BytesIO()
            img.save(buf, "PNG")
            qr = base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception:
            qr = ""
        self._log("모바일 지원 QR 발급")
        return {"token": tok, "url": url, "qr": qr}

    def mobile_list(self, token):
        import urllib.request
        token = (token or "").strip()
        if not token:
            return {"items": []}
        base = self._http_base()
        try:
            req = urllib.request.Request(base + "/m/" + token + "/list", headers={"User-Agent": _UA})
            data = json.loads(urllib.request.urlopen(req, timeout=10).read())
        except Exception:
            return {"items": []}
        items = data.get("items", [])
        for it in items:
            it["full"] = base + it.get("url", "")
        return {"items": items}

    # ---- QR 임시 채팅 (고객 폰 ↔ 기사, 기록은 기사 PC 에만 자동저장) ----
    def _chat_write(self, token, frm, text, t):
        """상담 메시지를 로컬 기록파일에 1회만 append (고객·기사 양방향)."""
        seen = self._chat_logged.setdefault(token, set())
        if not t or t in seen:
            return
        seen.add(t)
        path = self._chat_files.get(token)
        if not path:
            return
        who = "고객" if frm == "c" else "기사"
        try:
            ts = time.strftime("%H:%M", time.localtime(t / 1000.0))
        except Exception:
            ts = time.strftime("%H:%M")
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write("[%s] %s: %s\n" % (ts, who, text))
        except Exception:
            pass

    def chat_new(self):
        import urllib.request
        base = self._http_base()
        try:
            req = urllib.request.Request(base + "/m/new", data=b"", method="POST",
                                         headers={"User-Agent": _UA})
            tok = json.loads(urllib.request.urlopen(req, timeout=10).read()).get("token")
        except Exception as e:
            return {"error": "서버 연결 실패: %s" % e}
        if not tok:
            return {"error": "토큰 발급 실패"}
        url = base + "/m/" + tok + "/chat"
        # 상담 기록 파일 생성 (기사 PC 에만 저장)
        logfile = ""
        try:
            docs = os.path.join(os.path.expanduser("~"), "Documents", "원격상담기록")
            os.makedirs(docs, exist_ok=True)
            logfile = os.path.join(docs, "상담_%s_%s.txt" % (time.strftime("%Y%m%d_%H%M"), tok[:8]))
            with open(logfile, "w", encoding="utf-8") as f:
                f.write("원격 지원 상담 기록\n생성: %s\n토큰: %s\n%s\n\n"
                        % (time.strftime("%Y-%m-%d %H:%M:%S"), tok, "-" * 40))
            self._chat_files[tok] = logfile
            self._chat_logged[tok] = set()
        except Exception:
            pass
        # QR 생성
        qr = ""
        try:
            import qrcode
            q = qrcode.QRCode(box_size=8, border=2)
            q.add_data(url)
            q.make(fit=True)
            img = q.make_image(fill_color="#3a2f26", back_color="white").convert("RGB")
            buf = io.BytesIO()
            img.save(buf, "PNG")
            qr = base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception:
            qr = ""
        self._log("상담 채팅 QR 발급")
        callurl = base + "/m/" + tok + "/call?r=host"
        return {"token": tok, "url": url, "qr": qr, "logfile": logfile, "callurl": callurl}

    def chat_send(self, token, text):
        import urllib.request
        token = (token or "").strip()
        text = (text or "").strip()
        if not token or not text:
            return {"error": "빈 메시지"}
        base = self._http_base()
        try:
            body = json.dumps({"from": "t", "text": text}).encode("utf-8")
            req = urllib.request.Request(base + "/m/" + token + "/send", data=body, method="POST",
                                         headers={"User-Agent": _UA, "Content-Type": "application/json"})
            j = json.loads(urllib.request.urlopen(req, timeout=10).read())
        except Exception as e:
            return {"error": "전송 실패: %s" % e}
        t = j.get("t")
        if t:
            self._chat_write(token, "t", text, t)
        return {"ok": bool(t), "t": t}

    def chat_poll(self, token, since=0):
        import urllib.request
        token = (token or "").strip()
        if not token:
            return {"msgs": []}
        try:
            since = int(since or 0)
        except Exception:
            since = 0
        base = self._http_base()
        try:
            req = urllib.request.Request(base + "/m/" + token + "/poll?since=%d" % since,
                                         headers={"User-Agent": _UA})
            j = json.loads(urllib.request.urlopen(req, timeout=10).read())
        except Exception:
            return {"msgs": []}
        msgs = j.get("msgs", [])
        for m in msgs:
            self._chat_write(token, m.get("f"), m.get("x", ""), m.get("t"))
        return {"msgs": msgs}

    def chat_openlog(self, token):
        path = self._chat_files.get((token or "").strip())
        if path and os.path.exists(path):
            try:
                os.startfile(path)
                return {"ok": True, "path": path}
            except Exception as e:
                return {"error": str(e)}
        return {"error": "기록 파일이 없습니다"}
    # ---- 원격 ----
    def remote_connect(self, code):
        code = (code or "").strip()
        if not code or self.remote_running:
            return False
        self.remote_running = True
        threading.Thread(target=self._remote_loop, args=(code,), daemon=True).start()
        self._log("원격 연결 시작 (코드 %s)" % code)
        return True

    def remote_disconnect(self):
        self.remote_running = False
        self._log("원격 연결 종료")
        return True

    def _remote_loop(self, code):
        async def run():
            fails = 0
            while self.remote_running:
                connected_this_round = [False]

                def tracking_status(s, _box=connected_this_round):
                    if "제어 중" in s:
                        _box[0] = True
                    self._set_status(s)

                try:
                    await webrtc_app.viewer_run(self.relay, code, self._on_frame,
                                                self.out_q, tracking_status,
                                                lambda: self.remote_running, bridge=self.bridge,
                                                touch_fn=self.touch)
                except (Exception, asyncio.CancelledError) as e:
                    self._log("원격 연결 오류: %s: %s" % (type(e).__name__, e))

                if connected_this_round[0]:
                    reason = self.bridge.pop("last_p2p_state", None)
                    if reason:
                        self._log("P2P 연결 끊김 (상태: %s)" % reason)

                fails = 0 if connected_this_round[0] else fails + 1
                if self.remote_running:
                    if fails >= 15:
                        self._set_status("연결 실패 — 코드를 확인하고 다시 연결하세요")
                        self.remote_running = False
                        break
                    self._set_status("연결이 끊어졌습니다 · 재연결 중... (%d)" % fails)
                    await asyncio.sleep(1)
        try:
            asyncio.run(run())
        except Exception:
            pass

    def _on_frame(self, pil):
        try:
            if self._recording:
                if self.recorder is None:
                    import datetime
                    self.recorder = Recorder()
                    fn = "원격녹화_%s.mp4" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                    path = os.path.join(os.environ.get("USERPROFILE", ""), "Videos", fn)
                    try:
                        os.makedirs(os.path.dirname(path), exist_ok=True)
                        self.recorder.start(path, pil)
                    except Exception:
                        self.recorder = None
                        self._recording = False
                if self.recorder:
                    self.recorder.add(pil)
            buf = io.BytesIO()
            pil.convert("RGB").save(buf, "JPEG", quality=70)
            self._frame = base64.b64encode(buf.getvalue()).decode("ascii")
            self._frame_ver += 1
            self._frame_ts = time.monotonic()
        except Exception:
            pass

    def rec_start(self):
        if not self.remote_running:
            return {"error": "원격 연결 중에만 녹화할 수 있습니다."}
        if self._recording:
            return {"error": "이미 녹화 중입니다."}
        self.recorder = None
        self._recording = True
        self._rec_t0 = time.time()
        self._log("원격 녹화 시작")
        return {"ok": True}

    def rec_stop(self):
        if not self._recording:
            return {"error": "녹화 중이 아닙니다."}
        self._recording = False
        rec = self.recorder
        self.recorder = None
        path = rec.stop() if rec else None
        frames = rec.frames if rec else 0
        self._log("원격 녹화 종료 (%d 프레임)" % frames)
        if path and frames > 0:
            try:
                os.startfile(os.path.dirname(path))   # 저장 폴더 열기
            except Exception:
                pass
            return {"ok": True, "path": path, "frames": frames}
        return {"error": "녹화된 프레임이 없습니다."}

    def rec_status(self):
        return {"recording": self._recording,
                "secs": int(time.time() - self._rec_t0) if self._recording else 0,
                "frames": self.recorder.frames if self.recorder else 0}

    def _set_status(self, s):
        self._rstatus = s

    def remote_status(self):
        return self._rstatus

    def remote_get_frame(self, seen):
        try:
            seen = int(seen)
        except Exception:
            seen = 0
        if self._frame is not None and self._frame_ver > seen:
            return [self._frame_ver, self._frame]
        return None

    def remote_input(self, ev):
        try:
            self.out_q.put((MSG_INPUT, json.dumps(ev).encode("utf-8")))
        except Exception:
            pass
        return True

    def remote_request_diag(self, auto=False):
        self.out_q.put((MSG_DIAG_REQUEST, b"auto" if auto else b""))
        if not auto:
            self._log("원격 진단 요청")
        return True

    # ---- 원격 명령(CMD / PowerShell) ----
    def remote_cmd(self, shell, cmd):
        shell = "powershell" if shell == "powershell" else "cmd"
        try:
            self.out_q.put((MSG_CMD_REQUEST,
                            json.dumps({"shell": shell, "cmd": cmd}).encode("utf-8")))
        except Exception:
            return False
        self._log("원격 명령(%s): %s" % (shell, (cmd or "")[:100]))
        return True

    def remote_cmd_result(self, seen):
        try:
            seen = int(seen)
        except Exception:
            seen = 0
        ver = self.bridge.get("cmd_ver", 0)
        if ver > seen:
            return [ver, self.bridge.get("cmd_result", "")]
        return None

    def remote_send_file(self):
        path = _open_dialog()
        if not path:
            return False
        fn = self.bridge.get("send_file")
        if fn:
            fn(path)
        return True


# ---- 파일 대화상자 (tkinter — pythonnet 아님, 안전) ----
def _open_dialog():
    try:
        import tkinter
        from tkinter import filedialog
        r = tkinter.Tk()
        r.withdraw()
        r.attributes("-topmost", True)
        p = filedialog.askopenfilename(parent=r, title="보낼 파일 선택")
        r.destroy()
        return p or None
    except Exception:
        return None



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
    _HTML = open(_res(os.path.join("webui", "server.html")), "rb").read()
    try:
        _ICON = open(_res(os.path.join("webui", "icon.ico")), "rb").read()
    except Exception:
        _ICON = b""

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    _API.port = port          # 이 인스턴스의 Edge 창을 식별(재시작 시 닫기)용
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    url = "http://127.0.0.1:%d/" % port
    browser = _find_chromium()
    # 인스턴스별 고유 프로필 → 재시작 시 옛 창이 새 창에 붙지 않고 독립적으로 닫힘
    profile = os.path.join(os.environ.get("TEMP", "."), "asstudio_edge_%d" % port)
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
