# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import json

import aws_cdk as cdk
import pytest
from aws_cdk import assertions

from infra import policies
from infra.stack import SplitHarnessStack

TENANT_VAR = "${aws:PrincipalTag/TenantId}"


def synth(outdir, **context):
    app = cdk.App(outdir=str(outdir), context=context or None)
    stack = SplitHarnessStack(app, "SplitHarness", env=cdk.Environment(account="111111111111", region="ap-southeast-2"))
    return app.synth().get_stack_by_name(stack.stack_name).template


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    return synth(tmp_path_factory.mktemp("cdk.out"))


@pytest.fixture(scope="module")
def sandbox_template(tmp_path_factory):
    return synth(tmp_path_factory.mktemp("cdk.out.sandbox"), sandbox="true")


def resources(template, kind):
    return {k: v for k, v in template["Resources"].items() if v["Type"] == kind}


def role_id(template, prefix):
    [key] = [k for k in resources(template, "AWS::IAM::Role") if k.startswith(prefix)]
    return key


def statements_for(template, role_prefix):
    rid = role_id(template, role_prefix)
    out = []
    for policy in resources(template, "AWS::IAM::Policy").values():
        if {"Ref": rid} in policy["Properties"].get("Roles", []):
            out.extend(policy["Properties"]["PolicyDocument"]["Statement"])
    return out


def actions(statement):
    value = statement.get("Action", [])
    return [value] if isinstance(value, str) else value


def test_shared_worker_role_holds_no_tenant_data_access(template):
    granted = [a for s in statements_for(template, "WorkerRole") for a in actions(s)]
    assert granted, "expected worker statements"
    assert not [a for a in granted if a.startswith("s3:")]
    assert "bedrock-agentcore:InvokeGateway" not in granted
    assert not [a for a in granted if a == "bedrock:*" or a == "*"]


def test_model_access_is_pinned_not_wildcarded(template):
    [model] = [s for s in statements_for(template, "WorkerRole") if s.get("Sid") == "PinnedModelsOnly"]
    text = json.dumps(model["Resource"])
    assert "foundation-model/amazon.nova-lite-v1:0" in text and "foundation-model/*" not in text


def test_runtime_execution_role_is_useless_to_hostile_code(template):
    granted = [a for s in statements_for(template, "RuntimeExecutionRole") for a in actions(s)]
    assert set(granted) <= {"logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents",
                            "logs:DescribeLogStreams", "s3:GetObject"}
    [code] = [s for s in statements_for(template, "RuntimeExecutionRole") if s.get("Sid") == "ReadOwnCodePackage"]
    assert ".zip" in json.dumps(code["Resource"])  # one object, not the bucket
    [logs] = [s for s in statements_for(template, "RuntimeExecutionRole") if s.get("Sid") == "RuntimeLogs"]
    # Scoped to this runtime's own log groups, not every AgentCore runtime in the account.
    assert "/aws/bedrock-agentcore/runtimes/split_harness_exec-*" in json.dumps(logs["Resource"])
    assert "runtimes/*" not in json.dumps(logs["Resource"])


@pytest.mark.parametrize("role", ["TenantHarnessRole", "TenantRuntimeRole", "TenantToolsRole"])
def test_tenant_roles_are_abac_scoped(template, role):
    for statement in statements_for(template, role):
        if statement.get("Action") == "bedrock-agentcore:InvokeGateway":
            continue
        assert TENANT_VAR in json.dumps(statement), statement.get("Sid")
    trust = template["Resources"][role_id(template, role)]["Properties"]["AssumeRolePolicyDocument"]["Statement"]
    assert all("sts:TagSession" in s["Action"] and "aws:RequestTag/TenantId" in json.dumps(s["Condition"]) for s in trust)


def test_runtime_tenant_credentials_only_work_from_the_runtime_vpc(template):
    s3_statements = [s for s in statements_for(template, "TenantRuntimeRole") if any(a.startswith("s3:") for a in actions(s))]
    assert s3_statements and all("aws:SourceVpce" in s.get("Condition", {}).get("StringEquals", {}) for s in s3_statements)
    harness = json.dumps(statements_for(template, "TenantRuntimeRole"))
    assert "/harness/" not in harness  # the runtime can never touch conversation state


