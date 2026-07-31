"""Windows Job Object backend for the fixed P2-028A synthetic helper."""

from __future__ import annotations

import _winapi
import ctypes
import hashlib
import msvcrt
import os
import subprocess
import sys
import threading
import time
import unicodedata
from collections.abc import Callable
from contextlib import suppress
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from poker_deliberation.budgets.execution import (
    IsolationRequirementV1,
    RM028IsolationEvidenceV1,
)
from poker_deliberation.isolated_jobs.canonical import (
    canonical_child_argv,
    canonical_windows_command_line,
    command_line_sha256,
    isolated_job_sha256,
)
from poker_deliberation.isolated_jobs.identity import (
    sha256_file,
    verify_execution_identity,
)
from poker_deliberation.isolated_jobs.models import (
    IsolatedJobError,
    IsolatedJobPolicyV1,
    IsolatedJobRequestV1,
    JobEvidenceV1,
    JobFailureCode,
    SyntheticOperation,
)
from poker_deliberation.isolated_jobs.paths import (
    verify_filesystem_policy,
    verify_open_file_identity,
)
from poker_deliberation.local_data_policy import contains_restricted_secret_shape

_CREATE_SUSPENDED: Final = 0x00000004
_CREATE_NO_WINDOW: Final = 0x08000000
_STARTF_USESTDHANDLES: Final = 0x00000100
_WAIT_OBJECT_0: Final = 0
_WAIT_TIMEOUT: Final = 258
_INFINITE: Final = 0xFFFFFFFF
_PROCESS_QUERY_LIMITED_INFORMATION: Final = 0x1000
_SYNCHRONIZE: Final = 0x00100000
_ERROR_INVALID_PARAMETER: Final = 87

_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS: Final = 9
_JOB_OBJECT_BASIC_AND_IO_ACCOUNTING_INFORMATION_CLASS: Final = 8
_JOB_OBJECT_LIMIT_PROCESS_TIME: Final = 0x00000002
_JOB_OBJECT_LIMIT_JOB_TIME: Final = 0x00000004
_JOB_OBJECT_LIMIT_ACTIVE_PROCESS: Final = 0x00000008
_JOB_OBJECT_LIMIT_PROCESS_MEMORY: Final = 0x00000100
_JOB_OBJECT_LIMIT_JOB_MEMORY: Final = 0x00000200
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: Final = 0x00002000
_REQUIRED_LIMIT_FLAGS: Final = (
    _JOB_OBJECT_LIMIT_PROCESS_TIME
    | _JOB_OBJECT_LIMIT_JOB_TIME
    | _JOB_OBJECT_LIMIT_ACTIVE_PROCESS
    | _JOB_OBJECT_LIMIT_PROCESS_MEMORY
    | _JOB_OBJECT_LIMIT_JOB_MEMORY
    | _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
)

_TERMINATION_EXIT_CODE: Final = 0xE0280001
_POLL_INTERVAL_SECONDS: Final = 0.01
_POST_TERMINATION_WAIT_MS: Final = 5_000
_PROCESS_CREATION_LOCK: Final = threading.RLock()


class _LARGE_INTEGER(ctypes.Structure):
    _fields_ = [("QuadPart", ctypes.c_longlong)]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", _LARGE_INTEGER),
        ("PerJobUserTimeLimit", _LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", _LARGE_INTEGER),
        ("TotalKernelTime", _LARGE_INTEGER),
        ("ThisPeriodTotalUserTime", _LARGE_INTEGER),
        ("ThisPeriodTotalKernelTime", _LARGE_INTEGER),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


class _JOBOBJECT_BASIC_AND_IO_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicInfo", _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
    ]


class _FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", wintypes.DWORD),
        ("dwHighDateTime", wintypes.DWORD),
    ]


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
_kernel32.CreateJobObjectW.restype = wintypes.HANDLE
_kernel32.SetInformationJobObject.argtypes = (
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wintypes.DWORD,
)
_kernel32.SetInformationJobObject.restype = wintypes.BOOL
_kernel32.QueryInformationJobObject.argtypes = (
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
)
_kernel32.QueryInformationJobObject.restype = wintypes.BOOL
_kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
_kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
_kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
_kernel32.TerminateJobObject.restype = wintypes.BOOL
_kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
_kernel32.TerminateProcess.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.ResumeThread.argtypes = (wintypes.HANDLE,)
_kernel32.ResumeThread.restype = wintypes.DWORD
_kernel32.GetProcessTimes.argtypes = (
    wintypes.HANDLE,
    ctypes.POINTER(_FILETIME),
    ctypes.POINTER(_FILETIME),
    ctypes.POINTER(_FILETIME),
    ctypes.POINTER(_FILETIME),
)
_kernel32.GetProcessTimes.restype = wintypes.BOOL
_kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
_kernel32.WaitForSingleObject.restype = wintypes.DWORD


