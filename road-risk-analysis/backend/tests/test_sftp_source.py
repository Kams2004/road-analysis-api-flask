"""
SFTP video source — the pure rules (no server needed): secrets file, remote
path safety, the video filter, the dedupe key, resume offsets, host-key trust
on first use, error classification, the FileZilla-style log lines and the
browse-session pool's retry on a dropped connection.
"""
import base64
import hashlib
import socket
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.modules.m07_ingestion import cloud_import as ci
from app.modules.m07_ingestion import sftp_core as core
from app.modules.m07_ingestion import sftp_source as ss
from app.modules.m07_ingestion.cloud_providers import importable


# ── secrets ──────────────────────────────────────────────────────────────────

def test_parse_env_handles_comments_quotes_export_and_special_chars():
    text = """
# comment
SFTP_PASSWORD_CAMTRACK=p#ss=w0rd with space
export SFTP_PASSWORD_OTHER="quoted value"
SFTP_PASSWORD_SINGLE='x'
not a line
1BAD=no
"""
    v = core.parse_env(text)
    assert v["SFTP_PASSWORD_CAMTRACK"] == "p#ss=w0rd with space"
    assert v["SFTP_PASSWORD_OTHER"] == "quoted value"
    assert v["SFTP_PASSWORD_SINGLE"] == "x"
    assert "1BAD" not in v and len(v) == 3


def test_normalize_key():
    assert core.normalize_key("camtrack") == "CAMTRACK"
    assert core.normalize_key(" cam-track.2 ") == "CAM_TRACK_2"
    # typed as the full variable name: only the suffix is kept
    assert core.normalize_key("SFTP_PASSWORD_CAMTRACK") == "CAMTRACK"
    assert core.normalize_key("sftp_password_camtrack") == "CAMTRACK"
    for bad in ("", "a;b", "x" * 65, "../etc"):
        with pytest.raises(ValueError):
            core.normalize_key(bad)


def test_load_credentials_from_file_env_fallback_and_never_printed(tmp_path):
    (tmp_path / "sftp.env").write_text("SFTP_PASSWORD_CAMTRACK=s3cr3t-value\n")
    creds = core.load_credentials("camtrack", tmp_path, environ={})
    assert creds.method == "password" and creds.password == "s3cr3t-value"
    assert "s3cr3t" not in repr(creds) and "s3cr3t" not in str(creds) and "s3cr3t" not in f"{creds}"
    # environment variable when the file has no entry
    creds = core.load_credentials("TEST", tmp_path, environ={"SFTP_PASSWORD_TEST": "from-env"})
    assert creds.password == "from-env"
    # missing → a readable error naming the line to add, without any value
    with pytest.raises(core.SftpError) as e:
        core.load_credentials("NOPE", tmp_path, environ={})
    assert "SFTP_PASSWORD_NOPE" in str(e.value) and "sftp.env" in str(e.value)
    assert core.credentials_status("camtrack", tmp_path, environ={}) == "password"
    assert core.credentials_status("NOPE", tmp_path, environ={}) is None
    assert core.credentials_status("bad;key", tmp_path, environ={}) is None


def test_key_file_must_stay_inside_the_secrets_dir(tmp_path):
    (tmp_path / "id_ed25519").write_text("KEY")
    (tmp_path / "sftp.env").write_text("SFTP_KEYFILE_A=id_ed25519\nSFTP_KEYFILE_B=../outside\n"
                                       "SFTP_KEYFILE_C=missing\nSFTP_KEY_PASSPHRASE_A=pp\n")
    creds = core.load_credentials("A", tmp_path, environ={})
    assert creds.method == "key" and creds.key_path == tmp_path / "id_ed25519" and creds.passphrase == "pp"
    assert "pp" not in repr(creds).replace("key_path", "")
    with pytest.raises(core.SftpError):
        core.load_credentials("B", tmp_path, environ={})
    with pytest.raises(core.SftpError):
        core.load_credentials("C", tmp_path, environ={})


# ── remote paths ─────────────────────────────────────────────────────────────

