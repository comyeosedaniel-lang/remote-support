"""
인터넷 복구 조치 (Windows).
  - DNS 캐시 초기화 / IP 갱신 : 일반 권한, 즉시
  - Winsock 초기화 / TCP-IP 초기화 : 관리자 권한 필요 + 재부팅 후 적용
각 조치는 (성공여부, 메시지) 반환. GUI/원격에서 확인 후 호출한다.
"""
import subprocess
import sys

_NOWINDOW = 0x08000000 if sys.platform == "win32" else 0   # CREATE_NO_WINDOW


def _run(cmd, timeout=40):
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           creationflags=_NOWINDOW)
        out = (r.stdout or b"") + (r.stderr or b"")
        text = out.decode("mbcs", "ignore").strip() if out else ""
        return r.returncode == 0, text
    except Exception as e:
        return False, str(e)


def _is_admin():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def flush_dns():
    return _run(["ipconfig", "/flushdns"])


def renew_ip():
    _run(["ipconfig", "/release"], timeout=25)
    return _run(["ipconfig", "/renew"], timeout=50)


def reset_winsock():        # 관리자 + 재부팅
    return _run(["netsh", "winsock", "reset"])


def reset_tcpip():          # 관리자 + 재부팅
    return _run(["netsh", "int", "ip", "reset"])


# ---------------- Windows 복구 (관리자, 오래 걸림) ----------------
def run_sfc():
    """시스템 파일 검사·복구 (sfc /scannow). 수 분 소요."""
    return _run(["sfc", "/scannow"], timeout=900)


def run_dism():
    """Windows 이미지 복구 (DISM RestoreHealth). 수 분~십수 분 소요."""
    return _run(["DISM", "/Online", "/Cleanup-Image", "/RestoreHealth"], timeout=1800)


# ---------------- 성능 / 시스템 조정 ----------------
def power_high_performance():
    """전원 계획을 '고성능'으로 변경 (powercfg /setactive). 관리자 없어도 대개 동작."""
    ok, out = _run(["powercfg", "/setactive",
                    "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"], timeout=15)
    if ok:
        return True, "전원 계획을 고성능으로 변경했습니다."
    return False, "전원 계획 변경 실패: %s" % (out or "알 수 없는 오류")


def disable_fast_startup():
    """빠른 시작(Fast Startup) 끄기. 관리자 권한 필요. 종료/재부팅 문제에 도움."""
    if not _is_admin():
        return False, "관리자 권한이 필요합니다. 관리자로 다시 실행해 주세요."
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r"SYSTEM\CurrentControlSet\Control\Session Manager\Power",
                             0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, "HiberbootEnabled", 0, winreg.REG_DWORD, 0)
        winreg.CloseKey(key)
        return True, "빠른 시작을 껐습니다. 다음 종료부터 완전 종료로 동작합니다."
    except Exception as e:
        return False, "빠른 시작 끄기 실패: %s" % e


def backup_wifi_profiles(dest_dir=None):
    """저장된 Wi-Fi 프로파일을 XML 로 내보내기(백업). 비밀번호 포함(key=clear)."""
    import os
    try:
        if not dest_dir:
            dest_dir = os.path.join(os.environ.get("USERPROFILE", ""),
                                    "Desktop", "WiFi백업")
        os.makedirs(dest_dir, exist_ok=True)
    except Exception as e:
        return False, "백업 폴더 생성 실패: %s" % e
    ok, out = _run(["netsh", "wlan", "export", "profile", "key=clear",
                    "folder=%s" % dest_dir], timeout=15)
    if ok:
        return True, "Wi-Fi 프로파일을 백업했습니다:\n%s" % dest_dir
    return False, "Wi-Fi 백업 실패(무선 어댑터가 없을 수 있음): %s" % (out or "")


def reregister_store_apps():
    """Microsoft Store 앱 재등록 (시작 메뉴/스토어 문제 복구). 관리자 필요, 수 분 소요."""
    if not _is_admin():
        return False, "관리자 권한이 필요합니다. 관리자로 다시 실행해 주세요."
    ps = ('Get-AppxPackage -AllUsers | Foreach {Add-AppxPackage '
          '-DisableDevelopmentMode -Register '
          '"$($_.InstallLocation)\\AppXManifest.xml" -ErrorAction SilentlyContinue}')
    ok, out = _run(["powershell", "-NoProfile", "-Command", ps], timeout=120)
    if ok:
        return True, "Microsoft Store 앱을 재등록했습니다. (몇 분 걸릴 수 있습니다)"
    return False, "Store 앱 재등록 실패 또는 시간 초과: %s" % (out or "")