def test_gateway_is_iam_authenticated_with_a_header_aware_interceptor(template):
    [gateway] = resources(template, "AWS::BedrockAgentCore::Gateway").values()
    props = gateway["Properties"]
    assert props["AuthorizerType"] == "AWS_IAM"
    [interceptor] = props["InterceptorConfigurations"]
    assert interceptor["InterceptionPoints"] == ["REQUEST"]
    assert interceptor["InputConfiguration"]["PassRequestHeaders"] is True


def test_runtime_has_no_shared_tenant_filesystem_and_no_internet(template):
    [runtime] = resources(template, "AWS::BedrockAgentCore::Runtime").values()
    props = runtime["Properties"]
    assert props["NetworkConfiguration"]["NetworkMode"] == "VPC"
    assert props["FilesystemConfigurations"] == [{"SessionStorage": {"MountPath": "/mnt/session"}}]
    assert not resources(template, "AWS::EC2::NatGateway") and not resources(template, "AWS::EC2::InternetGateway")


def test_runtime_egress_is_https_to_s3_and_logs_only(template):
    rules = []
    for sg in resources(template, "AWS::EC2::SecurityGroup").values():
        rules += sg["Properties"].get("SecurityGroupEgress", [])
    rules += [r["Properties"] for r in resources(template, "AWS::EC2::SecurityGroupEgress").values()]
    text = json.dumps(rules)
    assert "0.0.0.0/0" not in text and "::/0" not in text
    assert all(r.get("FromPort") == 443 and r.get("ToPort") == 443 for r in rules if "IpProtocol" in r and r["IpProtocol"] != "icmp")
    assert any("DestinationPrefixListId" in r for r in rules)


def _resolve_json(value):
    """Turn a synthesized Fn::Join JSON string back into a document, with refs as placeholders."""
    if isinstance(value, str):
        return json.loads(value)
    _, parts = value["Fn::Join"]
    text = "".join(p if isinstance(p, str) else "ref:" + json.dumps(p).replace('"', "'") for p in parts)
    return json.loads(text)


def test_only_the_harness_can_invoke_and_nobody_can_shell_into_tenant_sessions(template):
    policies_by_target = {}
    for rp in resources(template, "AWS::BedrockAgentCore::ResourcePolicy").values():
        target = json.dumps(rp["Properties"]["ResourceArn"])
        policies_by_target["runtime" if "ExecutionRuntime" in target else "gateway"] = \
            _resolve_json(rp["Properties"]["Policy"])["Statement"]
    runtime, gateway = policies_by_target["runtime"], policies_by_target["gateway"]
    [invoke] = [s for s in runtime if s["Sid"] == "OnlyTheHarnessInvokesTenantSessions"]
    assert invoke["Effect"] == "Deny" and "WorkerRole" in json.dumps(invoke["Condition"])
    [shell] = [s for s in runtime if s["Sid"] == "NoInteractiveAccessToTenantSessions"]
    assert shell["Effect"] == "Deny" and "Condition" not in shell  # no break-glass roles configured
    assert "bedrock-agentcore:InvokeAgentRuntimeCommandShell" in shell["Action"]
    [gw] = gateway
    assert gw["Effect"] == "Deny" and "TenantHarnessRole" in json.dumps(gw["Condition"])


def test_logs_endpoint_cannot_ship_data_to_other_accounts(template):
    [logs] = [v for v in resources(template, "AWS::EC2::VPCEndpoint").values()
              if v["Properties"].get("VpcEndpointType") == "Interface"]
    [statement] = logs["Properties"]["PolicyDocument"]["Statement"]
    assert statement["Condition"]["StringEquals"]["aws:PrincipalAccount"] == "111111111111"


def test_s3_endpoint_policy_is_not_allow_all(template):
    [s3_endpoint] = [v for v in resources(template, "AWS::EC2::VPCEndpoint").values()
                     if v["Properties"].get("VpcEndpointType", "Gateway") == "Gateway"]
    for statement in s3_endpoint["Properties"]["PolicyDocument"]["Statement"]:
        assert statement["Resource"] != "*" and "*" not in actions(statement)


def test_turn_queue_is_tenant_ordered_and_only_ingress_can_enqueue(template):
    turn_queue = [v for k, v in resources(template, "AWS::SQS::Queue").items() if k.startswith("TurnQueue")][0]
    props = turn_queue["Properties"]
    assert props["FifoQueue"] and props["DeduplicationScope"] == "messageGroup"
    assert props["FifoThroughputLimit"] == "perMessageGroupId"
    policies = json.dumps(list(resources(template, "AWS::SQS::QueuePolicy").values()))
    assert "OnlyIngressEnqueuesTurns" in policies and "aws:SecureTransport" in policies