def _last_error() -> OSError:
    return ctypes.WinError(ctypes.get_last_error())


def _close_handle(handle: int | None) -> None:
    if handle:
        _kernel32.CloseHandle(wintypes.HANDLE(handle))


def _filetime_value(value: _FILETIME) -> int:
    return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)


def _process_creation_time(handle: int) -> int:
    creation, _kernel, _user = _process_times(handle)
    return creation


def _process_times(handle: int) -> tuple[int, int, int]:
    creation = _FILETIME()
    exit_time = _FILETIME()
    kernel = _FILETIME()
    user = _FILETIME()
    if not _kernel32.GetProcessTimes(
        wintypes.HANDLE(handle),
        ctypes.byref(creation),
        ctypes.byref(exit_time),
        ctypes.byref(kernel),
        ctypes.byref(user),
    ):
        raise _last_error()
    return (
        _filetime_value(creation),
        _filetime_value(kernel),
        _filetime_value(user),
    )


def _set_job_limits(job: int, policy: IsolatedJobPolicyV1) -> None:
    limits = policy.limits
    value = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    value.BasicLimitInformation.PerProcessUserTimeLimit.QuadPart = (
        limits.process_cpu_time_ms * 10_000
    )
    value.BasicLimitInformation.PerJobUserTimeLimit.QuadPart = limits.job_cpu_time_ms * 10_000
    value.BasicLimitInformation.LimitFlags = _REQUIRED_LIMIT_FLAGS
    value.BasicLimitInformation.ActiveProcessLimit = limits.maximum_processes
    value.ProcessMemoryLimit = limits.process_memory_bytes
    value.JobMemoryLimit = limits.job_memory_bytes
    if not _kernel32.SetInformationJobObject(
        wintypes.HANDLE(job),
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
        ctypes.byref(value),
        ctypes.sizeof(value),
    ):
        raise _last_error()


def _query_extended(job: int) -> _JOBOBJECT_EXTENDED_LIMIT_INFORMATION:
    value = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    returned = wintypes.DWORD()
    if not _kernel32.QueryInformationJobObject(
        wintypes.HANDLE(job),
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
        ctypes.byref(value),
        ctypes.sizeof(value),
        ctypes.byref(returned),
    ):
        raise _last_error()
    if returned.value not in {0, ctypes.sizeof(value)}:
        raise OSError("unexpected Job Object limit query size")
    return value


def _query_accounting(job: int) -> _JOBOBJECT_BASIC_AND_IO_ACCOUNTING_INFORMATION:
    value = _JOBOBJECT_BASIC_AND_IO_ACCOUNTING_INFORMATION()
    returned = wintypes.DWORD()
    if not _kernel32.QueryInformationJobObject(
        wintypes.HANDLE(job),
        _JOB_OBJECT_BASIC_AND_IO_ACCOUNTING_INFORMATION_CLASS,
        ctypes.byref(value),
        ctypes.sizeof(value),
        ctypes.byref(returned),
    ):
        raise _last_error()
    if returned.value not in {0, ctypes.sizeof(value)}:
        raise OSError("unexpected Job Object accounting query size")
    return value


def _verify_job_limits(job: int, policy: IsolatedJobPolicyV1) -> None:
    observed = _query_extended(job)
    limits = policy.limits
    basic = observed.BasicLimitInformation
    if (
        int(basic.LimitFlags) & _REQUIRED_LIMIT_FLAGS != _REQUIRED_LIMIT_FLAGS
        or int(basic.PerProcessUserTimeLimit.QuadPart) != limits.process_cpu_time_ms * 10_000
        or int(basic.PerJobUserTimeLimit.QuadPart) != limits.job_cpu_time_ms * 10_000
        or int(basic.ActiveProcessLimit) != limits.maximum_processes
        or int(observed.ProcessMemoryLimit) != limits.process_memory_bytes
        or int(observed.JobMemoryLimit) != limits.job_memory_bytes
    ):
        raise OSError("Job Object limits failed exact requery")


