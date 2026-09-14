"""
PC 진단 코어 — 사양 / 메모리 / 디스크 / GPU / 배터리 / 온도·팬 수집,
원클릭 분석(문제 플래그) + 사람이 읽는 리포트 텍스트 생성.

의존성(모두 선택; 없으면 해당 항목만 건너뜀):
  psutil, wmi, py-cpuinfo, HardwareMonitor(LibreHardwareMonitor 래퍼)
- 온도/팬/정확한 GPU VRAM 은 LibreHardwareMonitor 로 읽음.
  · GPU 온도·VRAM·로드는 일반 권한으로도 읽힘.
  · CPU/메인보드 온도는 관리자 권한이 있어야 나옴.
GUI 비의존 — dict / 텍스트로만 반환.
"""
import platform
import subprocess
import time

_CREATE_NO_WINDOW = 0x08000000  # 콘솔 창 안 뜨게

try:
    import psutil
except Exception:
    psutil = None

try:
    import wmi as _wmi
except Exception:
    _wmi = None


def _wmi_conn(namespace=None):
    if _wmi is None:
        return None
    try:
        return _wmi.WMI(namespace=namespace) if namespace else _wmi.WMI()
    except Exception:
        return None


def _gb(n):
    return round(n / (1024 ** 3), 1)


def is_admin():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _run_text(cmd, timeout=8):
    """subprocess 실행 후 (stdout+stderr 텍스트, returncode) 반환.
    실패/타임아웃/명령없음이면 ("", None). 콘솔 창 안 뜨게 + 절대 예외 안 냄.
    한글 콘솔 출력을 위해 bytes 를 mbcs 로 디코드(errors=ignore)."""
    try:
        r = subprocess.run(cmd, capture_output=True,
                           timeout=timeout, creationflags=_CREATE_NO_WINDOW)
        out = (r.stdout or b"") + (r.stderr or b"")
        return (out.decode("mbcs", "ignore") if out else ""), r.returncode
    except Exception:
        return "", None


# ---------------- LibreHardwareMonitor (온도/팬/GPU) ----------------
def read_sensors():
    """LibreHardwareMonitor 로 온도/팬/GPU 상세를 읽어 dict 반환.
    없거나 실패하면 빈 구조. (admin 여부는 별도)"""
    out = {"temps": {}, "fans": {}, "gpu": {},
           "volts": {}, "powers": {}, "clocks": {}, "ok": False}
    try:
        from HardwareMonitor.Hardware import Computer, SensorType  # noqa
    except Exception:
        return out
    try:
        c = Computer()
        c.IsCpuEnabled = True
        c.IsGpuEnabled = True
        c.IsMemoryEnabled = True
        c.IsMotherboardEnabled = True
        c.IsStorageEnabled = True
        c.Open()
        cpu_candidates = []
        for hw in c.Hardware:
            hw.Update()
            for sub in hw.SubHardware:
                sub.Update()
            htype = str(hw.HardwareType)
            is_cpu = "Cpu" in htype
            is_gpu = "Gpu" in htype
            is_sto = "Storage" in htype
            hwlabel = "CPU" if is_cpu else ("GPU" if is_gpu else "MB")
            for s in hw.Sensors:
                if s.Value is None:
                    continue
                st = str(s.SensorType)
                name = s.Name or ""
                if st == "Temperature":
                    if is_cpu:
                        cpu_candidates.append((name, float(s.Value)))
                    elif is_gpu and "Hot Spot" in name:
                        out["temps"]["GPU 핫스팟"] = round(s.Value, 1)
                    elif is_gpu and "Core" in name:
                        out["temps"]["GPU"] = round(s.Value, 1)
                        out["gpu"]["temp"] = round(s.Value, 1)
                    elif is_sto and "Critical" not in name:
                        out["temps"].setdefault("디스크", round(s.Value, 1))
                elif st == "Fan" and s.Value > 0:
                    out["fans"][name] = round(s.Value)
                elif is_gpu and st == "SmallData":
                    if name == "GPU Memory Total":
                        out["gpu"]["vram_total_mb"] = round(s.Value)
                    elif name == "GPU Memory Used":
                        out["gpu"]["vram_used_mb"] = round(s.Value)
                elif is_gpu and st == "Load" and name == "GPU Core":
                    out["gpu"]["load_percent"] = round(s.Value, 1)
                elif st == "Voltage":
                    # 0 은 제외(비관리자 CPU 전압 등 빈 값이 지저분하게 쌓이는 것 방지)
                    if s.Value != 0:
                        out["volts"][hwlabel + " " + name] = round(s.Value, 3)
                    if is_gpu and "Core" in name:
                        out["gpu"]["voltage_v"] = round(s.Value, 3)
                elif st == "Power":
                    if s.Value != 0:
                        out["powers"][hwlabel + " " + name] = round(s.Value, 1)
                    if is_gpu:
                        out["gpu"]["power_w"] = round(s.Value, 1)
                elif st == "Clock":
                    # 클럭은 낮은 값이라도 유지
                    if is_gpu:
                        if name == "GPU Core":
                            out["gpu"]["core_clock_mhz"] = round(s.Value)
                        elif name == "GPU Memory":
                            out["gpu"]["mem_clock_mhz"] = round(s.Value)
                    out["clocks"][hwlabel + " " + name] = round(s.Value)
        # CPU 온도: Package 우선, 없으면 최댓값
        if cpu_candidates:
            pkg = [v for n, v in cpu_candidates if "Package" in n or "CPU" in n]
            out["temps"]["CPU"] = round(pkg[0] if pkg else max(v for _, v in cpu_candidates), 1)
        c.Close()
        out["ok"] = True
    except Exception:
        pass
    return out


# ---------------- 수집 ----------------
def collect_system():
    info = {
        "os": platform.system(),
        "arch": platform.machine(),
        "hostname": platform.node(),
    }
    if psutil:
        try:
            info["uptime_hours"] = round((time.time() - psutil.boot_time()) / 3600, 1)
        except Exception:
            pass
    c = _wmi_conn()
    if c:
        try:
            os_ = c.Win32_OperatingSystem()[0]
            info["edition"] = os_.Caption
            info["build"] = os_.BuildNumber
            # 설치일 / 마지막 부팅 / 등록 사용자 (각 항목 개별 가드)
            try:
                idate = str(os_.InstallDate or "")[:8]
                if len(idate) == 8 and idate.isdigit():
                    info["install_date"] = "%s-%s-%s" % (idate[:4], idate[4:6], idate[6:8])
            except Exception:
                pass
            try:
                lb = str(os_.LastBootUpTime or "")
                if len(lb) >= 12 and lb[:12].isdigit():
                    info["last_boot"] = "%s-%s-%s %s:%s" % (
                        lb[:4], lb[4:6], lb[6:8], lb[8:10], lb[10:12])
            except Exception:
                pass
            try:
                owner = (os_.RegisteredUser or "").strip()
                if owner:
                    info["owner"] = owner
            except Exception:
                pass
        except Exception:
            pass
        # 시간대 (Caption 우선, 없으면 Description)
        try:
            tz = c.Win32_TimeZone()[0]
            cap = (getattr(tz, "Caption", None) or getattr(tz, "Description", None) or "").strip()
            if cap:
                info["timezone"] = cap
        except Exception:
            pass
    return info


def collect_cpu():
    # CPU 이름은 WMI 로 (py-cpuinfo 는 frozen exe 에서 자기 재실행 문제가 있어 사용 안 함)
    info = {"name": platform.processor() or "?"}
    c = _wmi_conn()
    if c:
        try:
            p = c.Win32_Processor()[0]
        except Exception:
            p = None
        if p is not None:
            # 이름 (각 항목은 개별 try 로 감싸 실패 시 해당 키만 건너뜀)
            try:
                info["name"] = (p.Name or info["name"]).strip()
            except Exception:
                pass
            # CPU-Z 수준 상세: 클럭 / 소켓 / 캐시 / 전압 / 제조사
            try:
                if p.MaxClockSpeed is not None:
                    info["max_clock_mhz"] = int(p.MaxClockSpeed)
            except Exception:
                pass
            try:
                if p.CurrentClockSpeed is not None:
                    info["cur_clock_mhz"] = int(p.CurrentClockSpeed)
            except Exception:
                pass
            try:
                sock = (p.SocketDesignation or "").strip()
                if sock:
                    info["socket"] = sock
            except Exception:
                pass
            try:
                if p.L2CacheSize is not None:
                    info["l2_kb"] = int(p.L2CacheSize)
            except Exception:
                pass
            try:
                if p.L3CacheSize is not None:
                    info["l3_kb"] = int(p.L3CacheSize)
            except Exception:
                pass
            # CurrentVoltage: bit7(0x80) 이 켜져 있으면 (값 & 0x7f)/10.0 V.
            # 아니면 SETVoltage 레지스터 값이라 신뢰도 낮음 → 0/None 이면 None.
            try:
                cv = p.CurrentVoltage
                volt = None
                if cv is not None:
                    cv = int(cv)
                    if cv & 0x80:
                        volt = (cv & 0x7f) / 10.0
                    elif cv:
                        volt = cv / 10.0
                info["voltage_v"] = round(volt, 3) if volt else None
            except Exception:
                pass
            try:
                mfr = (p.Manufacturer or "").strip()
                if mfr:
                    info["manufacturer"] = mfr
            except Exception:
                pass
    if psutil:
        try:
            info["cores_physical"] = psutil.cpu_count(logical=False)
            info["cores_logical"] = psutil.cpu_count(logical=True)
            fr = psutil.cpu_freq()
            if fr:
                info["freq_mhz"] = round(fr.current)
            info["usage_percent"] = psutil.cpu_percent(interval=0.5)
        except Exception:
            pass
    return info