def test_bucket_encryption_binds_objects_to_tenants(template):
    [bucket] = resources(template, "AWS::S3::Bucket").values()
    rule = bucket["Properties"]["BucketEncryption"]["ServerSideEncryptionConfiguration"][0]
    assert rule["ServerSideEncryptionByDefault"]["SSEAlgorithm"] == "aws:kms" and rule["BucketKeyEnabled"] is False
    assert bucket["Properties"]["VersioningConfiguration"]["Status"] == "Enabled"
    policy = json.dumps(list(resources(template, "AWS::S3::BucketPolicy").values()))
    assert "TenantDataOnlyThroughTenantRoles" in policy and "aws:SecureTransport" in policy


def bucket_statements(template):
    [policy] = resources(template, "AWS::S3::BucketPolicy").values()
    return {s.get("Sid"): s for s in policy["Properties"]["PolicyDocument"]["Statement"]}


def test_bucket_backstop_covers_every_object_action_and_listing(template):
    statements = bucket_statements(template)
    data, listing = statements["TenantDataOnlyThroughTenantRoles"], statements["ListingOnlyThroughTenantRoles"]
    # s3:* covers GetObjectVersion, DeleteObject and every other object action, not just Get/Put.
    assert data["Effect"] == "Deny" and data["Action"] == "s3:*"
    assert {"s3:ListBucket", "s3:ListBucketVersions", "s3:ListBucketMultipartUploads"} <= set(listing["Action"])
    readers = json.dumps([data["Condition"], listing["Condition"]])
    assert role_id(template, "TenantHarnessRole") in readers and role_id(template, "TenantRuntimeRole") in readers
    # The deployment role never needs tenant data.
    assert "cfn-exec-role" not in readers
    # By default there is no auto-delete function, so nothing else is exempt.
    assert "CustomS3AutoDeleteObjectsCustomResourceProviderRole" not in readers


def data_resources(template):
    """The tenant bucket, both tables, the tenant data key and the user pool."""
    keys = {k: v for k, v in resources(template, "AWS::KMS::Key").items() if k.startswith("TenantDataKey")}
    return {**resources(template, "AWS::S3::Bucket"), **resources(template, "AWS::DynamoDB::GlobalTable"),
            **keys, **resources(template, "AWS::Cognito::UserPool")}


def test_tenant_data_survives_stack_deletion_by_default(template):
    data = data_resources(template)
    assert len(data) == 5
    for name, resource in data.items():
        assert resource.get("DeletionPolicy") == "Retain" and resource.get("UpdateReplacePolicy") == "Retain", name
    for table in resources(template, "AWS::DynamoDB::GlobalTable").values():
        assert all(replica.get("DeletionProtectionEnabled") for replica in table["Properties"]["Replicas"])
    [pool] = resources(template, "AWS::Cognito::UserPool").values()
    assert pool["Properties"]["DeletionProtection"] == "ACTIVE"
    # No auto-delete function exists, so no invoke can empty the bucket.
    assert not resources(template, "Custom::S3AutoDeleteObjects")


def test_sandbox_deploys_delete_everything(sandbox_template):
    for name, resource in data_resources(sandbox_template).items():
        assert resource.get("DeletionPolicy") == "Delete", name
    assert resources(sandbox_template, "Custom::S3AutoDeleteObjects")
    # The auto-delete function must be able to list, delete and lock the bucket on stack deletion.
    statements = bucket_statements(sandbox_template)
    for sid in ("TenantDataOnlyThroughTenantRoles", "ListingOnlyThroughTenantRoles", "BucketConfigOnlyThroughDeployments"):
        assert "CustomS3AutoDeleteObjectsCustomResourceProviderRole" in json.dumps(statements[sid]["Condition"]), sid


def test_bucket_settings_change_only_through_deployments(template):
    config = bucket_statements(template)["BucketConfigOnlyThroughDeployments"]
    assert config["Effect"] == "Deny"
    assert {"s3:PutBucketPolicy", "s3:DeleteBucketPolicy", "s3:PutBucketVersioning",
            "s3:PutReplicationConfiguration", "s3:PutLifecycleConfiguration"} <= set(config["Action"])
    allowed = json.dumps(config["Condition"])
    assert "cfn-exec-role-111111111111-ap-southeast-2" in allowed
    assert role_id(template, "TenantHarnessRole") not in allowed


