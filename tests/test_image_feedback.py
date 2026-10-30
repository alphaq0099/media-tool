from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QPoint, QPointF
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QSlider

from media_tool.images import plan_images
from media_tool.planning import PlanRequest
from test_foundation import app, window, wait_until
from test_images import fixture_image, run


def test_default_destination_uses_each_input_folder_and_protects_originals(tmp_path):
    sources = [fixture_image(tmp_path / folder / "same.png") for folder in ("one", "two")]
    before = [Path(source.path).read_bytes() for source in sources]
    preview = plan_images(sources, PlanRequest("images", extension="png"))
    assert preview.counts == {"Planned": 2}
    assert {Path(item.output) for item in preview.items} == {
        tmp_path / "one" / "same_1.png", tmp_path / "two" / "same_1.png"}
    assert all(item["state"] == "succeeded" for item in run(tmp_path, preview))
    assert [Path(source.path).read_bytes() for source in sources] == before


def test_default_destination_collision_skip(tmp_path):
    source = fixture_image(tmp_path / "same.png")
    fixture_image(tmp_path / "same.jpg")
    preview = plan_images([source], PlanRequest("images", collision="skip"))
    assert preview.counts == {"Skipped": 1}
    assert len(list(tmp_path.iterdir())) == 2


def test_ui_default_conversion_and_slider_quality(window, tmp_path):
    source = fixture_image(tmp_path / "picture.png")
    assert not window.destination.text()
    assert isinstance(window.image_quality, QSlider)
    window.image_quality.setFocus()
    window.image_quality.setValue(84)
    QTest.keyClick(window.image_quality, Qt.Key.Key_Right)
    assert window.image_quality.value() == 85
    window.start_scan([source.path])
    wait_until(lambda: not window.busy)
    window.start_rename_button.click()
    wait_until(lambda: not window.busy)
    assert window.result_model.items[0]["state"] == "succeeded", window.status.text()
    assert Path(window.result_model.items[0]["output"]) == tmp_path / "picture.jpg"
    assert Path(source.path).exists()


@pytest.mark.parametrize("tool,control_name", [
    ("Images", "format"), ("Images", "collision"), ("Images", "image_quality"),
    ("Images", "alpha_background"), ("Images", "image_metadata"),
    ("Images", "resize_controls.width"), ("Images", "resize_controls.percent"),
    ("Rename", "start_number"), ("Rename", "detail"), ("Video", "detail"),
])
def test_wheel_scrolls_settings_without_changing_focused_control(window, tool, control_name):
    window._tool_changed(tool)
    # Constrain just the settings viewport to reproduce a panel that needs scrolling.
    window.settings_scroll.setFixedHeight(230)
    QTest.qWait(20)
    if control_name.startswith("resize_controls."):
        window.resize_controls.mode.setCurrentIndex(1 if control_name.endswith("width") else 2)
    control = window
    for part in control_name.split("."):
        control = getattr(control, part)
    control.setFocus()
    QTest.qWait(10)
    bar = window.settings_scroll.verticalScrollBar()
    bar.setValue(0)
    assert bar.maximum() > 0
    value = control.value if hasattr(control, "value") else control.currentIndex
    before = value()
    center = control.rect().center()
    event = QWheelEvent(QPointF(center), QPointF(control.mapToGlobal(center)), QPoint(), QPoint(0, -120),
                        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(control, event)
    assert value() == before
    assert bar.value() > 0


def test_custom_destination_can_be_cleared_back_to_input_folder(window, tmp_path):
    source = fixture_image(tmp_path / "input" / "photo.png")
    custom = tmp_path / "custom"
    custom.mkdir()
    window.model.append([source])
    window.destination.setText(str(custom))
    window.preview_changes()
    wait_until(lambda: not window.busy)
    assert Path(window.last_plan.items[0].output).parent == custom
    window.destination.clear()
    assert window.last_plan is None
    window.preview_changes()
    wait_until(lambda: not window.busy)
    assert Path(window.last_plan.items[0].output).parent == Path(source.path).parent
