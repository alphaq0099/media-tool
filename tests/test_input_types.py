from pathlib import Path

from PySide6.QtWidgets import QDialog

from media_tool.domain import FileRecord,FilterSpec
from test_foundation import app,window,wait_until


def add_files(window,tmp_path,names):
    for name in names:(tmp_path/name).write_bytes(b'Untouched input fixture')
    assert window.start_scan([str(tmp_path)])
    wait_until(lambda:not window.busy)


def shown(window):
    return {record.name for record in window.model.items if window.proxy.spec.matches(record)}


def test_mixed_folder_images_filter_automatically_and_notice_is_inline(window,tmp_path):
    names=('photo.PNG','photo.JPG','frame.webp','scan.tiff','clip.mp4','song.mp3','notes.txt','animation.gif','phone.heic')
    add_files(window,tmp_path,names)
    assert shown(window)=={'photo.PNG','photo.JPG','frame.webp','scan.tiff'}
    assert window.proxy.rowCount()==4
    assert '4 visible / 4 total' in window.count_label.text()
    assert window.input_types.notice.isVisible()
    assert '5 files are not supported in Images' in window.input_types.message.text()
    assert not any(child.isVisible() for child in window.findChildren(QDialog))
    assert [window.category_filter.itemText(i) for i in range(window.category_filter.count())]==[
        'All images','PNG','JPG / JPEG','WebP','Other supported']
    assert all((tmp_path/name).read_bytes()==b'Untouched input fixture' for name in names)


def test_extensions_other_and_custom_filters_intersect_without_widening_scope(window,tmp_path):
    add_files(window,tmp_path,('a.png','b.jpg','c.jpeg','scan.bmp','scan.tif','x.mp4'))
    window.category_filter.setCurrentText('JPG / JPEG')
    assert shown(window)=={'b.jpg','c.jpeg'}
    window.category_filter.setCurrentText('Other supported')
    assert shown(window)=={'scan.bmp','scan.tif'}
    window.extension_filter.setText('mp4')
    window.apply_filters()
    assert window.proxy.rowCount()==0
    assert shown(window)==set()
    window.reset_filters()
    assert window.category_filter.currentText()=='All images'
    assert window.proxy.rowCount()==5
    assert 'x.mp4' not in shown(window)


def test_switching_tools_changes_scope_and_keeps_filter_choices_independent(window,tmp_path):
    add_files(window,tmp_path,('one.png','two.jpg','one.MKV','two.mp4','song.mp3'))
    window.category_filter.setCurrentText('PNG')
    window._tool_changed('Video')
    assert window.category_filter.currentText()=='All videos'
    assert shown(window)=={'one.MKV','two.mp4'}
    window.category_filter.setCurrentText('MKV')
    assert shown(window)=={'one.MKV'}
    window._tool_changed('Images')
    assert window.category_filter.currentText()=='PNG'
    assert shown(window)=={'one.png'}
    window._tool_changed('Rename')
    assert window.category_filter.currentText()=='All types'
    assert window.proxy.rowCount()==5
    window._tool_changed('Archives')
    assert window.proxy.rowCount()==5
    window._tool_changed('Video')
    assert window.category_filter.currentText()=='MKV'
    assert shown(window)=={'one.MKV'}


def test_audio_defaults_to_audio_and_video_sources_remain_available(window,tmp_path):
    window._tool_changed('Audio')
    add_files(window,tmp_path,('sound.mp3','sound.wav','voice.wma','movie.mkv','image.png'))
    assert window.category_filter.currentText()=='All audio'
    assert shown(window)=={'sound.mp3','sound.wav','voice.wma'}
    assert '1 file is not supported in Audio' in window.input_types.message.text()
    assert '1 video source is available' in window.input_types.message.text()
    window.category_filter.setCurrentText('Video sources')
    assert shown(window)=={'movie.mkv'}
    assert '1 visible / 1 total' in window.count_label.text()
    assert 'available under' not in window.input_types.message.text()
    window.category_filter.setCurrentText('Other supported')
    assert shown(window)=={'voice.wma'}
    window.category_filter.setCurrentText('Audio + video')
    assert shown(window)=={'sound.mp3','sound.wav','voice.wma','movie.mkv'}
    assert '4 visible / 4 total' in window.count_label.text()


def test_unsupported_direct_input_disables_actions_and_clear_removes_notice(window,tmp_path):
    source=tmp_path/'clip.mp4'
    source.write_bytes(b'video')
    assert window.start_scan([str(source)])
    wait_until(lambda:not window.busy)
    assert window.proxy.rowCount()==0
    assert '1 file is not supported in Images' in window.input_types.message.text()
    assert not window.start_rename_button.isEnabled()
    assert not window.preview_button.isEnabled()
    window.clear_files()
    assert not window.input_types.notice.isVisible()


def test_supported_preview_ignores_other_media_and_uses_extension_scope(window,tmp_path):
    from PIL import Image
    Image.new('RGB',(8,8)).save(tmp_path/'first.png')
    Image.new('RGB',(8,8)).save(tmp_path/'second.jpg')
    (tmp_path/'movie.mp4').write_bytes(b'video')
    assert window.start_scan([str(tmp_path)])
    wait_until(lambda:not window.busy)
    assert window.preview_changes()
    wait_until(lambda:not window.busy)
    assert {item.source.name for item in window.last_plan.items}=={'first.png','second.jpg'}
    assert window.last_plan.counts=={'Planned':2}
    window.category_filter.setCurrentText('PNG')
    assert window.last_plan is None
    assert window.preview_changes()
    wait_until(lambda:not window.busy)
    assert [item.source.name for item in window.last_plan.items]==['first.png']


def test_extension_scope_empty_is_no_matches_and_does_not_override_name():
    row=FileRecord('sample://1','PHOTO.JPG',1,'Image',True)
    assert FilterSpec.parse(name='photo',allowed_extensions=frozenset({'.jpg'})).matches(row)
    assert not FilterSpec.parse(name='other',allowed_extensions=frozenset({'.jpg'})).matches(row)
    assert not FilterSpec.parse(allowed_extensions=frozenset()).matches(row)


def test_notice_and_counts_follow_row_removal(window,tmp_path):
    add_files(window,tmp_path,('image.png','image.jpg','movie.mp4'))
    window.table.selectRow(0)
    window.remove_selected()
    assert '1 visible / 1 total' in window.count_label.text()
    window._tool_changed('Video')
    window.table.selectRow(0)
    window.remove_selected()
    window._tool_changed('Images')
    assert not window.input_types.notice.isVisible()
    assert '1 visible / 1 total' in window.count_label.text()
