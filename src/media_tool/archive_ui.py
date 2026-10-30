"""ZIP options and a virtual contents preview; ZIP engines remain lazy imports."""
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QSize
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFrame, QHeaderView,
    QLabel, QLineEdit, QPushButton, QTableView, QVBoxLayout)


class EntriesModel(QAbstractTableModel):
    def __init__(self, spec, excluded=False, parent=None):
        super().__init__(parent)
        self.spec, self.excluded = spec, excluded
        self.entries = spec["manifest"]["excluded" if excluded else "entries"]

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.entries)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else 3

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return ("Archive entry / page", "Type / bytes", "Source")[section]
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Vertical:
            return section + 1

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.entries):
            return None
        entry = self.entries[index.row()]
        if self.excluded:
            return (entry, "Excluded", "")[index.column()] if role == Qt.ItemDataRole.DisplayRole else None
        if role == Qt.ItemDataRole.ToolTipRole:
            return entry["name"] + "\n" + entry["source"]
        if role == Qt.ItemDataRole.DisplayRole:
            return (entry["name"], "Folder" if entry["folder"] else f"{entry['identity'][2]:,}", entry["source"])[index.column()]


class ArchiveControls:
    def __init__(self, host):
        self.host = host
        self.panel = QFrame()
        layout = QVBoxLayout(self.panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(host.settings_layout.spacing())
        self._dependency_key = None
        self._last_kind = None
        layout.addWidget(QLabel("Archive mode"))
        self.mode = QComboBox()
        self.mode.addItem("ZIP to one file", "combined")
        self.mode.addItem("ZIP per file", "separate")
        self.mode.setAccessibleName("Archive mode")
        layout.addWidget(self.mode)
        self.name_label = QLabel("File name")
        self.name = QLineEdit("Archive")
        self.name.setAccessibleName("Combined archive file name")
        self.name.setToolTip("Enter a name with or without .zip. Existing outputs are never overwritten.")
        position = host.settings_layout.indexOf(host.collision_label)
        host.settings_layout.insertWidget(position, self.name_label)
        host.settings_layout.insertWidget(position + 1, self.name)
        self.number_pages = QCheckBox("Number pages (0001, 0002…)")
        self.number_pages.setChecked(True)
        layout.addWidget(self.number_pages)
        layout.addWidget(QLabel("Compression"))
        self.level = QComboBox()
        for name, value in (("Store (fastest)", 0), ("Moderate (fast)", 1),
                            ("Balanced (normal)", 6), ("Maximum (slowest)", 9)):
            self.level.addItem(name, value)
        self.level.setCurrentIndex(self.level.findData(6))
        self.level.setToolTip("Higher compression spends more CPU time. Already compressed media may not shrink. Store uses less processing.")
        layout.addWidget(self.level)
        self.password_label = QLabel("Password")
        self.encrypt_names = QCheckBox("Encrypt filenames")
        self.encrypt_names.setChecked(True)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("Blank: no encryption")
        self.password.setAccessibleName("Archive password")
        self.password.setToolTip("Passwords are not saved. Retrying or reopening a saved job asks again.")
        self.password_visibility = self.password.addAction(QIcon(), QLineEdit.ActionPosition.TrailingPosition)
        self.password_visibility.setCheckable(True)
        self.password_visibility.triggered.connect(self.set_password_visible)
        self.confirm_label = QLabel("Confirm password")
        self.confirm_password = QLineEdit()
        self.confirm_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.confirm_password.setPlaceholderText("Confirm password")
        self.confirm_password.setAccessibleName("Confirm archive password")
        for widget in (self.password_label, self.password, self.confirm_label, self.confirm_password, self.encrypt_names):
            layout.addWidget(widget)
        self.hidden = QCheckBox("Include hidden files")
        self.hidden.setChecked(True)
        self.hidden_folders = QCheckBox("Include hidden folders")
        self.hidden_folders.setChecked(True)
        layout.addWidget(self.hidden)
        layout.addWidget(self.hidden_folders)
        self.exclusions_label = QLabel("Exclude patterns")
        self.exclusions = QLineEdit()
        self.exclusions.setAccessibleName("Archive exclusion patterns")
        self.exclusions.setPlaceholderText("*.tmp; cache; build/*")
        self.exclusions.setToolTip("Separate patterns with semicolons. Match names or relative paths at any depth; you can include the selected root folder. Case-insensitive. Check excluded paths in Archive contents.")
        layout.addWidget(self.exclusions_label)
        layout.addWidget(self.exclusions)
        self.empty = QCheckBox("Include empty folders")
        self.empty.setChecked(True)
        layout.addWidget(self.empty)
        self.dependency = QLabel("7-Zip not found")
        self.dependency.setToolTip("7-Zip is required for this format. Install it, then preview again.")
        self.dependency.setWordWrap(True)
        layout.addWidget(self.dependency)
        host.settings_layout.insertWidget(host.settings_layout.indexOf(host.keep), self.panel)
        for control, signal in ((self.mode, "currentIndexChanged"), (self.name, "textChanged"),
                (self.level, "currentIndexChanged"), (self.exclusions, "textChanged"),
                (self.hidden, "toggled"), (self.hidden_folders, "toggled"), (self.empty, "toggled"),
                (self.number_pages, "toggled"), (self.password, "textChanged"),
                (self.encrypt_names, "toggled")):
            getattr(control, signal).connect(self.changed)
        host.format.currentTextChanged.connect(self.changed)
        self.set_password_visible(False)
        self.update_visibility()

    def options(self):
        kind = self.host.format.currentText().lower() if self.host.tool == "Archives" else "zip"
        return dict(archive_mode=self.mode.currentData(), archive_name=self.name.text(),
            archive_level=self.level.currentData(), archive_exclusions=self.exclusions.text(),
            archive_hidden=self.hidden.isChecked(), archive_hidden_folders=self.hidden_folders.isChecked(),
            archive_empty_dirs=self.empty.isChecked(),
            archive_format=kind, archive_number_pages=self.number_pages.isChecked(),
            archive_encrypt=bool(self.password.text()) and kind != "cbz",
            archive_encrypt_names=self.encrypt_names.isChecked())

    def authorize(self, specs):
        if not any(s.get("encrypted") for s in specs):
            return specs
        if not self.password_visibility.isChecked() and self.password.text() != self.confirm_password.text():
            raise ValueError("Archive passwords do not match.")
        from .seven_archive import remember_password
        token = remember_password(self.password.text())
        return tuple(dict(spec, password_token=token) if spec.get("encrypted") else spec for spec in specs)

    def update_visibility(self):
        archives = self.host.tool == "Archives"
        self.panel.setVisible(archives)
        self.name.setVisible(archives and self.mode.currentData() == "combined")
        self.name_label.setVisible(archives and self.mode.currentData() == "combined")
        kind = self.host.format.currentText().lower() if archives else "zip"
        title = kind.upper() if kind != "7z" else "7z"
        for index, label in enumerate((f"{title} to one file", f"{title} per file")):
            self.mode.setItemText(index, label)
        if kind == "7z" and self._last_kind != kind:
            self.encrypt_names.blockSignals(True)
            self.encrypt_names.setChecked(True)
            self.encrypt_names.blockSignals(False)
        self._last_kind = kind
        self.name.setToolTip(f"Enter a name with or without .{kind}. Existing outputs are never overwritten.")
        self.number_pages.setVisible(kind == "cbz")
        self.empty.setVisible(kind != "cbz")
        encrypted = kind != "cbz" and bool(self.password.text())
        self.encrypt_names.setVisible(kind == "7z")
        self.encrypt_names.setEnabled(encrypted)
        self.password_label.setVisible(kind != "cbz")
        self.password.setVisible(kind != "cbz")
        confirmation = kind != "cbz" and not self.password_visibility.isChecked()
        self.confirm_label.setVisible(confirmation)
        self.confirm_password.setVisible(confirmation)
        external = archives and (kind == "7z" or encrypted)
        dependency_key = (archives, kind, external)
        if external and dependency_key != self._dependency_key:
            from .tool_paths import resolve_tool
            self.dependency.setVisible(not resolve_tool("7z"))
        elif not external:
            self.dependency.hide()
        self._dependency_key = dependency_key
        if archives:
            self.host.detail.hide()
            self.host.detail_title.hide()

    def changed(self, *args):
        self.update_visibility()
        self.host.invalidate_plan()

    def set_password_visible(self, visible):
        self.password_visibility.setChecked(visible)
        self.password.setEchoMode(QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password)
        icon = "eye.svg" if visible else "eye-off.svg"
        self.password_visibility.setIcon(QIcon(str(Path(__file__).with_name("icons") / icon)))
        text = "Hide password" if visible else "Show password"
        self.password_visibility.setText(text)
        self.password_visibility.setToolTip(text)
        self.update_visibility()

    def show_contents(self):
        host = self.host
        if host.busy or host.last_plan is None or host.last_plan.request.operation != "archives":
            return
        plans = [item for item in host.last_plan.items if item.archive_spec]
        if not plans:
            return
        existing = getattr(host, "archive_contents_dialog", None)
        if existing is not None:
            existing.close()
        dialog = QDialog(host)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        if host.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen):
            dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        comic = host.last_plan.request.archive_format == "cbz"
        dialog.setWindowTitle("Comic pages" if comic else "Archive contents preview")
        dialog.resize(900, 550)
        layout = QVBoxLayout(dialog)
        choose = QComboBox()
        for item in plans:
            choose.addItem(str(item.output))
        layout.addWidget(choose)
        exclusions = QCheckBox("Show explicitly excluded paths")
        layout.addWidget(exclusions)
        summary = QLabel()
        summary.setWordWrap(True)
        layout.addWidget(summary)
        table = QTableView()
        table.setWordWrap(False)
        table.setAlternatingRowColors(True)
        table.setShowGrid(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(table, 1)
        cover = QLabel()
        cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cover.setMinimumHeight(160)
        cover.setVisible(comic)
        layout.addWidget(cover)
        def preview_page():
            if not comic or exclusions.isChecked():
                cover.clear()
                return
            spec = plans[choose.currentIndex()].archive_spec
            rows = table.selectionModel().selectedRows()
            index = rows[0].row() if rows else 0
            if not spec["manifest"]["entries"]:
                return
            entry = spec["manifest"]["entries"][index]
            try:
                from .zip_archive import check_entry
                from PySide6.QtGui import QImageReader, QPixmap
                check_entry(entry, spec)
                reader = QImageReader(entry["source"])
                reader.setAutoTransform(True)
                reader.setScaledSize(reader.size().scaled(QSize(240, 180), Qt.AspectRatioMode.KeepAspectRatio))
                image = reader.read()
                if image.isNull():
                    raise ValueError("Page preview unavailable.")
                cover.setPixmap(QPixmap.fromImage(image))
                cover.setToolTip(f"Page {index + 1}: {entry['name']}")
            except (OSError, ValueError):
                cover.setText("Page changed or preview unavailable. Preview the archive again.")
        def refresh():
            spec = plans[choose.currentIndex()].archive_spec
            previous = table.model()
            table.setModel(EntriesModel(spec, exclusions.isChecked(), table))
            if previous is not None:
                previous.deleteLater()
            summary.setText(f"{spec['file_count']:,} {'pages' if comic else 'files'}; {spec['total_bytes']:,} source bytes; {len(spec['manifest']['excluded']):,} exclusions.")
            table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
            table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
            table.selectionModel().selectionChanged.connect(preview_page)
            preview_page()
        choose.currentIndexChanged.connect(refresh)
        exclusions.toggled.connect(refresh)
        close = QPushButton("Close")
        close.clicked.connect(dialog.accept)
        layout.addWidget(close)
        refresh()
        host.archive_contents_dialog = dialog
        dialog.destroyed.connect(lambda: setattr(host, "archive_contents_dialog", None) if getattr(host, "archive_contents_dialog", None) is dialog else None)
        dialog.show()