def collect_memory():
    if not psutil:
        return {}
    try:
        vm = psutil.virtual_memory()
        return {"total_gb": _gb(vm.total), "used_gb": _gb(vm.used),
                "available_gb": _gb(vm.available), "percent": vm.percent}
    except Exception:
        return {}


def collect_swap():
    """페이지 파일(스왑) 사용량. psutil 없거나 실패하면 빈 dict."""
    if not psutil:
        return {}
    try:
        sw = psutil.swap_memory()
        return {"total_gb": _gb(sw.total), "used_gb": _gb(sw.used),
                "percent": sw.percent}
    except Exception:
        return {}


def collect_memory_modules():
    """물리 메모리 모듈 상세 (CPU-Z 수준: 슬롯/용량/속도/제조사/파트넘버/타입).
    Win32_PhysicalMemory 기반. 절대 예외를 던지지 않음."""
    out = {"type": None, "channels": None, "total_gb": 0.0, "modules": []}
    c = _wmi_conn()
    if not c:
        return out
    # SMBIOSMemoryType 매핑 (없으면 MemoryType 을 같은 표로 대략 매핑)
    smbios = {21: "DDR2", 24: "DDR3", 26: "DDR4", 34: "DDR5"}
    try:
        mods = c.Win32_PhysicalMemory()
    except Exception:
        mods = []
    total = 0.0
    for pm in mods:
        try:
            try:
                slot = (pm.DeviceLocator or pm.BankLabel or "").strip()
            except Exception:
                slot = ""
            try:
                size_gb = _gb(int(pm.Capacity)) if pm.Capacity else 0.0
            except Exception:
                size_gb = 0.0
            try:
                speed_mhz = int(pm.Speed) if pm.Speed is not None else None
            except Exception:
                speed_mhz = None
            try:
                configured_mhz = int(pm.ConfiguredClockSpeed) if pm.ConfiguredClockSpeed is not None else None
            except Exception:
                configured_mhz = None
            try:
                manufacturer = (pm.Manufacturer or "").strip()
            except Exception:
                manufacturer = ""
            try:
                part = (pm.PartNumber or "").strip()
            except Exception:
                part = ""
            mtype = None
            try:
                smb = pm.SMBIOSMemoryType
                if smb is not None:
                    mtype = smbios.get(int(smb), str(int(smb)))
                elif pm.MemoryType is not None:
                    mtype = smbios.get(int(pm.MemoryType), str(int(pm.MemoryType)))
            except Exception:
                mtype = None
            total += size_gb
            out["modules"].append({
                "slot": slot,
                "size_gb": size_gb,
                "speed_mhz": speed_mhz,
                "configured_mhz": configured_mhz,
                "manufacturer": manufacturer,
                "part": part,
                "type": mtype,
            })
        except Exception:
            pass
        if len(out["modules"]) >= 8:  # 최대 8개
            break
    out["total_gb"] = round(total, 1)
    if out["modules"]:
        out["type"] = out["modules"][0].get("type")
    return out


def collect_mainboard():
    """메인보드 / BIOS 정보 (제조사·모델 / BIOS 벤더·버전·날짜). 모두 예외 가드."""
    out = {"board_mfr": None, "board_model": None,
           "bios_vendor": None, "bios_version": None, "bios_date": None}
    c = _wmi_conn()
    if not c:
        return out
    try:
        bb = c.Win32_BaseBoard()[0]
        try:
            out["board_mfr"] = (bb.Manufacturer or "").strip() or None
        except Exception:
            pass
        try:
            out["board_model"] = (bb.Product or "").strip() or None
        except Exception:
            pass
    except Exception:
        pass
    try:
        bios = c.Win32_BIOS()[0]
        try:
            out["bios_vendor"] = (bios.Manufacturer or "").strip() or None
        except Exception:
            pass
        try:
            out["bios_version"] = (bios.SMBIOSBIOSVersion or "").strip() or None
        except Exception:
            pass
        try:
            rd = bios.ReleaseDate  # WMI datetime: YYYYMMDDHHMMSS...
            if rd:
                d = str(rd)[:8]
                if len(d) == 8 and d.isdigit():
                    out["bios_date"] = "%s-%s-%s" % (d[:4], d[4:6], d[6:8])
        except Exception:
            pass
    except Exception:
        pass
    return out


def collect_disks():
    vols = []
    if psutil:
        try:
            for p in psutil.disk_partitions(all=False):
                try:
                    u = psutil.disk_usage(p.mountpoint)
                    vols.append({"device": p.device, "fstype": p.fstype,
                                 "total_gb": _gb(u.total), "used_gb": _gb(u.used),
                                 "free_gb": _gb(u.free), "percent": u.percent})
                except Exception:
                    pass
        except Exception:
            pass
    phys = []
    c = _wmi_conn(namespace="root/microsoft/windows/storage")
    if c:
        try:
            media = {0: "미상", 3: "HDD", 4: "SSD", 5: "SCM"}
            health = {0: "정상", 1: "주의", 2: "불량"}
            # BusType(인터페이스) 매핑
            bus_map = {1: "SCSI", 2: "ATAPI", 3: "ATA", 4: "1394", 5: "SSA",
                       6: "Fibre", 7: "USB", 8: "RAID", 9: "iSCSI", 10: "SAS",
                       11: "SATA", 12: "SD", 13: "MMC", 17: "NVMe"}
            for d in c.MSFT_PhysicalDisk():
                entry = {"model": (d.FriendlyName or "").strip(),
                         "type": media.get(int(d.MediaType or 0), "미상"),
                         "health": health.get(int(d.HealthStatus or 0), "?"),
                         "size_gb": _gb(int(d.Size or 0))}
                # 버스(인터페이스) 종류 — BusType 속성이 없을 수 있어 가드
                try:
                    bt = getattr(d, "BusType", None)
                    if bt is not None:
                        entry["bus"] = bus_map.get(int(bt), str(int(bt)))
                except Exception:
                    pass
                # 회전 속도(HDD) — 쉽게 얻히고 값이 정상 범위일 때만 (SSD/미상은 제외)
                try:
                    sp = getattr(d, "SpindleSpeed", None)
                    if sp is not None and 0 < int(sp) < 60000:
                        entry["spindle_rpm"] = int(sp)
                except Exception:
                    pass
                phys.append(entry)
        except Exception:
            pass
    return {"volumes": vols, "physical": phys}


def collect_gpu(sensor_gpu=None):
    gpus = []
    c = _wmi_conn()
    if c:
        try:
            for g in c.Win32_VideoController():
                item = {"name": g.Name, "driver": g.DriverVersion}
                # AdapterRAM 은 큰 VRAM 에서 4GB 로 잘려 신뢰 낮음 → >0 일 때만 참고용
                try:
                    ram = int(g.AdapterRAM) if g.AdapterRAM else 0
                    if ram > 0:
                        item["adapter_ram_gb"] = _gb(ram)
                except Exception:
                    pass
                try:
                    hz = g.CurrentHorizontalResolution
                    vt = g.CurrentVerticalResolution
                    if hz and vt:
                        res = "%sx%s" % (int(hz), int(vt))
                        rr = g.CurrentRefreshRate
                        if rr:
                            res += " @%sHz" % int(rr)
                        item["resolution"] = res
                except Exception:
                    pass
                gpus.append(item)
        except Exception:
            pass
    sensor_gpu = sensor_gpu or {}
    if gpus:  # 첫 GPU 에 센서값(정확한 VRAM/온도/로드/클럭/전력/전압) 병합
        if sensor_gpu.get("vram_total_mb"):
            gpus[0]["vram_total_gb"] = round(sensor_gpu["vram_total_mb"] / 1024, 1)
            gpus[0]["vram_used_gb"] = round(sensor_gpu.get("vram_used_mb", 0) / 1024, 1)
        if "temp" in sensor_gpu:
            gpus[0]["temp"] = sensor_gpu["temp"]
        if "load_percent" in sensor_gpu:
            gpus[0]["load_percent"] = sensor_gpu["load_percent"]
        for k in ("core_clock_mhz", "mem_clock_mhz", "power_w", "voltage_v"):
            if k in sensor_gpu:
                gpus[0][k] = sensor_gpu[k]
    return gpus


