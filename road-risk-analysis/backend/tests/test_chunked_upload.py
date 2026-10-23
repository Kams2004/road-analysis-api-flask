"""Chunked (resumable) uploads — app/modules/m07_ingestion/chunked_upload.py."""
import os
import time

import pytest

from app.modules.m07_ingestion import chunked_upload as cu


@pytest.fixture
def small_chunks(monkeypatch):
    monkeypatch.setattr(cu, "CHUNK_SIZE", 4)


def test_chunks_in_any_order_and_retried_rebuild_the_file(tmp_path, small_chunks):
    data = b"0123456789"                       # 3 chunks: 4 + 4 + 2
    up = cu.create(tmp_path, "a" * 32, "clip.mp4", len(data), "user@x")
    assert up.total_chunks == 3 and up.missing() == [0, 1, 2]
    cu.write_chunk(up, 2, data[8:])
    cu.write_chunk(up, 0, data[:4])
    cu.write_chunk(up, 0, data[:4])            # retry of the same chunk
    assert up.missing() == [1]
    with pytest.raises(cu.UploadError) as e:
        cu.take_file(up, tmp_path / "out.mp4")
    assert e.value.code == "upload_incomplete"
    cu.write_chunk(up, 1, data[4:8])
    out = cu.take_file(up, tmp_path / "out.mp4")
    assert out.read_bytes() == data and not up.dir.exists()


def test_wrong_chunk_size_and_index_are_refused(tmp_path, small_chunks):
    up = cu.create(tmp_path, "b" * 32, "clip.mp4", 10, "user@x")
    for index, body in ((0, b"123"), (2, b"1234"), (3, b"12"), (-1, b"1234")):
        with pytest.raises(cu.UploadError):
            cu.write_chunk(up, index, body)


def test_only_the_owner_can_use_an_upload(tmp_path):
    cu.create(tmp_path, "c" * 32, "clip.mp4", 10, "user@x")
    assert cu.load(tmp_path, "c" * 32, "user@x").size == 10
    for uid, owner in (("c" * 32, "other@x"), ("../etc", "user@x"), ("d" * 32, "user@x")):
        with pytest.raises(cu.UploadError) as e:
            cu.load(tmp_path, uid, owner)
        assert e.value.status == 404


def test_limits_and_stale_uploads(tmp_path):
    with pytest.raises(cu.UploadError):
        cu.create(tmp_path, "e" * 32, "x.mp4", 0, "u")
    with pytest.raises(cu.UploadError):
        cu.create(tmp_path, "f" * 32, "x.mp4", cu.MAX_FILE_SIZE + 1, "u")
    old = cu.create(tmp_path, "1" * 32, "x.mp4", 5, "u")
    past = time.time() - cu.STALE_AFTER_S - 10
    os.utime(old.dir, (past, past))
    cu.create(tmp_path, "2" * 32, "x.mp4", 5, "u")
    assert cu.sweep(tmp_path) == 1
    assert not old.dir.exists() and (cu.root(tmp_path) / ("2" * 32)).exists()
