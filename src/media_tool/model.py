from pathlib import Path
from collections import Counter
from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QSortFilterProxyModel
from .domain import FilterSpec, format_size, path_key


class FileModel(QAbstractTableModel):
    HEADERS = ("File name", "Type", "Size", "Modified", "Folder", "Status")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.items = []
        self.paths = set()
        self.extension_counts = Counter()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.HEADERS[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.items):
            return None
        item = self.items[index.row()]
        if role == Qt.ItemDataRole.DecorationRole and index.column()==0:
            from .navigation import navigation_icon
            from PySide6.QtWidgets import QApplication
            names={'Image':'Images','Video':'Video','Audio':'Audio','Document':'Documents','Archive':'Archives','Folder':'Folders'}
            return navigation_icon(names.get(item.kind,'Folders'),QApplication.palette().link().color().name())
        if role == Qt.ItemDataRole.ToolTipRole:
            return item.path
        if role == Qt.ItemDataRole.DisplayRole:
            column = index.column()
            if column == 0:
                return item.name
            if column == 1:
                return item.kind
            if column == 2:
                return "-" if item.kind == "Folder" else format_size(item.size)
            if column == 3:
                return item.modified_display or "-"
            if column == 4:
                return str(Path(item.path).parent) if not item.sample else "Sample data"
            return "Sample" if item.sample else "Listed"
        return None

    def append(self, records):
        fresh = []
        for item in records:
            key = item.identity_key
            if key not in self.paths:
                self.paths.add(key)
                fresh.append(item)
        if fresh:
            start = len(self.items)
            self.beginInsertRows(QModelIndex(), start, start + len(fresh) - 1)
            self.items.extend(fresh)
            self.extension_counts.update(item.extension for item in fresh)
            self.endInsertRows()

    def clear(self):
        self.beginResetModel()
        self.items.clear()
        self.paths.clear()
        self.extension_counts.clear()
        self.endResetModel()

    def remove_indices(self, rows):
        removed = set(rows)
        self.beginResetModel()
        self.items = [item for row, item in enumerate(self.items) if row not in removed]
        self.paths = {item.identity_key for item in self.items}
        self.extension_counts = Counter(item.extension for item in self.items)
        self.endResetModel()

    def sort(self, column, order=Qt.SortOrder.AscendingOrder):
        self.replace_order(sorted(self.items, key=lambda item: item.sort_key))

    def replace_order(self, records):
        self.beginResetModel()
        self.items = records
        self.paths = {record.identity_key for record in records}
        self.extension_counts = Counter(record.extension for record in records)
        self.endResetModel()


class FileFilter(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.spec = FilterSpec()
        self._accept_all = True
        self._category = "All types"
        self._name = ""
        self._extensions = frozenset()
        self._allowed_extensions = None

    def setSourceModel(self, source):
        # Avoid a C++ -> Python model lookup for every filter callback.
        self._source = source
        super().setSourceModel(source)

    def set_spec(self, spec):
        if spec != self.spec:
            self.beginFilterChange()
            self.spec = spec
            self._category = spec.category
            self._name = spec.name
            self._extensions = spec.extensions
            self._allowed_extensions = spec.allowed_extensions
            self._accept_all = spec == FilterSpec()
            self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def filterAcceptsRow(self, source_row, source_parent):
        if self._accept_all:
            return True
        item = self._source.items[source_row]
        return ((self._category == "All types" or self._category == item.kind)
                and (self._allowed_extensions is None or item.extension in self._allowed_extensions)
                and (not self._name or self._name in item.search_name)
                and (not self._extensions or item.extension in self._extensions))


class PlanModel(QAbstractTableModel):
    HEADERS = ("Source name", "Output name", "Result", "Dimensions")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.items = ()
        self.contents_mode = False
        self.audio_mode = False
        self.document_mode = False

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else 4

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            if section == 3 and self.contents_mode:
                return "Contents"
            if section == 3 and self.audio_mode:
                return "Audio"
            if section == 3 and self.document_mode:
                return "Pages / size"
            return self.HEADERS[section]

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        item = self.items[index.row()]
        if role == Qt.ItemDataRole.ToolTipRole:
            if item.audio_spec and item.audio_spec.get("sources"):
                order = "\n".join(f"{n}. {s['source']}" for n, s in enumerate(item.audio_spec["sources"], 1))
                return f"Join order:\n{order}\nOutput: {item.output or '-'}\n{item.reason}"
            return f"Source: {item.source.path}\nOutput: {item.output or '-'}\n{item.reason}"
        if role == Qt.ItemDataRole.DisplayRole:
            if index.column() == 3:
                if item.document_spec:
                    spec=item.document_spec
                    return f"{spec['raster_size'][0]}×{spec['raster_size'][1]}" if "raster_size" in spec else f"{spec['page_count']} pages"
                if item.audio_spec:
                    spec = item.audio_spec
                    settings = spec["settings"]
                    if spec.get("edit"):
                        return f"{spec['duration']:.3f} s; {settings['sample_rate']} Hz; {settings['channel_layout']}"
                    return f"Track {spec['track']}; {settings['sample_rate']} Hz; {settings['channel_layout']}"
                if item.archive_spec:
                    unit = "pages" if item.archive_spec.get("archive_format") == "cbz" else "files"
                    return f"{item.archive_spec['file_count']:,} {unit}"
                spec = item.image_spec
                if spec:
                    before = spec["image"]["output_size"]
                    after = spec.get("output_size", before)
                    return f"{before[0]}×{before[1]} → {after[0]}×{after[1]}"
                if item.video_spec:
                    w, h = item.video_spec["output_size"]
                    if item.video_spec.get("action") == "remux":
                        return f"{w}×{h} (copied)"
                    before = item.video_spec.get("display_size", (w, h))
                    return f"{before[0]}×{before[1]} → {w}×{h}"
                return "-"
            return (item.archive_spec["name"] if item.archive_spec else item.source.name,
                    Path(item.output).name if item.output else "-", item.status)[index.column()]

    def set_items(self, items=()):
        self.beginResetModel()
        self.items = items
        self.endResetModel()


class ResultModel(PlanModel):
    HEADERS = ("Source name", "Output name", "Status", "Details")

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.HEADERS)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.items):
            return None
        item = self.items[index.row()]
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"Source: {item['source']}\nOutput: {item['output']}\n{item['details']}"
        if role == Qt.ItemDataRole.DisplayRole:
            return (item.get('source_label') or Path(item['source']).name, Path(item['output']).name if item['output'] else "-",
                    item['state'].capitalize(), item['details'])[index.column()]