def collect_battery():
    """배터리 상태 — 데스크톱은 {}. 노트북은 잔량/충전여부 + 설계·완충 용량/수명/사이클.
    psutil 로 잔량·충전, root/wmi 로 용량(mWh)·사이클. 모든 접근을 개별 가드."""
    if not psutil:
        return {}
    try:
        b = psutil.sensors_battery()
    except Exception:
        return {}
    if b is None:  # 배터리 없음 = 데스크톱
        return {}
    out = {"percent": round(b.percent, 1) if b.percent is not None else None,
           "plugged": b.power_plugged}
    # 용량(mWh)·수명·사이클 — root/wmi (있으면 채우고, 없으면 해당 필드 생략)
    cw = _wmi_conn(namespace="root/wmi")
    if cw:
        design = None
        full = None
        try:
            bsd = cw.BatteryStaticData()
            if bsd:
                dc = getattr(bsd[0], "DesignedCapacity", None)
                if dc:
                    design = round(int(dc) / 1000.0, 1)
                    out["design_wh"] = design
        except Exception:
            pass
        try:
            bfc = cw.BatteryFullChargedCapacity()
            if bfc:
                fc = getattr(bfc[0], "FullChargedCapacity", None)
                if fc:
                    full = round(int(fc) / 1000.0, 1)
                    out["full_wh"] = full
        except Exception:
            pass
        try:
            if design and full:
                out["health_pct"] = round(full / design * 100)
        except Exception:
            pass
        try:
            bcc = cw.BatteryCycleCount()
            if bcc:
                cc = getattr(bcc[0], "CycleCount", None)
                if cc is not None:
                    out["cycle_count"] = int(cc)
        except Exception:
            pass
    return out


def collect_smart():
    """물리 디스크 SMART 상세 (LibreHardwareMonitor 스토리지 센서 기반, 관리자 불필요).
    온도 / 전원인가시간 / 총기록량(TB) / 총읽기량(TB) / SSD 남은 수명(%).
    실패 시 [] — 절대 예외를 던지지 않음. 최대 6개."""
    out = []
    try:
        from HardwareMonitor.Hardware import Computer  # noqa
    except Exception:
        return out
    # health 는 기존 collect_disks 의 physical HealthStatus 에서 모델명으로 매칭
    health_map = {}
    try:
        for ph in collect_disks().get("physical", []):
            nm = (ph.get("model") or "").strip()
            if nm:
                health_map[nm] = ph.get("health", "?")
    except Exception:
        health_map = {}
    try:
        c = Computer()
        c.IsStorageEnabled = True
        c.Open()
        for hw in c.Hardware:
            if "Storage" not in str(hw.HardwareType):
                continue
            hw.Update()
            for sub in hw.SubHardware:
                sub.Update()
            model = (hw.Name or "").strip()
            entry = {"model": model, "temp_c": None, "power_on_hours": None,
                     "data_written_tb": None, "data_read_tb": None,
                     "wear_pct": None, "health": "?"}
            # 모델명으로 health 매칭 (부분 일치 허용)
            if model in health_map:
                entry["health"] = health_map[model]
            else:
                for k, v in health_map.items():
                    if k and (k in model or model in k):
                        entry["health"] = v
                        break
            for s in hw.Sensors:
                try:
                    st = str(s.SensorType)
                    name = s.Name or ""
                    val = s.Value
                    if st == "Temperature" and val is not None and entry["temp_c"] is None:
                        entry["temp_c"] = round(float(val), 1)
                    elif "Data Written" in name and val is not None:
                        entry["data_written_tb"] = round(float(val), 2)
                    elif "Data Read" in name and val is not None:
                        entry["data_read_tb"] = round(float(val), 2)
                    elif ("Remaining Life" in name or "Wear Level" in name) and val is not None:
                        entry["wear_pct"] = round(float(val))
                    elif "Power On" in name:
                        poh = val if val is not None else getattr(s, "RawValue", None)
                        if poh is not None:
                            entry["power_on_hours"] = int(round(float(poh)))
                except Exception:
                    pass
            out.append(entry)
            if len(out) >= 6:  # 최대 6개
                break
        c.Close()
    except Exception:
        pass
    return out


def collect_displays():
    """디스플레이(모니터) 목록 — 해상도 / 주사율 / 제조사 / 모델명. 최대 4개.
    Win32_VideoController 로 해상도·주사율, root/wmi WmiMonitorID 로 모니터 이름(best-effort)."""
    out = []
    try:
        # 1) 모니터 이름(제조사/친화명) — root/wmi WmiMonitorID (UINT16 배열 -> ASCII)
        monitors = []
        cw = _wmi_conn(namespace="root/wmi")
        if cw:
            def _decode(arr):
                try:
                    return "".join(chr(x) for x in arr if x).strip()  # 0 제거
                except Exception:
                    return ""
            try:
                for mid in cw.WmiMonitorID():
                    try:
                        mfr = _decode(mid.ManufacturerName) if getattr(mid, "ManufacturerName", None) else ""
                        fn = _decode(mid.UserFriendlyName) if getattr(mid, "UserFriendlyName", None) else ""
                        if mfr or fn:
                            monitors.append({"manufacturer": mfr, "name": fn})
                    except Exception:
                        pass
            except Exception:
                pass
        # 2) 해상도/주사율 — Win32_VideoController (해상도 없는 컨트롤러는 제외)
        controllers = []
        c = _wmi_conn()
        if c:
            try:
                for g in c.Win32_VideoController():
                    try:
                        hz = g.CurrentHorizontalResolution
                        vt = g.CurrentVerticalResolution
                        if not hz or not vt:
                            continue
                        rr = None
                        try:
                            rr = int(g.CurrentRefreshRate) if g.CurrentRefreshRate else None
                        except Exception:
                            rr = None
                        controllers.append({"name": (g.Name or "").strip(),
                                            "resolution": "%sx%s" % (int(hz), int(vt)),
                                            "refresh_hz": rr})
                    except Exception:
                        pass
            except Exception:
                pass
        # 3) 결합 — 모니터 이름이 있으면 이름/제조사 사용, 없으면 컨트롤러 이름
        for i, ctrl in enumerate(controllers):
            name = ctrl["name"]
            mfr = ""
            if i < len(monitors):
                mn = monitors[i]
                if mn.get("name"):
                    name = mn["name"]
                mfr = mn.get("manufacturer", "")
            out.append({"name": name, "resolution": ctrl["resolution"],
                        "refresh_hz": ctrl["refresh_hz"], "manufacturer": mfr})
            if len(out) >= 4:
                break
        # 컨트롤러 해상도를 못 읽었지만 모니터 이름만 있는 경우
        if not out and monitors:
            for mn in monitors[:4]:
                out.append({"name": mn.get("name", ""), "resolution": None,
                            "refresh_hz": None, "manufacturer": mn.get("manufacturer", "")})
    except Exception:
        pass
    return out


def collect_security():
    """보안 상태 — TPM / 보안 부팅(Secure Boot) / Windows 정품 인증. 모든 접근 가드."""
    out = {"tpm": "없음/확인불가", "secure_boot": None,
           "activated": None, "activation": "미인증/확인불가"}
    # TPM — root/cimv2/security/microsofttpm 의 Win32_Tpm
    try:
        ct = _wmi_conn(namespace="root/cimv2/security/microsofttpm")
        if ct:
            try:
                tpms = ct.Win32_Tpm()
                if tpms:
                    t = tpms[0]
                    if getattr(t, "IsEnabled_InitialValue", None):
                        spec = ""
                        try:
                            spec = (t.SpecVersion or "").split(",")[0].strip()
                        except Exception:
                            spec = ""
                        out["tpm"] = ("사용 (버전 %s)" % spec) if spec else "사용"
                    else:
                        out["tpm"] = "있음(비활성)"
            except Exception:
                pass
    except Exception:
        pass
    # 보안 부팅 — Confirm-SecureBootUEFI (True/False/그 외)
    try:
        text, _rc = _run_text(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                               "Confirm-SecureBootUEFI"], timeout=15)
        if "True" in text:
            out["secure_boot"] = True
        elif "False" in text:
            out["secure_boot"] = False
    except Exception:
        pass
    # 정품 인증 — SoftwareLicensingProduct (Windows OS 앱ID 로 한정)
    try:
        c = _wmi_conn()
        if c:
            try:
                q = ("SELECT LicenseStatus FROM SoftwareLicensingProduct "
                     "WHERE PartialProductKey IS NOT NULL "
                     "AND ApplicationID='55c92734-d682-4d71-983e-d6ec3f16059f'")
                prods = c.query(q)
                if prods:
                    activated = False
                    for p in prods:
                        try:
                            if int(p.LicenseStatus) == 1:
                                activated = True
                                break
                        except Exception:
                            pass
                    out["activated"] = activated
                    out["activation"] = "정품 인증됨" if activated else "미인증/확인불가"
            except Exception:
                pass
    except Exception:
        pass
    return out


def collect_top_processes(n=5):
    procs = []
    if not psutil:
        return procs
    try:
        lst = []
        for p in psutil.process_iter(["name", "memory_info"]):
            try:
                mem = p.info["memory_info"].rss if p.info["memory_info"] else 0
                lst.append((p.info["name"] or "?", mem))
            except Exception:
                pass
        lst.sort(key=lambda x: x[1], reverse=True)
        procs = [{"name": n_, "ram_mb": round(m / (1024 ** 2), 1)} for n_, m in lst[:n]]
    except Exception:
        pass
    return procs


