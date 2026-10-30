"""Native theme painting, cursor behavior, branding and released Tools menu."""
from PySide6.QtCore import QPoint,Qt
from PySide6.QtTest import QTest
import pytest

from test_foundation import app,window,wait_until


def hover(widget,pos=None):
    QTest.mouseMove(widget,pos or widget.rect().center())
    QTest.qWait(20)


def test_clickable_controls_use_hand_and_text_inputs_keep_ibeam(window):
    for control in (window.add_button,window.format,window.keep,window.visual.heading_help):
        hover(control)
        assert control.cursor().shape()==Qt.CursorShape.PointingHandCursor
    hover(window.name_filter)
    assert window.name_filter.cursor().shape()==Qt.CursorShape.IBeamCursor
    hover(window.tabs.tabBar(),window.tabs.tabBar().tabRect(0).center())
    assert window.tabs.tabBar().cursor().shape()==Qt.CursorShape.PointingHandCursor
    window.add_button.setEnabled(False)
    assert window.add_button.cursor().shape()==Qt.CursorShape.ArrowCursor


def test_lazy_folder_buttons_and_rows_get_hand_cursors(window,tmp_path):
    page=window.show_folders()
    (tmp_path/'example.txt').write_text('fixture')
    page.root.setText(str(tmp_path));page.scan()
    wait_until(lambda:not window.busy)
    hover(page.open_button)
    assert page.open_button.cursor().shape()==Qt.CursorShape.PointingHandCursor
    index=page.tree.model().index(0,0)
    hover(page.tree.viewport(),page.tree.visualRect(index).center())
    assert page.tree.viewport().cursor().shape()==Qt.CursorShape.PointingHandCursor
    hover(page.minimum.lineEdit())
    assert page.minimum.lineEdit().cursor().shape()==Qt.CursorShape.IBeamCursor


def test_no_sample_menu_or_pulsing_indicator_and_icon_has_small_sizes(window):
    texts=[a.text() for a in window.diagnostics_button.menu().actions()]
    assert not any('sample' in s.lower() or '10,000' in s for s in texts)
    assert 'Dependencies…' in texts and 'Check media tools' in texts
    assert not hasattr(window,'availability')
    icon=window.windowIcon()
    assert not icon.isNull()
    assert {s.width() for s in icon.availableSizes()}>={16,24,32,48,64,128,256}


def background(window,page):
    point=page.mapTo(window,QPoint(6,6))
    pixmap=window.grab()
    ratio=pixmap.devicePixelRatio()
    return pixmap.toImage().pixelColor(round(point.x()*ratio),round(point.y()*ratio)).name()


@pytest.mark.parametrize('size',[(1180,740),(880,610)])
def test_folder_and_queue_backgrounds_follow_theme_after_hidden_switches(window,size):
    window.resize(*size)
    folders=window.show_folders()
    QTest.qWait(30)
    window.open_jobs()
    queue=window.jobs_dialog
    QTest.qWait(30)
    for theme,color in (('light','#dce3dd'),('dark','#15191f'),('light','#dce3dd')):
        window.set_theme(theme)
        for show,page in ((window.show_folders,folders),(window.open_jobs,queue)):
            show();QTest.qWait(40)
            assert background(window,page)==color,(theme,type(page).__name__,background(window,page))
        queue.tabs.setCurrentWidget(queue.workflow_editor)
        QTest.qWait(40)
        assert background(window,queue.workflow_editor)==color