class _OutputBudget:
    def __init__(self, *, stdout_limit: int, stderr_limit: int, combined_limit: int) -> None:
        self.stream_limits = {"stdout": stdout_limit, "stderr": stderr_limit}
        self.combined_limit = combined_limit
        self.buffers = {"stdout": bytearray(), "stderr": bytearray()}
        self.seen = {"stdout": 0, "stderr": 0}
        self.overflow: JobFailureCode | None = None
        self.reader_error = False
        self.lock = threading.Lock()

    def consume(self, stream: Literal["stdout", "stderr"], chunk: bytes) -> None:
        with self.lock:
            self.seen[stream] += len(chunk)
            existing_stream = len(self.buffers[stream])
            combined = len(self.buffers["stdout"]) + len(self.buffers["stderr"])
            stream_remaining = max(0, self.stream_limits[stream] - existing_stream)
            combined_remaining = max(0, self.combined_limit - combined)
            keep = min(len(chunk), stream_remaining, combined_remaining)
            if keep:
                self.buffers[stream].extend(chunk[:keep])
            stream_exceeded = len(chunk) > stream_remaining
            combined_exceeded = len(chunk) > combined_remaining
            if self.overflow is None and (stream_exceeded or combined_exceeded):
                if combined_exceeded and (
                    not stream_exceeded or combined_remaining <= stream_remaining
                ):
                    self.overflow = JobFailureCode.COMBINED_OUTPUT_LIMIT
                else:
                    self.overflow = (
                        JobFailureCode.STDOUT_LIMIT
                        if stream == "stdout"
                        else JobFailureCode.STDERR_LIMIT
                    )

    def record_reader_error(self) -> None:
        with self.lock:
            self.reader_error = True

    def snapshot(self) -> tuple[bytes, bytes, JobFailureCode | None, bool]:
        with self.lock:
            return (
                bytes(self.buffers["stdout"]),
                bytes(self.buffers["stderr"]),
                self.overflow,
                self.reader_error,
            )


def _reader(
    file_descriptor: int,
    stream: Literal["stdout", "stderr"],
    budget: _OutputBudget,
    done: threading.Event,
) -> None:
    try:
        while True:
            try:
                chunk = os.read(file_descriptor, 65_536)
            except OSError:
                budget.record_reader_error()
                break
            if not chunk:
                break
            budget.consume(stream, chunk)
    finally:
        with suppress(OSError):
            os.close(file_descriptor)
        done.set()


@dataclass(frozen=True, slots=True)
class WindowsJobOutcome:
    evidence: JobEvidenceV1
    stdout: bytes
    stderr: bytes
    failure_code: JobFailureCode | None
    cancelled: bool


