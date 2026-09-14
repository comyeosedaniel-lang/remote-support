"""
고객(진단) 이력 — 진단 리포트를 로컬에 저장하고 조회.
저장 위치: %LOCALAPPDATA%\\PCDiagnostic\\history (JSON 요약 + HTML 리포트)
GUI/Studio 에서 과거 진단을 다시 볼 수 있게 한다.
"""
import json
import os
import time


def _dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "PCDiagnostic", "history")
    os.makedirs(d, exist_ok=True)
    return d


def save(data, analysis_result=None, html=None):
    """진단 결과를 이력에 저장. base 경로(확장자 제외) 반환."""
    try:
        import analysis as _an
        a = analysis_result or _an.analyze(data)
    except Exception:
        a = {"score": 0, "grade": "?", "summary_customer": "", "causes": []}
    host = data.get("system", {}).get("hostname", "PC")
    ts = data.get("collected_at", int(time.time()))
    entry = {
        "time": ts,
        "hostname": host,
        "score": a.get("score"),
        "grade": a.get("grade"),
        "summary": a.get("summary_customer", ""),
        "causes": [c["label"] for c in a.get("causes", [])],
    }
    d = _dir()
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(ts))
    safe_host = "".join(ch for ch in host if ch.isalnum() or ch in "-_") or "PC"
    base = os.path.join(d, "%s_%s" % (safe_host, stamp))
    try:
        with open(base + ".json", "w", encoding="utf-8") as f:
            json.dump(entry, f, ensure_ascii=False, indent=2)
        if html:
            with open(base + ".html", "w", encoding="utf-8") as f:
                f.write(html)
    except Exception:
        pass
    return base


def list_history(hostname=None, limit=100):
    """과거 진단 목록 (최신순). 각 항목에 _file(base 경로) 포함."""
    d = _dir()
    out = []
    try:
        names = os.listdir(d)
    except Exception:
        names = []
    for fn in names:
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, fn), encoding="utf-8") as f:
                e = json.load(f)
            if hostname and e.get("hostname") != hostname:
                continue
            e["_file"] = os.path.join(d, fn[:-5])
            out.append(e)
        except Exception:
            pass
    out.sort(key=lambda e: e.get("time", 0), reverse=True)
    return out[:limit]


def format_list(items):
    """이력 목록을 사람이 읽는 텍스트로."""
    if not items:
        return "저장된 진단 이력이 없습니다."
    lines = ["[진단 이력] %d건\n" % len(items)]
    for e in items:
        t = time.strftime("%Y-%m-%d %H:%M", time.localtime(e.get("time", 0)))
        causes = ", ".join(e.get("causes", [])[:3]) or "이상 없음"
        lines.append("  %s  [%s] %s/100  — %s" % (t, e.get("hostname", "?"),
                                                  e.get("score", "?"), causes))
    return "\n".join(lines)
