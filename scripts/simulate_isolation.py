#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Prove the tenant isolation policies with the IAM policy simulator, without deploying anything.

Read-only: calls iam:SimulateCustomPolicy (and sts:GetCallerIdentity). The documents come from
infra/policies.py, the same functions CDK deploys, filled with placeholder ARNs.

    AWS_PROFILE=<sandbox> .venv/bin/python scripts/simulate_isolation.py --out .build/evidence/iam-simulation.md
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from infra import policies  # noqa: E402
from shared.tenancy import storage_id_for  # noqa: E402

REGION = "ap-southeast-2"
A, B = storage_id_for("anycompany-plumbing"), storage_id_for("example-corp-electrical")
VPCE, OTHER_VPCE = "vpce-0a1b2c3d4e5f60001", "vpce-0ffffffffffff9999"


def case(name, doc, action, resource, context=None, expect="allowed", resource_policy=None, caller=None):
    return {"name": name, "doc": doc, "action": action, "resource": resource, "context": context or {},
            "expect": expect, "resource_policy": resource_policy, "caller": caller}


def build_cases(account: str) -> list[dict]:
    bucket = "arn:aws:s3:::split-harness-sim-tenant-data"
    key = f"arn:aws:kms:{REGION}:{account}:key/11111111-2222-3333-4444-555555555555"
    gateway = f"arn:aws:bedrock-agentcore:{REGION}:{account}:gateway/split-harness-tools-sim"
    runtime = f"arn:aws:bedrock-agentcore:{REGION}:{account}:runtime/split_harness_exec-sim"
    table = f"arn:aws:dynamodb:{REGION}:{account}:table/split-harness-saas"
    role = lambda name: f"arn:aws:iam::{account}:role/{name}"  # noqa: E731
    as_a = {"aws:PrincipalTag/TenantId": A}
    s3_via = f"s3.{REGION}.amazonaws.com"

    harness = policies.tenant_harness_policy(bucket_arn=bucket, data_key_arn=key, gateway_arn=gateway, region=REGION)
    runtime_tenant = policies.tenant_runtime_policy(bucket_arn=bucket, data_key_arn=key, s3_endpoint_id=VPCE, region=REGION)
    tools = policies.tenant_tools_policy(table_arn=table)
    worker = policies.worker_policy(harness_role_arn=role("TenantHarnessRole"), runtime_role_arn=role("TenantRuntimeRole"),
                                    token_key_arn=key, runtime_arn=runtime,
                                    model_arns=[f"arn:aws:bedrock:{REGION}:{account}:inference-profile/apac.amazon.nova-lite-v1:0",
                                                "arn:aws:bedrock:*::foundation-model/amazon.nova-lite-v1:0"])
    execution = policies.runtime_execution_policy(account=account, region=REGION, runtime_name="split_harness_exec",
                                                  code_object_arn="arn:aws:s3:::cdk-assets-sim/runtime.zip")
    deployer = role(f"cdk-hnb659fds-cfn-exec-role-{account}-{REGION}")
    guardrail = {"Version": "2012-10-17", "Statement": policies.bucket_guardrail_statements(
        bucket_arn=bucket, tenant_role_arns=[role("TenantHarnessRole"), role("TenantRuntimeRole")],
        deployer_role_arns=[deployer])}
    admin = {"aws:PrincipalArn": role("SomeAdmin")}
    allow_all_s3 = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "s3:*", "Resource": "*"}]}
    allow_all_agentcore = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "bedrock-agentcore:*", "Resource": "*"}]}
    runtime_rp = policies.runtime_resource_policy(runtime_arn=runtime, worker_role_arn=role("WorkerRole"), admin_role_arns=[])
    gateway_rp = policies.gateway_resource_policy(gateway_arn=gateway, harness_role_arn=role("TenantHarnessRole"), admin_role_arns=[])

    return [
        # TenantHarnessRole session tagged TenantId=A
        case("harness A reads its own conversation", harness, "s3:GetObject", f"{bucket}/tenants/{A}/harness/session/{A}/scopes/agent/harness/snapshots/snapshot_latest.json", as_a),
        case("harness A reads tenant B's conversation", harness, "s3:GetObject", f"{bucket}/tenants/{B}/harness/session/{B}/scopes/agent/harness/snapshots/snapshot_latest.json", as_a, "implicitDeny"),
        case("harness A writes its own transfer object", harness, "s3:PutObject", f"{bucket}/transfer/{A}/req-1/t/script.sh", as_a),
        case("harness A writes tenant B's transfer prefix", harness, "s3:PutObject", f"{bucket}/transfer/{B}/req-1/t/script.sh", as_a, "implicitDeny"),
        case("harness A reads tenant A's workspace", harness, "s3:GetObject", f"{bucket}/tenants/{A}/workspace/out.csv", as_a, "implicitDeny"),
        case("harness A lists its own harness prefix", harness, "s3:ListBucket", bucket, {**as_a, "s3:prefix": f"tenants/{A}/harness/session/{A}/"}),
        case("harness A lists tenant B's prefix", harness, "s3:ListBucket", bucket, {**as_a, "s3:prefix": f"tenants/{B}/harness/"}, "implicitDeny"),
        case("harness A lists the bucket root", harness, "s3:ListBucket", bucket, {**as_a, "s3:prefix": ""}, "implicitDeny"),
        case("harness A decrypts its own object via S3", harness, "kms:Decrypt", key,
             {**as_a, "kms:ViaService": s3_via, "kms:EncryptionContext:aws:s3:arn": f"{bucket}/tenants/{A}/harness/x.json"}),
        case("harness A decrypts tenant B's object via S3", harness, "kms:Decrypt", key,
             {**as_a, "kms:ViaService": s3_via, "kms:EncryptionContext:aws:s3:arn": f"{bucket}/tenants/{B}/harness/x.json"}, "implicitDeny"),
        case("harness A calls KMS directly (not via S3)", harness, "kms:Decrypt", key,
             {**as_a, "kms:EncryptionContext:aws:s3:arn": f"{bucket}/tenants/{A}/harness/x.json"}, "implicitDeny"),
        case("harness A invokes the Gateway", harness, "bedrock-agentcore:InvokeGateway", gateway, as_a),
        # TenantRuntimeRole session tagged TenantId=A (credentials inside the untrusted runtime)
        case("runtime A writes its workspace from the runtime VPC", runtime_tenant, "s3:PutObject", f"{bucket}/tenants/{A}/workspace/out.csv", {**as_a, "aws:SourceVpce": VPCE}),
        case("runtime A writes its workspace from elsewhere (stolen creds)", runtime_tenant, "s3:PutObject", f"{bucket}/tenants/{A}/workspace/out.csv", {**as_a, "aws:SourceVpce": OTHER_VPCE}, "implicitDeny"),
        case("runtime A uses its creds outside any VPC endpoint", runtime_tenant, "s3:GetObject", f"{bucket}/tenants/{A}/workspace/out.csv", as_a, "implicitDeny"),
        case("runtime A reads tenant B's workspace", runtime_tenant, "s3:GetObject", f"{bucket}/tenants/{B}/workspace/out.csv", {**as_a, "aws:SourceVpce": VPCE}, "implicitDeny"),
        case("runtime A reads its own conversation state", runtime_tenant, "s3:GetObject", f"{bucket}/tenants/{A}/harness/session/{A}/scopes/agent/harness/snapshots/snapshot_latest.json", {**as_a, "aws:SourceVpce": VPCE}, "implicitDeny"),
        case("runtime A reads its uploads", runtime_tenant, "s3:GetObject", f"{bucket}/tenants/{A}/uploads/prices.csv", {**as_a, "aws:SourceVpce": VPCE}),
        case("runtime A overwrites an upload", runtime_tenant, "s3:PutObject", f"{bucket}/tenants/{A}/uploads/prices.csv", {**as_a, "aws:SourceVpce": VPCE}, "implicitDeny"),
        case("runtime A lists tenant B's workspace", runtime_tenant, "s3:ListBucket", bucket, {**as_a, "aws:SourceVpce": VPCE, "s3:prefix": f"tenants/{B}/workspace/"}, "implicitDeny"),
        # TenantToolsRole session tagged TenantId=A
        case("tools A queries its own partition", tools, "dynamodb:Query", table, {**as_a, "dynamodb:LeadingKeys": [f"TENANT#{A}"]}),
        case("tools A queries tenant B's partition", tools, "dynamodb:Query", table, {**as_a, "dynamodb:LeadingKeys": [f"TENANT#{B}"]}, "implicitDeny"),
        case("tools A scans the whole table", tools, "dynamodb:Scan", table, as_a, "implicitDeny"),
        # Shared worker role: no S3 or Gateway access of its own (its one data store is the coordination table)
        case("worker reads any tenant object directly", worker, "s3:GetObject", f"{bucket}/tenants/{A}/harness/session/{A}/scopes/agent/harness/snapshots/snapshot_latest.json", expect="implicitDeny"),
        case("worker calls the Gateway with its own role", worker, "bedrock-agentcore:InvokeGateway", gateway, expect="implicitDeny"),
        case("worker vends a tenant harness session", worker, "sts:AssumeRole", role("TenantHarnessRole")),
        case("worker invokes an unpinned model", worker, "bedrock:InvokeModel", "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-opus-4-1-20250805-v1:0", expect="implicitDeny"),
        # Runtime execution role: hostile code can read these credentials
        case("execution role reads tenant data", execution, "s3:GetObject", f"{bucket}/tenants/{A}/workspace/out.csv", expect="implicitDeny"),
        case("execution role invokes a model (LLMjacking)", execution, "bedrock:InvokeModel", "arn:aws:bedrock:*::foundation-model/amazon.nova-lite-v1:0", expect="implicitDeny"),
        case("execution role invokes another runtime session", execution, "bedrock-agentcore:InvokeAgentRuntime", runtime, expect="implicitDeny"),
        case("execution role assumes a tenant role", execution, "sts:AssumeRole", role("TenantRuntimeRole"), expect="implicitDeny"),
        # Bucket policy backstop against a broad grant added later
        case("an admin with s3:* reads tenant data (bucket backstop)", allow_all_s3, "s3:GetObject", f"{bucket}/tenants/{A}/harness/x.json",
             admin, "explicitDeny", resource_policy=guardrail, caller=role("SomeAdmin")),
        case("an admin with s3:* reads an earlier version of a tenant object", allow_all_s3, "s3:GetObjectVersion",
             f"{bucket}/tenants/{A}/harness/x.json", admin, "explicitDeny", resource_policy=guardrail, caller=role("SomeAdmin")),
        case("an admin with s3:* deletes a tenant object", allow_all_s3, "s3:DeleteObject", f"{bucket}/tenants/{A}/workspace/a.csv",
             admin, "explicitDeny", resource_policy=guardrail, caller=role("SomeAdmin")),
        case("an admin with s3:* reads a transfer object", allow_all_s3, "s3:GetObject", f"{bucket}/transfer/{A}/req-1/t/out.json",
             admin, "explicitDeny", resource_policy=guardrail, caller=role("SomeAdmin")),
        case("an admin with s3:* lists the bucket", allow_all_s3, "s3:ListBucket", bucket,
             admin, "explicitDeny", resource_policy=guardrail, caller=role("SomeAdmin")),
        case("an admin with s3:* lists object versions", allow_all_s3, "s3:ListBucketVersions", bucket,
             admin, "explicitDeny", resource_policy=guardrail, caller=role("SomeAdmin")),
        case("an admin with s3:* rewrites the bucket policy", allow_all_s3, "s3:PutBucketPolicy", bucket,
             admin, "explicitDeny", resource_policy=guardrail, caller=role("SomeAdmin")),
        case("an admin with s3:* suspends versioning", allow_all_s3, "s3:PutBucketVersioning", bucket,
             admin, "explicitDeny", resource_policy=guardrail, caller=role("SomeAdmin")),
        case("an admin with s3:* replicates the bucket elsewhere", allow_all_s3, "s3:PutReplicationConfiguration", bucket,
             admin, "explicitDeny", resource_policy=guardrail, caller=role("SomeAdmin")),
        case("CloudFormation updates the bucket policy", allow_all_s3, "s3:PutBucketPolicy", bucket,
             {"aws:PrincipalArn": deployer}, "allowed", resource_policy=guardrail, caller=deployer),
        case("CloudFormation reads tenant data", allow_all_s3, "s3:GetObject", f"{bucket}/tenants/{A}/harness/x.json",
             {"aws:PrincipalArn": deployer}, "explicitDeny", resource_policy=guardrail, caller=deployer),
        case("harness A reads its own conversation through the bucket policy", harness, "s3:GetObject",
             f"{bucket}/tenants/{A}/harness/session/{A}/scopes/agent/harness/snapshots/snapshot_latest.json",
             {**as_a, "aws:PrincipalArn": role("TenantHarnessRole")}, "allowed", resource_policy=guardrail,
             caller=role("TenantHarnessRole")),
        # AgentCore resource policies: who may reach tenant sessions and the Gateway
        case("worker invokes a tenant's execution session", worker, "bedrock-agentcore:InvokeAgentRuntime", runtime,
             {"aws:PrincipalArn": role("WorkerRole")}, "allowed", resource_policy=runtime_rp, caller=role("WorkerRole")),
        case("an admin with bedrock-agentcore:* invokes a tenant session", allow_all_agentcore, "bedrock-agentcore:InvokeAgentRuntime",
             runtime, {"aws:PrincipalArn": role("SomeAdmin")}, "explicitDeny", resource_policy=runtime_rp, caller=role("SomeAdmin")),
        case("an admin opens a shell in a tenant session", allow_all_agentcore, "bedrock-agentcore:InvokeAgentRuntimeCommandShell",
             runtime, {"aws:PrincipalArn": role("SomeAdmin")}, "explicitDeny", resource_policy=runtime_rp, caller=role("SomeAdmin")),
        case("the worker itself runs a command in a tenant session", allow_all_agentcore, "bedrock-agentcore:InvokeAgentRuntimeCommand",
             runtime, {"aws:PrincipalArn": role("WorkerRole")}, "explicitDeny", resource_policy=runtime_rp, caller=role("WorkerRole")),
        case("an admin calls the Gateway directly", allow_all_agentcore, "bedrock-agentcore:InvokeGateway", gateway,
             {"aws:PrincipalArn": role("SomeAdmin")}, "explicitDeny", resource_policy=gateway_rp, caller=role("SomeAdmin")),
        case("a tenant harness session calls the Gateway", harness, "bedrock-agentcore:InvokeGateway", gateway,
             {**as_a, "aws:PrincipalArn": role("TenantHarnessRole")}, "allowed", resource_policy=gateway_rp,
             caller=role("TenantHarnessRole")),
    ]