def test_tenant_context_key_is_hmac(template):
    keys = [v["Properties"] for v in resources(template, "AWS::KMS::Key").values()]
    assert {"KeySpec": "HMAC_256", "KeyUsage": "GENERATE_VERIFY_MAC"}.items() <= [k for k in keys if "KeySpec" in k][0].items()


def test_users_cannot_change_their_own_tenant(template):
    [pool] = resources(template, "AWS::Cognito::UserPool").values()
    [attr] = [a for a in pool["Properties"]["Schema"] if a["Name"] == "tenant_id"]
    assert attr["Mutable"] is False
    [client] = resources(template, "AWS::Cognito::UserPoolClient").values()
    assert "custom:tenant_id" not in client["Properties"]["WriteAttributes"]
    assert "custom:tenant_id" in client["Properties"]["ReadAttributes"]


def test_the_web_client_is_srp_only_with_no_demo_client_by_default(template):
    clients = resources(template, "AWS::Cognito::UserPoolClient")
    assert len(clients) == 1  # the admin-password demo client exists only with -c demo=true
    [web] = clients.values()
    assert set(web["Properties"]["ExplicitAuthFlows"]) == {"ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"}


def test_the_password_policy_sets_every_field(template):
    [pool] = resources(template, "AWS::Cognito::UserPool").values()
    policy = pool["Properties"]["Policies"]["PasswordPolicy"]
    assert policy["MinimumLength"] >= 12
    assert policy["RequireLowercase"] and policy["RequireUppercase"]
    assert policy["RequireNumbers"] and policy["RequireSymbols"]
    assert policy["TemporaryPasswordValidityDays"] == 1


def test_demo_adds_an_admin_password_client_wired_into_the_authorizer(tmp_path):
    template = synth(tmp_path, demo="true")
    flows = [set(c["Properties"]["ExplicitAuthFlows"]) for c in resources(template, "AWS::Cognito::UserPoolClient").values()]
    assert len(flows) == 2
    admin = [f for f in flows if "ALLOW_ADMIN_USER_PASSWORD_AUTH" in f]
    srp_only = [f for f in flows if "ALLOW_ADMIN_USER_PASSWORD_AUTH" not in f]
    assert len(admin) == 1 and len(srp_only) == 1 and srp_only[0] == {"ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"}
    # The API's JWT authoriser accepts tokens from both clients.
    [authorizer] = resources(template, "AWS::ApiGatewayV2::Authorizer").values()
    assert len(authorizer["Properties"]["JwtConfiguration"]["Audience"]) == 2


FUNCTIONS = ("IngressFunction", "ToolsFunction", "InterceptorFunction", "WorkerFunction", "DeadLetterFunction")
BREAK_GLASS = "arn:aws:iam::111111111111:role/BreakGlass"


def invoke_policies(template):
    """Each function's resource-policy statements by Sid, keyed by the function's construct ID."""
    out = {}
    for policy in resources(template, "AWS::Lambda::ResourcePolicy").values():
        function, _ = policy["Properties"]["ResourceArn"]["Fn::GetAtt"]
        [name] = [f for f in FUNCTIONS if function.startswith(f)]
        out[name] = {s["Sid"]: s for s in policy["Properties"]["PolicyDocument"]["Statement"]}
    return out


def only_deny(statements):
    [deny] = [s for s in statements.values() if s["Effect"] == "Deny"]
    return deny


def test_each_function_is_invoked_only_by_its_own_caller(template):
    by_function = invoke_policies(template)
    assert set(by_function) == set(FUNCTIONS)
    for name, statements in by_function.items():
        deny = only_deny(statements)
        assert deny["Principal"] == "*" and deny["Action"] == "lambda:InvokeFunction", name
        assert ":*" in json.dumps(deny["Resource"]), name  # every version and alias as well
    gateway = {"Fn::GetAtt": [role_id(template, "GatewayRole"), "Arn"]}
    for name in ("ToolsFunction", "InterceptorFunction"):
        assert only_deny(by_function[name])["Condition"] == {"ArnNotEquals": {"aws:PrincipalArn": [gateway]}}, name
    for name, role in (("WorkerFunction", "WorkerRole"), ("DeadLetterFunction", "DeadLetterRole")):
        condition = only_deny(by_function[name])["Condition"]
        assert condition["ArnNotEquals"] == {"aws:PrincipalArn": [{"Fn::GetAtt": [role_id(template, role), "Arn"]}]}
        assert condition["StringNotEquals"] == {"aws:PrincipalServiceName": "lambda.amazonaws.com"}  # the SQS trigger
    ingress = by_function["IngressFunction"]
    allow, deny = ingress["ThisApiInvokes"], ingress["OnlyThisApiInvokesIngress"]
    assert allow["Principal"] == {"Service": "apigateway.amazonaws.com"}
    assert allow["Condition"]["ArnLike"] == deny["Condition"]["ArnNotLike"]
    [api] = resources(template, "AWS::ApiGatewayV2::Api")
    assert {"Ref": api} in deny["Condition"]["ArnNotLike"]["aws:SourceArn"]["Fn::Join"][1]  # this API only


