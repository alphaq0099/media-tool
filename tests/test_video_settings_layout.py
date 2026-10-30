"""Expanded video settings must grow the scroll content rather than overlap."""
import pytest
from PySide6.QtCore import QPoint
from PySide6.QtTest import QTest
from test_foundation import app, window


@pytest.mark.parametrize('width,height', [(1180,740),(880,610)])
@pytest.mark.parametrize('theme', ['dark','light'])
def test_video_dimensions_and_target_size_fields_do_not_overlap(window,width,height,theme):
    window.resize(width,height)
    window.set_theme(theme)
    window._tool_changed('Video')
    resize=window.resize_controls
    compression=window.compression_controls
    resize.mode.setCurrentIndex(resize.mode.findData('dimensions'))
    compression.mode.setCurrentIndex(compression.mode.findData('target_size'))
    for _ in range(2):
        window._tool_changed('Images')
        window._tool_changed('Video')
        window.toggle_theme()
        QTest.qWait(40)
    for panel in (resize.panel,compression.panel,compression.target_panel):
        layout=panel.layout()
        assert panel.height()>=layout.minimumSize().height()
        visible=[layout.itemAt(i) for i in range(layout.count())
                 if layout.itemAt(i).widget() is None or not layout.itemAt(i).widget().isHidden()]
        for before,after in zip(visible,visible[1:]):
            assert before.geometry().bottom()<after.geometry().top(), (panel, before.geometry(),after.geometry())
    for control in (resize.width,resize.height,compression.amount,compression.audio):
        assert control.height()>=control.fontMetrics().height()+8
        point=control.mapTo(window.settings_panel,QPoint(0,0))
        assert point.y()>=0
