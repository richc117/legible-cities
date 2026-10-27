"""bin/preflight's message form never calls clean what it could not read (issue 88)."""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT = ROOT / "bin" / "preflight"

pytestmark = pytest.mark.skipif(os.name == "nt", reason="bin/preflight is a bash script")


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(PREFLIGHT), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_a_missing_message_file_is_refused(tmp_path):
    result = run("--message-file", str(tmp_path / "absent.txt"))
    assert result.returncode == 1
    assert "could not be read" in result.stderr
    assert "clean" not in result.stdout


def test_a_folder_given_as_the_message_file_is_refused(tmp_path):
    result = run("--message-file", str(tmp_path))
    assert result.returncode == 1
    assert "could not be read" in result.stderr
    assert "clean" not in result.stdout


def test_a_clean_message_is_called_clean(tmp_path):
    message = tmp_path / "message.txt"
    message.write_text("Fix a thing\n\nBecause it was broken.\n", encoding="utf-8")
    result = run("--message-file", str(message))
    assert result.returncode == 0
    assert "commit message clean" in result.stdout
