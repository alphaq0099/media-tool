import json
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings,QItemSelectionModel,QEvent,QPoint,QCoreApplication,QTimer
from PySide6.QtGui import QHelpEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QFileDialog,QTreeView,QDialog,QDialogButtonBox,QWidget,QToolTip

from media_tool.window import Workspace
from media_tool.input_picker import InputPicker
from test_foundation import app,window,wait_until


def reopen(window):
    settings=QSettings(window.settings.fileName(),QSettings.Format.IniFormat)
    window.close()
    wait_until(lambda:not window.isVisible())
    other=Workspace(settings)
    other.show()
    return other


def test_tabs_keep_their_position_when_changing_pages(window):
    bar=window.tabs.tabBar()
    original=bar.mapTo(window,bar.tabRect(0).center())
    for index in (1,2,0,2,1,0):
        window.tabs.setCurrentIndex(index)
        QTest.qWait(10)
        assert bar.mapTo(window,bar.tabRect(0).center())==original
    assert window.add_button.parentWidget() is window.visual.files_page
    assert window.add_button.menu() is None
    assert window.add_button.popupMode()==window.add_button.ToolButtonPopupMode.DelayedPopup
    assert window.add_button.text()=='+ Open'


def test_sidebar_order_current_theme_and_help_hover_only(window):
    layout=window.sidebar_layout
    widgets=[layout.itemAt(i).widget() for i in range(layout.count()) if layout.itemAt(i).widget()]
    assert widgets[-2:] == [window.diagnostics_button,window.theme_button]
    for theme in ('dark','light'):
        window.set_theme(theme)
        assert window.theme_button.text()==theme.title()
    help_event=QHelpEvent(QEvent.Type.ToolTip,QPoint(1,1),QPoint(1,1))
    assert window.visual.help_policy.eventFilter(window.start_rename_button,help_event)
    info=window.visual.heading_help
    assert not window.visual.help_policy.eventFilter(info,help_event)
    QToolTip.hideText()
    info.click()
    assert not QToolTip.isVisible()


def test_picker_accepts_files_and_folders_together(window,tmp_path):
    file=tmp_path/'one.txt'
    folder=tmp_path/'folder'
    file.write_text('one')
    folder.mkdir()
    picker=InputPicker(window)
    picker.setViewMode(QFileDialog.ViewMode.Detail)
    picker.setDirectory(str(tmp_path))
    picker.show()
    tree=picker.findChild(QTreeView,'treeView')
    assert tree is not None
    model=tree.model()
    wait_until(lambda:model.index(str(folder)).isValid() and model.index(str(file)).isValid())
    flags=QItemSelectionModel.SelectionFlag.Select|QItemSelectionModel.SelectionFlag.Rows
    for path in (file,folder):tree.selectionModel().select(model.index(str(path)),flags)
    assert set(map(Path,picker.selectedFiles()))=={file,folder}
    picker.accept()
    assert picker.result()==QDialog.DialogCode.Accepted
    assert set(map(Path,picker.paths))=={file,folder}


def test_quality_resize_and_checkbox_defaults_survive_restart(window,tmp_path):
    window.image_quality.setValue(100)
    window.resize_controls.mode.setCurrentIndex(window.resize_controls.mode.findData('dimensions'))
    window.resize_controls.width.setValue(1234)
    window.resize_controls.no_upscale.setChecked(False)
    window.destination.setText(str(tmp_path))
    window._tool_changed('Archives')
    window.archive_controls.password.setText('never-store-this')
    window.archive_controls.name.setText('temporary-source-bound-name')
    window.archive_controls.level.setCurrentIndex(window.archive_controls.level.findData(9))
    window.archive_controls.hidden.setChecked(False)
    other=reopen(window)
    try:
        assert other.image_quality.value()==100
        assert other.resize_controls.width.value()==1234
        assert not other.resize_controls.no_upscale.isChecked()
        assert other.destination.text()==''
        assert other.model.rowCount()==0
        other._tool_changed('Archives')
        assert other.archive_controls.level.currentData()==9
        assert not other.archive_controls.hidden.isChecked()
        assert not other.archive_controls.password.text()
        assert other.archive_controls.name.text()=='Archive'
        raw=str(other.settings.value('defaults/v1'))
        assert 'never-store-this' not in raw and 'temporary-source-bound-name' not in raw
    finally:other.close()


