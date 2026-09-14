# 원격 지원 프로그램 - 단일 실행파일(.exe) 빌드 스크립트
# 사용: PowerShell 에서  ./build.ps1
# 결과물: dist\RemoteSupport-Host.exe , dist\RemoteSupport-Viewer.exe

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# WebRTC(aiortc/PyAV) + 진단(pythonnet/LibreHardwareMonitor) 네이티브 라이브러리까지 번들
$common = @(
    "--noconfirm", "--clean", "--onefile", "--windowed",
    "--collect-submodules", "pynput",
    "--collect-submodules", "websockets",
    "--collect-all", "aiortc",
    "--collect-all", "av",
    "--collect-all", "aioice",
    "--collect-all", "pylibsrtp",
    "--collect-all", "google_crc32c",
    "--collect-all", "HardwareMonitor",
    "--collect-all", "pythonnet",
    "--collect-all", "clr_loader",
    "--copy-metadata", "pythonnet",
    "--collect-submodules", "wmi"
)

Write-Host "[1/3] 의존성 확인..." -ForegroundColor Cyan
python -m pip install -q -r requirements.txt pyinstaller

Write-Host "[2/3] 호스트(고객 PC용) 빌드..." -ForegroundColor Cyan
python -m PyInstaller @common --name RemoteSupport-Host host.py

Write-Host "[3/4] 뷰어(기사 PC용) 빌드..." -ForegroundColor Cyan
python -m PyInstaller @common --name RemoteSupport-Viewer viewer.py

Write-Host "[3.5] AS Studio(기사 통합 대문, tkinter) 빌드..." -ForegroundColor Cyan
python -m PyInstaller @common --name RemoteSupport-Studio studio.py

Write-Host "[3.6] AS Studio (웹 UI · 로컬서버+Edge앱창) 빌드..." -ForegroundColor Cyan
# pywebview 제거됨(먹통 원인). webui 폴더(html+아이콘) 통째로 번들 + exe 아이콘 지정
python -m PyInstaller @common `
    --collect-submodules qrcode `
    --add-data "webui;webui" `
    --icon "webui/icon.ico" `
    --name RemoteSupport-Studio-Web studio_web.py

# 독립 PC 진단 프로그램 (webrtc 불필요 → 가벼운 옵션)
$diagcommon = @(
    "--noconfirm", "--clean", "--onefile", "--windowed",
    "--collect-all", "HardwareMonitor",
    "--collect-all", "pythonnet",
    "--collect-all", "clr_loader",
    "--copy-metadata", "pythonnet",
    "--collect-submodules", "wmi"
)
Write-Host "[4/4] PC 진단 프로그램 빌드..." -ForegroundColor Cyan
python -m PyInstaller @diagcommon --name RemoteSupport-Diagnostic diagnostic_gui.py

Write-Host ""
Write-Host "완료! dist 폴더를 확인하세요:" -ForegroundColor Green
Get-ChildItem dist\*.exe | ForEach-Object {
    "{0,-30} {1,8:N1} MB" -f $_.Name, ($_.Length / 1MB)
}
