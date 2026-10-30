"""Small process supervisor: EOF from the app stops its FFmpeg/FFprobe child."""
import json
import os
import subprocess
import sys
import threading


def main():
    command = json.loads(sys.stdin.buffer.readline(1024 * 1024))
    cwd = None
    input_text = None
    if isinstance(command, dict) and "argv" in command and set(command) <= {"argv", "cwd", "input_text"}:
        cwd, input_text, command = command.get("cwd"), command.get("input_text"), command["argv"]
        if cwd is not None and (not isinstance(cwd, str) or not os.path.isabs(cwd)):
            raise ValueError("Invalid media working directory.")
        if input_text is not None and (not isinstance(input_text, str) or len(input_text.encode("utf-8")) > 65536):
            raise ValueError("Invalid media input text.")
    if not isinstance(command, list) or not command or not all(isinstance(arg, str) for arg in command):
        raise ValueError("Invalid media command.")
    disconnected = threading.Event()
    def watch():
        os.read(sys.stdin.fileno(), 1)
        disconnected.set()
    threading.Thread(target=watch, daemon=True).start()
    if disconnected.is_set():
        return 1
    process = subprocess.Popen(command, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL, close_fds=True, cwd=cwd,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0))
    if os.name != "nt" and hasattr(os, "setpriority"):
        try:
            os.setpriority(os.PRIO_PROCESS, process.pid, 5)
        except OSError:
            pass
    try:
        if input_text is not None:
            # A separate writer keeps parent-loss supervision responsive even if
            # a child stops reading its manifest. The control pipe stays open.
            def feed():
                try:
                    process.stdin.write(input_text.encode("utf-8"))
                    process.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
                finally:
                    try:
                        process.stdin.close()
                    except OSError:
                        pass
            threading.Thread(target=feed, daemon=True).start()
        while process.poll() is None:
            if disconnected.wait(0.05):
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                break
        return max(0, process.returncode) if process.returncode >= 0 else 1
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
