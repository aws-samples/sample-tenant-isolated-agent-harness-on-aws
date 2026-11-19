#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# Builds the two deployable packages. No Docker needed.
#   .build/layers/agent  Lambda layer for the harness worker (python3.13, arm64)
#   .build/runtime       AgentCore Runtime direct code deploy package (python3.13, arm64)
set -euo pipefail
cd "$(dirname "$0")/.."

PIP=".venv/bin/pip"
TARGET_FLAGS=(--platform manylinux2014_aarch64 --implementation cp --python-version 3.13 --only-binary=:all:)

rm -rf .build/layers/agent .build/runtime
mkdir -p .build/layers/agent/python .build/runtime

"$PIP" install --quiet "${TARGET_FLAGS[@]}" --target .build/layers/agent/python -r requirements/worker.txt
"$PIP" install --quiet "${TARGET_FLAGS[@]}" --target .build/runtime -r requirements/runtime.txt

cp -R src/runtime src/shared .build/runtime/
cp src/runtime/main.py .build/runtime/main.py

find .build -type d -name "__pycache__" -prune -exec rm -rf {} +
# AgentCore reads the package as a non-root user: 755 for directories, 644 for files.
find .build/runtime -type d -exec chmod 755 {} +
find .build/runtime -type f -exec chmod 644 {} +

du -sh .build/layers/agent .build/runtime
