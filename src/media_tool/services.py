"""Cancellable read-only workers and nonblocking process discovery."""
from pathlib import Path
import os

from PySide6.QtCore import QObject, QProcess, QThread, QTimer, Signal
from .domain import FileRecord, category, natural_key, path_key
from .planning import build_plan, PlanCancelled
from .tool_paths import resolve_tool


class ScanWorker(QThread):
    records = Signal(object)
    issue = Signal(str)
    progress = Signal(int)
    summary = Signal(int, bool)

    def __init__(self, paths=(), recursive=False, sample_count=0, parent=None, excluded_paths=(), preserve_folders=False):
        super().__init__(parent)
        self.paths = tuple(paths)
        self.recursive = recursive
        self.sample_count = sample_count
        self.excluded_paths = tuple(excluded_paths)
        self.preserve_folders = preserve_folders

    def run(self):
        pending = []
        count = 0

        def emit_record(record):
            nonlocal count
            pending.append(record)
            count += 1
            if len(pending) >= 200:
                self.records.emit(tuple(pending))
                pending.clear()
                self.progress.emit(count)

        if self.sample_count:
            for i in range(self.sample_count):
                if self.isInterruptionRequested():
                    break
                emit_record(FileRecord(f"sample://{i}", f"Sample_{i+1:05d}.jpg",
                                       1024 * (i % 2000 + 1), "Image", True))
                if i % 100 == 0:
                    self.msleep(4)
        else:
            seen = set()
            excluded = set()
            for path in self.excluded_paths:
                try:
                    excluded.add(path_key(path))
                except (OSError, RuntimeError) as error:
                    self.issue.emit(f"Cannot resolve excluded output folder: {error}")
            stack = [(path, True) for path in reversed(self.paths)]
            while stack and not self.isInterruptionRequested():
                raw, explicit = stack.pop()
                path = Path(raw)
                try:
                    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                        self.issue.emit(f"Skipped link: {path}")
                        continue
                    key = path_key(path)
                    if key in excluded and not explicit:
                        self.issue.emit(f"Skipped output folder: {path}")
                        continue
                    if key in seen:
                        continue
                    seen.add(key)
                    if path.is_dir():
                        if self.preserve_folders:
                            emit_record(FileRecord.from_path(path))
                            continue
                        with os.scandir(path) as entries:
                            children = sorted(entries, key=lambda item: (natural_key(item.name), item.name))
                        for item in reversed(children):
                            if self.isInterruptionRequested():
                                break
                            try:
                                if item.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(item.path)):
                                    self.issue.emit(f"Skipped link: {item.path}")
                                elif item.is_file(follow_symlinks=False) or self.recursive:
                                    stack.append((item.path, False))
                            except OSError as error:
                                self.issue.emit(f"Cannot inspect {item.path}: {error}")
                        continue
                    if not path.is_file():
                        # stat raises a useful error for missing/denied paths.
                        path.stat()
                        self.issue.emit(f"Not a regular file: {path}")
                        continue
                    emit_record(FileRecord.from_path(path))
                except (OSError, RuntimeError) as error:
                    self.issue.emit(f"Cannot read {path}: {error}")
        if pending:
            self.records.emit(tuple(pending))
        self.summary.emit(count, self.isInterruptionRequested())


class SortWorker(QThread):
    ordered = Signal(object)

    def __init__(self, records, parent=None):
        super().__init__(parent)
        self.snapshot = tuple(records)

    def run(self):
        from operator import attrgetter
        if self.isInterruptionRequested():
            return
        ordered = sorted(self.snapshot, key=attrgetter("sort_key"))
        if not self.isInterruptionRequested():
            self.ordered.emit(ordered)


class PlanWorker(QThread):
    planned = Signal(object)
    failed = Signal(str)

    def __init__(self, records, request, parent=None):
        super().__init__(parent)
        self.snapshot = tuple(records)
        self.request = request

    def run(self):
        try:
            if self.request.operation == "images":
                from .images import plan_images
                plan = plan_images(self.snapshot, self.request, self.isInterruptionRequested)
            elif self.request.operation == "documents":
                from .documents import plan_documents
                plan = plan_documents(self.snapshot, self.request, self.isInterruptionRequested)
            elif self.request.operation == "audio":
                from .audio import plan_audio
                plan = plan_audio(self.snapshot, self.request, self.isInterruptionRequested)
            elif self.request.operation == "video":
                from .video import plan_video
                plan = plan_video(self.snapshot, self.request, self.isInterruptionRequested)
            elif self.request.operation == "archives":
                from .zip_plan import plan_archives
                plan = plan_archives(self.snapshot, self.request, self.isInterruptionRequested)
            else:
                plan = build_plan(self.snapshot, self.request, self.isInterruptionRequested)
            if not self.isInterruptionRequested():
                self.planned.emit(plan)
        except PlanCancelled:
            pass
        except Exception as error:
            self.failed.emit(str(error))


class UndoPlanWorker(QThread):
    planned = Signal(object)
    failed = Signal(str)

    def __init__(self, directory, job_id, parent=None):
        super().__init__(parent)
        self.directory, self.job_id = directory, job_id

    def run(self):
        from .jobs import Store
        from .undo import plan_undo
        store = None
        try:
            store = Store(self.directory)
            plan = plan_undo(store, self.job_id, self.isInterruptionRequested)
            if not self.isInterruptionRequested():
                self.planned.emit(plan)
        except PlanCancelled:
            pass
        except Exception as error:
            self.failed.emit(str(error))
        finally:
            if store:
                store.close()


class ToolProbe(QObject):
    """Probe one executable, draining both channels with a bounded timeout."""
    result = Signal(str)
    checked = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.process = QProcess(self)
        self.process.readyReadStandardOutput.connect(self._read)
        self.process.readyReadStandardError.connect(self._read)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._error)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._timeout)
        self.output = bytearray()
        self.done = True
        self.name = ""

    def start(self, name, arguments=None):
        if not self.done:
            return False
        self.name = name
        self.output.clear()
        self.done = False
        program = resolve_tool(name)
        self.resolved_program = program
        if not program:
            self._report(f"{name}: not found. File intake and previews still work.")
            return False
        self.process.setProgram(program)
        default = ["i"] if Path(name).name.casefold().removesuffix(".exe") in ("7z", "7za", "7zz") else ["-version"]
        self.process.setArguments(arguments if arguments is not None else default)
        self.timer.start(5000)
        self.process.start()
        return True

    def _read(self):
        data = bytes(self.process.readAllStandardOutput()) + bytes(self.process.readAllStandardError())
        self.output.extend(data[:max(0, 16384 - len(self.output))])

    def _report(self, message, success=False):
        if self.done:
            return
        self.done = True
        self.timer.stop()
        checked=dict(name=self.name,path=self.resolved_program,success=success,message=message)
        self.result.emit(message)
        self.checked.emit(checked)

    def _finished(self, code, status):
        self._read()
        lines = self.output.decode("utf-8", errors="replace").splitlines()
        first = next((line.strip() for line in lines if line.strip()), "No version output")
        if code == 0 and status == QProcess.ExitStatus.NormalExit:
            self._report(f"{self.name}: {first}\nLocation: {self.resolved_program}",True)
        else:
            self._report(f"{self.name}: failed (exit {code}). {first}")

    def _error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self._report(f"{self.name}: could not start. {self.process.errorString()}")

    def _timeout(self):
        self.process.kill()
        self._report(f"{self.name}: timed out after 5 seconds.")

    def cancel(self):
        if not self.done:
            self.process.kill()
            self._report(f"{self.name}: check cancelled.")
