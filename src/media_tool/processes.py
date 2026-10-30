"""Bounded process output, cancellation, timeouts and parent-loss supervision."""
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

from .jobs import Cancelled, checkpoint


def run_process(arguments, cancelled=lambda: False, timeout=None, on_line=None, output_limit=4 * 1024 * 1024, cwd=None, input_text=None, include_stderr=False):
    if input_text is not None and (not isinstance(input_text, str) or len(input_text.encode("utf-8")) > 65536):
        raise ValueError("Media input text exceeds the supported manifest limit.")
    checkpoint(cancelled)
    supervisor = ([sys.executable, "--media-worker"] if getattr(sys, "frozen", False) else
                  [getattr(sys,"_base_executable",sys.executable), str(Path(__file__).with_name("media_worker.py"))])
    process = subprocess.Popen(supervisor, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        close_fds=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    from .process_tree import ProcessTree
    try:
        tree = ProcessTree(process)
    except Exception:
        process.stdin.close()
        process.terminate()
        process.wait()
        process.stdout.close()
        process.stderr.close()
        raise
    messages, stopping = queue.Queue(maxsize=64), threading.Event()
    def read(pipe, channel):
        try:
            while data := pipe.read1(4096):
                while not stopping.is_set():
                    try:
                        messages.put((channel, data), timeout=0.1)
                        break
                    except queue.Full:
                        pass
        finally:
            while not stopping.is_set():
                try:
                    messages.put((channel, None), timeout=0.1)
                    break
                except queue.Full:
                    pass
    threads = [threading.Thread(target=read, args=(pipe, channel), daemon=True)
               for pipe, channel in ((process.stdout, 0), (process.stderr, 1))]
    for thread in threads:
        thread.start()
    output, errors, pending = bytearray(), bytearray(), bytearray()
    ended, started = 0, time.monotonic()
    try:
        command = list(map(str, arguments))
        request = command if cwd is None else dict(argv=command, cwd=str(cwd))
        if input_text is not None:
            request = dict(argv=command, input_text=input_text)
            if cwd is not None:
                request["cwd"] = str(cwd)
        process.stdin.write((json.dumps(request, ensure_ascii=True) + "\n").encode())
        process.stdin.flush()
        while ended < 2:
            checkpoint(cancelled)
            if timeout is not None and time.monotonic() - started > timeout:
                raise TimeoutError("Media tool timed out.")
            try:
                channel, data = messages.get(timeout=0.1)
            except queue.Empty:
                continue
            if data is None:
                ended += 1
            elif channel == 1:
                errors.extend(data)
                del errors[:-32768]
            elif on_line is None:
                output.extend(data)
                if len(output) > output_limit:
                    raise ValueError("Media tool output exceeded the inspection limit.")
            else:
                pending.extend(data)
                while b"\n" in pending:
                    line, _, pending = pending.partition(b"\n")
                    on_line(line.decode("utf-8", errors="replace").strip())
                if len(pending) > 65536:
                    raise ValueError("Invalid oversized media progress line.")
        code = process.wait(timeout=5)
        checkpoint(cancelled)
        if code:
            message = errors.decode("utf-8", errors="replace").strip()[-4000:]
            raise ValueError(f"Media tool failed (exit {code}): {message or 'No error details.'}")
        return (output + errors if include_stderr else output).decode("utf-8", errors="replace")
    finally:
        # Closing this pipe also handles app/process loss: the supervisor stops its child.
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        tree.close()
        stopping.set()
        for thread in threads:
            thread.join(timeout=1)
        process.stdout.close()
        process.stderr.close()
