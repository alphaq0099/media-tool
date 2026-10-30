import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import zipfile

import pytest
from PIL import Image

from media_tool.domain import FileRecord
from media_tool.jobs import SCHEMA, Store, execute_job
from media_tool.planning import PlanRequest
from media_tool.zip_plan import plan_archives, archive_snapshot
from media_tool.seven_archive import remember_password, forget_password, seven_run
from media_tool.tool_paths import resolve_tool
from test_foundation import app, window, wait_until


def comic_tree(tmp_path):
    folder = tmp_path / "Book"
    folder.mkdir()
    for number, ext in ((10, "jpg"), (2, "png"), (1, "webp")):
        Image.new("RGB", (24, 32), (number, 40, 80)).save(folder / f"page{number}.{ext}")
    (folder / "notes.txt").write_text("not a comic page")
    return folder


def preview(paths, **options):
    return plan_archives([FileRecord.from_path(p) for p in paths], PlanRequest("archives", **options))


def execute(tmp_path, plan, password=None, **options):
    specs = archive_snapshot(plan)
    token = remember_password(password) if password is not None else None
    if token:
        specs = tuple(dict(s, password_token=token) for s in specs)
    store = Store(tmp_path / "history")
    try:
        job = store.enqueue("Archives", "archives", specs)
        execute_job(store, job, **options)
        return store.items(job)
    finally:
        store.close()
        if token:
            forget_password(token)


@pytest.mark.parametrize("numbered", [False, True])
def test_comic_order_mixed_images_nonimages_and_unchanged_bytes(tmp_path, numbered):
    folder = comic_tree(tmp_path)
    original = {p.name: p.read_bytes() for p in folder.iterdir()}
    plan = preview([folder], archive_format="cbz", archive_number_pages=numbered)
    assert plan.counts == {"Planned": 1}
    spec = plan.items[0].archive_spec
    assert spec["file_count"] == 3 and spec["manifest"]["excluded"] == ["Book/notes.txt"]
    entries = spec["manifest"]["entries"]
    assert [Path(e["source"]).stem for e in entries] == ["page1", "page2", "page10"]
    expected = ["0001.webp", "0002.png", "0003.jpg"] if numbered else ["Book/page1.webp", "Book/page2.png", "Book/page10.jpg"]
    assert [e["name"] for e in entries] == expected
    result = execute(tmp_path, plan)[0]
    assert result["state"] == "succeeded", result
    with zipfile.ZipFile(plan.items[0].output) as archive:
        assert archive.namelist() == expected
        assert [archive.read(name) for name in expected] == [original[Path(e["source"]).name] for e in entries]
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == original


def test_comic_nested_duplicate_pages_and_invalid_image(tmp_path):
    folder = comic_tree(tmp_path)
    nested = folder / "chapter2"
    nested.mkdir()
    Image.new("RGB", (16, 16)).save(nested / "page1.png")
    plan = preview([folder], archive_format="cbz")
    assert plan.items[0].archive_spec["file_count"] == 4
    assert len({e["name"] for e in plan.items[0].archive_spec["manifest"]["entries"]}) == 4
    (nested / "broken.jpg").write_bytes(b"not an image")
    assert preview([folder], archive_format="cbz").counts == {"Invalid": 1}


@pytest.mark.parametrize("kind,encrypt,headers", [("7z", False, False), ("7z", True, True), ("7z", True, False), ("zip", True, False)])
def test_seven_roundtrip_encryption_and_secret_not_in_history(tmp_path, kind, encrypt, headers):
    if not resolve_tool("7z"):
        pytest.skip("7-Zip is optional")
    folder = comic_tree(tmp_path)
    (folder / "empty").mkdir()
    (folder / "zero.txt").write_bytes(b"")
    plan = preview([folder], archive_format=kind, archive_encrypt=encrypt, archive_encrypt_names=headers)
    assert plan.counts == {"Planned": 1}
    password = 'fixture only "quotes" $ & ; [] \\ secret'
    result = execute(tmp_path, plan, password if encrypt else None)[0]
    assert result["state"] == "succeeded", result
    assert not list(tmp_path.glob(".mediatool-*"))
    if encrypt:
        with pytest.raises(ValueError):
            seven_run(["t", "-p", "--", plan.items[0].output], "wrong password", lambda: False)
    assert password not in result["spec"] and password not in result["message"]
    assert password.encode() not in (tmp_path / "history" / "history.sqlite3").read_bytes()


def test_missing_dependency_and_encrypted_cbz_are_explicit(tmp_path, monkeypatch):
    folder = comic_tree(tmp_path)
    monkeypatch.setattr("media_tool.tool_paths.resolve_tool", lambda name: None)
    with pytest.raises(ValueError, match="7-Zip"):
        preview([folder], archive_format="7z")
    with pytest.raises(ValueError, match="7-Zip"):
        preview([folder], archive_encrypt=True)
    with pytest.raises(ValueError, match="CBZ"):
        preview([folder], archive_format="cbz", archive_encrypt=True)


