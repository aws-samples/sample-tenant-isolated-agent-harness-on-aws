#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CDK entry point. Build assets first: scripts/build_assets.sh

The Region is explicit (``-c region=...``, default ap-southeast-2) rather than inherited from the
CLI profile, so a profile whose default Region is us-east-1 cannot silently move the stack.
Runtime subnets must sit in AgentCore-supported Availability Zone IDs; every Sydney AZ is supported.
"""

import os

import aws_cdk as cdk

from infra.stack import SplitHarnessStack

app = cdk.App()
region = app.node.try_get_context("region") or os.environ.get("SPLIT_HARNESS_REGION") or "ap-southeast-2"
# The stack name defaults to SplitHarness; -c stackName=... deploys an independent copy (handy for a
# clean redeploy without touching an existing one). The demo targets it with --stack.
stack_name = app.node.try_get_context("stackName") or "SplitHarness"
SplitHarnessStack(
    app, stack_name,
    env=cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region=region),
    description="Decoupled (split) agent harness sample with an ABAC tenant isolation boundary",
)
app.synth()
