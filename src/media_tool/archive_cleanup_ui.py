"""Review and run source-to-trash as a separate, cancellable Results action."""
import json
import threading

from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtWidgets import QDialog, QLabel, QVBoxLayout, QPlainTextEdit, QPushButton

from .jobs import Store


class CleanupWorker(QThread):
    completed = Signal(str)

    def __init__(self, directory, ids, parent):
        super().__init__(parent)
        self.directory, self.ids = directory, ids
        self.cancelled = threading.Event()

    def run(self):
        from .archive_cleanup import cleanup_archive
        store = None
        moved, kept, errors = 0, 0, []
        try:
            store = Store(self.directory)
            for item_id in self.ids:
                try:
                    before = json.loads(store.item(item_id)["original_cleanup"] or "{}")
                    result = cleanup_archive(store, item_id, self.cancelled.is_set)
                    moved += result["moved"] - before.get("moved", 0)
                    kept += result["kept"]
                except Exception as error:
                    errors.append(str(error))
            self.completed.emit(f"Archive saved. Source cleanup: {moved} moved to Recycle Bin / Trash; {kept} kept. " + " ".join(errors))
        except Exception as error:
            self.completed.emit(f"Source cleanup stopped: {error}. Saved archives remain intact.")
        finally:
            if store is not None:
                store.close()


def review_cleanup(host):
    if host.busy or host.jobs_dialog is None or not host.last_result_job:
        return
    queue = host.jobs_dialog
    if not queue.can_start_immediately:
        return
    job_id = host.last_result_job
    items = queue.store.items(job_id)
    selected = {index.row() for index in host.results_table.selectionModel().selectedRows()}
    chosen = [item for index, item in enumerate(items) if item["state"] == "succeeded" and (not selected or index in selected)]
    chosen = [item for item in chosen if item["source_digest"]]
    if not chosen:
        host.status.setText("Select a successfully verified archive with saved source checksums.")
        return
    sources = {e["source"]: e["identity"][2] for item in chosen for e in json.loads(item["spec"])["manifest"]["entries"] if not e["folder"]}
    dialog = QDialog(host)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    if host.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen):
        dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    dialog.setWindowTitle("Review archive source cleanup")
    dialog.resize(760, 440)
    layout = QVBoxLayout(dialog)
    summary = QLabel(f"{len(sources):,} included source files; {sum(sources.values()):,} bytes.\n"
        "Move verified files to Recycle Bin / Trash. Excluded files and folders stay in place.\n"
        "Changed sources, changed archives and sources needed by pending jobs are kept.")
    summary.setWordWrap(True)
    layout.addWidget(summary)
    paths = QPlainTextEdit()
    paths.setReadOnly(True)
    paths.setPlainText("\n".join(list(sources)[:1000]) + ("\n… More files included." if len(sources) > 1000 else ""))
    layout.addWidget(paths)
    move = QPushButton("Move verified sources to Recycle Bin / Trash")
    layout.addWidget(move)
    cancel = QPushButton("Cancel")
    cancel.clicked.connect(dialog.reject)
    layout.addWidget(cancel)
    def start():
        if host.busy or not queue.can_start_immediately:
            return
        worker = CleanupWorker(queue.directory, [item["id"] for item in chosen], host)
        host.archive_cleanup_worker = worker
        worker.completed.connect(host.status.setText)
        def finished():
            host.archive_cleanup_worker = None
            worker.deleteLater()
            if host._closing:
                host.close()
                return
            host.show_rename_results(queue.store.items(job_id), job_id, archives=True)
            host.update_controls()
        worker.finished.connect(finished)
        dialog.accept()
        host.update_controls()
        host.status.setText("Checking saved archives and moving verified sources to Recycle Bin / Trash…")
        worker.start()
    move.clicked.connect(start)
    host.archive_cleanup_dialog = dialog
    dialog.show()