def test_ui_comic_cover_and_encryption_controls(window, tmp_path):
    folder = comic_tree(tmp_path)
    window._tool_changed("Archives")
    window.format.setCurrentText("CBZ")
    assert window.archive_controls.number_pages.isVisible()
    assert not window.archive_controls.password.isVisible()
    window.start_scan([str(folder)])
    wait_until(lambda: not window.busy)
    assert window.preview_changes()
    wait_until(lambda: not window.busy)
    window.archive_controls.show_contents()
    dialog = window.archive_contents_dialog
    assert dialog.windowTitle() == "Comic pages"
    from PySide6.QtWidgets import QLabel
    assert any(not label.pixmap().isNull() for label in dialog.findChildren(QLabel))
    dialog.close()
    window.format.setCurrentText("7z")
    window.archive_controls.password.setText("generated UI password")
    assert window.archive_controls.password.isVisible()
    assert window.archive_controls.encrypt_names.isVisible()


@pytest.mark.parametrize("point", ["chunk", "written", "verify_chunk", "prepared"])
def test_encrypted_cancel_retry_and_recovery(tmp_path, point):
    if not resolve_tool("7z"):
        pytest.skip("7-Zip is optional")
    folder = comic_tree(tmp_path)
    plan = preview([folder], archive_format="7z", archive_encrypt=True)
    token = remember_password("generated retry fixture")
    store = Store(tmp_path / "history")
    cancel = threading.Event()
    try:
        job = store.enqueue("Encrypted", "archives", [dict(s, password_token=token) for s in archive_snapshot(plan)])
        execute_job(store, job, cancelled=cancel.is_set, hook=lambda p: cancel.set() if p == point else None)
        assert store.items(job)[0]["state"] == "cancelled"
        assert not Path(plan.items[0].output).exists() and not list(tmp_path.glob(".mediatool-*"))
        assert store.retry(job) == 1
        execute_job(store, job)
        assert store.items(job)[0]["state"] == "succeeded", store.items(job)[0]["message"]
    finally:
        store.close()
        forget_password(token)


