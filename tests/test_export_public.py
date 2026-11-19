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
