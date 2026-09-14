"""
PC 진단 분석 엔진 (규칙 기반).
diagnostic.collect_all() 결과를 받아:
  - 건강 점수 (0~100)
  - 원인 우선순위 [ {label, severity, probability, tech, customer, fixes[]} ]
  - 기사용 요약 / 고객용 쉬운 요약
를 만든다. 실제 LLM 백엔드로 업그레이드 가능하도록 순수 함수로 구성.

각 원인의 fixes 는 repair.py 의 액션 키를 참조 (자동 복구 매핑용):
  flush_dns, renew_ip, reset_winsock, reset_tcpip, run_sfc, run_dism, manual
"""

SEV_WEIGHT = {"high": 25, "medium": 12, "low": 5}


def analyze(data):
    causes = []

    # 디스크 공간
    for v in data.get("disks", {}).get("volumes", []):
        pct = v.get("percent", 0)
        if pct >= 95:
            causes.append(_cause(
                "저장공간 거의 가득참 (%s)" % v["device"], "high", 90,
                "%s 사용률 %s%%, %sGB 남음. 공간 부족은 성능 저하·업데이트 실패·오류의 흔한 원인."
                % (v["device"], pct, v["free_gb"]),
                "%s 드라이브가 거의 꽉 찼어요. 사진·영상·안 쓰는 프로그램을 정리하면 빨라집니다." % v["device"],
                ["임시파일/휴지통 정리", "큰 파일 이동·삭제", "안 쓰는 프로그램 제거"], "manual"))
        elif pct >= 90:
            causes.append(_cause(
                "저장공간 부족 (%s)" % v["device"], "medium", 70,
                "%s 사용률 %s%%. 여유 10%% 미만은 관리 권장." % (v["device"], pct),
                "%s 드라이브 공간이 부족합니다. 정리를 권장합니다." % v["device"],
                ["임시파일 정리", "안 쓰는 파일 정리"], "manual"))

    # 인터넷
    net = data.get("network", {})
    if net.get("adapters") and not net.get("internet"):
        causes.append(_cause(
            "인터넷 연결 안 됨", "high", 85,
            "외부(8.8.8.8) 접속 실패. NAT/DNS/Winsock 손상 또는 회선 문제 가능.",
            "인터넷이 안 됩니다. 자동 복구를 시도해볼 수 있어요.",
            ["DNS 초기화", "IP 갱신", "(관리자) Winsock·TCP/IP 초기화 후 재부팅"],
            "flush_dns,renew_ip,reset_winsock,reset_tcpip"))
    elif net.get("internet") and not net.get("dns"):
        causes.append(_cause(
            "DNS 조회 실패", "high", 80,
            "인터넷은 되지만 이름 조회 실패. DNS 캐시 손상/설정 문제.",
            "인터넷은 되는데 사이트 주소를 못 찾습니다. DNS 초기화로 해결되는 경우가 많아요.",
            ["DNS 캐시 초기화"], "flush_dns"))

    # 드라이버/장치
    pd = data.get("problem_devices", [])
    if pd:
        names = ", ".join(d["name"] for d in pd[:3])
        causes.append(_cause(
            "드라이버/장치 문제 %d건" % len(pd), "high", 75,
            "장치 관리자 오류 장치: %s%s. 드라이버 미설치/충돌." % (names, " 등" if len(pd) > 3 else ""),
            "일부 장치(그래픽·소리·네트워크 등) 드라이버에 문제가 있습니다. 재설치가 필요할 수 있어요.",
            ["장치 드라이버 재설치/업데이트"], "manual"))

    # 온도
    for label, temp in data.get("temps", {}).items():
        if temp >= 90:
            causes.append(_cause(
                "%s 과열 (%s°C)" % (label, temp), "high", 80,
                "%s %s°C — 발열 심각. 먼지/써멀 문제 가능, 성능 저하·꺼짐 유발." % (label, temp),
                "%s 온도가 매우 높습니다. 내부 먼지 청소·통풍 확인이 필요합니다." % label,
                ["내부 먼지 청소", "통풍 확인", "써멀 재도포(필요시)"], "manual"))
        elif temp >= 82:
            causes.append(_cause(
                "%s 온도 높음 (%s°C)" % (label, temp), "medium", 55,
                "%s %s°C — 다소 높음. 부하 시 주의." % (label, temp),
                "%s 온도가 조금 높습니다. 먼지 청소를 권장합니다." % label,
                ["내부 먼지 청소"], "manual"))

    # 메모리
    m = data.get("memory", {})
    if m.get("percent", 0) >= 92:
        causes.append(_cause(
            "메모리 부족 (%s%%)" % m["percent"], "medium", 60,
            "메모리 사용률 %s%%. 프로그램 과다 또는 RAM 용량 부족." % m["percent"],
            "메모리가 거의 다 찼습니다. 프로그램을 줄이거나 RAM 증설을 고려하세요.",
            ["불필요한 프로그램 종료", "시작프로그램 정리", "RAM 증설 검토"], "manual"))

    # 시스템 파일/오류 (이벤트 로그 다수)
    if len(data.get("event_log", [])) >= 10:
        causes.append(_cause(
            "시스템 오류 이벤트 다수", "medium", 50,
            "최근 시스템 오류가 잦음. 시스템 파일 손상 가능 → SFC/DISM 권장.",
            "시스템에 오류가 자주 기록됩니다. 시스템 파일 검사·복구를 권장합니다.",
            ["시스템 파일 검사(SFC)", "이미지 복구(DISM)"], "run_sfc,run_dism"))

    # 시작 프로그램
    if len(data.get("startup", [])) > 20:
        causes.append(_cause(
            "시작 프로그램 과다 (%d개)" % len(data["startup"]), "low", 40,
            "부팅 시 %d개 자동 실행 → 부팅 느림." % len(data["startup"]),
            "켤 때 실행되는 프로그램이 많아 부팅이 느립니다. 정리하면 빨라집니다.",
            ["불필요한 시작 프로그램 사용 안 함"], "manual"))

    # 정렬: severity -> probability
    order = {"high": 0, "medium": 1, "low": 2}
    causes.sort(key=lambda c: (order[c["severity"]], -c["probability"]))

    score = 100 - sum(SEV_WEIGHT[c["severity"]] for c in causes)
    score = max(0, min(100, score))

    return {
        "score": score,
        "grade": _grade(score),
        "causes": causes,
        "summary_tech": _summary_tech(score, causes),
        "summary_customer": _summary_customer(score, causes),
    }