def test_ingress_permissions_are_not_mixed_with_per_route_permissions(template):
    # PutResourcePolicy replaces whatever AddPermission added, so the two must never target one function.
    mixed = [p for p in resources(template, "AWS::Lambda::Permission").values() if any(f in json.dumps(p) for f in FUNCTIONS)]
    assert not mixed
    [integration] = resources(template, "AWS::ApiGatewayV2::Integration").values()
    props = integration["Properties"]
    assert props["IntegrationType"] == "AWS_PROXY" and props["PayloadFormatVersion"] == "2.0"
    assert props["IntegrationUri"]["Fn::GetAtt"][0].startswith("IngressFunction")


def test_break_glass_roles_can_still_invoke_every_function(tmp_path):
    for name, statements in invoke_policies(synth(tmp_path, adminRoleArns=BREAK_GLASS)).items():
        assert BREAK_GLASS in only_deny(statements)["Condition"]["ArnNotEquals"]["aws:PrincipalArn"], name


def test_a_function_nobody_may_invoke_is_rejected():
    with pytest.raises(ValueError, match="at least one permitted invoker"):
        policies.function_invoke_policy(function_arn="arn:aws:lambda:ap-southeast-2:111111111111:function:f", sid="X")


def dns_rules(template):
    """The DNS Firewall rules in priority order, each with its domain list's domains."""
    lists = {k: v["Properties"]["Domains"]
             for k, v in resources(template, "AWS::Route53Resolver::FirewallDomainList").items()}
    [group] = resources(template, "AWS::Route53Resolver::FirewallRuleGroup").values()
    rules = sorted(group["Properties"]["FirewallRules"], key=lambda r: r["Priority"])
    return [{**r, "Domains": lists[r["FirewallDomainListId"]["Fn::GetAtt"][0]]} for r in rules]


def test_dns_resolves_only_this_regions_s3_and_logs_names(template):
    allow, noise, rest = dns_rules(template)
    assert allow["Action"] == "ALLOW"
    assert sorted(allow["Domains"]) == ["*.s3.ap-southeast-2.amazonaws.com", "logs.ap-southeast-2.amazonaws.com",
                                        "s3.ap-southeast-2.amazonaws.com"]
    # A bucket name is a CNAME to an S3 host name outside the list; the allowed name's chain is trusted.
    assert allow["FirewallDomainRedirectionAction"] == "TRUST_REDIRECTION_DOMAIN"
    assert noise["Domains"] == ["time.aws.com"] and rest["Domains"] == ["*"]
    for blocked in (noise, rest):
        assert blocked["Action"] == "BLOCK" and blocked["BlockResponse"] == "NODATA"
        assert "FirewallDomainRedirectionAction" not in blocked


def test_a_blocked_lookup_raises_an_alarm(template):
    [alarm] = [a["Properties"] for a in resources(template, "AWS::CloudWatch::Alarm").values()
               if a["Properties"].get("MetricName") == "FirewallRuleQueryVolume"]
    assert alarm["Namespace"] == "AWS/Route53Resolver" and alarm["Statistic"] == "Sum"
    assert alarm["Threshold"] == 1 and alarm["ComparisonOperator"] == "GreaterThanOrEqualToThreshold"
    assert alarm["TreatMissingData"] == "notBreaching"
    dimensions = {d["Name"]: d["Value"] for d in alarm["Dimensions"]}
    # Only the catch-all list counts, so the expected time.aws.com lookups don't hold it in alarm.
    assert dimensions["FirewallDomainListId"] == dns_rules(template)[-1]["FirewallDomainListId"]
    assert dimensions["FirewallRuleGroupId"]["Fn::GetAtt"][0].startswith("DnsRules")