class PreparedWindowsJob:
    """One suspended process already assigned to its configured Job Object."""

    def __init__(
        self,
        *,
        request: IsolatedJobRequestV1,
        policy: IsolatedJobPolicyV1,
        job_handle: int,
        process_handle: Any,
        thread_handle: Any,
        process_id: int,
        creation_time_100ns: int,
        command_line: str,
        stdout_read_fd: int,
        stderr_read_fd: int,
        inherited_handle_count: int,
    ) -> None:
        self.request = request
        self.policy = policy
        self.job_handle = job_handle
        self.process_handle = process_handle
        self.thread_handle = thread_handle
        self.process_id = process_id
        self.creation_time_100ns = creation_time_100ns
        self.command_line = command_line
        self.stdout_read_fd = stdout_read_fd
        self.stderr_read_fd = stderr_read_fd
        self.inherited_handle_count = inherited_handle_count
        self._resumed = False
        self._closed = False
        self._identity_rechecked = False
        self._started_ns: int | None = None
        limits = policy.limits
        self._output = _OutputBudget(
            stdout_limit=limits.stdout_bytes,
            stderr_limit=limits.stderr_bytes,
            combined_limit=limits.combined_output_bytes,
        )
        self._stdout_done = threading.Event()
        self._stderr_done = threading.Event()
        self._threads: tuple[threading.Thread, threading.Thread] | None = None

    def _start_readers(self) -> None:
        stdout_thread = threading.Thread(
            target=_reader,
            args=(
                self.stdout_read_fd,
                "stdout",
                self._output,
                self._stdout_done,
            ),
            name=f"isolated-job-{self.process_id}-stdout",
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_reader,
            args=(
                self.stderr_read_fd,
                "stderr",
                self._output,
                self._stderr_done,
            ),
            name=f"isolated-job-{self.process_id}-stderr",
            daemon=True,
        )
        self._threads = (stdout_thread, stderr_thread)
        stdout_thread.start()
        stderr_thread.start()

    def resume(self) -> None:
        if self._resumed or self._closed:
            raise RuntimeError("isolated job cannot be resumed twice")
        try:
            verify_execution_identity(self.policy.execution_identity)
            self._identity_rechecked = True
            self._start_readers()
            self._started_ns = time.monotonic_ns()
            result = _kernel32.ResumeThread(wintypes.HANDLE(int(self.thread_handle)))
            if result == 0xFFFFFFFF:
                raise _last_error()
            _winapi.CloseHandle(self.thread_handle)
            self.thread_handle = None
            self._resumed = True
        except Exception:
            self.terminate_before_resume()
            raise

    def _terminate_job(self) -> bool:
        try:
            if not _kernel32.TerminateJobObject(
                wintypes.HANDLE(self.job_handle),
                _TERMINATION_EXIT_CODE,
            ):
                return False
            _kernel32.WaitForSingleObject(
                wintypes.HANDLE(int(self.process_handle)),
                _POST_TERMINATION_WAIT_MS,
            )
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if int(_query_accounting(self.job_handle).BasicInfo.ActiveProcesses) == 0:
                    return True
                time.sleep(_POLL_INTERVAL_SECONDS)
        except Exception:
            return False
        return False

    def terminate_before_resume(self) -> None:
        if self._closed:
            return
        if self._resumed:
            self._terminate_job()
        else:
            _kernel32.TerminateProcess(
                wintypes.HANDLE(int(self.process_handle)),
                _TERMINATION_EXIT_CODE,
            )
        self.close()

    def _infer_exit_failure(
        self,
        exit_code: int,
        accounting: _JOBOBJECT_BASIC_AND_IO_ACCOUNTING_INFORMATION,
        extended: _JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        process_user_time_100ns: int,
    ) -> JobFailureCode | None:
        if exit_code == 0:
            return None
        operation = self.request.operation
        arguments = self.request.arguments
        limits = self.policy.limits
        if (
            operation is SyntheticOperation.MEMORY_PRESSURE
            and arguments.memory_bytes is not None
            and arguments.memory_bytes >= limits.process_memory_bytes
        ):
            return JobFailureCode.MEMORY_LIMIT
        if operation is SyntheticOperation.CPU_SPIN and (
            process_user_time_100ns >= limits.process_cpu_time_ms * 9_000
            or int(accounting.BasicInfo.TotalUserTime.QuadPart) >= limits.job_cpu_time_ms * 9_000
        ):
            return JobFailureCode.CPU_LIMIT
        if (
            operation is SyntheticOperation.SPAWN_TREE
            and arguments.child_count is not None
            and arguments.child_count + 1 > limits.maximum_processes
        ):
            return JobFailureCode.PROCESS_LIMIT
        if int(extended.PeakJobMemoryUsed) >= limits.job_memory_bytes:
            return JobFailureCode.MEMORY_LIMIT
        return JobFailureCode.CHILD_EXIT_NONZERO

    def wait(
        self,
        *,
        cancelled: Callable[[], bool] | None = None,
        on_cancel_requested: Callable[[], None] | None = None,
    ) -> WindowsJobOutcome:
        if not self._resumed or self._started_ns is None or self._closed:
            raise RuntimeError("isolated job is not running")
        try:
            return self._wait_impl(
                cancelled=cancelled,
                on_cancel_requested=on_cancel_requested,
            )
        except Exception:
            return self._effect_unknown_outcome()

    def _wait_impl(
        self,
        *,
        cancelled: Callable[[], bool] | None,
        on_cancel_requested: Callable[[], None] | None,
    ) -> WindowsJobOutcome:
        assert self._started_ns is not None
        stop_reason: JobFailureCode | None = None
        cancelled_value = False
        wall_deadline_ns = self._started_ns + self.policy.limits.wall_clock_ms * 1_000_000
        finished_ns: int | None = None
        while True:
            now_ns = time.monotonic_ns()
            if now_ns >= wall_deadline_ns:
                stop_reason = JobFailureCode.WALL_CLOCK_LIMIT
                self._terminate_job()
                finished_ns = now_ns
                break
            wait_result = _kernel32.WaitForSingleObject(
                wintypes.HANDLE(int(self.process_handle)),
                0,
            )
            if wait_result == _WAIT_OBJECT_0:
                active = int(_query_accounting(self.job_handle).BasicInfo.ActiveProcesses)
                if active == 0:
                    finished_ns = time.monotonic_ns()
                    if finished_ns >= wall_deadline_ns:
                        stop_reason = JobFailureCode.WALL_CLOCK_LIMIT
                    break
                child_count = self.request.arguments.child_count
                if (
                    self.request.operation is SyntheticOperation.SPAWN_TREE
                    and child_count is not None
                    and child_count + 1 > self.policy.limits.maximum_processes
                ):
                    stop_reason = JobFailureCode.PROCESS_LIMIT
                    self._terminate_job()
                    finished_ns = time.monotonic_ns()
                    break
            elif wait_result != _WAIT_TIMEOUT:
                stop_reason = JobFailureCode.INTERNAL_INVARIANT_ERROR
                self._terminate_job()
                finished_ns = time.monotonic_ns()
                break
            _stdout, _stderr, overflow, reader_error = self._output.snapshot()
            if reader_error:
                stop_reason = JobFailureCode.EFFECT_UNKNOWN
                self._terminate_job()
                finished_ns = time.monotonic_ns()
                break
            if overflow is not None:
                stop_reason = overflow
                self._terminate_job()
                finished_ns = time.monotonic_ns()
                break
            if cancelled is not None and cancelled():
                try:
                    if on_cancel_requested is not None:
                        on_cancel_requested()
                    stop_reason = JobFailureCode.CANCELLED
                    cancelled_value = True
                except Exception:
                    stop_reason = JobFailureCode.EFFECT_UNKNOWN
                self._terminate_job()
                finished_ns = time.monotonic_ns()
                break
            accounting_now = _query_accounting(self.job_handle)
            _creation, _process_kernel_now, process_user_now = _process_times(
                int(self.process_handle)
            )
            if (
                int(accounting_now.BasicInfo.TotalUserTime.QuadPart)
                >= self.policy.limits.job_cpu_time_ms * 10_000
                or process_user_now >= self.policy.limits.process_cpu_time_ms * 10_000
            ):
                stop_reason = JobFailureCode.CPU_LIMIT
                self._terminate_job()
                finished_ns = time.monotonic_ns()
                break
            time.sleep(_POLL_INTERVAL_SECONDS)

        if finished_ns is None:
            finished_ns = time.monotonic_ns()
        _kernel32.WaitForSingleObject(
            wintypes.HANDLE(int(self.process_handle)),
            _POST_TERMINATION_WAIT_MS,
        )
        if self._threads is not None:
            for thread in self._threads:
                thread.join(timeout=5)
        stdout, stderr, overflow, reader_error = self._output.snapshot()
        if overflow is not None:
            stop_reason = overflow
        if reader_error:
            stop_reason = JobFailureCode.EFFECT_UNKNOWN
        exit_code = int(_winapi.GetExitCodeProcess(self.process_handle))
        accounting = _query_accounting(self.job_handle)
        extended = _query_extended(self.job_handle)
        _creation, process_kernel_time, process_user_time = _process_times(int(self.process_handle))
        active_processes = int(accounting.BasicInfo.ActiveProcesses)
        if stop_reason is None:
            stop_reason = self._infer_exit_failure(
                exit_code,
                accounting,
                extended,
                process_user_time,
            )
        wall_clock_ms = max(0, (finished_ns - self._started_ns) // 1_000_000)
        output_complete = (
            overflow is None
            and not reader_error
            and self._stdout_done.is_set()
            and self._stderr_done.is_set()
            and active_processes == 0
        )
        evidence = JobEvidenceV1(
            process_id=self.process_id,
            process_creation_time_100ns=self.creation_time_100ns,
            exit_code=exit_code,
            termination_reason=None if stop_reason is None else stop_reason.value,
            wall_clock_ms=wall_clock_ms,
            job_user_time_100ns=int(accounting.BasicInfo.TotalUserTime.QuadPart),
            job_kernel_time_100ns=int(accounting.BasicInfo.TotalKernelTime.QuadPart),
            process_user_time_100ns=process_user_time,
            process_kernel_time_100ns=process_kernel_time,
            peak_process_memory_bytes=int(extended.PeakProcessMemoryUsed),
            peak_job_memory_bytes=int(extended.PeakJobMemoryUsed),
            total_processes=int(accounting.BasicInfo.TotalProcesses),
            active_processes=active_processes,
            stdout_bytes=len(stdout),
            stderr_bytes=len(stderr),
            stdout_sha256=hashlib.sha256(stdout).hexdigest(),
            stderr_sha256=hashlib.sha256(stderr).hexdigest(),
            command_line_sha256=command_line_sha256(self.command_line),
            inherited_handle_count=self.inherited_handle_count,
            process_tree_termination_confirmed=active_processes == 0,
            job_limits_requeried=True,
            executable_identity_rechecked=self._identity_rechecked,
            output_complete=output_complete,
        )
        self.close()
        return WindowsJobOutcome(
            evidence=evidence,
            stdout=stdout,
            stderr=stderr,
            failure_code=stop_reason,
            cancelled=cancelled_value,
        )

    def _effect_unknown_outcome(self) -> WindowsJobOutcome:
        assert self._started_ns is not None
        terminated = self._terminate_job()
        if self._threads is not None:
            for thread in self._threads:
                thread.join(timeout=5)
        stdout, stderr, _overflow, _reader_error = self._output.snapshot()
        accounting = _JOBOBJECT_BASIC_AND_IO_ACCOUNTING_INFORMATION()
        extended = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        with suppress(Exception):
            accounting = _query_accounting(self.job_handle)
        with suppress(Exception):
            extended = _query_extended(self.job_handle)
        process_kernel_time = 0
        process_user_time = 0
        with suppress(Exception):
            _creation, process_kernel_time, process_user_time = _process_times(
                int(self.process_handle)
            )
        exit_code = _TERMINATION_EXIT_CODE
        with suppress(Exception):
            exit_code = int(_winapi.GetExitCodeProcess(self.process_handle))
        observed_active = int(accounting.BasicInfo.ActiveProcesses)
        active_processes = 0 if terminated else max(1, observed_active)
        total_processes = max(
            1,
            active_processes,
            int(accounting.BasicInfo.TotalProcesses),
        )
        evidence = JobEvidenceV1(
            process_id=self.process_id,
            process_creation_time_100ns=self.creation_time_100ns,
            exit_code=exit_code,
            termination_reason=JobFailureCode.EFFECT_UNKNOWN.value,
            wall_clock_ms=max(
                0,
                (time.monotonic_ns() - self._started_ns) // 1_000_000,
            ),
            job_user_time_100ns=int(accounting.BasicInfo.TotalUserTime.QuadPart),
            job_kernel_time_100ns=int(accounting.BasicInfo.TotalKernelTime.QuadPart),
            process_user_time_100ns=process_user_time,
            process_kernel_time_100ns=process_kernel_time,
            peak_process_memory_bytes=int(extended.PeakProcessMemoryUsed),
            peak_job_memory_bytes=int(extended.PeakJobMemoryUsed),
            total_processes=total_processes,
            active_processes=active_processes,
            stdout_bytes=len(stdout),
            stderr_bytes=len(stderr),
            stdout_sha256=hashlib.sha256(stdout).hexdigest(),
            stderr_sha256=hashlib.sha256(stderr).hexdigest(),
            command_line_sha256=command_line_sha256(self.command_line),
            inherited_handle_count=self.inherited_handle_count,
            process_tree_termination_confirmed=active_processes == 0,
            job_limits_requeried=True,
            executable_identity_rechecked=self._identity_rechecked,
            output_complete=False,
        )
        self.close()
        return WindowsJobOutcome(
            evidence=evidence,
            stdout=stdout,
            stderr=stderr,
            failure_code=JobFailureCode.EFFECT_UNKNOWN,
            cancelled=False,
        )

    def close(self) -> None:
        if self._closed:
            return
        if self.thread_handle is not None:
            _winapi.CloseHandle(self.thread_handle)
            self.thread_handle = None
        if self._threads is None:
            for file_descriptor in (self.stdout_read_fd, self.stderr_read_fd):
                with suppress(OSError):
                    os.close(file_descriptor)
        if self.process_handle is not None:
            _winapi.CloseHandle(self.process_handle)
            self.process_handle = None
        _close_handle(self.job_handle)
        self.job_handle = 0
        self._closed = True

    def __enter__(self) -> PreparedWindowsJob:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if exc is not None:
            self.terminate_before_resume()
        else:
            self.close()


class WindowsJobBackend:
    """Closed Windows Job Object launcher for the repository helper.

    Repository-owned launches share one creation lock while inheritable handles
    exist. Uncoordinated external process creation remains outside this boundary.
    """

    boundary_id = "windows-job-object-repository-synthetic-v1"

    def _open_input(
        self,
        request: IsolatedJobRequestV1,
        policy: IsolatedJobPolicyV1,
    ) -> int | None:
        verify_filesystem_policy(policy.filesystem)
        approved = policy.filesystem.approved_input
        if request.operation is SyntheticOperation.COPY_HANDLES:
            if approved is None:
                raise ValueError("copy_handles requires approved input identity")
            file_descriptor = os.open(approved.absolute_path, os.O_RDONLY | os.O_BINARY)
            try:
                verify_open_file_identity(file_descriptor, approved)
                data = os.read(file_descriptor, approved.size_bytes + 1)
                os.lseek(file_descriptor, 0, os.SEEK_SET)
                if (
                    len(data) != approved.size_bytes
                    or hashlib.sha256(data).hexdigest() != approved.sha256
                ):
                    raise IsolatedJobError(JobFailureCode.IDENTITY_MISMATCH)
                try:
                    text = data.decode("utf-8", errors="strict")
                except UnicodeDecodeError as exc:
                    raise IsolatedJobError(JobFailureCode.INVALID_REQUEST) from exc
                if (
                    data.startswith(b"\xef\xbb\xbf")
                    or b"\r" in data
                    or unicodedata.normalize("NFC", text) != text
                ):
                    raise IsolatedJobError(JobFailureCode.INVALID_REQUEST)
                if contains_restricted_secret_shape(text):
                    raise IsolatedJobError(JobFailureCode.SECRET_REJECTED)
                os.set_inheritable(file_descriptor, True)
                return file_descriptor
            except Exception:
                os.close(file_descriptor)
                raise
        if approved is not None:
            raise ValueError("only copy_handles may bind an approved input")
        return None

    def prepare(
        self,
        request: IsolatedJobRequestV1,
        policy: IsolatedJobPolicyV1,
    ) -> PreparedWindowsJob:
        if sys.platform != "win32":
            raise OSError("Windows Job Object backend is unavailable")
        with _PROCESS_CREATION_LOCK:
            return self._prepare_locked(request, policy)

    def _prepare_locked(
        self,
        request: IsolatedJobRequestV1,
        policy: IsolatedJobPolicyV1,
    ) -> PreparedWindowsJob:
        verify_execution_identity(policy.execution_identity)
        verify_filesystem_policy(policy.filesystem)
        job = int(_kernel32.CreateJobObjectW(None, None) or 0)
        if not job:
            raise _last_error()
        stdin_fd: int | None = None
        stdout_read: int | None = None
        stdout_write: int | None = None
        stderr_read: int | None = None
        stderr_write: int | None = None
        input_fd: int | None = None
        process_handle: Any = None
        thread_handle: Any = None
        try:
            _set_job_limits(job, policy)
            _verify_job_limits(job, policy)
            stdin_fd = os.open(os.devnull, os.O_RDONLY | os.O_BINARY)
            stdout_read, stdout_write = os.pipe()
            stderr_read, stderr_write = os.pipe()
            input_fd = self._open_input(request, policy)
            for inherited_descriptor in (stdin_fd, stdout_write, stderr_write):
                os.set_inheritable(inherited_descriptor, True)
            stdin_handle = int(msvcrt.get_osfhandle(stdin_fd))
            stdout_handle = int(msvcrt.get_osfhandle(stdout_write))
            stderr_handle = int(msvcrt.get_osfhandle(stderr_write))
            input_handle = None if input_fd is None else int(msvcrt.get_osfhandle(input_fd))
            handles = [stdin_handle, stdout_handle, stderr_handle]
            if input_handle is not None:
                handles.append(input_handle)
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= _STARTF_USESTDHANDLES
            startup.hStdInput = stdin_handle
            startup.hStdOutput = stdout_handle
            startup.hStdError = stderr_handle
            startup.lpAttributeList = {"handle_list": handles}
            argv = canonical_child_argv(
                request,
                policy,
                input_handle=input_handle,
            )
            command_line = canonical_windows_command_line(argv)
            process_handle, thread_handle, process_id, _thread_id = _winapi.CreateProcess(
                policy.execution_identity.interpreter.absolute_path,
                command_line,
                None,
                None,
                True,
                _CREATE_SUSPENDED | _CREATE_NO_WINDOW,
                {},
                policy.filesystem.workspace_root.absolute_path,
                startup,
            )
            for child_descriptor in (stdin_fd, stdout_write, stderr_write, input_fd):
                if child_descriptor is not None:
                    os.close(child_descriptor)
            stdin_fd = None
            stdout_write = None
            stderr_write = None
            input_fd = None
            if not _kernel32.AssignProcessToJobObject(
                wintypes.HANDLE(job),
                wintypes.HANDLE(int(process_handle)),
            ):
                raise _last_error()
            _verify_job_limits(job, policy)
            if int(_query_accounting(job).BasicInfo.ActiveProcesses) != 1:
                raise OSError("suspended child was not the sole active job process")
            creation_time = _process_creation_time(int(process_handle))
            verify_execution_identity(policy.execution_identity)
            assert stdout_read is not None
            assert stderr_read is not None
            prepared = PreparedWindowsJob(
                request=request,
                policy=policy,
                job_handle=job,
                process_handle=process_handle,
                thread_handle=thread_handle,
                process_id=int(process_id),
                creation_time_100ns=creation_time,
                command_line=command_line,
                stdout_read_fd=stdout_read,
                stderr_read_fd=stderr_read,
                inherited_handle_count=len(handles),
            )
            job = 0
            process_handle = None
            thread_handle = None
            stdout_read = None
            stderr_read = None
            return prepared
        except Exception:
            if process_handle is not None:
                _kernel32.TerminateProcess(
                    wintypes.HANDLE(int(process_handle)),
                    _TERMINATION_EXIT_CODE,
                )
            if thread_handle is not None:
                _winapi.CloseHandle(thread_handle)
            if process_handle is not None:
                _winapi.CloseHandle(process_handle)
            raise
        finally:
            for cleanup_descriptor in (
                stdin_fd,
                stdout_read,
                stdout_write,
                stderr_read,
                stderr_write,
                input_fd,
            ):
                if cleanup_descriptor is not None:
                    with suppress(OSError):
                        os.close(cleanup_descriptor)
            _close_handle(job)

    def inspect(self, requirement: IsolationRequirementV1) -> RM028IsolationEvidenceV1:
        """Expose only the guarantees this backend can actually establish."""

        evidence_payload = {
            "boundary_id": self.boundary_id,
            "requirement_sha256": requirement.request_sha256,
            "process_tree_termination": True,
            "os_resource_isolation": True,
            "remote_cancellation": False,
            "arbitrary_external_code_isolation": False,
        }
        return RM028IsolationEvidenceV1(
            requirement_sha256=requirement.request_sha256,
            boundary_id=self.boundary_id,
            isolation_evidence_sha256=isolated_job_sha256(evidence_payload),
            process_tree_termination_confirmed=True,
            remote_cancellation_confirmed=False,
            os_resource_isolation_confirmed=True,
            external_code_isolation_confirmed=False,
        )

    @staticmethod
    def process_identity_status(
        process_id: int,
        creation_time_100ns: int,
    ) -> Literal[
        "absent",
        "same_live_process",
        "different_live_process",
        "unverifiable",
    ]:
        handle = int(
            _kernel32.OpenProcess(
                _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE,
                False,
                process_id,
            )
            or 0
        )
        if not handle:
            if ctypes.get_last_error() == _ERROR_INVALID_PARAMETER:
                return "absent"
            return "unverifiable"
        try:
            observed_creation = _process_creation_time(handle)
            if observed_creation != creation_time_100ns:
                return "different_live_process"
            wait_result = int(_kernel32.WaitForSingleObject(wintypes.HANDLE(handle), 0))
            if wait_result == _WAIT_TIMEOUT:
                return "same_live_process"
            if wait_result == _WAIT_OBJECT_0:
                return "absent"
            return "unverifiable"
        finally:
            _close_handle(handle)


def backend_source_sha256() -> str:
    """Return exact identity for the repository backend source."""

    return sha256_file(Path(__file__))