def _cause(label, severity, probability, tech, customer, fixes, action_keys):
    return {"label": label, "severity": severity, "probability": probability,
            "tech": tech, "customer": customer, "fixes": fixes, "actions": action_keys}


def _grade(score):
    if score >= 85:
        return "양호"
    if score >= 65:
        return "보통"
    if score >= 40:
        return "주의"
    return "불량"


def _summary_tech(score, causes):
    if not causes:
        return "특이 이상 없음. 건강 점수 %d/100." % score
    top = causes[0]
    return ("건강 점수 %d/100 (%s). 우선순위 1위: %s (확률 %d%%). 총 %d개 원인 감지."
            % (score, _grade(score), top["label"], top["probability"], len(causes)))


def _summary_customer(score, causes):
    if not causes:
        return "컴퓨터 상태가 양호합니다. 특별한 문제는 없습니다."
    high = [c for c in causes if c["severity"] == "high"]
    if high:
        return ("가장 시급한 문제는 '%s' 입니다. %s"
                % (high[0]["label"].split(" (")[0], high[0]["customer"]))
    return "큰 문제는 없지만 몇 가지 개선점이 있습니다: " + causes[0]["customer"]


# ---------------- 자동 복구 매핑 ----------------
def recommended_actions(analysis, admin=False):
    """분석 결과에서 '자동 실행 가능한' repair 액션 키를 순서대로 수집 (중복 제거)."""
    ordered = []
    for c in analysis["causes"]:
        for key in (c.get("actions") or "").split(","):
            key = key.strip()
            if not key or key == "manual":
                continue
            if not admin and key in ("reset_winsock", "reset_tcpip", "run_sfc", "run_dism"):
                continue
            if key not in ordered:
                ordered.append(key)
    return ordered


