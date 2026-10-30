"""Windows job lifetime for a supervised process and every descendant."""
import os


class ProcessTree:
    def __init__(self,process):
        self.handle=None
        if os.name!="nt":return
        import ctypes
        from ctypes import wintypes
        class Basic(ctypes.Structure):
            _fields_=[("process_time",ctypes.c_longlong),("job_time",ctypes.c_longlong),("flags",wintypes.DWORD),
                ("min_ws",ctypes.c_size_t),("max_ws",ctypes.c_size_t),("active",wintypes.DWORD),
                ("affinity",ctypes.c_size_t),("priority",wintypes.DWORD),("scheduling",wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_=[(name,ctypes.c_ulonglong) for name in ("read_ops","write_ops","other_ops","read_bytes","write_bytes","other_bytes")]
        class Extended(ctypes.Structure):
            _fields_=[("basic",Basic),("io",IO),("process_memory",ctypes.c_size_t),("job_memory",ctypes.c_size_t),
                ("peak_process",ctypes.c_size_t),("peak_job",ctypes.c_size_t)]
        kernel=ctypes.WinDLL("kernel32",use_last_error=True)
        kernel.CreateJobObjectW.argtypes=[ctypes.c_void_p,wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype=wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
        kernel.SetInformationJobObject.restype=wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes=[wintypes.HANDLE,wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype=wintypes.BOOL
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        self.kernel=kernel
        self.handle=kernel.CreateJobObjectW(None,None)
        if not self.handle:raise ctypes.WinError(ctypes.get_last_error())
        info=Extended()
        info.basic.flags=0x2000 # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(self.handle,9,ctypes.byref(info),ctypes.sizeof(info)) or not kernel.AssignProcessToJobObject(self.handle,wintypes.HANDLE(int(process._handle))):
            error=ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle=None