def test_norm_remote():
    assert core.norm_remote("/a/b/../c//") == "/a/c"
    assert core.norm_remote("a/b") == "/a/b"
    assert core.norm_remote("/") == "/" and core.norm_remote("") == "/"
    with pytest.raises(core.SftpError):
        core.norm_remote("/a\x00b")


def test_resolve_in_base_refuses_traversal():
    base = "/home/Ymane-New/ft_violations"
    assert core.resolve_in_base(base, None) == base
    assert core.resolve_in_base(base, "root") == base
    assert core.resolve_in_base(base, "video") == f"{base}/video"
    assert core.resolve_in_base(base, f"{base}/video/../video/2026/") == f"{base}/video/2026"
    for bad in ("..", "../other", "/home/Ymane-New", "/etc/passwd", f"{base}/../ft_violations_x",
                f"{base}x/y", "video/../../..", "//etc"):
        with pytest.raises(core.SftpError):
            core.resolve_in_base(base, bad)
    assert core.resolve_in_base("/", "/etc") == "/etc"          # base "/" = the whole server


def test_crumbs_and_relative_dir():
    base = "/home/u/ft"
    assert core.crumbs_below(base, base) == []
    assert core.crumbs_below(base, "/home/u/ft/video/2026") == [("/home/u/ft/video", "video"),
                                                                 ("/home/u/ft/video/2026", "2026")]
    assert core.crumbs_below("/", "/data/x") == [("/data", "data"), ("/data/x", "x")]
    assert core.relative_dir(base, base) is None
    assert core.relative_dir(base, "/home/u/ft/video/2026") == "video/2026"


# ── files ────────────────────────────────────────────────────────────────────

def test_video_filter_case_insensitive_and_entry_importability():
    for ok in ("a.mp4", "B.MOV", "c.Avi", "d.mkv", "e.H264", "f.ts"):
        assert core.is_video_name(ok), ok
    for no in ("a.jpg", "b.webm", "c.mp4.part", "notes.txt", "mp4", ""):
        assert not core.is_video_name(no), no
    t = ss.SftpTarget("c1", "bi.camtrack.mg", 22, "u", None, "CAMTRACK")
    video = ss.to_entry(t, "/v/A.MP4", "A.MP4", False, 1000, 1_760_000_000)
    photo = ss.to_entry(t, "/v/p.jpg", "p.jpg", False, 10, 1_760_000_000)   # images are not taken from SFTP
    folder = ss.to_entry(t, "/v/sub", "sub", True, None, 1_760_000_000)
    assert importable(video) and video.media == "video" and video.mime_type == "video/mp4"
    assert not importable(photo) and photo.media is None
    assert folder.kind == "folder" and not importable(folder)
    assert video.modified == "2025-10-09T08:53:20Z"


def test_dedupe_by_host_path_size_mtime():
    v1 = core.file_version("BI.camtrack.mg", 22, 1000, 1_760_000_000.7)
    assert v1 == "1000:1760000000@bi.camtrack.mg:22"
    assert core.file_version("bi.camtrack.mg", 22, 1001, 1_760_000_000) != v1      # size changed
    assert core.file_version("bi.camtrack.mg", 22, 1000, 1_760_000_001) != v1      # mtime changed
    assert core.file_version("other.host", 22, 1000, 1_760_000_000) != v1           # other server
    assert core.dedupe_key("bi.camtrack.mg", 22, "/a//b.mp4", 1000, 1_760_000_000) == \
        "sftp://bi.camtrack.mg:22/a/b.mp4#1000:1760000000"
    # through the shared import planning: same version skipped, changed file re-imported
    t = ss.SftpTarget("c1", "bi.camtrack.mg", 22, "u", None, "K")
    same = ss.to_entry(t, "/v/a.mp4", "a.mp4", False, 1000, 1_760_000_000)
    changed = ss.to_entry(t, "/v/b.mp4", "b.mp4", False, 2000, 1_760_000_000)
    priors = {"/v/a.mp4": [ci.Prior(v1, "done", "j1")],
              "/v/b.mp4": [ci.Prior(core.file_version("bi.camtrack.mg", 22, 1999, 1_760_000_000), "done", "j2")]}
    plan = {p.entry.id: p for p in ci.plan_items([same, changed], priors)}
    assert plan["/v/a.mp4"].status == "skipped_duplicate" and plan["/v/a.mp4"].duplicate_of_job_id == "j1"
    assert plan["/v/b.mp4"].status == "queued"