def collect_event_log(max_items=12, days=3):
    """시스템/응용 프로그램 이벤트 로그에서 최근 '오류(Error)' 항목."""
    events = []
    try:
        import datetime
        import win32evtlog
        import win32evtlogutil
        cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
        for logtype in ("System", "Application"):
            try:
                h = win32evtlog.OpenEventLog(None, logtype)
                flags = (win32evtlog.EVENTLOG_BACKWARDS_READ
                         | win32evtlog.EVENTLOG_SEQUENTIAL_READ)
                stop = False
                while not stop:
                    recs = win32evtlog.ReadEventLog(h, flags, 0)
                    if not recs:
                        break
                    for r in recs:
                        tg = r.TimeGenerated
                        t = None
                        try:
                            if hasattr(tg, "year"):   # pywintypes datetime
                                t = datetime.datetime(tg.year, tg.month, tg.day,
                                                      tg.hour, tg.minute,
                                                      getattr(tg, "second", 0))
                            else:
                                t = datetime.datetime.fromtimestamp(int(tg))
                        except Exception:
                            t = None
                        if t and t < cutoff:
                            stop = True
                            break
                        if r.EventType == win32evtlog.EVENTLOG_ERROR_TYPE:
                            try:
                                msg = (win32evtlogutil.SafeFormatMessage(r, logtype) or "")
                            except Exception:
                                msg = ""
                            events.append({
                                "log": logtype,
                                "source": str(r.SourceName),
                                "id": r.EventID & 0xFFFF,
                                "time": t.strftime("%m-%d %H:%M") if t else "?",
                                "msg": msg.strip().replace("\r", " ").replace("\n", " ")[:110],
                            })
                    if len([e for e in events if e["log"] == logtype]) >= max_items:
                        break
                win32evtlog.CloseEventLog(h)
            except Exception:
                pass
    except Exception:
        pass
    events.sort(key=lambda e: e["time"], reverse=True)
    return events[:max_items]


def collect_network():
    """네트워크 어댑터(IP/속도) + 게이트웨이 + 인터넷·DNS 연결 확인."""
    import socket
    info = {"adapters": [], "gateway": None, "internet": False, "dns": False,
            "dns_servers": []}
    if psutil:
        try:
            addrs = psutil.net_if_addrs()
            stats = psutil.net_if_stats()
            for name, al in addrs.items():
                st = stats.get(name)
                if not st or not st.isup:
                    continue
                ipv4 = [a.address for a in al
                        if a.family == socket.AF_INET and not a.address.startswith("127.")]
                if ipv4:
                    info["adapters"].append({"name": name, "ip": ipv4[0],
                                             "speed_mbps": st.speed})
        except Exception:
            pass
    c = _wmi_conn()
    if c:
        try:
            # IPEnabled 어댑터 구성(MAC/DHCP/DNS/게이트웨이) 을 IP 로 매칭해 병합.
            # 실패해도 어댑터는 기존 필드(name/ip/speed) 그대로 유지.
            for cfg in c.Win32_NetworkAdapterConfiguration(IPEnabled=True):
                try:
                    cfg_ips = list(cfg.IPAddress) if cfg.IPAddress else []
                except Exception:
                    cfg_ips = []
                try:
                    mac = (cfg.MACAddress or "").strip() or None
                except Exception:
                    mac = None
                try:
                    dhcp = bool(cfg.DHCPEnabled) if cfg.DHCPEnabled is not None else None
                except Exception:
                    dhcp = None
                try:
                    dns_list = list(cfg.DNSServerSearchOrder) if cfg.DNSServerSearchOrder else []
                except Exception:
                    dns_list = []
                try:
                    cfg_gw = list(cfg.DefaultIPGateway) if cfg.DefaultIPGateway else []
                except Exception:
                    cfg_gw = []
                try:
                    cfg_subnets = list(cfg.IPSubnet) if cfg.IPSubnet else []
                except Exception:
                    cfg_subnets = []
                # 게이트웨이가 아직 없으면 이 구성에서 채움
                if cfg_gw and not info["gateway"]:
                    info["gateway"] = cfg_gw[0]
                # 게이트웨이 있는(인터넷용) 구성의 DNS 를 top-level 로 (중복 제거)
                if cfg_gw and dns_list and not info["dns_servers"]:
                    seen = []
                    for d in dns_list:
                        if d not in seen:
                            seen.append(d)
                    info["dns_servers"] = seen
                # IP 로 어댑터를 찾아 mac/dhcp/dns_servers 추가
                for a in info["adapters"]:
                    if a.get("ip") in cfg_ips:
                        if mac:
                            a["mac"] = mac
                        if dhcp is not None:
                            a["dhcp"] = dhcp
                        if dns_list:
                            a["dns_servers"] = dns_list
                        # IPv6 (IPAddress 중 ':' 포함) / 서브넷(첫 항목) 추가
                        ipv6s = [ip for ip in cfg_ips if ":" in ip]
                        if ipv6s:
                            a["ipv6"] = ipv6s[0]
                        if cfg_subnets:
                            a["subnet"] = cfg_subnets[0]
        except Exception:
            pass
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=2).close()
        info["internet"] = True
    except Exception:
        info["internet"] = False
    try:
        socket.gethostbyname("www.google.com")
        info["dns"] = True
    except Exception:
        info["dns"] = False
    # 공인(외부) IP — best-effort, 세션 중 캐시(재진단 속도 향상)
    info["public_ip"] = _cached("public_ip", _fetch_public_ip, None)
    return info


def _fetch_public_ip():
    import urllib.request
    try:
        req = urllib.request.Request("https://api.ipify.org",
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=3) as r:
            return ((r.read().decode("ascii", "ignore") or "").strip()) or None
    except Exception:
        return None


def collect_problem_devices():
    """드라이버/장치 문제 (장치 관리자의 노란 느낌표 = ConfigManagerErrorCode != 0)."""
    out = []
    c = _wmi_conn()
    if c:
        try:
            for d in c.Win32_PnPEntity():
                try:
                    code = d.ConfigManagerErrorCode
                    if code is not None and int(code) != 0:
                        out.append({"name": (d.Name or "알 수 없는 장치"),
                                    "error_code": int(code)})
                except Exception:
                    pass
        except Exception:
            pass
    return out


def collect_startup():
    """시작 프로그램 목록 (Win32_StartupCommand)."""
    out = []
    c = _wmi_conn()
    if c:
        try:
            for s in c.Win32_StartupCommand():
                out.append({"name": s.Name or "?",
                            "command": (s.Command or "")[:90],
                            "location": s.Location or ""})
        except Exception:
            pass
    return out


def collect_services_stopped():
    """자동 시작인데 멈춰 있는 서비스 (문제 소지)."""
    out = []
    if psutil:
        try:
            for s in psutil.win_service_iter():
                try:
                    d = s.as_dict()
                    if d.get("start_type") == "automatic" and d.get("status") != "running":
                        out.append({"name": d.get("display_name") or d.get("name"),
                                    "status": d.get("status")})
                except Exception:
                    pass
        except Exception:
            pass
    return out[:20]


def collect_installed():
    """설치된 프로그램 목록 (레지스트리 Uninstall). 이름순."""
    import winreg
    out, seen = [], set()
    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, path in roots:
        try:
            key = winreg.OpenKey(hive, path)
        except Exception:
            continue
        try:
            n = winreg.QueryInfoKey(key)[0]
        except Exception:
            n = 0
        for i in range(n):
            try:
                sk = winreg.OpenKey(key, winreg.EnumKey(key, i))

                def _v(name):
                    try:
                        return winreg.QueryValueEx(sk, name)[0]
                    except Exception:
                        return None
                dn = _v("DisplayName")
                if not dn or _v("SystemComponent") == 1 or dn in seen:
                    continue
                seen.add(dn)
                out.append({"name": dn, "version": _v("DisplayVersion") or "",
                            "publisher": _v("Publisher") or ""})
            except Exception:
                pass
    out.sort(key=lambda x: x["name"].lower())
    return out


def collect_dotnet():
    """.NET Framework 버전 + .NET(Core) 런타임 목록."""
    out = {"framework": "확인불가", "runtimes": []}
    try:
        import winreg
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                 r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full")
            rel = int(winreg.QueryValueEx(key, "Release")[0])
            if rel >= 533320:
                out["framework"] = "4.8.1"
            elif rel >= 528040:
                out["framework"] = "4.8"
            elif rel >= 461808:
                out["framework"] = "4.7.2"
            else:
                out["framework"] = "4.7 이하"
        except Exception:
            pass
    except Exception:
        pass
    try:
        text, _rc = _run_text(["dotnet", "--list-runtimes"], timeout=8)
        for line in text.splitlines():
            line = line.strip()
            if line:
                out["runtimes"].append(line)
    except Exception:
        pass
    return out


def collect_vcredist():
    """설치된 Visual C++ 재배포 패키지 목록 (이름순, 중복 제거)."""
    try:
        import winreg
    except Exception:
        return []
    found = set()
    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, path in roots:
        try:
            key = winreg.OpenKey(hive, path)
        except Exception:
            continue
        try:
            n = winreg.QueryInfoKey(key)[0]
        except Exception:
            n = 0
        for i in range(n):
            try:
                sk = winreg.OpenKey(key, winreg.EnumKey(key, i))

                def _v(name):
                    try:
                        return winreg.QueryValueEx(sk, name)[0]
                    except Exception:
                        return None
                dn = _v("DisplayName")
                if not dn or "Visual C++" not in dn or "Redistributable" not in dn:
                    continue
                ver = _v("DisplayVersion") or ""
                found.add("%s (%s)" % (dn, ver) if ver else dn)
            except Exception:
                pass
    return sorted(found)


