"""One Qt picker accepts files and folders in the same selection."""
from pathlib import Path

from PySide6.QtWidgets import QFileDialog,QDialog,QDialogButtonBox


class InputPicker(QFileDialog):
    def __init__(self,parent=None):
        super().__init__(parent,'Open files and folders')
        self.setOption(QFileDialog.Option.DontUseNativeDialog,True)
        self.setFileMode(QFileDialog.FileMode.ExistingFiles)
        self.setLabelText(QFileDialog.DialogLabel.Accept,'Open')
        self.setNameFilter('All files (*)')
        self.paths=[]
        self.resize(820,540)
        from .help_ui import InfoButton
        buttons=self.findChild(QDialogButtonBox)
        if buttons:buttons.addButton(InfoButton('Select files, folders, or both. Ctrl/Shift selects multiple entries. Double-click a folder to navigate; Open adds the selected entries.'),QDialogButtonBox.ButtonRole.HelpRole)

    def accept(self):
        paths=self.selectedFiles()
        if paths and all(Path(path).exists() for path in paths):
            self.paths=paths
            QDialog.accept(self)


def pick_inputs(parent):
    dialog=InputPicker(parent)
    try:return list(dialog.paths) if dialog.exec()==QDialog.DialogCode.Accepted else []
    finally:dialog.deleteLater()
