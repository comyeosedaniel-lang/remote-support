# 원격 지원 프로그램 - 단일 실행파일(.exe) 빌드 스크립트
# 사용: PowerShell 에서  ./build.ps1
# 결과물: dist\RemoteSupport-Host.exe , dist\RemoteSupport-Viewer.exe

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# WebRTC(aiortc/PyAV) 네이티브 라이브러리까지 번들하기 위한 공통 옵션
$common = @(
    "--noconfirm", "--clean", "--onefile", "--windowed",
    "--collect-submodules", "pynput",
    "--collect-submodules", "websockets",
    "--collect-all", "aiortc",
    "--collect-all", "av",
    "--collect-all", "aioice",
    "--collect-all", "pylibsrtp",
    "--collect-all", "google_crc32c"
)

Write-Host "[1/3] 의존성 확인..." -ForegroundColor Cyan
python -m pip install -q -r requirements.txt pyinstaller

Write-Host "[2/3] 호스트(고객 PC용) 빌드..." -ForegroundColor Cyan
python -m PyInstaller @common --name RemoteSupport-Host host.py

Write-Host "[3/3] 뷰어(기사 PC용) 빌드..." -ForegroundColor Cyan
python -m PyInstaller @common --name RemoteSupport-Viewer viewer.py

Write-Host ""
Write-Host "완료! dist 폴더를 확인하세요:" -ForegroundColor Green
Get-ChildItem dist\*.exe | ForEach-Object {
    "{0,-30} {1,8:N1} MB" -f $_.Name, ($_.Length / 1MB)
}