def collect_directx():
    """DirectX 버전. dxdiag 는 느리고 멈출 수 있어 호출하지 않음.
    레지스트리 Version 은 낡은 DDI 값(예: 4.09..)이라 사용자에게 오해를 줘서,
    OS 빌드 기준으로 사용자용 버전을 판단한다."""
    out = {"version": "확인불가"}
    try:
        if platform.system() == "Windows":
            build = 0
            try:
                build = int(platform.version().split(".")[-1])   # 예: 10.0.26200 -> 26200
            except Exception:
                build = 0
            if build >= 10240:
                out["version"] = "DirectX 12"
            elif build >= 9600:
                out["version"] = "DirectX 11.2"
            else:
                out["version"] = "DirectX 11"
    except Exception:
        pass
    return out


def collect_wmi_health():
    """WMI 저장소 일관성 확인 (winmgmt /verifyrepository).
    winmgmt.exe 는 System32\\wbem 에 있고 PATH 에 없을 수 있어 전체 경로로 시도."""
    try:
        import os
        exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                           "System32", "wbem", "winmgmt.exe")
        if not os.path.exists(exe):
            exe = "winmgmt"
        text, _rc = _run_text([exe, "/verifyrepository"], timeout=15)
        low = text.lower()
        # "inconsistent" 안에 "consistent" 가 있어 손상부터 먼저 검사
        if "inconsistent" in low or "손상" in text:
            return {"status": "손상"}
        if "consistent" in low or "일관" in text:
            return {"status": "정상"}
    except Exception:
        pass
    return {"status": "확인불가"}


def collect_wifi():
    """저장된 Wi-Fi 프로파일(SSID) 목록 (netsh wlan show profiles)."""
    out = {"count": 0, "profiles": []}
    try:
        text, _rc = _run_text(["netsh", "wlan", "show", "profiles"], timeout=8)
        for line in text.splitlines():
            if ":" not in line:
                continue
            if "All User Profile" in line or "모든 사용자 프로필" in line:
                ssid = line.split(":", 1)[1].strip()
                if ssid:
                    out["profiles"].append(ssid)
        out["count"] = len(out["profiles"])
    except Exception:
        pass
    return out


def collect_accounts():
    """로컬 사용자 계정 목록 (Win32_UserAccount WHERE LocalAccount=True). 최대 20개."""
    out = []
    c = _wmi_conn()
    if c:
        try:
            for u in c.Win32_UserAccount(LocalAccount=True):
                try:
                    name = u.Name or "?"
                    out.append({
                        "name": name,
                        "enabled": not bool(u.Disabled),
                        "lockout": bool(u.Lockout),
                        "pw_expires": bool(u.PasswordExpires),
                        "admin": name in ("Administrator", "administrator"),
                    })
                except Exception:
                    pass
                if len(out) >= 20:
                    break
        except Exception:
            pass
    return out


def collect_power():
    """전원 계획(활성) + 빠른 시작(Fast Startup) 설정."""
    out = {"plan": "확인불가", "fast_startup": None, "high_performance_available": True}
    try:
        text, _rc = _run_text(["powercfg", "/getactivescheme"], timeout=8)
        if text and "(" in text and ")" in text:
            out["plan"] = text[text.rfind("(") + 1:text.rfind(")")].strip() or "확인불가"
    except Exception:
        pass
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r"SYSTEM\CurrentControlSet\Control\Session Manager\Power")
        out["fast_startup"] = bool(int(winreg.QueryValueEx(key, "HiberbootEnabled")[0]))
    except Exception:
        out["fast_startup"] = None
    return out


def collect_recovery():
    """Windows 복구 환경(WinRE) 상태 (reagentc /info). 관리자 아니면 확인불가."""
    try:
        text, _rc = _run_text(["reagentc", "/info"], timeout=8)
        if text:
            low = text.lower()
            # "사용 안 함" 안에 "사용" 이 들어있어 비활성부터 먼저 검사
            if "disabled" in low or "사용 안 함" in text or "사용안함" in text:
                return {"winre": "사용안함"}
            if "enabled" in low or "사용" in text:
                return {"winre": "사용"}
    except Exception:
        pass
    return {"winre": "확인불가"}


def collect_shares():
    """공유 폴더 목록 (WMI Win32_Share). 관리자 기본 공유($ 로 끝남)도 포함. 최대 30개."""
    out = []
    c = _wmi_conn()
    if c:
        try:
            for s in c.Win32_Share():
                try:
                    out.append({"name": s.Name or "?", "path": s.Path or ""})
                except Exception:
                    pass
                if len(out) >= 30:
                    break
        except Exception:
            pass
    return out


def collect_rdp():
    """원격 데스크톱(RDP) 사용 여부. 레지스트리 fDenyTSConnections: 0=사용, 1=사용안함."""
    out = {"enabled": None}
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r"SYSTEM\CurrentControlSet\Control\Terminal Server")
        val = int(winreg.QueryValueEx(key, "fDenyTSConnections")[0])
        out["enabled"] = (val == 0)
    except Exception:
        out["enabled"] = None
    return out


def collect_restore():
    """시스템 복원 지점 개수/사용 여부. 관리자 권한이 필요할 수 있어 실패 시 확인불가."""
    out = {"enabled": None, "count": 0}
    try:
        text, rc = _run_text(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                              "(Get-ComputerRestorePoint | Measure-Object).Count"], timeout=20)
        if rc == 0:
            count = None
            for line in text.splitlines():
                t = line.strip()
                if t.isdigit():
                    count = int(t)
                    break
            if count is not None:
                out["count"] = count
                out["enabled"] = count > 0
    except Exception:
        pass
    return out


def collect_default_browser():
    """기본 브라우저 (HKCU UrlAssociations http UserChoice 의 ProgId)."""
    out = {"browser": "확인불가"}
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"SOFTWARE\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice")
        progid = winreg.QueryValueEx(key, "ProgId")[0] or ""
        if "MSEdge" in progid:
            out["browser"] = "Microsoft Edge"
        elif "Chrome" in progid:
            out["browser"] = "Google Chrome"
        elif "Firefox" in progid:
            out["browser"] = "Mozilla Firefox"
        elif "AppXq0" in progid or "IE" in progid:
            out["browser"] = "Internet Explorer"
        elif progid:
            out["browser"] = progid
    except Exception:
        pass
    return out


def collect_defender():
    """Windows Defender 실시간 보호 여부 + 백신 제품명."""
    out = {"realtime": None, "antivirus": "확인불가"}
    try:
        text, _rc = _run_text(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                               "(Get-MpComputerStatus).RealTimeProtectionEnabled"], timeout=20)
        if "True" in text:
            out["realtime"] = True
        elif "False" in text:
            out["realtime"] = False
    except Exception:
        out["realtime"] = None
    # 백신 제품명: SecurityCenter2 의 AntiVirusProduct
    name = None
    c = _wmi_conn(namespace="root/SecurityCenter2")
    if c:
        try:
            products = c.AntiVirusProduct()
            if products:
                name = getattr(products[0], "displayName", None)
        except Exception:
            name = None
    if name:
        out["antivirus"] = name
    else:
        out["antivirus"] = "Windows Defender" if out["realtime"] is not None else "확인불가"
    return out


def collect_security2():
    """추가 보안 점검: 방화벽·UAC·BitLocker·SMBv1·자동로그인·게스트·hosts 변조."""
    import os
    out = {}
    # 방화벽·BitLocker·SMBv1·게스트 를 PowerShell 한 번에 조회(프로세스 절약)
    ps = ("$fw=(Get-NetFirewallProfile -EA SilentlyContinue).Enabled -join ',';"
          "$bl=(Get-BitLockerVolume -MountPoint 'C:' -EA SilentlyContinue).ProtectionStatus;"
          "$smb=(Get-SmbServerConfiguration -EA SilentlyContinue).EnableSMB1Protocol;"
          "$g=(Get-LocalUser -Name 'Guest' -EA SilentlyContinue).Enabled;"
          "Write-Output \"FW=$fw|BL=$bl|SMB=$smb|GUEST=$g\"")
    txt, _ = _run_text(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], timeout=25)
    d = {}
    for part in (txt or "").split("|"):
        if "=" in part:
            k, v = part.split("=", 1)
            d[k.strip()] = v.strip()
    fw = d.get("FW", "")
    out["firewall"] = "켜짐" if "True" in fw else ("꺼짐" if "False" in fw else "확인불가")
    bl = d.get("BL", "")
    out["bitlocker"] = "암호화됨" if ("On" in bl) else ("해제됨(암호화 안 됨)" if ("Off" in bl) else "확인불가")
    smb = d.get("SMB", "")
    out["smb1"] = "활성(위험)" if "True" in smb else ("비활성(정상)" if "False" in smb else "확인불가")
    g = d.get("GUEST", "")
    out["guest"] = "활성(위험)" if "True" in g else ("비활성(정상)" if "False" in g else "확인불가")
    # UAC / 자동 로그인 (레지스트리)
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                           r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System")
        v = winreg.QueryValueEx(k, "EnableLUA")[0]
        winreg.CloseKey(k)
        out["uac"] = "켜짐" if v == 1 else "꺼짐(위험)"
    except Exception:
        out["uac"] = "확인불가"
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                           r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon")
        v = str(winreg.QueryValueEx(k, "AutoAdminLogon")[0])
        winreg.CloseKey(k)
        out["autologin"] = (v == "1")
    except Exception:
        out["autologin"] = False
    # hosts 파일 변조(기본 외 항목 수)
    try:
        hp = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                          "System32", "drivers", "etc", "hosts")
        with open(hp, "r", errors="ignore") as f:
            entries = [l.strip() for l in f if l.strip() and not l.strip().startswith("#")]
        out["hosts_entries"] = len(entries)
    except Exception:
        pass
    return out


