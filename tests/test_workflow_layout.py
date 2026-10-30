from pathlib import Path

from PySide6.QtCore import Qt,QPoint
from PySide6.QtTest import QTest

from media_tool.workflow_preview import project
from media_tool.workflow_ui import record_data
from media_tool.jobs import execute_job
from test_foundation import app,window,wait_until
from test_workflows import step,image,enqueue


def roots(*names):return [dict(name=name,kind='Image',path=str(Path('/fictional-inputs')/name)) for name in names]


def test_preview_connects_conversion_rename_and_encrypted_archive_without_files(tmp_path):
    steps=[step('images',extension='jpg',image_quality=100),step('rename',prefix='Photo_',padding=2),
        step('archives',archive_format='7z',archive_encrypt=True,archive_name='Photos')]
    result=project(roots('4.png'),steps)
    assert [(r['input'],r['output']) for r in result]==[
        ('4.png','4.jpg'),('4.jpg','Photo_01.jpg'),('Photo_01.jpg','Photos.7z')]
    assert result[0]['process']=='Format change'
    assert 'encrypted' in result[2]['process']
    assert list(tmp_path.iterdir())==[]


def test_preview_natural_order_random_names_and_unknown_tracks_remain_honest():
    result=project(roots('image10.png','image2.png'),[step('images',extension='jpg'),step('rename',prefix='Photo_',padding=2)])
    assert result[1]['input']=='image2.jpg\nimage10.jpg'
    assert result[1]['output']=='Photo_01.jpg\nPhoto_02.jpg'
    random=project(roots('image.png'),[step('rename',rename_style='random'),step('images',extension='webp')])
    assert random[0]['output']=='Random names; extensions kept'
    assert random[1]['output']=='Generated files (.webp)'
    tracks=project(roots('movie.mkv'),[step('audio',extension='mp3'),step('rename'),step('archives',archive_name='Music')])
    assert 'one per selected track' in tracks[0]['output']
    assert tracks[1]['input']==tracks[0]['output']
    assert tracks[1]['output']=='File_0001.mp3, …'
    assert tracks[2]['output']=='Music.zip'


def test_preview_pages_empty_inputs_and_stale_join_order():
    rows=project(roots('file.pdf'),[step('documents',document_task='render',extension='png'),step('rename')])
    assert rows[0]['output']=='Page images (.png)'
    assert rows[1]['output']=='File_0001.png, …'
    assert all(row['output']=='No inputs' for row in project([], [step('images'),step('archives')]))
    joined=project(roots('generated.wav'),[step('audio',audio_action='join',audio_join_order=('/old.wav',),extension='flac')])
    assert joined[0]['output']=='Joined audio (.flac)'


def test_step_list_settings_controls_inputs_and_preview_selection(window,tmp_path):
    source=image(tmp_path/'4.png')
    window.open_jobs()
    q=window.jobs_dialog
    e=q.workflow_editor
    e.inputs=[record_data(source)]
    e.steps=[step('images',extension='jpg',image_quality=100),step('rename',prefix='image',padding=1),
        step('archives',archive_format='7z',archive_encrypt=True)]
    e.refresh()
    q.tabs.setCurrentWidget(e)
    QTest.qWait(40)
    assert e.input_list.item(0).text()==f'Image: 4.png ({source.path})'
    assert 'JPEG' in e.step_list.item(0).text() and 'Quality: 100' in e.step_list.item(0).text()
    assert 'password protected' in e.step_list.item(2).text()
    assert len(e.preview.cards)==3
    top=e.step_list.mapTo(e,QPoint(0,0)).y()
    for button in e.step_buttons.values():assert button.mapTo(e,QPoint(0,0)).y()<top
    e.step_list.setCurrentRow(1)
    assert e.preview.cards[1].property('active')
    assert not e.preview.cards[0].property('active')
    assert e.enqueue_button.mapTo(e,QPoint(0,0)).x()>=8
    assert e.start_button.mapTo(e,QPoint(0,e.start_button.height())).y()<=e.height()-8
    e.step_list.setCurrentRow(0)
    assert not e.step_buttons['↑'].isEnabled()
    e.step_list.setCurrentRow(2)
    assert not e.step_buttons['↓'].isEnabled()


def test_workflow_preview_updates_with_capture_edit_move_and_remove(window,tmp_path):
    source=image(tmp_path/'input.png')
    window.model.append([source])
    window.open_jobs()
    q=window.jobs_dialog
    e=q.workflow_editor
    e.new()
    e.use_workspace()
    window._tool_changed('Images')
    window.format.setCurrentText('JPEG')
    window.image_quality.setValue(100)
    e.capture()
    window._tool_changed('Rename')
    window.prefix.setText('New_')
    e.capture()
    assert e.step_list.currentRow()==1
    assert e.preview.rows[1]['input']=='input.jpg'
    e.step_list.setCurrentRow(0)
    e.edit()
    window.format.setCurrentText('WebP')
    e.capture()
    assert e.preview.rows[1]['input']=='input.webp'
    assert '.webp' in e.preview.rows[1]['output']
    window._tool_changed('Archives')
    e.capture()
    e.move(-1)
    assert [r['request'].operation for r in e.preview.rows]==['images','archives','rename']
    assert e.preview.rows[2]['input'].endswith('.7z')
    e.remove()
    assert len(e.preview.cards)==2
    assert e.preview.rows[1]['input']=='input.webp'


def test_history_is_two_areas_and_responds_to_workspace_width(window):
    window.open_jobs()
    q=window.jobs_dialog
    q.tabs.setCurrentWidget(q.views.history)
    window.resize(1180,740)
    QTest.qWait(30)
    assert q.jobs_splitter.count()==2
    assert q.jobs_splitter.orientation()==Qt.Orientation.Vertical
    window.resize(1600,850)
    QTest.qWait(30)
    assert q.jobs_splitter.orientation()==Qt.Orientation.Horizontal
    window.resize(880,610)
    QTest.qWait(30)
    assert q.jobs_splitter.orientation()==Qt.Orientation.Vertical
    assert q.items_table.isVisible()


def test_history_child_files_are_tabbed_and_refresh_respects_manual_tab(window,tmp_path):
    source=image(tmp_path/'input.png')
    window.open_jobs()
    q=window.jobs_dialog
    job=enqueue(q.store,[source],[step('images',extension='jpg'),step('rename')])
    assert execute_job(q.store,job)=='succeeded'
    q.tabs.setCurrentWidget(q.views.history)
    q.refresh(job)
    q.items_table.selectRow(1)
    assert q.history_layout.tabs.tabText(0)=='Steps'
    assert q.child_model.rowCount()==1
    assert q.history_layout.tabs.currentIndex()==1
    assert q.child_table.isVisible() and not q.items_table.isVisible()
    q.history_layout.tabs.setCurrentIndex(0)
    q.refresh()
    QTest.qWait(20)
    assert q.history_layout.tabs.currentIndex()==0
    assert q.items_table.isVisible() and not q.child_table.isVisible()
    assert q.items_table.visualRect(q.items_table.currentIndex()).intersects(q.items_table.viewport().rect())
    q.items_table.selectRow(0)
    assert q.history_layout.tabs.currentIndex()==1
    ordinary=q.store.enqueue('Ordinary job','sample',[dict(name='file.txt',seconds=0)])
    q.refresh(ordinary)
    assert q.history_layout.tabs.currentIndex()==0
    assert q.history_layout.tabs.tabText(0)=='Files'
    assert not q.history_layout.tabs.isTabVisible(1)
    assert q.child_model.rowCount()==0
