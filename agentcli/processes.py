"""Cleanup for processes owned by this library, never PID-tree scans."""
import logging
import os


def attach_process_job(proc):
    """Best-effort Windows job; retain the original process handle, not a PID.

    Assignment happens immediately after spawn, not atomically with spawn. Hosts
    requiring strict containment must launch the application in their own job.
    """
    if os.name != "nt":
        return
    native = proc
    transport = getattr(proc, "_transport", None)
    if transport is not None:
        native = transport.get_extra_info("subprocess")
    handle = getattr(native, "_handle", None)
    if not isinstance(handle, int):
        return  # Test doubles and unsupported transports have no native handle.
    import ctypes
    from ctypes import wintypes as w

    class Basic(ctypes.Structure):
        _fields_ = [("user", ctypes.c_longlong), ("job", ctypes.c_longlong),
                    ("flags", w.DWORD), ("min", ctypes.c_size_t),
                    ("max", ctypes.c_size_t), ("active", w.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority", w.DWORD),
                    ("scheduling", w.DWORD)]

    class Extended(ctypes.Structure):
        _fields_ = [("basic", Basic), ("io", ctypes.c_ulonglong * 6),
                    ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                    ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
    kernel.CreateJobObjectW.restype = w.HANDLE
    kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
    kernel.SetInformationJobObject.restype = w.BOOL
    kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    kernel.AssignProcessToJobObject.restype = w.BOOL
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.CloseHandle.restype = w.BOOL
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        logging.getLogger(__name__).warning("Windows process job unavailable")
        return
    info = Extended()
    info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if (not kernel.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
            or not kernel.AssignProcessToJobObject(job, handle)):
        kernel.CloseHandle(job)
        logging.getLogger(__name__).warning("Windows process job assignment unavailable")
        return
    proc._agentcli_job = (job, kernel)


def close_process_job(proc):
    job = getattr(proc, "_agentcli_job", None)
    if not isinstance(job, tuple):
        return
    proc._agentcli_job = None
    job[1].CloseHandle(job[0])
