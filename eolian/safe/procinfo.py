"""进程 / 权限 / 完整性级别 / 服务状态（ctypes，零第三方依赖）。

只读为主；``kill_process`` 是唯一会改变系统状态的函数，且只由清理流程调用。
非 Windows 平台全部降级为"空结果"，方便在 CI/测试里跑。

注意：所有 Win32 调用都显式声明 argtypes/restype。句柄是 64 位值时，
不声明 argtypes 会被 ctypes 当成 32 位 int，直接 ``OverflowError``。
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass

IS_WINDOWS = sys.platform == "win32"

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
PROCESS_TERMINATE = 0x0001
TOKEN_QUERY = 0x0008
TokenIntegrityLevel = 25
LIST_MODULES_ALL = 0x03

SERVICE_RUNNING = 0x00000004
SERVICE_STOPPED = 0x00000001
SC_MANAGER_CONNECT = 0x0001
SERVICE_QUERY_STATUS = 0x0004

INTEGRITY_NAMES = {
    0x0000: "untrusted",
    0x1000: "low",
    0x2000: "medium",
    0x2100: "medium-plus",
    0x3000: "high",
    0x4000: "system",
    0x5000: "protected",
}


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    name: str


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


LPPROCESSENTRY32W = ctypes.POINTER(_PROCESSENTRY32W)


class _SERVICE_STATUS(ctypes.Structure):
    _fields_ = [
        ("dwServiceType", wintypes.DWORD),
        ("dwCurrentState", wintypes.DWORD),
        ("dwControlsAccepted", wintypes.DWORD),
        ("dwWin32ExitCode", wintypes.DWORD),
        ("dwServiceSpecificExitCode", wintypes.DWORD),
        ("dwCheckPoint", wintypes.DWORD),
        ("dwWaitHint", wintypes.DWORD),
    ]


_LP_SERVICE_STATUS = ctypes.POINTER(_SERVICE_STATUS)

_k32 = None
_advapi = None
_psapi = None


def _load() -> None:
    global _k32, _advapi, _psapi
    if _k32 is not None or not IS_WINDOWS:
        return
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.Process32FirstW.argtypes = [wintypes.HANDLE, LPPROCESSENTRY32W]
    _k32.Process32FirstW.restype = wintypes.BOOL
    _k32.Process32NextW.argtypes = [wintypes.HANDLE, LPPROCESSENTRY32W]
    _k32.Process32NextW.restype = wintypes.BOOL
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.TerminateProcess.restype = wintypes.BOOL

    _advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    _advapi.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)
    ]
    _advapi.OpenProcessToken.restype = wintypes.BOOL
    _advapi.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ]
    _advapi.GetTokenInformation.restype = wintypes.BOOL
    _advapi.GetSidSubAuthorityCount.argtypes = [wintypes.LPVOID]
    _advapi.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    _advapi.GetSidSubAuthority.argtypes = [wintypes.LPVOID, wintypes.DWORD]
    _advapi.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
    _advapi.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    _advapi.OpenSCManagerW.restype = wintypes.HANDLE
    _advapi.OpenServiceW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD]
    _advapi.OpenServiceW.restype = wintypes.HANDLE
    _advapi.QueryServiceStatus.argtypes = [wintypes.HANDLE, _LP_SERVICE_STATUS]
    _advapi.QueryServiceStatus.restype = wintypes.BOOL
    _advapi.CloseServiceHandle.argtypes = [wintypes.HANDLE]
    _advapi.CloseServiceHandle.restype = wintypes.BOOL

    _psapi = ctypes.WinDLL("psapi", use_last_error=True)
    _psapi.EnumProcessModulesEx.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), wintypes.DWORD,
    ]
    _psapi.EnumProcessModulesEx.restype = wintypes.BOOL
    _psapi.GetModuleBaseNameW.argtypes = [
        wintypes.HANDLE, wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD,
    ]
    _psapi.GetModuleBaseNameW.restype = wintypes.DWORD


def _close(handle) -> None:
    if not handle or not IS_WINDOWS:
        return
    try:
        _k32.CloseHandle(handle)
    except OSError:
        pass


def list_processes() -> list[ProcInfo]:
    """枚举进程（Toolhelp32）。失败返回空列表，绝不抛。"""
    if not IS_WINDOWS:
        return []
    _load()
    snapshot = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot:
        return []
    entry = _PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    result: list[ProcInfo] = []
    try:
        if not _k32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return []
        while True:
            result.append(ProcInfo(int(entry.th32ProcessID), entry.szExeFile))
            if not _k32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        _close(snapshot)
    return result


def find_processes(names: tuple[str, ...] | list[str]) -> list[ProcInfo]:
    wanted = {name.lower() for name in names}
    return [proc for proc in list_processes() if proc.name.lower() in wanted]


def is_admin() -> bool:
    if not IS_WINDOWS:
        return bool(getattr(os, "geteuid", lambda: 1)() == 0)
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def _open(pid: int, access: int) -> int | None:
    if not IS_WINDOWS or pid <= 0:
        return None
    _load()
    handle = _k32.OpenProcess(access, False, pid)
    return handle or None


def integrity_level(pid: int) -> str:
    """进程完整性级别（UIPI 门槛）。拿不到一律 "unknown"，由策略决定是否放行。"""
    handle = _open(pid, PROCESS_QUERY_LIMITED_INFORMATION)
    if not handle:
        return "unknown"
    token = wintypes.HANDLE()
    try:
        if not _advapi.OpenProcessToken(handle, TOKEN_QUERY, ctypes.byref(token)):
            return "unknown"
        size = wintypes.DWORD(0)
        _advapi.GetTokenInformation(token, TokenIntegrityLevel, None, 0, ctypes.byref(size))
        if size.value == 0:
            size.value = 64
        buffer = ctypes.create_string_buffer(size.value)
        if not _advapi.GetTokenInformation(
            token, TokenIntegrityLevel, buffer, size.value, ctypes.byref(size)
        ):
            return "unknown"
        sid = ctypes.cast(buffer, ctypes.POINTER(wintypes.LPVOID))[0]
        if not sid:
            return "unknown"
        count = _advapi.GetSidSubAuthorityCount(sid)[0]
        if not count:
            return "unknown"
        rid = int(_advapi.GetSidSubAuthority(sid, count - 1)[0])
        return INTEGRITY_NAMES.get(rid, f"0x{rid:X}")
    except (OSError, ValueError, TypeError):
        return "unknown"
    finally:
        _close(token)
        _close(handle)


def module_names(pid: int) -> list[str]:
    """目标进程已加载模块名（需要 PROCESS_QUERY_INFORMATION|VM_READ）。"""
    handle = _open(pid, PROCESS_QUERY_INFORMATION | PROCESS_VM_READ)
    if not handle:
        return []
    names: list[str] = []
    try:
        capacity = 1024
        array = (wintypes.HMODULE * capacity)()
        needed = wintypes.DWORD(0)
        if not _psapi.EnumProcessModulesEx(
            handle, ctypes.byref(array), ctypes.sizeof(array),
            ctypes.byref(needed), LIST_MODULES_ALL,
        ):
            return []
        count = needed.value // ctypes.sizeof(wintypes.HMODULE)
        buffer = ctypes.create_unicode_buffer(260)
        for index in range(min(count, capacity)):
            if _psapi.GetModuleBaseNameW(handle, array[index], buffer, 260):
                names.append(buffer.value)
    except (OSError, ValueError):
        return names
    finally:
        _close(handle)
    return names


def service_state(name: str) -> str:
    """返回 running / stopped / absent / unknown。"""
    if not IS_WINDOWS:
        return "unknown"
    _load()
    manager = _advapi.OpenSCManagerW(None, None, SC_MANAGER_CONNECT)
    if not manager:
        return "unknown"
    service = None
    try:
        service = _advapi.OpenServiceW(manager, name, SERVICE_QUERY_STATUS)
        if not service:
            return "absent"
        status = _SERVICE_STATUS()
        if not _advapi.QueryServiceStatus(service, ctypes.byref(status)):
            return "unknown"
        if status.dwCurrentState == SERVICE_RUNNING:
            return "running"
        if status.dwCurrentState == SERVICE_STOPPED:
            return "stopped"
        return "other"
    except (OSError, ValueError, TypeError):
        return "unknown"
    finally:
        if service:
            _advapi.CloseServiceHandle(service)
        _advapi.CloseServiceHandle(manager)


def kill_process(pid: int, timeout: float = 3.0) -> bool:
    """终止进程并等待其消失；返回是否确认消失。"""
    handle = _open(pid, PROCESS_TERMINATE)
    if not handle:
        return False
    try:
        _k32.TerminateProcess(handle, 1)
    finally:
        _close(handle)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(proc.pid == pid for proc in list_processes()):
            return True
        time.sleep(0.1)
    return False


def remote_debugger_present(pid: int) -> bool:
    """CheckRemoteDebuggerPresent（拿不到句柄时按"未附加"处理）。"""
    handle = _open(pid, PROCESS_QUERY_LIMITED_INFORMATION)
    if not handle:
        return False
    _load()
    _k32.CheckRemoteDebuggerPresent.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    _k32.CheckRemoteDebuggerPresent.restype = wintypes.BOOL
    try:
        present = wintypes.BOOL(False)
        if _k32.CheckRemoteDebuggerPresent(handle, ctypes.byref(present)):
            return bool(present.value)
        return False
    finally:
        _close(handle)


def describe(pid: int) -> dict[str, object]:
    """给报告用的快照。"""
    return {
        "pid": pid,
        "integrity": integrity_level(pid),
        "admin": is_admin(),
    }
