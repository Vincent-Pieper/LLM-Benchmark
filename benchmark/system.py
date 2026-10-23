"""Read-only Windows system snapshot used before comparable benchmark runs."""
from __future__ import annotations
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
from .store import now


def command(args, timeout=5):
    try:
        return subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, check=False,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return None


class MemoryStatus(ctypes.Structure):
    _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def memory():
    if os.name != "nt":
        return 0, 0
    status = MemoryStatus()
    status.dwLength = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return 0, 0
    return status.ullTotalPhys, status.ullAvailPhys


def gpu_info():
    candidates = [shutil.which("nvidia-smi.exe"),
                  r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"]
    for executable in filter(None, candidates):
        result = command([executable, "--query-gpu=name,memory.total,memory.used,utilization.gpu",
                          "--format=csv,noheader,nounits"], timeout=4)
        if result and result.returncode == 0 and result.stdout.strip():
            try:
                name, total, used, utilization = [part.strip() for part in result.stdout.splitlines()[0].split(",")]
                return {"name": name, "totalMiB": int(total), "usedMiB": int(used),
                        "utilization": int(utilization)}
            except (ValueError, IndexError):
                pass
    return None


PROCESS_SCRIPT = (
    'Get-Process | Sort-Object WorkingSet64 -Descending | '
    'Select-Object -First 8 ProcessName,Id,@{Name="ramMiB";'
    'Expression={[math]::Round($_.WorkingSet64/1MB)}} | ConvertTo-Json -Compress'
)
BROWSER_SCRIPT = (
    '$p=Get-Process -ErrorAction SilentlyContinue | Where-Object '
    '{ $_.ProcessName -in @("chrome","msedge","firefox") }; '
    '[math]::Round(($p | Measure-Object WorkingSet64 -Sum).Sum / 1MB)'
)


def powershell(script):
    return command(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script])


def processes():
    result = powershell(PROCESS_SCRIPT)
    if not result or result.returncode or not result.stdout.strip():
        return []
    try:
        value = json.loads(result.stdout.lstrip("\ufeff"))
        return value if isinstance(value, list) else [value]
    except ValueError:
        return []


def browser_ram():
    result = powershell(BROWSER_SCRIPT)
    try:
        return float(result.stdout.strip()) if result and result.returncode == 0 else None
    except ValueError:
        return None


def cpu_name():
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
                return winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
        except OSError:
            pass
    result = powershell("(Get-CimInstance Win32_Processor | Select-Object -First 1 -ExpandProperty Name)")
    return result.stdout.strip() if result and result.returncode == 0 and result.stdout.strip() else platform.processor() or "Unknown"


def snapshot():
    total, free = memory()
    gpu = gpu_info()
    browser = browser_ram()
    warnings = []
    if total and free / total < 0.25:
        warnings.append("Available RAM is below 25%. Close other memory-heavy programs before a comparable run.")
    if gpu and gpu["totalMiB"] and gpu["usedMiB"] / gpu["totalMiB"] > 0.5:
        warnings.append("More than half of GPU memory is already in use.")
    if gpu and gpu["utilization"] > 30:
        warnings.append("GPU is busy before the run.")
    if browser is not None and browser > 4096:
        warnings.append(f"Browsers are using {browser:g} MiB of RAM. Close them for a cleaner comparison.")
    return {"at": now(), "host": socket.gethostname(), "cpu": cpu_name(), "cores": os.cpu_count() or 1,
            "totalRamGiB": round(total / 2**30, 1), "freeRamGiB": round(free / 2**30, 1),
            "browserRamMiB": browser, "gpu": gpu, "processes": processes(), "warnings": warnings}


def codex_exe():
    root = Path.home() / "AppData" / "Local" / "OpenAI" / "Codex" / "bin"
    try:
        candidates = [path / "codex.exe" for path in root.iterdir() if path.is_dir()
                      and (path / "codex.exe").is_file()]
        return max(candidates, key=lambda item: item.stat().st_mtime) if candidates else None
    except OSError:
        return None