# ── resume ───────────────────────────────────────────────────────────────────

def test_resume_offset():
    assert core.resume_offset(0, 1000) == 0
    assert core.resume_offset(400, 1000) == 400
    assert core.resume_offset(1000, 1000) == 1000          # already complete
    assert core.resume_offset(1200, 1000) == 0             # remote file shrank/changed → start over
    assert core.resume_offset(300, None) == 300


def test_retry_count_resets_when_bytes_progressed():
    assert ci.resume_retry_count(3, 5000, 4000) == 0       # last attempt moved forward
    assert ci.resume_retry_count(3, 4000, 4000) == 3       # no progress → keep counting
    assert ci.resume_retry_count(0, 100, None) == 0
    assert [core.retry_delay_s(i) for i in range(6)] == [2, 4, 8, 16, 30, 30]


# ── host key (trust on first use) ────────────────────────────────────────────

def test_fingerprint_matches_openssh_format():
    blob = b"\x00\x00\x00\x0bssh-ed25519" + bytes(range(36))
    b64 = base64.b64encode(blob).decode()
    expected = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    assert core.fingerprint(b64) == expected and not expected.endswith("=")


def test_host_key_decision_and_persistence():
    k1 = "ssh-ed25519 " + base64.b64encode(b"key-one").decode()
    k2 = "ssh-ed25519 " + base64.b64encode(b"key-two").decode()
    assert core.host_key_decision(None, k1) == "new"
    assert core.host_key_decision(k1, k1) == "match"
    assert core.host_key_decision(k1, k2) == "mismatch"
    assert core.host_key_decision(k1, "garbage") == "mismatch"
    msg = core.mismatch_message("bi.camtrack.mg", k1, k2)
    assert "mismatch" in msg and "Forget host key" in msg and core.fingerprint(k2.split()[1]) in msg
    conn = SimpleNamespace(sftp_host_key=None, sftp_host_key_fp=None, sftp_host_key_at=None)
    t = ss.SftpTarget("c1", "h", 22, "u", None, "K", learned_host_key=k1)
    assert ss.remember_host_key(conn, t) is True
    assert conn.sftp_host_key == k1 and conn.sftp_host_key_fp.startswith("ssh-ed25519 SHA256:")
    assert ss.remember_host_key(conn, ss.SftpTarget("c1", "h", 22, "u", None, "K", learned_host_key=k2)) is False
    assert conn.sftp_host_key == k1                        # never overwritten silently


# ── errors ───────────────────────────────────────────────────────────────────

class AuthenticationException(Exception):
    pass


class SSHException(Exception):
    pass


def test_classify_errors():
    assert core.classify(EOFError()) == ("dropped", core.DROPPED, True)
    assert core.classify(ConnectionResetError())[0] == "dropped"
    assert core.classify(SSHException("Error reading SSH protocol banner"))[1] == core.DROPPED
    assert core.classify(socket.timeout())[0:3:2] == ("timeout", True)
    assert core.classify(ConnectionRefusedError())[1] == "Could not connect to server: Connection refused"
    kind, msg, transient = core.classify(AuthenticationException("Authentication failed."))
    assert kind == "auth" and not transient and "Authentication failed" in msg
    assert core.classify(core.HostKeyMismatch("x"))[0] == "host_key"
    assert core.classify(FileNotFoundError(2, "No such file", "/x.mp4"))[0] == "not_found"
    assert core.classify(socket.gaierror(-2, "Name or service not known"), "nohost")[1].startswith(
        'Could not resolve host "nohost"')


# ── connection log ───────────────────────────────────────────────────────────