def test_alert_mode_is_opt_in_and_warns(tmp_path):
    app = cdk.App(outdir=str(tmp_path), context={"dnsFirewallMode": "ALERT"})
    stack = SplitHarnessStack(app, "SplitHarness", env=cdk.Environment(account="111111111111", region="ap-southeast-2"))
    assertions.Annotations.from_stack(stack).has_warning(
        "*", assertions.Match.string_like_regexp("dnsFirewallMode=ALERT"))
    rules = dns_rules(app.synth().get_stack_by_name("SplitHarness").template)
    assert [r["Action"] for r in rules] == ["ALLOW", "ALERT", "ALERT"]
    assert all("BlockResponse" not in r for r in rules)


def test_the_dead_letter_queue_has_a_consumer_that_only_updates_request_records(template):
    [dlq] = [k for k in resources(template, "AWS::SQS::Queue") if k.startswith("TurnDlq")]
    [mapping] = [m["Properties"] for m in resources(template, "AWS::Lambda::EventSourceMapping").values()
                 if dlq in json.dumps(m["Properties"]["EventSourceArn"])]
    assert mapping["FunctionName"]["Ref"].startswith("DeadLetterFunction")
    assert mapping["BatchSize"] == 1 and mapping["FunctionResponseTypes"] == ["ReportBatchItemFailures"]
    granted = {a for s in statements_for(template, "DeadLetterRole") for a in actions(s)}
    assert {a for a in granted if a.startswith("dynamodb:")} == {"dynamodb:GetItem", "dynamodb:UpdateItem",
                                                                 "dynamodb:DeleteItem"}
    assert not [a for a in granted if a.split(":")[0] in ("s3", "kms", "sts", "bedrock", "bedrock-agentcore")]


def function_props(template, prefix):
    [fn] = [v["Properties"] for k, v in resources(template, "AWS::Lambda::Function").items() if k.startswith(prefix)]
    return fn


def test_one_tenant_cannot_flood_the_queue(template):
    assert function_props(template, "IngressFunction")["Environment"]["Variables"]["MAX_OPEN_REQUESTS"] == "20"
    [stage] = resources(template, "AWS::ApiGatewayV2::Stage").values()
    throttle = stage["Properties"]["DefaultRouteSettings"]
    assert throttle == {"ThrottlingRateLimit": 50, "ThrottlingBurstLimit": 100}
    [mapping] = [m["Properties"] for m in resources(template, "AWS::Lambda::EventSourceMapping").values()
                 if m["Properties"]["FunctionName"]["Ref"].startswith("WorkerFunction")]
    reserved = function_props(template, "WorkerFunction")["ReservedConcurrentExecutions"]
    assert reserved == mapping["ScalingConfig"]["MaximumConcurrency"] == 66  # 200 req/min / 3 calls per turn
    [backlog] = [a["Properties"] for a in resources(template, "AWS::CloudWatch::Alarm").values()
                 if a["Properties"].get("MetricName") == "ApproximateAgeOfOldestMessage"]
    assert backlog["Namespace"] == "AWS/SQS" and backlog["Threshold"] == 900 and backlog["Statistic"] == "Maximum"


def key_policy(template, prefix):
    [key] = [v for k, v in resources(template, "AWS::KMS::Key") .items() if k.startswith(prefix)]
    return {s.get("Sid"): s for s in key["Properties"]["KeyPolicy"]["Statement"]}


def test_the_hmac_key_mints_only_through_the_worker_and_verifies_only_through_the_interceptor(template):
    stmts = key_policy(template, "TenantContextKey")
    gen, ver = stmts["WorkerMintsTenantContext"], stmts["InterceptorVerifiesTenantContext"]
    assert gen["Action"] == "kms:GenerateMac" and role_id(template, "WorkerRole") in json.dumps(gen["Principal"])
    assert ver["Action"] == "kms:VerifyMac" and role_id(template, "InterceptorRole") in json.dumps(ver["Principal"])
    # The account's administration statement grants no MAC operation, so an IAM grant alone can't forge one.
    admin = json.dumps(stmts["KeyAdministrationByAccount"]["Action"])
    assert "GenerateMac" not in admin and "VerifyMac" not in admin and '"kms:*"' not in json.dumps(list(stmts.values()))


