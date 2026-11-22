# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tenant workspace = a prefix in the shared bucket, cached in this session's storage.

The usual pattern keeps the permanent workspace on a shared AgentCore S3 Files mount at
``/mnt/persistent/customers/{customerStorageId}``. An S3 Files access point configured on an
AgentCore Runtime is mounted into *every* session of that runtime with one POSIX identity, so
any tenant's script could ``ls /mnt/persistent/customers/`` and read the others. This module
keeps the same "permanent files on a filesystem" experience without a shared mount:

* the source of truth is ``tenants/{sid}/workspace/`` in the shared bucket, reachable only with
  this tenant's short-lived runtime credentials;
* the working copy lives in AgentCore session storage (``/mnt/session``), which AgentCore
  isolates per session, and the session ID is per tenant;
* sync is incremental: an ETag/mtime manifest means a warm or resumed session downloads only
  what changed, and a reset session (14-day idle or a runtime version update) re-hydrates.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

MAX_FILES = 10_000
MAX_BYTES = 900 * 1024 * 1024  # session storage is capped at 1 GB per session


class WorkspaceError(Exception):
    pass


def safe_relative(rel: str) -> str:
    if (not rel or len(rel) > 1024 or rel.startswith("/") or "\\" in rel
            or any(part in ("", ".", "..") for part in rel.split("/"))
            or any(ord(c) < 32 for c in rel)):
        raise WorkspaceError(f"unsafe workspace path: {rel!r}")
    return rel


def safe_join(root: Path, rel: str) -> Path:
    target = (root / safe_relative(rel)).resolve()
    if not str(target).startswith(str(root.resolve()) + os.sep):
        raise WorkspaceError(f"path escapes workspace: {rel!r}")
    return target


class WorkspaceSync:
    def __init__(self, s3: Any, bucket: str, prefix: str, local_dir: Path, state_dir: Path,
                 max_files: int = MAX_FILES, max_bytes: int = MAX_BYTES):
        self._s3, self._bucket, self._prefix = s3, bucket, prefix
        self._dir, self._max_files, self._max_bytes = local_dir, max_files, max_bytes
        self._manifest_path = state_dir / "workspace-manifest.json"
        # The data key's policy refuses an object whose key names this tenant's own prefix again
        # further down (infra/policies.py, s3_object_context), so such a path is neither saved nor
        # fetched. Failing the sync instead would fail every later task of this session.
        sid = prefix.split("/")[1] if prefix.startswith("tenants/") else ""
        self._refused_markers = (f"/tenants/{sid}/", f"/transfer/{sid}/") if sid else ()
        local_dir.mkdir(parents=True, exist_ok=True)
        state_dir.mkdir(parents=True, exist_ok=True)

    def _key_policy_refuses(self, rel: str) -> bool:
        return any(marker in f"/{rel}" for marker in self._refused_markers)

    def _load(self) -> dict:
        try:
            return json.loads(self._manifest_path.read_text()).get("objects", {})
        except (FileNotFoundError, ValueError):
            return {}

    def _save(self, objects: dict) -> None:
        tmp = self._manifest_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"objects": objects}))
        tmp.replace(self._manifest_path)

    def _remote(self) -> dict[str, dict]:
        remote: dict[str, dict] = {}
        total = 0
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=self._prefix):
            for obj in page.get("Contents", []):
                rel = obj["Key"][len(self._prefix):]
                if not rel or rel.endswith("/") or self._key_policy_refuses(rel):
                    continue
                remote[safe_relative(rel)] = {"etag": obj["ETag"].strip('"'), "size": obj["Size"]}
                total += obj["Size"]
                if len(remote) > self._max_files or total > self._max_bytes:
                    raise WorkspaceError("workspace exceeds the session storage budget")
        return remote

    def pull(self) -> int:
        remote, manifest = self._remote(), self._load()
        downloaded = 0
        for rel, meta in remote.items():
            local = safe_join(self._dir, rel)
            entry = manifest.get(rel)
            if entry and entry["etag"] == meta["etag"] and local.is_file():
                stat = local.stat()
                if stat.st_size == entry["size"] and stat.st_mtime_ns == entry["mtime_ns"]:
                    continue
            local.parent.mkdir(parents=True, exist_ok=True)
            self._s3.download_file(self._bucket, self._prefix + rel, str(local))
            stat = local.stat()
            manifest[rel] = {"etag": meta["etag"], "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
            downloaded += 1
        for rel in [r for r in manifest if r not in remote]:
            try:
                safe_join(self._dir, rel).unlink()
            except FileNotFoundError:
                pass
            del manifest[rel]
        self._save(manifest)
        return downloaded

    def push(self) -> dict:
        manifest = self._load()
        seen: set[str] = set()
        not_saved: list[str] = []
        uploaded = total = 0
        for dirpath, dirnames, filenames in os.walk(self._dir, followlinks=False):
            dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
            for name in filenames:
                path = Path(dirpath) / name
                if path.is_symlink() or not path.is_file():
                    continue  # symlinks never leave the session
                rel = safe_relative(path.relative_to(self._dir).as_posix())
                seen.add(rel)
                stat = path.lstat()
                total += stat.st_size
                if len(seen) > self._max_files or total > self._max_bytes:
                    raise WorkspaceError("workspace exceeds the session storage budget")
                if self._key_policy_refuses(rel):
                    not_saved.append(rel)
                    continue
                entry = manifest.get(rel)
                if entry and entry["size"] == stat.st_size and entry["mtime_ns"] == stat.st_mtime_ns:
                    continue
                with open(path, "rb") as handle:
                    response = self._s3.put_object(Bucket=self._bucket, Key=self._prefix + rel, Body=handle)
                manifest[rel] = {"etag": response["ETag"].strip('"'), "size": stat.st_size,
                                 "mtime_ns": stat.st_mtime_ns}
                uploaded += 1
        deleted = 0
        for rel in [r for r in manifest if r not in seen]:
            # Deletes are recoverable: the tenant bucket is versioned.
            self._s3.delete_object(Bucket=self._bucket, Key=self._prefix + rel)
            del manifest[rel]
            deleted += 1
        self._save(manifest)
        return {"uploaded": uploaded, "deleted": deleted, **({"notSaved": sorted(not_saved)[:20]} if not_saved else {})}