@pytest.mark.parametrize("point", ["chunk", "committed"])
def test_actual_encrypted_process_loss_cleans_owned_staging_or_recovers_commit(tmp_path, point):
    if not resolve_tool("7z"):
        pytest.skip("7-Zip is optional")
    folder = comic_tree(tmp_path)
    plan = preview([folder], archive_format="7z", archive_encrypt=True)
    token = remember_password("generated process fixture")
    store = Store(tmp_path / "history")
    try:
        job = store.enqueue("Encrypted", "archives", [dict(s, password_token=token) for s in archive_snapshot(plan)])
        code = """import os,sys
from media_tool.jobs import Store,execute_job
from media_tool.seven_archive import remember_password
store=Store(sys.argv[1])
remember_password('generated process fixture',sys.argv[4])
execute_job(store,sys.argv[2],hook=lambda p: os._exit(73) if p==sys.argv[3] else None)
"""
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        result = subprocess.run([sys.executable, "-c", code, str(store.directory), job, point, token],
            env=env, capture_output=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        assert result.returncode == 73, result.stderr
        assert store.recover() == store.retry(job) == 1
        execute_job(store, job)
        assert store.items(job)[0]["state"] == "succeeded", store.items(job)[0]["message"]
        assert not list(tmp_path.glob(".mediatool-*"))
        if point == "committed":
            assert "Recovered" in store.items(job)[0]["message"]
    finally:
        store.close()
        forget_password(token)


def test_unicode_password_and_plaintext_file_listing_policy(tmp_path):
    if not resolve_tool("7z"):
        pytest.skip("7-Zip is optional")
    folder = comic_tree(tmp_path)
    password = "Unicode \u65e5\u672c \u20ac \U0001f512"
    plan = preview([folder], archive_format="7z", archive_encrypt=True, archive_encrypt_names=True)
    item = execute(tmp_path, plan, password)[0]
    assert item["state"] == "succeeded", item["message"]
    with pytest.raises(ValueError):
        seven_run(["l", "--", plan.items[0].output], None, lambda: False)


def test_schema15_zip_job_runs_after_encrypted_archive_upgrade(tmp_path):
    folder = comic_tree(tmp_path)
    plan = preview([folder])
    store = Store(tmp_path / "history")
    job = store.enqueue("Old ZIP", "archives", archive_snapshot(plan))
    # Strip new fields to match an existing 7A snapshot.
    for row in store.items(job):
        spec = json.loads(row["spec"])
        for key in ("archive_format", "encrypted", "encrypt_names"):
            spec.pop(key, None)
        store.db.execute("UPDATE items SET spec=? WHERE id=?", (json.dumps(spec), row["id"]))
    store.db.execute("PRAGMA user_version=15")
    store.db.commit()
    store.close()
    store = Store(tmp_path / "history")
    try:
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA
        execute_job(store, job)
        assert store.items(job)[0]["state"] == "succeeded"
    finally:
        store.close()


def test_saved_encrypted_job_requires_password_after_session_loss(tmp_path):
    if not resolve_tool("7z"):
        pytest.skip("7-Zip is optional")
    folder = comic_tree(tmp_path)
    plan = preview([folder], archive_format="7z", archive_encrypt=True)
    token = remember_password("session fixture")
    store = Store(tmp_path / "history")
    try:
        job = store.enqueue("Encrypted", "archives", [dict(s, password_token=token) for s in archive_snapshot(plan)])
        forget_password(token)
        execute_job(store, job)
        assert "Password required" in store.items(job)[0]["message"]
        assert not Path(plan.items[0].output).exists()
        remember_password("session fixture", token)
        store.retry(job)
        execute_job(store, job)
        assert store.items(job)[0]["state"] == "succeeded"
    finally:
        store.close()
        forget_password(token)


def test_ui_password_confirmation_and_saved_queue_password_prompt(window, tmp_path, monkeypatch):
    if not resolve_tool("7z"):
        pytest.skip("7-Zip is optional")
    source = tmp_path / "private.txt"
    source.write_text("generated private source")
    window._tool_changed("Archives")
    window.format.setCurrentText("7z")
    controls = window.archive_controls
    controls.password.setText("generated password")
    controls.confirm_password.setText("different password")
    window.start_scan([str(source)])
    wait_until(lambda: not window.busy, timeout=30000)
    window.preview_changes()
    wait_until(lambda: not window.busy, timeout=30000)
    window.start_rename_button.click()
    assert "do not match" in window.status.text()
    assert window.jobs_dialog.store.jobs() == []
    controls.confirm_password.setText("generated password")
    window.run_button.click()
    queue = window.jobs_dialog
    job = queue.store.jobs()[-1]
    token = json.loads(queue.store.items(job["id"])[0]["spec"])["password_token"]
    assert not controls.password.text() and not controls.confirm_password.text()
    forget_password(token)
    from PySide6.QtWidgets import QInputDialog
    asked = []
    def enter(*args):
        asked.append(True)
        return "generated password", True
    monkeypatch.setattr(QInputDialog, "getText", enter)
    queue.start_single(job["id"])
    wait_until(lambda: not window.busy and not queue.enabled, timeout=30000)
    assert asked == [True]
    assert queue.store.items(job["id"])[0]["state"] == "succeeded"


@pytest.mark.parametrize("kind,level", [("7z", 0), ("7z", 1), ("7z", 9), ("zip", 0), ("zip", 9)])
def test_encryption_with_supported_compression_levels(tmp_path, kind, level):
    if not resolve_tool("7z"):
        pytest.skip("7-Zip is optional")
    source = tmp_path / "bytes.txt"
    source.write_bytes(b"generated compression fixture\n" * 50)
    plan = preview([source], archive_format=kind, archive_level=level, archive_encrypt=True)
    item = execute(tmp_path, plan, "generated compression password")[0]
    assert item["state"] == "succeeded", item["message"]


def test_replaced_staging_folder_is_never_removed(tmp_path):
    from media_tool.jobs import identity
    from media_tool.seven_archive import remove_stage
    source = tmp_path / "source.txt"
    source.write_text("source")
    plan = preview([source])
    store = Store(tmp_path / "history")
    try:
        job = store.enqueue("Archive", "archives", archive_snapshot(plan))
        item = store.items(job)[0]
        root = tmp_path / f".mediatool-stage-{item['id']}-fixture"
        root.mkdir()
        saved = dict(path=str(root), identity=identity(root.stat())[:2])
        store.set_item(item["id"], "running", stage=json.dumps(saved))
        backup = tmp_path / "original-staging"
        assert root.resolve().parent == backup.resolve().parent == tmp_path.resolve()
        root.rename(backup)
        root.mkdir()
        (root / "foreign.txt").write_text("must remain")
        with pytest.raises(ValueError, match="replaced"):
            remove_stage(store, store.item(item["id"]))
        assert (root / "foreign.txt").read_text() == "must remain" and backup.exists()
    finally:
        store.close()


@pytest.mark.parametrize("kind", ["zip", "7z"])
def test_encrypted_staging_preserves_original_modified_times(tmp_path, kind):
    if not resolve_tool("7z"):
        pytest.skip("7-Zip is optional")
    from datetime import datetime
    source = tmp_path / "old.txt"
    source.write_text("old source fixture")
    os.utime(source, (1600000000, 1600000000))
    expected = datetime.fromtimestamp(source.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    plan = preview([source], archive_format=kind, archive_encrypt=True)
    result = execute(tmp_path, plan, "generated timestamp password")[0]
    assert result["state"] == "succeeded", result["message"]
    listing = seven_run(["l", "-slt", "-ba", "-p", "--", plan.items[0].output], "generated timestamp password", lambda: False)
    assert ("Modified = " + expected).encode() in listing