def test_the_data_key_is_usable_only_through_s3_by_the_tenant_roles(template):
    stmts = key_policy(template, "TenantDataKey")
    use = stmts["TenantRolesUseThroughS3"]
    assert use["Condition"]["StringEquals"]["kms:ViaService"] == "s3.ap-southeast-2.amazonaws.com"
    principals = json.dumps(use["Principal"])
    assert role_id(template, "TenantHarnessRole") in principals and role_id(template, "TenantRuntimeRole") in principals
    assert "kms:GenerateDataKey*" in use["Action"] and "kms:Decrypt" in use["Action"]
    admin = json.dumps(stmts["KeyAdministrationByAccount"]["Action"])
    assert "Decrypt" not in admin and "GenerateDataKey" not in admin
    assert "BreakGlassReadsTenantData" not in stmts  # no break-glass statement without -c adminRoleArns


def test_break_glass_can_read_tenant_data_through_the_key(tmp_path):
    stmts = key_policy(synth(tmp_path, adminRoleArns=BREAK_GLASS), "TenantDataKey")
    glass = stmts["BreakGlassReadsTenantData"]
    assert glass["Action"] == ["kms:Decrypt", "kms:DescribeKey"] and BREAK_GLASS in json.dumps(glass["Principal"])


def test_the_gateway_declares_the_protocol_version_the_client_sends(template):
    from worker import gateway_client
    [gw] = resources(template, "AWS::BedrockAgentCore::Gateway").values()
    supported = gw["Properties"]["ProtocolConfiguration"]["Mcp"]["SupportedVersions"]
    assert gateway_client.MCP_PROTOCOL_VERSION in supported  # not left empty / defaulted


def test_the_runtime_is_pinned_to_platform_v2(template, tmp_path):
    [runtime] = resources(template, "AWS::BedrockAgentCore::Runtime").values()
    assert runtime["Properties"]["PlatformVersion"] == "V2"  # not left to the account default (V1)
    override = synth(tmp_path, runtimePlatformVersion="V1")
    [pinned] = resources(override, "AWS::BedrockAgentCore::Runtime").values()
    assert pinned["Properties"]["PlatformVersion"] == "V1"


def test_a_cross_region_profile_that_does_not_cover_the_region_is_refused():
    from infra.stack import check_model_region
    assert check_model_region("amazon.nova-lite-v1:0", "ap-southeast-2") is None  # in-Region: valid anywhere
    assert check_model_region("apac.amazon.nova-lite-v1:0", "ap-southeast-2") is None  # geo covers the Region
    with pytest.raises(ValueError, match="does not route to ap-southeast-2"):
        check_model_region("us.amazon.nova-lite-v1:0", "ap-southeast-2")


def test_the_default_model_is_in_region(template):
    [model] = [s for s in statements_for(template, "WorkerRole") if s.get("Sid") == "PinnedModelsOnly"]
    text = json.dumps(model["Resource"])
    assert "foundation-model/amazon.nova-lite-v1:0" in text  # a Region-local foundation model
    assert "inference-profile/" not in text  # not a cross-Region profile by default


def test_availability_zones_can_be_pinned(tmp_path):
    template = synth(tmp_path, availabilityZones="ap-southeast-2a,ap-southeast-2b")
    azs = {s["Properties"]["AvailabilityZone"] for s in resources(template, "AWS::EC2::Subnet").values()}
    assert azs == {"ap-southeast-2a", "ap-southeast-2b"}


def test_no_break_glass_and_no_demo_operator_by_default(template):
    # Nothing is granted past the tenant policies, and the demo operator exists only with -c demo=true.
    assert "BreakGlassReadsTenantData" not in key_policy(template, "TenantDataKey")
    roles = {k for k in resources(template, "AWS::IAM::Role")}
    assert not any(r.startswith("DemoOperatorRole") for r in roles)


def test_the_demo_operator_role_is_scoped_and_not_break_glass(tmp_path):
    template = synth(tmp_path, demo="true")
    [role_key] = [k for k in resources(template, "AWS::IAM::Role") if k.startswith("DemoOperatorRole")]
    granted = [a for s in statements_for_role_key(template, role_key) for a in actions(s)]
    # It can set up the demo and read the bucket, and may attempt the AgentCore calls for the refusal check.
    assert "bedrock-agentcore:InvokeAgentRuntime" in granted and "iam:SimulatePrincipalPolicy" in granted
    # The Gateway checks pass only on the Gateway's 403: with this grant, its resource policy is the only deny.
    assert "bedrock-agentcore:InvokeGateway" in granted
    assert "s3:GetObject" in granted and "cognito-idp:AdminInitiateAuth" in granted
    # But it is not break-glass: the runtime, Gateway and bucket policies do not list it.
    assert "*" not in granted and "s3:*" not in granted and "kms:Decrypt" not in granted  # KMS is via the key policy
    runtime = [s for s in resources(template, "AWS::BedrockAgentCore::ResourcePolicy").values()
               if "ExecutionRuntime" in json.dumps(s["Properties"]["ResourceArn"])]
    assert role_key not in json.dumps(runtime)  # the runtime policy never admits the operator
    gateway = [s for s in resources(template, "AWS::BedrockAgentCore::ResourcePolicy").values()
               if "ToolGateway" in json.dumps(s["Properties"]["ResourceArn"])]
    assert gateway and role_key not in json.dumps(gateway)  # nor does the Gateway policy


