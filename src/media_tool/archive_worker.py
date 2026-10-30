"""Private Windows console transport for 7-Zip password prompts.

The parent supplies the request through a pipe. Neither executable arguments
nor environment variables carry the password. Binary stdout remains a pipe.
"""
import ctypes
from ctypes import wintypes as W
import json
import os
import subprocess
import sys
import threading
import time


class Key(ctypes.Structure):
    _fields_ = [('down', W.BOOL), ('repeat', W.WORD), ('virtual', W.WORD),
                ('scan', W.WORD), ('char', W.WCHAR), ('control', W.DWORD)]


class EventData(ctypes.Union):
    _fields_ = [('key', Key), ('padding', ctypes.c_byte * 16)]


class InputRecord(ctypes.Structure):
    _fields_ = [('kind', W.WORD), ('data', EventData)]


def main():
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetStdHandle.argtypes = [W.DWORD]
    kernel.GetStdHandle.restype = W.HANDLE
    kernel.GetCurrentProcess.restype = W.HANDLE
    kernel.DuplicateHandle.argtypes = [W.HANDLE, W.HANDLE, W.HANDLE, ctypes.POINTER(W.HANDLE), W.DWORD, W.BOOL, W.DWORD]
    duplicate = W.HANDLE()
    current = kernel.GetCurrentProcess()
    if not kernel.DuplicateHandle(current, kernel.GetStdHandle(-10 & 0xffffffff), current, ctypes.byref(duplicate), 0, False, 2):
        return 1
    # Windowed frozen executables have sys.stdin=None; use the inherited pipe.
    control = os.fdopen(msvcrt.open_osfhandle(duplicate.value, os.O_RDONLY), 'rb')
    request = json.loads(control.readline(65536))
    command, password = request['argv'], request['password']
    if not command or not all(isinstance(a, str) for a in command):
        return 1
    if not isinstance(password, str) or not password or any(ord(c) < 32 or ord(c) == 127 for c in password) or len(password) > 1024:
        return 1
    kernel.GetConsoleWindow.restype = W.HWND
    if not kernel.GetConsoleWindow():
        if not kernel.AllocConsole():
            return 1
    # A reversible single-byte console mapping avoids Windows console handling
    # of surrogate pairs. 7-Zip decodes the resulting bytes explicitly as UTF-8.
    if not kernel.SetConsoleCP(437):
        return 1
    kernel.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, ctypes.c_void_p, W.DWORD, W.DWORD, W.HANDLE]
    kernel.CreateFileW.restype = W.HANDLE
    kernel.WriteConsoleInputW.argtypes = [W.HANDLE, ctypes.POINTER(InputRecord), W.DWORD, ctypes.POINTER(W.DWORD)]
    kernel.CloseHandle.argtypes = [W.HANDLE]
    handle = kernel.CreateFileW('CONIN$', 0xC0000000, 3, None, 3, 0, None)
    if handle == ctypes.c_void_p(-1).value:
        return 1
    kernel.GetConsoleMode.argtypes = [W.HANDLE, ctypes.POINTER(W.DWORD)]
    kernel.SetConsoleMode.argtypes = [W.HANDLE, W.DWORD]
    mode = W.DWORD()
    if not kernel.GetConsoleMode(handle, ctypes.byref(mode)) or not kernel.SetConsoleMode(handle, mode.value | 4):
        kernel.CloseHandle(handle)
        return 1
    disconnected = threading.Event()
    def watch():
        control.read(1)
        disconnected.set()
    threading.Thread(target=watch, daemon=True).start()
    # Duplicate the console handle into a Python file; the child inherits only
    # the explicit standard handles, with the parent's private console input.
    stream = os.fdopen(msvcrt.open_osfhandle(handle, os.O_RDONLY), 'rb', buffering=0)
    process = None
    try:
        command = [command[0], '-sccUTF-8', *(arg for arg in command[1:] if not arg.lower().startswith('-scc'))]
        process = subprocess.Popen(command, stdin=stream, cwd=request.get('cwd'), close_fds=True)
        deadline = time.monotonic() + 15
        # 7-Zip clears queued input when entering its password prompt. Wait
        # until it disables echo before sending any secret characters.
        while process.poll() is None:
            if disconnected.wait(.01) or time.monotonic() > deadline:
                return 1
            if not kernel.GetConsoleMode(handle, ctypes.byref(mode)):
                return 1
            if not mode.value & 4:
                break
        if process.poll() is not None:
            return process.returncode
        units = (password.encode('utf-8').decode('cp437') + '\r').encode('utf-16-le')
        records = (InputRecord * (len(units) // 2))()
        for index in range(len(records)):
            records[index].kind = 1
            records[index].data.key = Key(True, 1, 0, 0, chr(int.from_bytes(units[index*2:index*2+2], 'little')), 0)
        records[-1].data.key.virtual = 13
        records[-1].data.key.scan = 28
        written = W.DWORD()
        if not kernel.WriteConsoleInputW(handle, records, len(records), ctypes.byref(written)) or written.value != len(records):
            return 1
        deadline = time.monotonic() + 15
        while process.poll() is None:
            if disconnected.wait(.01) or time.monotonic() > deadline:
                return 1
            if not kernel.GetConsoleMode(handle, ctypes.byref(mode)):
                return 1
            if mode.value & 4:
                break
        while process.poll() is None:
            if disconnected.wait(.05):
                return 1
        return process.returncode
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        stream.close()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        # A traceback could contain a request or tool output; fail closed.
        raise SystemExit(1)