_BUGCHECKS = {
    "0x0000000a": "IRQL_NOT_LESS_OR_EQUAL — 드라이버/메모리",
    "0x0000001a": "MEMORY_MANAGEMENT — 메모리 불량 의심",
    "0x0000001e": "KMODE_EXCEPTION_NOT_HANDLED — 드라이버",
    "0x0000003b": "SYSTEM_SERVICE_EXCEPTION — 드라이버",
    "0x00000050": "PAGE_FAULT_IN_NONPAGED_AREA — 메모리/드라이버",
    "0x0000007e": "SYSTEM_THREAD_EXCEPTION_NOT_HANDLED — 드라이버",
    "0x0000007f": "UNEXPECTED_KERNEL_MODE_TRAP — 하드웨어/메모리",
    "0x0000009f": "DRIVER_POWER_STATE_FAILURE — 드라이버/전원",
    "0x000000c2": "BAD_POOL_CALLER — 드라이버/메모리",
    "0x000000d1": "DRIVER_IRQL_NOT_LESS_OR_EQUAL — 드라이버",
    "0x000000ef": "CRITICAL_PROCESS_DIED — 시스템 파일",
    "0x000000f4": "CRITICAL_OBJECT_TERMINATION — 디스크/시스템",
    "0x00000101": "CLOCK_WATCHDOG_TIMEOUT — CPU/오버클럭 의심",
    "0x00000109": "CRITICAL_STRUCTURE_CORRUPTION — 드라이버/메모리",
    "0x00000116": "VIDEO_TDR_FAILURE — 그래픽 드라이버",
    "0x00000124": "WHEA_UNCORRECTABLE_ERROR — 하드웨어(CPU/메모리/메인보드) 의심",
    "0x00000133": "DPC_WATCHDOG_VIOLATION — 드라이버/SSD 펌웨어",
}


