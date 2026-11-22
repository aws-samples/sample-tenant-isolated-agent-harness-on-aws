# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""scripts/export_public.sh: one clean commit, or a refusal that names every internal reference."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "export_public.sh"
pytestmark = pytest.mark.skipif(not (shutil.which("bash") and shutil.which("git")), reason="needs bash and git")
PUBLISHER = {"GIT_AUTHOR_NAME": "Example Author", "GIT_AUTHOR_EMAIL": "author@example.com",
             "GIT_COMMITTER_NAME": "Example Author", "GIT_COMMITTER_EMAIL": "author@example.com"}
# Built from parts so that this file passes the check it tests.
ACCOUNT = "2109" + "87654321"
WORK_EMAIL = "someone@" + "amazon.com"


def _env(**overrides):
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", **PUBLISHER)
    env.update(overrides)
    return env


def _repo(root: Path, files: dict[str, str]) -> Path:
    (root / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, root / "scripts" / "export_public.sh")
    (root / ".gitignore").write_text(".export-denylist\n")
    (root / ".export-denylist").write_text("# private patterns\n\nexample-internal-host\n")
    for name, text in files.items():
        (root / name).write_text(text)
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "work"]):
        subprocess.run(["git", *args], cwd=root, env=_env(), check=True)
    return root


def _export(repo: Path, dest: Path | str, cwd: Path | None = None, **env) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(repo / "scripts" / "export_public.sh"), str(dest)],
                          cwd=cwd, env=_env(**env), capture_output=True, text=True)


def test_export_is_one_clean_commit_and_refuses_internal_references(tmp_path):
    public = {"README.md": "arn:aws:iam::123456789012:role/x, https://docs.aws.amazon.com/iam/ and "
                           "opensource-codeofconduct@amazon.com\n"}
    repo = _repo(tmp_path / "public", public)
    done = _export(repo, tmp_path / "out")
    assert done.returncode == 0, done.stderr
    log = subprocess.run(["git", "log", "--format=%an <%ae> on %D"], cwd=tmp_path / "out", env=_env(),
                         capture_output=True, text=True, check=True)
    assert log.stdout.split("\n")[:-1] == ["Example Author <author@example.com> on HEAD -> main"]
    assert not (tmp_path / "out" / ".export-denylist").exists()  # git-ignored, so never exported
    relative = _export(repo, "out-relative", cwd=tmp_path)  # resolved against the caller's directory
    assert relative.returncode == 0 and (tmp_path / "out-relative" / "README.md").exists(), relative.stderr

    leaky = _repo(tmp_path / "leaky", {**public, "notes.md": f"| {ACCOUNT} |\nsee example-internal-host\n{WORK_EMAIL}\n"})
    refused = _export(leaky, tmp_path / "out-leaky")
    assert refused.returncode == 1 and not (tmp_path / "out-leaky").exists()
    for hit in (f"./notes.md:1:{ACCOUNT}", "./notes.md:2:example-internal-host", f"./notes.md:3:{WORK_EMAIL}"):
        assert hit in refused.stderr

    work_identity = _export(repo, tmp_path / "out-identity", GIT_AUTHOR_EMAIL=WORK_EMAIL)
    assert work_identity.returncode == 1 and f"commit-identity:1:{WORK_EMAIL}" in work_identity.stderr
    assert not (tmp_path / "out-identity").exists()


def test_a_joined_account_id_is_caught_but_longer_digit_runs_are_not(tmp_path):
    # An account ID glued to letters or underscores must be caught (grep -w would miss it), while a
    # 13- or 16-digit run must not. Every digit value is built at runtime so no literal 12-digit ID
    # sits in this file.
    joined = f"acct_{ACCOUNT}_prod"
    thirteen = ACCOUNT + "3"       # a 13-digit run
    sixteen = ACCOUNT + "3456"     # a 16-digit run
    repo = _repo(tmp_path / "ids", {"notes.md": f"{joined}\nthirteen {thirteen}\nsixteen {sixteen}\n"})
    refused = _export(repo, tmp_path / "out-ids")
    assert refused.returncode == 1 and not (tmp_path / "out-ids").exists()
    assert f"./notes.md:1:{ACCOUNT}" in refused.stderr          # the joined ID, reported as 12 digits
    assert thirteen not in refused.stderr and sixteen not in refused.stderr  # longer runs are not flagged


def test_a_crlf_denylist_still_catches_a_planted_token(tmp_path):
    # A denylist saved with CRLF endings used to leave a trailing \r on every pattern, so it matched
    # nothing while the script still reported success. The pattern must now catch a planted token.
    marker = "secret-internal-marker"
    repo = _repo(tmp_path / "crlf", {"notes.md": f"see {marker} here\n"})
    (repo / ".export-denylist").write_bytes(f"# private\r\n{marker}\r\n".encode())  # .gitignored, read fresh
    refused = _export(repo, tmp_path / "out-crlf")
    assert refused.returncode == 1 and not (tmp_path / "out-crlf").exists()
    assert f"./notes.md:1:{marker}" in refused.stderr


def test_a_denylist_with_no_usable_pattern_is_refused(tmp_path):
    # Only comments, blanks and whitespace (CRLF included) yield no pattern: the script refuses
    # rather than run a leak check that would match nothing and pass vacuously.
    repo = _repo(tmp_path / "empty-deny", {"notes.md": "nothing to see\n"})
    (repo / ".export-denylist").write_bytes(b"# only a comment\r\n\r\n   \r\n")
    refused = _export(repo, tmp_path / "out-empty-deny")
    assert refused.returncode == 1 and not (tmp_path / "out-empty-deny").exists()
    assert "no usable patterns" in refused.stderr


def test_a_non_utf8_denylist_is_refused(tmp_path):
    # A Latin-1 / Windows-1252 byte (e.g. an accented name saved by legacy Windows) makes the
    # denylist invalid UTF-8. It must be refused, not partly read: the byte tools would otherwise
    # abort mid-stream and drop every later pattern while the leak check still passed.
    repo = _repo(tmp_path / "latin1", {"notes.md": "nothing to see\n"})
    (repo / ".export-denylist").write_bytes(b"caf\xe9-internal-host\n")  # 0xe9 is Latin-1 e-acute
    refused = _export(repo, tmp_path / "out-latin1")
    assert refused.returncode == 1 and not (tmp_path / "out-latin1").exists()
    assert "not valid UTF-8" in refused.stderr


def test_a_bom_denylist_catches_its_pattern_and_a_bom_comment_only_file_is_refused(tmp_path):
    bom = b"\xef\xbb\xbf"
    marker = "bom-internal-marker"
    # A UTF-8 byte-order mark on line 1 must not hide the first pattern.
    repo = _repo(tmp_path / "bom", {"notes.md": f"see {marker}\n"})
    (repo / ".export-denylist").write_bytes(bom + f"{marker}\n".encode())
    refused = _export(repo, tmp_path / "out-bom")
    assert refused.returncode == 1 and f"./notes.md:1:{marker}" in refused.stderr
    # A byte-order mark followed only by comments still yields no usable pattern.
    repo2 = _repo(tmp_path / "bomc", {"notes.md": "clean\n"})
    (repo2 / ".export-denylist").write_bytes(bom + b"# only a comment\n")
    refused2 = _export(repo2, tmp_path / "out-bomc")
    assert refused2.returncode == 1 and "no usable patterns" in refused2.stderr