def simulate(iam, item, account):
    entries = []
    for name, value in item["context"].items():
        values = value if isinstance(value, list) else [value]
        entries.append({"ContextKeyName": name, "ContextKeyValues": values,
                        "ContextKeyType": "stringList" if isinstance(value, list) else "string"})
    kwargs = {"PolicyInputList": [json.dumps(item["doc"])], "ActionNames": [item["action"]],
              "ResourceArns": [item["resource"]], "ContextEntries": entries}
    if item["resource_policy"]:
        kwargs.update(ResourcePolicy=json.dumps(item["resource_policy"]), CallerArn=item["caller"],
                      ResourceOwner=f"arn:aws:iam::{account}:root")
    return iam.simulate_custom_policy(**kwargs)["EvaluationResults"][0]["EvalDecision"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", help="write a markdown evidence file (use .build/evidence/ to keep "
                                      "the committed docs/evidence/ example in place)")
    args = parser.parse_args()
    session = boto3.Session()
    account = session.client("sts").get_caller_identity()["Account"]
    iam = session.client("iam")
    rows, failures = [], 0
    for item in build_cases(account):
        decision = simulate(iam, item, account)
        ok = decision == item["expect"]
        failures += not ok
        rows.append((item["name"], item["action"], item["expect"], decision, "PASS" if ok else "FAIL"))
        print(f"{'PASS' if ok else 'FAIL'}  {decision:<13} {item['name']}")
    print(f"\n{len(rows) - failures}/{len(rows)} cases matched")
    if args.out:
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines = [f"# IAM policy simulation: tenant isolation boundary", "",
                 f"Run {stamp} with `iam:SimulateCustomPolicy` (read-only). Tenant A = `{A}`, tenant B = `{B}`.",
                 f"Documents: `infra/policies.py`, the same functions CDK deploys, with placeholder ARNs.", "",
                 f"**{len(rows) - failures}/{len(rows)} cases matched the expected decision.**", "",
                 "| Case | Action | Expected | Simulator | Result |", "| --- | --- | --- | --- | --- |"]
        lines += [f"| {n} | `{a}` | {e} | {d} | {r} |" for n, a, e, d, r in rows]
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text("\n".join(lines) + "\n")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