def collect_bluescreen():
    """블루스크린 분석: 미니덤프 파일 + BugCheck 이벤트(버그체크 코드→원인)."""
    import glob
    import os
    import re
    import datetime as _dt
    out = {"minidumps": [], "events": []}
    try:
        md = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Minidump")
        files = glob.glob(os.path.join(md, "*.dmp"))
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for p in files[:10]:
            out["minidumps"].append({
                "file": os.path.basename(p),
                "date": _dt.datetime.fromtimestamp(os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M")})
    except Exception:
        pass
    ps = ("Get-WinEvent -FilterHashtable @{LogName='System';Id=1001;"
          "ProviderName='Microsoft-Windows-WER-SystemErrorReporting'} -MaxEvents 6 -EA SilentlyContinue |"
          "ForEach-Object { $_.TimeCreated.ToString('yyyy-MM-dd HH:mm') + '|' + ($_.Message -replace \"`r`n\",' ') }")
    txt, _ = _run_text(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], timeout=25)
    for line in (txt or "").splitlines():
        if "|" not in line:
            continue
        t, msg = line.split("|", 1)
        m = re.search(r"0x[0-9a-fA-F]{8}", msg)
        code = m.group(0).lower() if m else "?"
        out["events"].append({"time": t.strip(), "code": code,
                              "name": _BUGCHECKS.get(code, "알 수 없는 코드")})
    return out


# 정적(세션 중 안 변하는) + 느린 수집기 결과 캐시 → 재진단 속도 대폭 향상
_STATIC_CACHE = {}


def _cached(key, fn, default=None):
    """느린 정적 수집기는 프로세스당 1회만 실행하고 결과를 캐시."""
    if key not in _STATIC_CACHE:
        try:
            _STATIC_CACHE[key] = fn()
        except Exception:
            _STATIC_CACHE[key] = default
    return _STATIC_CACHE[key]


def clear_static_cache():
    _STATIC_CACHE.clear()


def collect_all():
    sensors = read_sensors()
    return {
        "collected_at": int(time.time()),
        "admin": is_admin(),
        "sensors_ok": sensors.get("ok", False),
        # --- 실시간(매번 갱신) ---
        "system": collect_system(),
        "cpu": collect_cpu(),
        "memory": collect_memory(),
        "swap": collect_swap(),
        "disks": collect_disks(),
        "gpu": collect_gpu(sensors.get("gpu")),
        "battery": collect_battery(),
        "temps": sensors.get("temps", {}),
        "fans": sensors.get("fans", {}),
        "volts": sensors.get("volts", {}),
        "powers": sensors.get("powers", {}),
        "clocks": sensors.get("clocks", {}),
        "top_processes": collect_top_processes(),
        "network": collect_network(),
        "event_log": collect_event_log(),
        "problem_devices": collect_problem_devices(),
        "services_stopped": collect_services_stopped(),
        "wifi": collect_wifi(),
        "accounts": collect_accounts(),
        "power": collect_power(),
        "startup": collect_startup(),
        # --- 정적/느림(캐시: 첫 진단만 느리고 재진단은 즉시) ---
        "memory_modules": _cached("memory_modules", collect_memory_modules, {}),
        "mainboard": _cached("mainboard", collect_mainboard, {}),
        "smart": _cached("smart", collect_smart, []),
        "displays": _cached("displays", collect_displays, []),
        "installed_count": _cached("installed_count", lambda: len(collect_installed()), 0),
        "dotnet": _cached("dotnet", collect_dotnet, {}),
        "vcredist": _cached("vcredist", collect_vcredist, []),
        "directx": _cached("directx", collect_directx, {}),
        "wmi_health": _cached("wmi_health", collect_wmi_health, {}),
        "recovery": _cached("recovery", collect_recovery, {}),
        "shares": _cached("shares", collect_shares, []),
        "rdp": _cached("rdp", collect_rdp, {}),
        "restore": _cached("restore", collect_restore, {}),
        "default_browser": _cached("default_browser", collect_default_browser, {}),
        "defender": _cached("defender", collect_defender, {}),
        "security": _cached("security", collect_security, {}),
        "security2": _cached("security2", collect_security2, {}),
        "bluescreen": _cached("bluescreen", collect_bluescreen, {}),
    }


# ---------------- 원클릭 분석 ----------------
def analyze(data):
    issues = []
    mem = data.get("memory", {})
    if mem.get("percent", 0) >= 90:
        issues.append(("warn", "메모리 사용률이 %s%% 로 높습니다." % mem["percent"]))
    for v in data.get("disks", {}).get("volumes", []):
        if v.get("percent", 0) >= 90:
            issues.append(("warn", "%s 여유공간 부족 (%s%% 사용, %sGB 남음)"
                           % (v["device"], v["percent"], v["free_gb"])))
    for ph in data.get("disks", {}).get("physical", []):
        if ph.get("health") not in ("정상", "?", None):
            issues.append(("warn", "디스크 상태 이상: %s (%s)" % (ph["model"], ph["health"])))
    if data.get("cpu", {}).get("usage_percent", 0) >= 90:
        issues.append(("warn", "CPU 사용률이 %s%% 로 높습니다." % data["cpu"]["usage_percent"]))
    for label, temp in data.get("temps", {}).items():
        if temp >= 85:
            issues.append(("warn", "%s 온도가 %s°C 로 높습니다." % (label, temp)))
    b = data.get("battery")
    if b and not b["plugged"] and b["percent"] <= 20:
        issues.append(("info", "배터리 잔량 %s%% (충전 권장)" % b["percent"]))
    net = data.get("network", {})
    if net.get("adapters") and not net.get("internet"):
        issues.append(("warn", "인터넷 연결이 안 됩니다 (외부 접속 실패) — 인터넷 복구 권장."))
    elif net.get("internet") and not net.get("dns"):
        issues.append(("warn", "인터넷은 되지만 DNS 조회 실패 — DNS 플러시 권장."))
    if len(data.get("event_log", [])) >= 8:
        issues.append(("info", "최근 3일 시스템 오류 이벤트가 %d건 이상입니다." % len(data["event_log"])))
    if data.get("problem_devices"):
        issues.append(("warn", "드라이버/장치 문제 %d건 (장치 관리자 확인 필요)."
                       % len(data["problem_devices"])))
    if len(data.get("startup", [])) > 20:
        issues.append(("info", "시작 프로그램이 %d개 — 부팅이 느릴 수 있습니다."
                       % len(data["startup"])))
    if data.get("services_stopped"):
        issues.append(("info", "자동 시작 서비스 %d개가 멈춰 있습니다."
                       % len(data["services_stopped"])))
    if not data.get("temps"):
        issues.append(("info", "CPU 온도는 관리자 권한으로 실행하면 표시됩니다." if not data.get("admin")
                       else "온도 센서를 읽지 못했습니다."))
    if not any(lv == "warn" for lv, _ in issues):
        issues.insert(0, ("info", "특별한 이상 징후는 발견되지 않았습니다."))
    return issues


# ---------------- 리포트 텍스트 ----------------
def report_text(data=None):
    if data is None:
        data = collect_all()
    L = ["=" * 46, " PC 진단 리포트", "=" * 46]
    s = data["system"]
    L.append("[시스템]")
    L.append("  호스트명 : %s" % s.get("hostname", "?"))
    L.append("  OS       : %s (빌드 %s)" % (s.get("edition", s.get("os")), s.get("build", "?")))
    if "uptime_hours" in s:
        L.append("  가동시간 : %s 시간" % s["uptime_hours"])

    c = data["cpu"]
    L.append("[CPU] %s" % c.get("name", "?"))
    if "cores_physical" in c:
        L.append("  코어 %s (스레드 %s) · 사용률 %s%%"
                 % (c.get("cores_physical"), c.get("cores_logical"), c.get("usage_percent", "?")))

    m = data["memory"]
    if m:
        L.append("[메모리] %sGB 중 %sGB 사용 (%s%%)" % (m["total_gb"], m["used_gb"], m["percent"]))

    L.append("[디스크]")
    for v in data["disks"].get("volumes", []):
        L.append("  %s  %sGB 중 %sGB 사용 (%s%%, %sGB 남음)"
                 % (v["device"], v["total_gb"], v["used_gb"], v["percent"], v["free_gb"]))
    for ph in data["disks"].get("physical", []):
        L.append("  · %s [%s, %sGB, 상태:%s]"
                 % (ph["model"], ph["type"], ph["size_gb"], ph.get("health", "?")))

    if data["gpu"]:
        L.append("[그래픽]")
        for it in data["gpu"]:
            extra = []
            if "vram_total_gb" in it:
                extra.append("VRAM %s/%sGB" % (it.get("vram_used_gb", "?"), it["vram_total_gb"]))
            if "temp" in it:
                extra.append("%s°C" % it["temp"])
            if "load_percent" in it:
                extra.append("로드 %s%%" % it["load_percent"])
            L.append("  %s%s" % (it["name"], ("  [" + ", ".join(extra) + "]") if extra else ""))

    temps = data.get("temps", {})
    L.append("[온도]")
    if temps:
        for k, v in temps.items():
            L.append("  %-10s %s°C" % (k, v))
    else:
        L.append("  (관리자 권한으로 실행하면 CPU 온도가 표시됩니다)")

    if data.get("fans"):
        L.append("[팬] " + ", ".join("%s %sRPM" % (k, v) for k, v in data["fans"].items()))

    b = data["battery"]
    if b:
        L.append("[배터리] %s%% (%s)" % (b["percent"], "충전 중" if b["plugged"] else "배터리"))

    L.append("[메모리 상위 프로세스]")
    for p in data["top_processes"]:
        L.append("  %-24s %sMB" % (p["name"], p["ram_mb"]))

    net = data.get("network", {})
    L.append("[네트워크]")
    for a in net.get("adapters", []):
        sp = " · %sMbps" % a["speed_mbps"] if a.get("speed_mbps") else ""
        L.append("  %s  %s%s" % (a["name"], a["ip"], sp))
    if net.get("gateway"):
        L.append("  게이트웨이 %s" % net["gateway"])
    L.append("  인터넷 %s · DNS %s"
             % ("정상" if net.get("internet") else "실패",
                "정상" if net.get("dns") else "실패"))

    errs = data.get("event_log", [])
    if errs:
        L.append("[최근 오류 이벤트]")
        for e in errs[:8]:
            L.append("  %s [%s/%s] %s" % (e["time"], e["source"], e["id"], e["msg"]))

    pd = data.get("problem_devices", [])
    if pd:
        L.append("[드라이버/장치 문제]")
        for d in pd[:8]:
            L.append("  ⚠️ %s (코드 %s)" % (d["name"], d["error_code"]))

    st = data.get("startup", [])
    if st:
        L.append("[시작 프로그램] %d개" % len(st))
        for s in st[:10]:
            L.append("  · %s" % s["name"])

    ss = data.get("services_stopped", [])
    if ss:
        L.append("[멈춘 자동 서비스] %d개" % len(ss))
        for s in ss[:8]:
            L.append("  · %s (%s)" % (s["name"], s["status"]))

    L.append("[설치 프로그램] 총 %d개 (상세는 별도 목록)" % data.get("installed_count", 0))

    L.append("-" * 46)
    try:
        import analysis as _an
        L.extend(_an.report_lines(_an.analyze(data)))
    except Exception:
        L.append("[분석 결과]")
        for level, msg in analyze(data):
            L.append("  %s%s" % ("⚠️ " if level == "warn" else "· ", msg))
    L.append("=" * 46)
    return "\n".join(L)


def report_html(data=None):
    """공유·저장용 HTML 리포트 (디자인은 최소, 자체 완결형)."""
    if data is None:
        data = collect_all()
    import html as _html
    import datetime as _dt
    esc = _html.escape
    ts = _dt.datetime.fromtimestamp(data.get("collected_at", int(time.time())))
    s, c, m = data["system"], data["cpu"], data["memory"]

    def rows(pairs):
        return "".join("<tr><th>%s</th><td>%s</td></tr>" % (esc(str(k)), esc(str(v)))
                       for k, v in pairs)

    parts = []
    parts.append(rows([
        ("호스트명", s.get("hostname", "?")),
        ("OS", "%s (빌드 %s)" % (s.get("edition", s.get("os")), s.get("build", "?"))),
        ("가동시간", "%s 시간" % s.get("uptime_hours", "?")),
        ("CPU", c.get("name", "?")),
        ("CPU 코어", "%s코어 / %s스레드 · 사용률 %s%%"
         % (c.get("cores_physical", "?"), c.get("cores_logical", "?"), c.get("usage_percent", "?"))),
        ("메모리", "%sGB 중 %sGB 사용 (%s%%)" % (m.get("total_gb", "?"), m.get("used_gb", "?"), m.get("percent", "?"))
         if m else "?"),
    ]))
    disk_rows = ""
    for v in data["disks"].get("volumes", []):
        disk_rows += "<tr><th>%s</th><td>%sGB 중 %sGB 사용 (%s%%, %sGB 남음)</td></tr>" % (
            esc(v["device"]), v["total_gb"], v["used_gb"], v["percent"], v["free_gb"])
    for ph in data["disks"].get("physical", []):
        disk_rows += "<tr><th>%s</th><td>%s, %sGB, 상태 %s</td></tr>" % (
            esc(ph["model"]), ph["type"], ph["size_gb"], esc(str(ph.get("health", "?"))))
    gpu_rows = ""
    for it in data["gpu"]:
        d = []
        if "vram_total_gb" in it:
            d.append("VRAM %s/%sGB" % (it.get("vram_used_gb", "?"), it["vram_total_gb"]))
        if "temp" in it:
            d.append("%s°C" % it["temp"])
        if "load_percent" in it:
            d.append("로드 %s%%" % it["load_percent"])
        gpu_rows += "<tr><th>%s</th><td>%s</td></tr>" % (esc(it["name"]), esc(", ".join(d)))
    temp_rows = "".join("<tr><th>%s</th><td>%s°C</td></tr>" % (esc(str(k)), v)
                        for k, v in data.get("temps", {}).items()) or \
        "<tr><td colspan=2>관리자 권한으로 실행하면 CPU 온도가 표시됩니다</td></tr>"
    net = data.get("network", {})
    net_rows = ""
    for a in net.get("adapters", []):
        sp = " · %sMbps" % a["speed_mbps"] if a.get("speed_mbps") else ""
        net_rows += "<tr><th>%s</th><td>%s%s</td></tr>" % (esc(a["name"]), esc(a["ip"]), esc(sp))
    if net.get("gateway"):
        net_rows += "<tr><th>게이트웨이</th><td>%s</td></tr>" % esc(net["gateway"])
    net_rows += "<tr><th>인터넷 / DNS</th><td>%s / %s</td></tr>" % (
        "정상" if net.get("internet") else "실패", "정상" if net.get("dns") else "실패")
    ev = data.get("event_log", [])
    ev_rows = "".join(
        "<tr><td style='width:110px;color:#888;white-space:nowrap'>%s</td><td>[%s/%s] %s</td></tr>"
        % (esc(e["time"]), esc(e["source"]), e["id"], esc(e["msg"])) for e in ev[:8]) \
        or "<tr><td colspan=2>최근 오류 이벤트 없음</td></tr>"
    pd = data.get("problem_devices", [])
    pd_rows = "".join("<tr><td>⚠️ %s</td><td>코드 %s</td></tr>" % (esc(d["name"]), d["error_code"])
                      for d in pd[:10]) or "<tr><td colspan=2>문제 없음</td></tr>"
    st = data.get("startup", [])
    st_count = len(st)
    st_rows = "".join("<tr><td>%s</td></tr>" % esc(s["name"]) for s in st[:15]) \
        or "<tr><td>없음</td></tr>"
    inst = data.get("installed_count", 0)

    # 시스템 구성 요소 섹션
    dn = data.get("dotnet", {})
    vc = data.get("vcredist", [])
    dx = data.get("directx", {})
    wh = data.get("wmi_health", {})
    wf = data.get("wifi", {})
    accts = data.get("accounts", [])
    pw = data.get("power", {})
    rec = data.get("recovery", {})
    _admin_acct = next((a for a in accts if a.get("admin")), None)
    admin_state = ("활성" if _admin_acct.get("enabled") else "비활성") if _admin_acct else "없음"
    _fast = pw.get("fast_startup")
    fast_txt = "켜짐" if _fast else ("꺼짐" if _fast is False else "확인불가")
    comp_rows = "".join([
        "<tr><th>.NET Framework</th><td>%s (런타임 %d개)</td></tr>"
        % (esc(str(dn.get("framework", "확인불가"))), len(dn.get("runtimes", []))),
        "<tr><th>Visual C++ 재배포</th><td>%d개 설치됨</td></tr>" % len(vc),
        "<tr><th>DirectX</th><td>%s</td></tr>" % esc(str(dx.get("version", "확인불가"))),
        "<tr><th>WMI 저장소</th><td>%s</td></tr>" % esc(str(wh.get("status", "확인불가"))),
        "<tr><th>Wi-Fi 프로파일</th><td>%d개</td></tr>" % wf.get("count", 0),
        "<tr><th>로컬 계정</th><td>%d개 (Administrator %s)</td></tr>" % (len(accts), admin_state),
        "<tr><th>전원 플랜</th><td>%s · 빠른 시작 %s</td></tr>"
        % (esc(str(pw.get("plan", "확인불가"))), fast_txt),
        "<tr><th>WinRE(복구환경)</th><td>%s</td></tr>" % esc(str(rec.get("winre", "확인불가"))),
    ])

    # 공유·보안·기타 섹션
    shares = data.get("shares", [])
    rdp = data.get("rdp", {})
    restore = data.get("restore", {})
    browser = data.get("default_browser", {})
    defender = data.get("defender", {})
    _rdp = rdp.get("enabled")
    rdp_txt = "사용" if _rdp else ("사용안함" if _rdp is False else "확인불가")
    _rest = restore.get("enabled")
    if _rest is None:
        restore_txt = "확인불가"
    elif _rest:
        restore_txt = "사용 · 복원지점 %d개" % restore.get("count", 0)
    else:
        restore_txt = "사용안함 (복원지점 0개)"
    _rt = defender.get("realtime")
    rt_txt = "켜짐" if _rt else ("꺼짐" if _rt is False else "확인불가")
    misc_rows = "".join([
        "<tr><th>공유 폴더</th><td>%d개</td></tr>" % len(shares),
        "<tr><th>RDP(원격데스크톱)</th><td>%s</td></tr>" % rdp_txt,
        "<tr><th>시스템 복원</th><td>%s</td></tr>" % restore_txt,
        "<tr><th>기본 브라우저</th><td>%s</td></tr>" % esc(str(browser.get("browser", "확인불가"))),
        "<tr><th>실시간 보호(백신)</th><td>%s (%s)</td></tr>"
        % (rt_txt, esc(str(defender.get("antivirus", "확인불가")))),
    ])

    # 디스플레이 · 보안 섹션
    disp = data.get("displays", [])
    sec = data.get("security", {})
    first_disp = disp[0] if disp else {}
    if first_disp:
        _res = first_disp.get("resolution") or "?"
        _rf = first_disp.get("refresh_hz")
        mon_txt = "%s%s" % (_res, (" @%sHz" % _rf) if _rf else "")
        if first_disp.get("name"):
            mon_txt = "%s (%s)" % (first_disp["name"], mon_txt)
    else:
        mon_txt = "확인불가"
    _sb = sec.get("secure_boot")
    sb_txt = "사용" if _sb else ("사용안함" if _sb is False else "확인불가")
    pubip = data.get("network", {}).get("public_ip") or "확인불가"
    dispsec_rows = "".join([
        "<tr><th>모니터</th><td>%s</td></tr>" % esc(str(mon_txt)),
        "<tr><th>TPM</th><td>%s</td></tr>" % esc(str(sec.get("tpm", "확인불가"))),
        "<tr><th>보안 부팅</th><td>%s</td></tr>" % sb_txt,
        "<tr><th>Windows 정품 인증</th><td>%s</td></tr>" % esc(str(sec.get("activation", "확인불가"))),
        "<tr><th>공인 IP</th><td>%s</td></tr>" % esc(str(pubip)),
    ])

    # 저장장치 SMART 섹션 (디스크별 온도·전원인가시간·총기록량·수명)
    smart_rows = ""
    for sm in data.get("smart", []):
        parts_sm = []
        if sm.get("temp_c") is not None:
            parts_sm.append("%s°C" % sm["temp_c"])
        if sm.get("power_on_hours") is not None:
            parts_sm.append("전원인가 %s시간" % sm["power_on_hours"])
        if sm.get("data_written_tb") is not None:
            parts_sm.append("기록 %sTB" % sm["data_written_tb"])
        if sm.get("data_read_tb") is not None:
            parts_sm.append("읽기 %sTB" % sm["data_read_tb"])
        if sm.get("wear_pct") is not None:
            parts_sm.append("수명 %s%%" % sm["wear_pct"])
        detail = ", ".join(parts_sm) if parts_sm else "정보 없음"
        smart_rows += "<tr><th>%s</th><td>%s (상태 %s)</td></tr>" % (
            esc(str(sm.get("model", "?"))), esc(detail), esc(str(sm.get("health", "?"))))
    if not smart_rows:
        smart_rows = "<tr><td colspan=2>SMART 정보를 읽지 못했습니다</td></tr>"

    issue_items = "".join(
        '<li class="%s">%s</li>' % ("warn" if lv == "warn" else "ok", esc(msg))
        for lv, msg in analyze(data))

    try:
        import analysis as _an
        _a = _an.analyze(data)
        ai_html = ('<div style="font-size:26px;font-weight:800;color:#5a4632">건강 점수 %d/100 · %s</div>'
                   '<p>%s</p>' % (_a["score"], esc(_a["grade"]), esc(_a["summary_customer"])))
        if _a["causes"]:
            ai_html += "<ol>"
            for c in _a["causes"]:
                cls = "warn" if c["severity"] in ("high", "medium") else "ok"
                ai_html += ('<li class="%s"><b>%s</b> <span style="color:#666">(%s %d%%)</span>'
                            '<br>해결: %s</li>'
                            % (cls, esc(c["label"]), c["severity"].upper(), c["probability"],
                               esc(" → ".join(c["fixes"]))))
            ai_html += "</ol>"
    except Exception:
        ai_html = "<ul>" + issue_items + "</ul>"

    return """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<title>PC 진단 리포트</title><style>
body{font-family:'Malgun Gothic',sans-serif;max-width:840px;margin:26px auto;padding:0 18px;color:#38302a;font-size:16px;line-height:1.65}
h1{font-size:25px}h2{font-size:18px;margin-top:28px;border-bottom:2px solid #bd8f5d;padding-bottom:5px;color:#5a4632}
table{width:100%%;border-collapse:collapse;margin-top:8px}th,td{text-align:left;padding:9px 11px;border-bottom:1px solid #ece2d1;font-size:15.5px}
th{width:210px;color:#8a7862;font-weight:600}
ul{list-style:none;padding:0}li{padding:11px 15px;border-radius:9px;margin:8px 0;font-size:15.5px}
li.warn{background:#f6e4dc;color:#a3402a}li.ok{background:#eaf0dd;color:#4c6a24}
.ts{color:#a89a86;font-size:13px}
</style></head><body>
<h1>🖥️ PC 진단 리포트</h1><div class="ts">생성: %s</div>
<h2>AI 분석</h2>%s
<h2>시스템 · CPU · 메모리</h2><table>%s</table>
<h2>디스크</h2><table>%s</table>
<h2>저장장치 SMART</h2><table>%s</table>
<h2>그래픽</h2><table>%s</table>
<h2>디스플레이 · 보안</h2><table>%s</table>
<h2>온도</h2><table>%s</table>
<h2>네트워크</h2><table>%s</table>
<h2>최근 오류 이벤트</h2><table>%s</table>
<h2>드라이버/장치 문제</h2><table>%s</table>
<h2>시작 프로그램 (%d개)</h2><table>%s</table>
<h2>시스템 구성 요소</h2><table>%s</table>
<h2>공유·보안·기타</h2><table>%s</table>
<p style="color:#555;font-size:13px">설치된 프로그램: 총 %d개</p>
</body></html>""" % (ts.strftime("%Y-%m-%d %H:%M"), ai_html,
                     parts[0], disk_rows, smart_rows, gpu_rows, dispsec_rows, temp_rows,
                     net_rows, ev_rows, pd_rows, st_count, st_rows, comp_rows, misc_rows, inst)


if __name__ == "__main__":
    print(report_text())
