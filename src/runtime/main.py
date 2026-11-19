# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""AgentCore Runtime entry point (direct code deploy: entryPoint ["main.py"])."""

from runtime.server import serve

if __name__ == "__main__":
    serve()
