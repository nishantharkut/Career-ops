from __future__ import annotations

from dataclasses import dataclass
import math
import os
import signal
import subprocess
import sys
import threading
import time
from typing import BinaryIO, Mapping, Sequence


_CREATE_SUSPENDED = 0x00000004


@dataclass(frozen=True)
class ProcessOutcome:
    returncode: int
    stdout: bytes
    stderr: bytes


class ProcessDeadlineExceeded(RuntimeError):
    pass


class ProcessStartError(RuntimeError):
    pass


class _WindowsJob:
    def __init__(self, process: subprocess.Popen[bytes]):
        self._handle = None
        self._process = process
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes

            class BasicLimitInformation(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class IoCounters(ctypes.Structure):
                _fields_ = [
                    ("ReadOperationCount", ctypes.c_uint64),
                    ("WriteOperationCount", ctypes.c_uint64),
                    ("OtherOperationCount", ctypes.c_uint64),
                    ("ReadTransferCount", ctypes.c_uint64),
                    ("WriteTransferCount", ctypes.c_uint64),
                    ("OtherTransferCount", ctypes.c_uint64),
                ]

            class ExtendedLimitInformation(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", BasicLimitInformation),
                    ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.SetInformationJobObject.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                wintypes.LPVOID,
                wintypes.DWORD,
            ]
            kernel32.SetInformationJobObject.restype = wintypes.BOOL
            kernel32.AssignProcessToJobObject.argtypes = [
                wintypes.HANDLE,
                wintypes.HANDLE,
            ]
            kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
            ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
            ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
            ntdll.NtResumeProcess.restype = ctypes.c_long
            self._kernel32 = kernel32
            self._ntdll = ntdll
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
            information = ExtendedLimitInformation()
            information.BasicLimitInformation.LimitFlags = 0x00002000
            if not kernel32.SetInformationJobObject(
                handle, 9, ctypes.byref(information), ctypes.sizeof(information)
            ):
                kernel32.CloseHandle(handle)
                raise OSError(
                    ctypes.get_last_error(), "SetInformationJobObject failed"
                )
            if not kernel32.AssignProcessToJobObject(handle, process._handle):
                kernel32.CloseHandle(handle)
                raise OSError(
                    ctypes.get_last_error(), "AssignProcessToJobObject failed"
                )
            self._handle = handle
        except Exception as error:
            process.kill()
            process.wait()
            raise ProcessStartError("Could not isolate subprocess tree") from error

    def resume(self) -> None:
        if self._handle is None:
            return
        status = int(self._ntdll.NtResumeProcess(self._process._handle))
        if status == 0:
            return
        self.terminate()
        self._process.kill()
        self._process.wait()
        raise ProcessStartError("Could not resume isolated subprocess")

    def terminate(self) -> None:
        if self._handle is not None:
            self._kernel32.TerminateJobObject(self._handle, 1)

    def close(self) -> None:
        if self._handle is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


def _drain(stream: BinaryIO, limit: int, destination: bytearray) -> None:
    try:
        while True:
            chunk = stream.read(16 * 1024)
            if not chunk:
                return
            if len(destination) < limit:
                destination.extend(chunk[: limit - len(destination)])
    finally:
        stream.close()


def _write_input(stream: BinaryIO, value: bytes) -> None:
    try:
        if value:
            stream.write(value)
            stream.flush()
    except (BrokenPipeError, OSError):
        pass
    finally:
        stream.close()


def _kill_posix_group(process: subprocess.Popen[bytes]) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def run_process(
    command: Sequence[str],
    *,
    input_bytes: bytes,
    timeout_seconds: float,
    environment: Mapping[str, str],
    capture_limit: int,
) -> ProcessOutcome:
    if (
        not command
        or not all(isinstance(item, str) and item for item in command)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
        or capture_limit < 0
    ):
        raise ValueError("Invalid subprocess boundary configuration")
    started = time.monotonic()
    platform_values: dict[str, object] = {}
    if sys.platform == "win32":
        platform_values["creationflags"] = (
            subprocess.CREATE_NO_WINDOW | _CREATE_SUSPENDED
        )
    else:
        platform_values["start_new_session"] = True
    capture_output = capture_limit > 0
    try:
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE if capture_output else subprocess.DEVNULL,
            stderr=subprocess.PIPE if capture_output else subprocess.DEVNULL,
            env=dict(environment),
            shell=False,
            bufsize=0,
            **platform_values,
        )
    except (OSError, ValueError) as error:
        raise ProcessStartError("Could not start subprocess") from error

    job: _WindowsJob | None = None
    stdout = bytearray()
    stderr = bytearray()
    threads: list[threading.Thread] = []
    try:
        job = _WindowsJob(process)
        job.resume()
        if process.stdin is None:
            raise ProcessStartError("Subprocess stdin was unavailable")
        writer = threading.Thread(
            target=_write_input,
            args=(process.stdin, input_bytes),
            daemon=True,
        )
        threads.append(writer)
        writer.start()
        if capture_output:
            assert process.stdout is not None
            assert process.stderr is not None
            for stream, destination in (
                (process.stdout, stdout),
                (process.stderr, stderr),
            ):
                reader = threading.Thread(
                    target=_drain,
                    args=(stream, capture_limit, destination),
                    daemon=True,
                )
                threads.append(reader)
                reader.start()
        remaining = timeout_seconds - (time.monotonic() - started)
        if remaining <= 0:
            raise subprocess.TimeoutExpired(command, timeout_seconds)
        process.wait(timeout=remaining)
    except subprocess.TimeoutExpired as error:
        if job is not None:
            job.terminate()
        _kill_posix_group(process)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise ProcessDeadlineExceeded() from error
    finally:
        if process.poll() is not None:
            _kill_posix_group(process)
        if job is not None:
            job.close()
        for thread in threads:
            thread.join(timeout=1)
    return ProcessOutcome(process.returncode, bytes(stdout), bytes(stderr))


class ProcessRunner:
    def run(self, command: list[str], **values: object) -> ProcessOutcome:
        return run_process(command, **values)
