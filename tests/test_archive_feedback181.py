from pathlib import Path
import zipfile

import pytest

from media_tool.seven_archive import seven_run
from media_tool.tool_paths import resolve_tool
from test_archives import plan, run
from test_foundation import app, window, wait_until


@pytest.mark.parametrize("files,folders", [(False, False), (False, True), (True, False), (True, True)])
def test_hidden_files_and_folders_have_independent_archive_scope(tmp_path, files, folders):
    root = tmp_path / "Inputs"
    (root / ".private").mkdir(parents=True)
    for relative in ("normal.txt", ".secret.txt", ".private/normal.txt", ".private/.secret.txt"):
        (root / relative).write_text(relative)
    preview = plan([root], archive_hidden=files, archive_hidden_folders=folders)
    assert preview.counts == {"Planned": 1}
    assert run(tmp_path, preview)[0]["state"] == "succeeded"
    expected = {"Inputs/normal.txt"}
    if files:
        expected.add("Inputs/.secret.txt")
    if folders:
        expected.add("Inputs/.private/normal.txt")
        if files:
            expected.add("Inputs/.private/.secret.txt")
    with zipfile.ZipFile(preview.items[0].output) as archive:
        assert {i.filename for i in archive.infolist() if not i.is_dir()} == expected


@pytest.mark.parametrize("visible,password,confirmation,encrypted", [
    (False, "", "", False),
    (False, "generated fixture", "generated fixture", True),
    (True, "generated fixture", "", True),
])
def test_password_field_and_eye_control_actual_archive_encryption(window, tmp_path, visible, password, confirmation, encrypted):
    if not resolve_tool("7z"):
        pytest.skip("Optional 7-Zip is unavailable")
    source = tmp_path / "source.txt"
    source.write_text("generated source")
    window._tool_changed("Archives")
    assert window.format.currentText() == "7z"
    controls = window.archive_controls
    window.start_scan([str(source)])
    wait_until(lambda: not window.busy)
    window.preview_changes()
    wait_until(lambda: not window.busy)
    controls.set_password_visible(visible)
    assert controls.confirm_password.isVisible() == (not visible)
    controls.password.setText(password)
    controls.confirm_password.setText(confirmation)
    if encrypted:
        assert window.last_plan is None  # A password edit must replace the plain preview.
        assert controls.encrypt_names.isChecked()
    window.start_rename_button.click()
    wait_until(lambda: not window.busy and window.jobs_dialog is not None and not window.jobs_dialog.enabled, timeout=30000)
    assert window.result_model.items[0]["state"] == "succeeded", window.status.text()
    output = window.result_model.items[0]["output"]
    if encrypted:
        with pytest.raises(ValueError):
            seven_run(["l", "--", output], None, lambda: False)
    else:
        assert b"source.txt" in seven_run(["l", "--", output], None, lambda: False)
    assert source.read_text() == "generated source"


def test_clearing_password_removes_encryption_and_old_confirmation_is_ignored(window, tmp_path):
    window._tool_changed("Archives")
    window.format.setCurrentText("ZIP")
    controls = window.archive_controls
    controls.password.setText("temporary generated fixture")
    controls.confirm_password.setText("mismatched stale confirmation")
    controls.password.clear()
    source = tmp_path / "source.txt"
    source.write_text("generated source")
    window.start_scan([str(source)])
    wait_until(lambda: not window.busy)
    window.start_rename_button.click()
    wait_until(lambda: not window.busy and window.jobs_dialog is not None and not window.jobs_dialog.enabled, timeout=30000)
    assert window.result_model.items[0]["state"] == "succeeded", window.status.text()
    with zipfile.ZipFile(window.result_model.items[0]["output"]) as archive:
        assert archive.read("source.txt") == source.read_bytes()
        assert not archive.infolist()[0].flag_bits & 1