def report_lines(analysis):
    """텍스트 리포트용 라인."""
    L = ["[AI 분석] 건강 점수 %d/100 (%s)" % (analysis["score"], analysis["grade"])]
    L.append("  기사: " + analysis["summary_tech"])
    L.append("  고객: " + analysis["summary_customer"])
    if analysis["causes"]:
        L.append("  원인 우선순위:")
        for i, c in enumerate(analysis["causes"], 1):
            L.append("   %d. [%s %d%%] %s" % (i, c["severity"].upper(), c["probability"], c["label"]))
            L.append("      해결: " + " → ".join(c["fixes"]))
    return L


def hardware_suspicion(data):
    """수집 데이터로 하드웨어 고장 '의심'을 추론(100% 확정 아님).
    반환: [{part, level('높음'/'중간'/'낮음'), reason}]."""
    out = []
    temps = data.get("temps") or {}
    # 저장장치 SMART/수명/온도
    for ph in (data.get("disks") or {}).get("physical", []):
        h = str(ph.get("health", ""))
        if h not in ("정상", "OK", "Healthy", "?", ""):
            out.append({"part": "저장장치 %s" % ph.get("model", ""), "level": "높음",
                        "reason": "SMART 상태 '%s' — 디스크 고장 의심, 즉시 백업 권장" % h})
    for sm in data.get("smart", []):
        w = sm.get("wear_pct")
        if w is not None and w <= 20:
            out.append({"part": "SSD %s" % sm.get("model", ""), "level": "높음",
                        "reason": "남은 수명 %s%% — 교체 임박" % w})
        tc = sm.get("temp_c")
        if tc and tc >= 70:
            out.append({"part": "디스크 %s" % sm.get("model", ""), "level": "중간",
                        "reason": "디스크 온도 %d°C — 발열 과다" % tc})
    # 블루스크린 버그체크 코드 기반
    for e in (data.get("bluescreen") or {}).get("events", []):
        nm, code = e.get("name", ""), e.get("code", "")
        if "메모리 불량" in nm:
            out.append({"part": "메모리(RAM)", "level": "높음",
                        "reason": "블루스크린 %s — RAM 불량 의심(메모리 진단 권장)" % code})
        elif "WHEA" in nm or "하드웨어" in nm:
            out.append({"part": "CPU/메모리/메인보드", "level": "높음",
                        "reason": "블루스크린 %s(WHEA) — 하드웨어 오류 의심" % code})
        elif "그래픽" in nm:
            out.append({"part": "그래픽카드(GPU)", "level": "중간",
                        "reason": "블루스크린 %s — GPU 드라이버/하드웨어 의심" % code})
        elif "SSD 펌웨어" in nm:
            out.append({"part": "SSD", "level": "중간",
                        "reason": "블루스크린 %s — SSD 펌웨어/연결 의심" % code})
        elif "CPU/오버클럭" in nm:
            out.append({"part": "CPU", "level": "중간",
                        "reason": "블루스크린 %s — CPU/오버클럭 불안정 의심" % code})
    # 과열
    for k, v in temps.items():
        if v >= 90:
            out.append({"part": k, "level": "중간",
                        "reason": "%s 온도 %d°C — 과열(쿨링/써멀그리스 점검)" % (k, v)})
    # 팬 정지 + 고온
    fans = data.get("fans") or {}
    if fans and any(r == 0 for r in fans.values()) and any(v >= 60 for v in temps.values()):
        out.append({"part": "쿨링팬", "level": "중간",
                    "reason": "팬 0 RPM인데 온도 높음 — 팬 고장 의심"})
    # 배터리
    bat = data.get("battery") or {}
    if isinstance(bat, dict) and bat.get("health_pct") is not None and bat["health_pct"] < 50:
        out.append({"part": "배터리", "level": "중간",
                    "reason": "배터리 수명 %s%% — 노후, 교체 고려" % bat["health_pct"]})
    # 이벤트 로그: 디스크 오류
    for ev in (data.get("event_log") or []):
        src = (ev.get("source") or "").lower()
        if src in ("disk", "ntfs"):
            out.append({"part": "저장장치", "level": "중간",
                        "reason": "이벤트 로그에 디스크(%s) 오류 — 디스크/케이블 점검" % ev.get("source")})
            break
    return out
