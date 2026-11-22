# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""IAM policy documents for the tenant isolation boundary.

Pure functions: CDK deploys these exact documents, and scripts/simulate_isolation.py runs them
through the IAM policy simulator with concrete ARNs, without deploying anything.

Pattern: attribute-based access control. A constant number of roles serves every tenant. A
caller assumes the role with a ``TenantId`` session tag and every resource ARN or condition
references ``${aws:PrincipalTag/TenantId}``.

  TenantHarnessRole  conversation state + transfer objects + InvokeGateway   (shared worker)
  TenantRuntimeRole  workspace read/write + uploads read, runtime VPC only   (untrusted runtime)
  TenantToolsRole    DynamoDB items whose partition key is TENANT#<tenant>   (Gateway tools)
"""

from __future__ import annotations

TENANT_TAG = "TenantId"
T = "${aws:PrincipalTag/" + TENANT_TAG + "}"
# STS session-tag values are matched for presence, the only tag key, and a 32-character value.
STORAGE_ID_PATTERN = "?" * 32


def _doc(*statements: dict) -> dict:
    return {"Version": "2012-10-17", "Statement": list(statements)}


def tenant_trust_conditions() -> dict:
    """Conditions for the trust statement of every tenant role (AssumeRole + TagSession)."""
    return {
        "Null": {f"aws:RequestTag/{TENANT_TAG}": "false"},
        "StringLike": {f"aws:RequestTag/{TENANT_TAG}": STORAGE_ID_PATTERN},
        "ForAllValues:StringEquals": {"aws:TagKeys": [TENANT_TAG]},
    }


def _kms_via_s3(data_key_arn: str, region: str, object_arn_patterns: list[str], sid: str) -> dict:
    # Bucket Keys are off, so S3 passes the object ARN as KMS encryption context. This IAM grant restricts
    # nothing on its own, because the data key's policy grants the tenant roles directly; that key policy
    # holds them to their tenant's prefixes (s3_object_context), so even an S3 policy mistake cannot
    # decrypt another tenant's objects with this session.
    return {
        "Sid": sid, "Effect": "Allow",
        "Action": ["kms:Decrypt", "kms:GenerateDataKey"],
        "Resource": data_key_arn,
        "Condition": {
            "StringEquals": {"kms:ViaService": f"s3.{region}.amazonaws.com"},
            "StringLike": {"kms:EncryptionContext:aws:s3:arn": object_arn_patterns},
        },
    }


S3_ARN_CONTEXT = "kms:EncryptionContext:aws:s3:arn"
# The bucket's two top-level areas. Every object the data key encrypts sits under one of them.
TOP_LEVEL_PREFIXES = ("tenants/", "transfer/")


def s3_object_context(*, region: str, key_prefixes: list[str]) -> dict:
    """Key-policy condition: the key is used only through S3, for objects under ``key_prefixes``.

    S3 sends the object ARN as the encryption context because S3 Bucket Keys are off (with them on,
    it sends the bucket ARN and this condition refuses every request). The bucket part is a
    wildcard: the key policy cannot name the bucket, whose encryption names the key. In StringLike
    '*' also matches '/', so 'arn:aws:s3:::*/tenants/A/*' alone would also match
    tenants/B/workspace/tenants/A/x; the StringNotLike guard refuses a prefix that sits below a
    tenants/ or transfer/ path. The cost: an object whose key repeats its own tenant's prefix
    further down (tenants/A/workspace/tenants/A/x) is refused as well.

    IAM Access Analyzer reports a wildcard straight after ${aws:PrincipalTag/TenantId} as too
    permissive. That value is the tenant's 32-character storage ID, so the pattern is the tenant's
    own prefix; and a session without the tag matches nothing here, so it is refused.
    """
    if not key_prefixes or not all(p.endswith("/") for p in key_prefixes):
        raise ValueError("key prefixes must be given, each ending in '/'")
    return {
        "StringEquals": {"kms:ViaService": f"s3.{region}.amazonaws.com"},
        "StringLike": {S3_ARN_CONTEXT: [f"arn:aws:s3:::*/{p}*" for p in key_prefixes]},
        "StringNotLike": {S3_ARN_CONTEXT: [f"arn:aws:s3:::*/{area}*/{p}*"
                                           for area in TOP_LEVEL_PREFIXES for p in key_prefixes]},
    }


TENANT_KEY_PREFIXES = [f"tenants/{T}/", f"transfer/{T}/"]


def tenant_harness_policy(*, bucket_arn: str, data_key_arn: str, gateway_arn: str, region: str) -> dict:
    return _doc(
        {"Sid": "OwnHarnessStateAndTransfer", "Effect": "Allow",
         "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
         "Resource": [f"{bucket_arn}/tenants/{T}/harness/*", f"{bucket_arn}/transfer/{T}/*"]},
        {"Sid": "OwnUploadsRead", "Effect": "Allow", "Action": "s3:GetObject",
         "Resource": f"{bucket_arn}/tenants/{T}/uploads/*"},
        {"Sid": "ListOwnHarnessPrefixes", "Effect": "Allow", "Action": "s3:ListBucket", "Resource": bucket_arn,
         "Condition": {"StringLike": {"s3:prefix": [f"tenants/{T}/harness/*", f"transfer/{T}/*"]}}},
        _kms_via_s3(data_key_arn, region, [f"{bucket_arn}/tenants/{T}/*", f"{bucket_arn}/transfer/{T}/*"],
                    "DataKeyForOwnObjectsOnly"),
        {"Sid": "InvokeGatewayAsTenant", "Effect": "Allow", "Action": "bedrock-agentcore:InvokeGateway",
         "Resource": gateway_arn},
    )


def tenant_runtime_policy(*, bucket_arn: str, data_key_arn: str, s3_endpoint_id: str, region: str) -> dict:
    from_runtime_vpc = {"StringEquals": {"aws:SourceVpce": s3_endpoint_id}}
    return _doc(
        {"Sid": "OwnWorkspaceFromRuntimeVpc", "Effect": "Allow",
         "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
         "Resource": f"{bucket_arn}/tenants/{T}/workspace/*", "Condition": from_runtime_vpc},
        {"Sid": "OwnUploadsReadFromRuntimeVpc", "Effect": "Allow", "Action": "s3:GetObject",
         "Resource": f"{bucket_arn}/tenants/{T}/uploads/*", "Condition": from_runtime_vpc},
        {"Sid": "ListOwnWorkspaceFromRuntimeVpc", "Effect": "Allow", "Action": "s3:ListBucket",
         "Resource": bucket_arn,
         "Condition": {**from_runtime_vpc,
                       "StringLike": {"s3:prefix": [f"tenants/{T}/workspace/*", f"tenants/{T}/uploads/*"]}}},
        # No aws:SourceVpce here: S3 calls KMS on the caller's behalf from outside the VPC.
        _kms_via_s3(data_key_arn, region, [f"{bucket_arn}/tenants/{T}/workspace/*", f"{bucket_arn}/tenants/{T}/uploads/*"],
                    "DataKeyForOwnWorkspaceOnly"),
    )


def tenant_tools_policy(*, table_arn: str) -> dict:
    return _doc(
        {"Sid": "OwnPartitionOnly", "Effect": "Allow",
         "Action": ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:PutItem", "dynamodb:UpdateItem",
                    "dynamodb:DeleteItem"],
         "Resource": table_arn,
         "Condition": {"ForAllValues:StringEquals": {"dynamodb:LeadingKeys": [f"TENANT#{T}"]},
                       "Null": {"dynamodb:LeadingKeys": "false"}}},
    )


def worker_policy(*, harness_role_arn: str, runtime_role_arn: str, token_key_arn: str, runtime_arn: str,
                  model_arns: list[str]) -> dict:
    """The shared harness role. Deliberately absent: any S3 access and InvokeGateway."""
    return _doc(
        {"Sid": "VendTenantSessions", "Effect": "Allow", "Action": ["sts:AssumeRole", "sts:TagSession"],
         "Resource": [harness_role_arn, runtime_role_arn]},
        {"Sid": "MintTenantContext", "Effect": "Allow", "Action": "kms:GenerateMac", "Resource": token_key_arn,
         "Condition": {"StringEquals": {"kms:MacAlgorithm": "HMAC_SHA_256"}}},
        {"Sid": "PinnedModelsOnly", "Effect": "Allow",
         "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"], "Resource": model_arns},
        {"Sid": "InvokeExecutionRuntime", "Effect": "Allow", "Action": "bedrock-agentcore:InvokeAgentRuntime",
         "Resource": [runtime_arn, f"{runtime_arn}/*"]},
    )


def interceptor_policy(*, token_key_arn: str) -> dict:
    return _doc({"Sid": "VerifyTenantContextOnly", "Effect": "Allow", "Action": "kms:VerifyMac",
                 "Resource": token_key_arn, "Condition": {"StringEquals": {"kms:MacAlgorithm": "HMAC_SHA_256"}}})


def tools_function_policy(*, tools_role_arn: str) -> dict:
    return _doc({"Sid": "VendToolSessions", "Effect": "Allow", "Action": ["sts:AssumeRole", "sts:TagSession"],
                 "Resource": tools_role_arn})


def runtime_execution_policy(*, account: str, region: str, code_object_arn: str, runtime_name: str) -> dict:
    """The runtime execution role. Hostile code in the microVM can read these credentials, so
    they grant nothing that reaches tenant data, models, Gateway or other runtime sessions."""
    # Only this runtime's own log groups. AgentCore names them
    # /aws/bedrock-agentcore/runtimes/<runtime_name>-<generated-id>..., and the service (through
    # this role) creates the group on first use, so CreateLogGroup stays but is scoped to this name.
    log_group = f"arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/runtimes/{runtime_name}-*"
    return _doc(
        {"Sid": "RuntimeLogs", "Effect": "Allow",
         "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"],
         "Resource": [log_group, f"{log_group}:log-stream:*"]},
        {"Sid": "ReadOwnCodePackage", "Effect": "Allow", "Action": "s3:GetObject", "Resource": code_object_arn},
    )


# Listing reveals tenant storage IDs and file names; every listing action is covered.
BUCKET_LIST_ACTIONS = ["s3:ListBucket", "s3:ListBucketVersions", "s3:ListBucketMultipartUploads"]
# The listings that take the s3:prefix condition key.
PREFIX_LIST_ACTIONS = ["s3:ListBucket", "s3:ListBucketVersions"]
# Bucket settings that could loosen the backstop or copy, expire, expose or unprotect tenant data.
BUCKET_CONFIG_ACTIONS = [
    "s3:PutBucketPolicy", "s3:DeleteBucketPolicy", "s3:PutBucketVersioning", "s3:PutLifecycleConfiguration",
    "s3:PutReplicationConfiguration", "s3:PutInventoryConfiguration", "s3:PutBucketNotification",
    "s3:PutBucketAcl", "s3:PutBucketOwnershipControls", "s3:PutBucketPublicAccessBlock",
    "s3:PutEncryptionConfiguration",
]


def bucket_guardrail_statements(*, bucket_arn: str, tenant_role_arns: list[str], deployer_role_arns: list[str],
                                admin_role_arns: list[str] = (), cleanup_role_arns: list[str] = (),
                                demo_operator_role_arns: list[str] = (),
                                demo_tenant_prefixes: list[str] = ()) -> list[dict]:
    """Resource-policy backstop on the tenant bucket, against a broad S3 grant added later to any
    other principal.

    * Every action on tenant and transfer objects, old versions and deletes included, and every
      listing, only through the tenant roles.
    * The bucket policy and the settings that could copy, expire or expose data only through the
      CloudFormation execution role, so loosening the backstop takes a logged deployment. That role
      stays exempt from these actions only, or the stack could not be updated.

    Break-glass roles are exempt from all three. The cleanup role is the stack's auto-delete
    function (sandbox deployments): on stack deletion it locks the bucket policy, lists and deletes.
    The demo operator (present only with -c demo=true) is exempt for ``demo_tenant_prefixes`` only:
    it may touch the demo tenants' objects and listings, never another tenant's, the transfer area
    or the bucket's own policy and settings.
    """
    readers = [*tenant_role_arns, *admin_role_arns, *cleanup_role_arns, *demo_operator_role_arns]
    configurers = [*deployer_role_arns, *admin_role_arns, *cleanup_role_arns]

    def deny(sid: str, action: object, resource: object, allowed: list[str]) -> dict:
        return {"Sid": sid, "Effect": "Deny", "Principal": {"AWS": "*"}, "Action": action, "Resource": resource,
                "Condition": {"ArnNotEquals": {"aws:PrincipalArn": allowed}}}

    statements = [
        deny("TenantDataOnlyThroughTenantRoles", "s3:*", [f"{bucket_arn}/tenants/*", f"{bucket_arn}/transfer/*"],
             readers),
        deny("ListingOnlyThroughTenantRoles", BUCKET_LIST_ACTIONS, bucket_arn, readers),
    ]
    if demo_operator_role_arns:
        if not demo_tenant_prefixes or not all(p.endswith("/") for p in demo_tenant_prefixes):
            raise ValueError("the demo operator needs the demo tenants' prefixes, each ending in '/'")
        operator = {"ArnEquals": {"aws:PrincipalArn": list(demo_operator_role_arns)}}
        statements += [
            {"Sid": "DemoOperatorOnlyDemoTenantObjects", "Effect": "Deny", "Principal": {"AWS": "*"},
             "Action": "s3:*", "NotResource": [bucket_arn, *[f"{bucket_arn}/{p}*" for p in demo_tenant_prefixes]],
             "Condition": operator},
            {"Sid": "DemoOperatorListsOnlyDemoTenants", "Effect": "Deny", "Principal": {"AWS": "*"},
             "Action": PREFIX_LIST_ACTIONS, "Resource": bucket_arn,
             "Condition": {**operator, "StringNotLike": {"s3:prefix": [f"{p}*" for p in demo_tenant_prefixes]}}},
            # s3:prefix does not apply to this listing, so it is refused outright.
            {"Sid": "DemoOperatorListsNoUploads", "Effect": "Deny", "Principal": {"AWS": "*"},
             "Action": "s3:ListBucketMultipartUploads", "Resource": bucket_arn, "Condition": operator},
        ]
    if deployer_role_arns:
        statements.append(deny("BucketConfigOnlyThroughDeployments", BUCKET_CONFIG_ACTIONS, bucket_arn, configurers))
    return statements


def function_invoke_policy(*, function_arn: str, sid: str, invoker_role_arns: list[str] = (),
                           admin_role_arns: list[str] = (), api_source_arn: str | None = None,
                           lambda_service: bool = False) -> dict:
    """Resource policy for one of the stack's functions: nobody else in the account invokes it.

    Each function trusts its input to name the right tenant: the tools function takes the tenantId
    the interceptor injected, ingress takes the claims API Gateway verified, the worker takes the
    message ingress queued. So a direct invoke by any principal holding lambda:InvokeFunction could
    act as any tenant. An explicit Deny here overrides every identity-policy grant in the account.

    invoker_role_arns: the IAM roles that invoke it (the Gateway's role, for tools and interceptor).
    api_source_arn: ingress is invoked by API Gateway as a service; allowed for this API only.
    lambda_service: the worker's SQS event source mapping is run by Lambda itself.
    Break-glass roles stay exempt. The Deny covers every version and alias. Only a principal allowed
    to rewrite this policy can lift it, and CloudTrail records that call.
    """
    condition: dict = {}
    if invoker_role_arns or admin_role_arns:
        condition["ArnNotEquals"] = {"aws:PrincipalArn": [*invoker_role_arns, *admin_role_arns]}
    if api_source_arn:
        condition["ArnNotLike"] = {"aws:SourceArn": api_source_arn}
    if lambda_service:
        condition["StringNotEquals"] = {"aws:PrincipalServiceName": "lambda.amazonaws.com"}
    if not condition:
        raise ValueError("a function needs at least one permitted invoker")
    statements = [{"Sid": sid, "Effect": "Deny", "Principal": "*", "Action": "lambda:InvokeFunction",
                   "Resource": [function_arn, f"{function_arn}:*"], "Condition": condition}]
    if api_source_arn:
        statements.insert(0, {"Sid": "ThisApiInvokes", "Effect": "Allow",
                              "Principal": {"Service": "apigateway.amazonaws.com"},
                              "Action": "lambda:InvokeFunction", "Resource": function_arn,
                              "Condition": {"ArnLike": {"aws:SourceArn": api_source_arn}}})
    return _doc(*statements)


def s3_endpoint_statements(*, bucket_arn: str, region: str) -> list[dict]:
    """Runtime VPC S3 gateway endpoint policy: only the tenant bucket and AgentCore's own
    code and session-storage buckets. Closes the endpoint as an exfiltration path."""
    managed = [f"arn:aws:s3:::acr-code-*-{region}-an", f"arn:aws:s3:::acr-storage-*-{region}-an"]
    return [
        {"Sid": "TenantBucketOnly", "Effect": "Allow", "Principal": {"AWS": "*"},
         "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"],
         "Resource": [bucket_arn, f"{bucket_arn}/*"]},
        {"Sid": "AgentCoreManagedBuckets", "Effect": "Allow", "Principal": {"AWS": "*"},
         "Action": ["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
         "Resource": managed + [f"{arn}/*" for arn in managed],
         "Condition": {"StringEquals": {"aws:PrincipalServiceName": "bedrock-agentcore.amazonaws.com"}}},
    ]


def _deny_unless(principals: list[str]) -> dict:
    return {"Condition": {"ArnNotEquals": {"aws:PrincipalArn": principals}}} if principals else {}


def runtime_resource_policy(*, runtime_arn: str, worker_role_arn: str, admin_role_arns: list[str]) -> dict:
    """Who may reach a tenant's execution session.

    InvokeAgentRuntime chooses the runtimeSessionId, so anyone who can call it can run code in any
    tenant's session and read that tenant's cached workspace. Only the shared harness gets it.
    The interactive actions (command, shell, WebSocket) are never needed by the harness and would
    give a shell into a live tenant session, so they are denied to everyone except break-glass roles.
    """
    interactive = ["bedrock-agentcore:InvokeAgentRuntimeCommand", "bedrock-agentcore:InvokeAgentRuntimeCommandShell",
                   "bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream",
                   "bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStreamForUser",
                   "bedrock-agentcore:InvokeAgentRuntimeForUser"]
    return _doc(
        {"Sid": "OnlyTheHarnessInvokesTenantSessions", "Effect": "Deny", "Principal": "*",
         "Action": ["bedrock-agentcore:InvokeAgentRuntime", "bedrock-agentcore:StopRuntimeSession"],
         "Resource": runtime_arn, **_deny_unless([worker_role_arn, *admin_role_arns])},
        {"Sid": "NoInteractiveAccessToTenantSessions", "Effect": "Deny", "Principal": "*", "Action": interactive,
         "Resource": runtime_arn, **_deny_unless(list(admin_role_arns))},
    )


def gateway_resource_policy(*, gateway_arn: str, harness_role_arn: str, admin_role_arns: list[str]) -> dict:
    """Only tenant-scoped harness sessions may call the tool Gateway (the interceptor still
    requires a valid tenant context on every call)."""
    return _doc({"Sid": "OnlyTenantHarnessSessions", "Effect": "Deny", "Principal": "*",
                 "Action": "bedrock-agentcore:InvokeGateway", "Resource": gateway_arn,
                 **_deny_unless([harness_role_arn, *admin_role_arns])})


def logs_endpoint_statement(*, account: str, region: str) -> dict:
    """Runtime VPC Logs endpoint: this account's principals writing this account's log groups.
    Without it, code in the runtime could ship data out with its own credentials through the
    endpoint (the same exfiltration class as an allow-all S3 endpoint policy)."""
    return {"Sid": "ThisAccountLogsOnly", "Effect": "Allow", "Principal": {"AWS": "*"},
            "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"],
            "Resource": f"arn:aws:logs:{region}:{account}:log-group:*",
            "Condition": {"StringEquals": {"aws:PrincipalAccount": account}}}
