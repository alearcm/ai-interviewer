import shutil

import pytest

from harness.runner import run_command

LOCAL = {"timeout_s": 10.0, "output_max_chars": 1000, "backend": "local"}


def test_local_backend_captures_everything(tmp_path):
    row = run_command("printf out; printf err >&2; exit 3", str(tmp_path), LOCAL)
    assert row["out"] == "out" and row["err"] == "err"
    assert row["exit_status"] == 3
    assert row["cmd"].startswith("printf")


def test_unknown_backend_rejected(tmp_path):
    with pytest.raises(ValueError):
        run_command("true", str(tmp_path), {**LOCAL, "backend": "carrier-pigeon"})


def test_container_backend_without_image_fails_loudly(tmp_path):
    row = run_command("true", str(tmp_path), {**LOCAL, "backend": "container"})
    assert row["exit_status"] != 0
    assert "container_image" in row["err"]
    assert row["cmd"] == "true"  # the candidate's command survives in the record


def test_container_backend_preserves_original_cmd(tmp_path):
    """Whether or not docker exists here, the transcript row must show
    the candidate's command, not the docker wrapper."""
    cfg = {**LOCAL, "backend": "container", "container_image": "busybox"}
    row = run_command("printf sandboxed", str(tmp_path), cfg)
    assert row["cmd"] == "printf sandboxed"
    assert row["backend"] == "container"
    if shutil.which("docker") is None:
        assert row["exit_status"] != 0  # docker absent -> loud failure, not silence
    else:
        pass  # with docker present the run may pass or fail on image pull; shape is what matters


def test_timeout_kills_the_run(tmp_path):
    row = run_command("sleep 30", str(tmp_path), {**LOCAL, "timeout_s": 1.0})
    assert row["exit_status"] != 0
    assert "terminated" in row["err"]
    assert row["duration_ms"] < 10_000