def create_restore_point(desc="AS Studio 복구 지점"):
    """시스템 복원 지점 생성 (Checkpoint-Computer). 관리자 권한 필요.
    Windows 정책상 24시간에 한 번만 생성될 수 있습니다."""
    import diagnostic
    if not diagnostic.is_admin():
        return False, "관리자 권한이 필요합니다. 관리자로 다시 실행해 주세요."
    safe_desc = str(desc).replace("'", "''")   # PowerShell 작은따옴표 이스케이프
    ps = ("Checkpoint-Computer -Description '%s' "
          "-RestorePointType MODIFY_SETTINGS" % safe_desc)
    ok, out = _run(["powershell", "-NoProfile", "-Command", ps], timeout=90)
    if ok:
        return True, ("복구 지점을 생성했습니다. "
                      "(Windows 정책상 24시간에 한 번만 생성될 수 있습니다)")
    return False, "복구 지점 생성 실패(24시간 내 이미 생성됐을 수 있음): %s" % (out or "")


def clear_temp():
    """임시 폴더(%TEMP%) 정리. 순수 파이썬으로 삭제하며 사용 중 파일은 건너뜀. 관리자 불필요."""
    import os
    import shutil
    temp = os.environ.get("TEMP") or os.environ.get("TMP")
    if not temp or not os.path.isdir(temp):
        return True, "임시 폴더를 찾지 못했습니다."
    removed = 0
    try:
        for entry in os.scandir(temp):
            try:
                if entry.is_dir(follow_symlinks=False):
                    shutil.rmtree(entry.path, ignore_errors=True)
                else:
                    os.remove(entry.path)
                removed += 1
            except Exception:
                pass   # 사용 중인 파일 등은 무시
    except Exception:
        pass
    return True, "임시 파일 정리: %d개 삭제, 일부는 사용 중이라 건너뜀" % removed


def clear_update_cache():
    """Windows Update 캐시(SoftwareDistribution\\Download) 정리. 관리자 권한 필요.
    업데이트 서비스 중지 → 캐시 삭제 → 서비스 재시작."""
    if not _is_admin():
        return False, "관리자 권한이 필요합니다. 관리자로 다시 실행해 주세요."
    import os
    import shutil
    _run(["net", "stop", "wuauserv"], timeout=60)
    _run(["net", "stop", "bits"], timeout=60)
    removed = 0
    dl = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                      "SoftwareDistribution", "Download")
    try:
        if os.path.isdir(dl):
            for entry in os.scandir(dl):
                try:
                    if entry.is_dir(follow_symlinks=False):
                        shutil.rmtree(entry.path, ignore_errors=True)
                    else:
                        os.remove(entry.path)
                    removed += 1
                except Exception:
                    pass   # 사용 중인 파일 등은 무시
    except Exception:
        pass
    _run(["net", "start", "wuauserv"], timeout=60)
    _run(["net", "start", "bits"], timeout=60)
    return True, ("Windows Update 캐시 정리: %d개 항목 삭제 후 서비스를 재시작했습니다." % removed)


# (라벨, 함수, 관리자필요, 재부팅필요)
ACTIONS = [
    ("DNS 캐시 초기화", flush_dns, False, False),
    ("IP 주소 갱신", renew_ip, False, False),
    ("Winsock 초기화", reset_winsock, True, True),
    ("TCP/IP 초기화", reset_tcpip, True, True),
]


def repair_internet(admin=False, include_reset=True, on_step=None):
    """복구 조치를 순서대로 실행.
    admin=False 면 관리자 필요 항목은 건너뜀. include_reset=False 면 리셋류 제외.
    on_step(label, ok, msg) 콜백. 결과 [(label, ok|None, status)] 반환."""
    results = []
    reboot_needed = False
    for label, fn, need_admin, need_reboot in ACTIONS:
        if need_reboot and not include_reset:
            continue
        if need_admin and not admin:
            results.append((label, None, "관리자 권한 필요 — 건너뜀"))
            if on_step:
                on_step(label, None, "관리자 권한 필요")
            continue
        ok, msg = fn()
        status = ("재부팅 후 적용" if (ok and need_reboot) else ("완료" if ok else "실패"))
        if ok and need_reboot:
            reboot_needed = True
        results.append((label, ok, status))
        if on_step:
            on_step(label, ok, status)
    return {"results": results, "reboot_needed": reboot_needed}


if __name__ == "__main__":
    # 안전한 항목만 (리셋류 제외)
    print(repair_internet(admin=False, include_reset=False))