def test_audio_tasks_keep_separate_defaults_and_document_format_restores(window):
    window._tool_changed('Audio')
    audio=window.audio_controls
    audio.quality.setCurrentIndex(audio.quality.findData(0))
    audio.task.setCurrentIndex(audio.task.findData('trim'))
    audio.quality.setCurrentIndex(audio.quality.findData(7))
    audio.start.setText('10')
    audio.task.setCurrentIndex(audio.task.findData('convert'))
    assert audio.quality.currentData()==0
    window._tool_changed('Documents')
    doc=window.document_controls
    doc.task.setCurrentIndex(doc.task.findData('render'))
    window.format.setCurrentText('JPEG')
    doc.dpi.setValue(240)
    other=reopen(window)
    try:
        other._tool_changed('Documents')
        assert other.document_controls.task.currentData()=='render'
        assert other.format.currentText()=='JPEG'
        assert other.document_controls.dpi.value()==240
        other._tool_changed('Audio')
        assert other.audio_controls.quality.currentData()==0
        other.audio_controls.task.setCurrentIndex(other.audio_controls.task.findData('trim'))
        assert other.audio_controls.quality.currentData()==7
        assert other.audio_controls.start.text()=='0'
    finally:other.close()


def test_current_queue_is_one_table_and_completed_rows_go_to_history(window):
    queue=window.ensure_jobs()
    queue.show()
    assert [queue.tabs.tabText(i) for i in range(3)]==['Jobs','Workflow','History']
    assert queue.views.page.findChildren(type(queue.views.table))==[queue.views.table]
    a=queue.store.enqueue('Waiting conversion','sample',[dict(name='one.mp4',seconds=.02),dict(name='two.mp4',seconds=.02)])
    queue.refresh(a)
    assert queue.views.model.rowCount()==2
    assert queue.views.model.rows[0][:2]==('Waiting conversion','one.mp4')
    assert queue.selected_job()==a
    queue.start_selected()
    wait_until(lambda:queue.worker is None and not queue.enabled)
    assert queue.views.model.rowCount()==0
    queue.tabs.setCurrentWidget(queue.views.history)
    assert a in queue.jobs_model.keys
    assert len(queue.store.items(a))==2


def test_folder_actions_and_picker_match_other_tools(window):
    page=window.show_folders()
    assert page.open_button.mapTo(page,page.open_button.rect().topLeft()).x()<page.root.mapTo(page,page.root.rect().topLeft()).x()
    assert page.run_button.parentWidget() is page.inspector
    assert page.queue_button.parentWidget() is page.inspector
    assert page.preview_button.parentWidget() is page.inspector


def test_empty_jobs_does_not_enable_actions_for_hidden_history(window):
    queue=window.ensure_jobs()
    job=queue.store.enqueue('Old failure','sample',[dict(name='old.mp4',seconds=0)])
    queue.store.set_item(queue.store.items(job)[0]['id'],'failed','Old test failure')
    queue.store.set_job(job,'failed')
    queue.refresh()
    assert queue.views.model.rowCount()==0
    assert queue.selected_job() is None
    assert not queue.retry_button.isEnabled()
    assert not queue.cancel_button.isEnabled()
    queue.tabs.setCurrentWidget(queue.views.history)
    assert queue.selected_job()==job
    assert queue.retry_button.isEnabled()


def test_workflow_draft_values_do_not_replace_personal_defaults(window):
    window.image_quality.setValue(96)
    window.preferences.save_current()
    editor=window.ensure_jobs().workflow_editor
    editor.new()
    window.image_quality.setValue(40)
    editor.leave()
    assert window.image_quality.value()==96


def test_repeated_picker_cancel_releases_dialogs(window,tmp_path,monkeypatch):
    from media_tool.input_picker import pick_inputs
    original=InputPicker.exec
    def cancel(dialog):
        dialog.setDirectory(str(tmp_path))
        QTimer.singleShot(0,dialog.reject)
        return original(dialog)
    monkeypatch.setattr(InputPicker,'exec',cancel)
    for _ in range(3):
        assert pick_inputs(window)==[]
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        QTest.qWait(20)
        assert window.findChildren(InputPicker)==[]