def test_log_lines_and_formatting():
    ts = datetime(2026, 10, 20, 14, 2, 11, 204000, tzinfo=timezone.utc)
    e = core.log_entry("status", "Connecting   to bi.camtrack.mg...", seq=7, ts=ts)
    assert e == {"seq": 7, "ts": "2026-10-20T14:02:11.204Z", "level": "status",
                 "message": "Connecting to bi.camtrack.mg...", "source": "api"}
    assert core.format_line(e) == "2026-10-20 14:02:11  Status: Connecting to bi.camtrack.mg..."
    err = core.log_entry("error", "Authentication failed.", ts=ts)
    assert core.format_line(err) == "2026-10-20 14:02:11  Error:  Authentication failed."
    assert core.log_entry("weird", "x")["level"] == "status"
    # a secret never reaches the log
    assert "hunter22" not in core.log_entry("error", "bad hunter22 here", secrets=("hunter22",))["message"]
    assert core.human_size(468_480) == "457.5 KB" and core.human_size(900) == "900 bytes"
    assert core.transfer_summary(468_480, 6.6) == "File transfer successful, transferred 457.5 KB in 7 seconds"
    assert core.transfer_summary(10, 0.2).endswith("in 1 second")


def test_sftp_log_without_redis_keeps_lines_and_hides_secrets():
    log = ss.SftpLog("c1", redis=None)
    log.hide("p@ssw0rd!")
    log.status("Using username \"roadusertoread\".")
    log.error("Authentication failed for p@ssw0rd!")
    assert [x["level"] for x in log.lines] == ["status", "error"]
    assert log.lines[1]["message"] == "Authentication failed for ********"
    log.error("Error: Could not connect to server")
    assert log.lines[2]["message"] == "Could not connect to server"


def test_effective_link_state():
    assert core.effective_state("connected", 1000.0, 1100.0) == "connected"
    assert core.effective_state("connected", 1000.0, 1000.0 + core.IDLE_CLOSE_S + 10) == "disconnected"
    assert core.effective_state("error", 0.0, 1e9) == "error"
    assert core.effective_state(None, None, 0) == "disconnected"
    assert ss.link_state({})["state"] == "disconnected"


# ── pooled browse session: reconnect + retry on a dropped connection ─────────

class FakeSession:
    opened = 0
    fail_opens = 0

    def __init__(self, target, log):
        self.target, self.log, self.transport, self.base = target, log, None, "/home/u"

    def open(self):
        FakeSession.opened += 1
        if FakeSession.fail_opens:
            FakeSession.fail_opens -= 1
            raise core.SftpTransientError("Could not connect to server: Connection refused")
        self.transport = object()
        return self

    def alive(self):
        return self.transport is not None

    def close(self, quiet=False):
        self.transport = None


def test_pool_reuses_session_and_retries_after_drop(monkeypatch):
    monkeypatch.setattr(ss, "SftpSession", FakeSession)
    FakeSession.opened, FakeSession.fail_opens = 0, 1
    sleeps = []
    pool = ss.SessionPool(sleep=sleeps.append)
    log = ss.SftpLog("c1")
    t = ss.SftpTarget("c1", "h", 22, "u", None, "K")
    calls = {"n": 0}

    def op(s):
        calls["n"] += 1
        if calls["n"] == 1:
            raise EOFError()                               # server closed the channel mid-listing
        return s.base

    assert pool.run(t, log, op) == "/home/u"
    assert sleeps == [2.0, 4.0]                            # refused once, dropped once → two backoffs
    assert any(core.DROPPED in x["message"] for x in log.lines)
    assert any(x["message"].startswith("Retrying in") for x in log.lines)
    opened = FakeSession.opened
    assert pool.run(t, log, lambda s: "again") == "again"
    assert FakeSession.opened == opened                    # the live session is reused
    # permanent errors are not retried
    with pytest.raises(core.SftpError):
        pool.run(t, log, lambda s: (_ for _ in ()).throw(core.SftpError("outside base")))
    assert pool.close("c1") is False