def statements_for_role_key(template, role_key):
    out = []
    for policy in resources(template, "AWS::IAM::Policy").values():
        if {"Ref": role_key} in policy["Properties"].get("Roles", []):
            out.extend(policy["Properties"]["PolicyDocument"]["Statement"])
    return out


def test_the_demo_operator_reads_usage_for_the_cost_tab_from_this_stack_only(tmp_path):
    template = synth(tmp_path, demo="true")
    [role_key] = [k for k in resources(template, "AWS::IAM::Role") if k.startswith("DemoOperatorRole")]
    statements = statements_for_role_key(template, role_key)
    by_sid = {s.get("Sid"): s for s in statements}

    def resource_id(kind, prefix):
        [key] = [k for k in resources(template, kind) if k.startswith(prefix)]
        return key

    # demo/measured.py's reads, each on this stack's own resource.
    assert by_sid["MeasureListThisStack"]["Action"] == "cloudformation:ListStackResources"
    assert by_sid["MeasureListThisStack"]["Resource"] == {"Ref": "AWS::StackId"}
    worker = resource_id("AWS::Lambda::Function", "WorkerFunction")
    assert by_sid["MeasureReadWorkerConfig"]["Action"] == "lambda:GetFunctionConfiguration"
    assert by_sid["MeasureReadWorkerConfig"]["Resource"] == {"Fn::GetAtt": [worker, "Arn"]}
    reports = by_sid["MeasureReadWorkerReports"]
    assert reports["Action"] == "logs:FilterLogEvents"
    assert reports["Resource"]["Fn::Join"][1][-2:] == [
        ":logs:ap-southeast-2:111111111111:log-group:", {"Ref": resource_id("AWS::Logs::LogGroup", "WorkerFunctionLogs")}]
    coordination = resource_id("AWS::DynamoDB::GlobalTable", "Coordination")
    assert by_sid["MeasureCountTurns"]["Action"] == "dynamodb:Scan"
    assert by_sid["MeasureCountTurns"]["Resource"] == {"Fn::GetAtt": [coordination, "Arn"]}
    # GetMetricData takes no resource for standard metrics, so it is held to the stack's Region.
    metrics = by_sid["MeasureReadRuntimeMetrics"]
    assert metrics["Action"] == "cloudwatch:GetMetricData" and metrics["Resource"] == "*"
    assert metrics["Condition"] == {"StringEquals": {"aws:RequestedRegion": "ap-southeast-2"}}
    # Nothing broader: no service wildcards, and these actions appear in no other statement.
    granted = [a for s in statements for a in actions(s)]
    assert not [a for a in granted if a.endswith(":*") or a == "*"]
    measure = {"cloudformation:ListStackResources", "lambda:GetFunctionConfiguration", "logs:FilterLogEvents",
               "dynamodb:Scan", "cloudwatch:GetMetricData"}
    assert sorted(a for a in granted if a in measure) == sorted(measure)
    # The break-glass statement on the data key is untouched: read only, and absent unless asked for.
    assert "BreakGlassReadsTenantData" not in key_policy(template, "TenantDataKey")


def test_worker_concurrency_follows_the_model_request_quota(tmp_path):
    # Default: in-Region 200 req/min over 3 calls per turn.
    assert function_props(synth(tmp_path / "a"), "WorkerFunction")["ReservedConcurrentExecutions"] == 66
    # The cross-Region quota doubles it.
    cross = synth(tmp_path / "b", modelRequestsPerMinute="400")
    assert function_props(cross, "WorkerFunction")["ReservedConcurrentExecutions"] == 133
    # An explicit worker cap wins over the derived value.
    override = synth(tmp_path / "c", workerMaxConcurrency="40")
    assert function_props(override, "WorkerFunction")["ReservedConcurrentExecutions"] == 40
