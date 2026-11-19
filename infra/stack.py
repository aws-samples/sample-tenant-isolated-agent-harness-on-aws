# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Split harness: shared Strands control plane on Lambda, tenant data and execution plane on
AgentCore Gateway and an isolated AgentCore Runtime, with an ABAC tenant isolation boundary."""

from __future__ import annotations

import os
from pathlib import Path

from aws_cdk import (
    Annotations,
    CfnOutput,
    Duration,
    Fn,
    RemovalPolicy,
    Stack,
    Tags,
    aws_apigatewayv2 as apigw,
    aws_apigatewayv2_authorizers as authorizers,
    aws_bedrockagentcore as agentcore,
    aws_cloudwatch as cloudwatch,
    aws_cognito as cognito,
    aws_dynamodb as dynamodb,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as lambda_,
    aws_lambda_event_sources as event_sources,
    aws_logs as logs,
    aws_route53resolver as resolver,
    aws_s3 as s3,
    aws_s3_assets as s3_assets,
    aws_sqs as sqs,
)
from constructs import Construct

from infra import policies

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
BUILD = ROOT / ".build"
# In-Region by default: inference stays in the deployment Region. In Sydney, Nova Lite's default
# in-Region quota is half the cross-Region one (200 against 400 requests a minute); a cross-Region
# profile (apac.amazon.nova-lite-v1:0) trades Region residency for the higher rate.
DEFAULT_MODEL_ID = "amazon.nova-lite-v1:0"
GEO_PREFIXES = ("apac.", "au.", "us.", "eu.", "jp.", "global.")
# A cross-Region inference profile only routes within its own geography, so its prefix must match
# the deployment Region's geography.
REGION_GEO_PREFIXES = {"us": {"us."}, "ca": {"us."}, "eu": {"eu."}, "ap": {"apac.", "au.", "jp."}}
WORKER_TIMEOUT = Duration.minutes(10)
SESSION_MOUNT = "/mnt/session"
# The AgentCore Runtime contract requires the server to listen on all interfaces inside its microVM.
# Nothing is published: InvokeAgentRuntime (SigV4) is the only way in.
RUNTIME_BIND_HOST = "0.0.0.0"  # nosec B104
# The MCP protocol version the Gateway accepts. Must match worker/gateway_client.py
# MCP_PROTOCOL_VERSION: the Gateway rejects any other version with JSON-RPC -32600.
MCP_PROTOCOL_VERSION = "2025-03-26"

GATEWAY_TOOLS = [
    ("list_jobs", "List the business's jobs, optionally filtered by status.",
     {"status": ("string", "Optional status filter, for example open or scheduled.")}, []),
    ("get_job", "Get one job by its ID.", {"jobId": ("string", "The job ID.")}, ["jobId"]),
    ("create_quote", "Create a quote for a job.",
     {"jobId": ("string", "The job ID."), "description": ("string", "What the quote covers."),
      "amountCents": ("integer", "Quote total in cents.")}, ["jobId", "description", "amountCents"]),
]
TENANT_ARGUMENT_DOC = "Injected by the Gateway interceptor from the verified tenant context. Never set by the model."
MUTATING_TOOL_NAMES = frozenset({"create_quote"})
OPERATION_KEY_DOC = "Set by the harness from requestId and toolUseId so a retried call returns its stored outcome. Never set by the model."


def asset_dir(relative: str) -> str:
    path = BUILD / relative
    if not path.exists():
        if os.environ.get("SPLIT_HARNESS_ALLOW_PLACEHOLDER_ASSETS") != "1":
            raise RuntimeError(f"{path} is missing: run scripts/build_assets.sh before synth or deploy")
        path.mkdir(parents=True, exist_ok=True)
        (path / "PLACEHOLDER").write_text("synth-only placeholder; run scripts/build_assets.sh before deploy\n")
    return str(path)


class LambdaProxyIntegration(apigw.HttpRouteIntegration):
    """HTTP API Lambda proxy integration that leaves the function's invoke permissions alone.

    CDK's HttpLambdaIntegration adds an AWS::Lambda::Permission per route. Ingress keeps a single
    AWS::Lambda::ResourcePolicy instead (allow this API, deny everyone else), and the two must not be
    mixed: PutResourcePolicy replaces whatever AddPermission added.
    """

    def __init__(self, integration_id: str, handler: lambda_.IFunction):
        super().__init__(integration_id)
        self._handler = handler

    def bind(self, *, route: apigw.IHttpRoute, scope: Construct) -> apigw.HttpRouteIntegrationConfig:
        return apigw.HttpRouteIntegrationConfig(type=apigw.HttpIntegrationType.AWS_PROXY,
                                                uri=self._handler.function_arn,
                                                payload_format_version=apigw.PayloadFormatVersion.VERSION_2_0)


def model_arns(model_id: str, region: str, account: str) -> list[str]:
    for prefix in GEO_PREFIXES:
        if model_id.startswith(prefix):
            base = model_id[len(prefix):]
            # Cross-Region inference: the profile, plus the one base model in the destination Regions.
            return [f"arn:aws:bedrock:{region}:{account}:inference-profile/{model_id}",
                    f"arn:aws:bedrock:*::foundation-model/{base}"]
    return [f"arn:aws:bedrock:{region}::foundation-model/{model_id}"]


def check_model_region(model_id: str, region: str) -> str | None:
    """Reject a cross-Region inference profile whose geography does not cover the deployment Region.

    Returns a warning string for a geo-prefixed model in a Region this map doesn't know, None
    otherwise. A bare (in-Region) model id and the global profile are valid wherever the model is.
    """
    geo = next((p for p in GEO_PREFIXES if model_id.startswith(p)), None)
    if geo is None or geo == "global.":
        return None
    allowed = REGION_GEO_PREFIXES.get(region.split("-")[0])
    if allowed is None:
        return f"cannot confirm model {model_id!r} is offered in {region}; check the inference-profile geography"
    if geo not in allowed:
        raise ValueError(
            f"model {model_id!r} is a '{geo.rstrip('.')}' cross-Region inference profile, which does not route "
            f"to {region}; use the in-Region id 'amazon.nova-lite-v1:0' or a profile for this Region's geography")
    return None


class SplitHarnessStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("project", "split-harness")
        ctx = self.node.try_get_context
        model_id = ctx("modelId") or DEFAULT_MODEL_ID
        model_warning = check_model_region(model_id, self.region)
        if model_warning:
            Annotations.of(self).add_warning(model_warning)
        dns_mode = (ctx("dnsFirewallMode") or "BLOCK").upper()
        admin_roles = [a for a in (ctx("adminRoleArns") or "").split(",") if a]
        if ctx("eventProducerRoleNames"):
            raise ValueError("eventProducerRoleNames is no longer accepted (a role name alone did not pin the "
                             "account); use -c eventProducerRoleArns with full role ARNs, account included")
        producer_role_arns = ctx("eventProducerRoleArns") or ""
        event_turn_tools = (ctx("eventTurnTools") or "scoped").lower()
        if event_turn_tools not in ("scoped", "all"):
            raise ValueError("eventTurnTools must be 'scoped' (default) or 'all'")
        demo = (ctx("demo") or "").lower() == "true"
        prefix_list_id = ctx("s3PrefixListId")
        suffix = self.node.addr[:6]
        # Tenant data, its key and the user pool survive `cdk destroy` and are deletion-protected.
        # Only a throwaway sandbox (-c sandbox=true) deletes them, and only there does the bucket
        # empty itself through CDK's auto-delete function.
        sandbox = str(ctx("sandbox") or "").lower() in ("1", "true", "yes")
        data_removal = RemovalPolicy.DESTROY if sandbox else RemovalPolicy.RETAIN

        # ---------------------------------------------------------------- tenant data plane
        # Explicit key policies, not CDK's default (which lets any IAM principal in the account use
        # the key). The account root keeps administration only; the crypto operations are granted
        # to named principals below, once the roles exist.
        data_key = kms.Key(self, "TenantDataKey", description="split-harness tenant data",
                           enable_key_rotation=True, pending_window=Duration.days(7),
                           policy=self._kms_admin_only_policy(), removal_policy=data_removal)
        token_key = kms.Key(self, "TenantContextKey", description="split-harness tenant context HMAC",
                            key_spec=kms.KeySpec.HMAC_256, key_usage=kms.KeyUsage.GENERATE_VERIFY_MAC,
                            policy=self._kms_admin_only_policy(),
                            pending_window=Duration.days(7), removal_policy=RemovalPolicy.DESTROY)
        bucket = s3.Bucket(
            self, "TenantData",
            encryption=s3.BucketEncryption.KMS, encryption_key=data_key, bucket_key_enabled=False,
            enforce_ssl=True, minimum_tls_version=1.2, versioned=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            removal_policy=data_removal, auto_delete_objects=sandbox,
            lifecycle_rules=[
                s3.LifecycleRule(id="ExpireTransfer", prefix="transfer/", expiration=Duration.days(1),
                                 noncurrent_version_expiration=Duration.days(1)),
                s3.LifecycleRule(id="TrimOldVersions", noncurrent_version_expiration=Duration.days(30),
                                 abort_incomplete_multipart_upload_after=Duration.days(1)),
            ],
        )
        coordination = dynamodb.TableV2(
            self, "Coordination",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing=dynamodb.Billing.on_demand(), time_to_live_attribute="expiresAt",
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True),
            removal_policy=data_removal, deletion_protection=not sandbox)
        saas = dynamodb.TableV2(
            self, "SaasData",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing=dynamodb.Billing.on_demand(), removal_policy=data_removal, deletion_protection=not sandbox)

        # ---------------------------------------------------------------- runtime network
        # CDK picks two AZs by name. AgentCore VPC mode is not offered in every AZ: Sydney supports
        # all three AZ IDs, but us-east-1 supports only use1-az1, use1-az2 and use1-az4, and an AZ
        # name maps to a different AZ ID in each account. Pass -c availabilityZones=<name>,<name>
        # (names whose AZ IDs are supported; `aws ec2 describe-availability-zones` shows the mapping).
        az_names = [a for a in (ctx("availabilityZones") or "").split(",") if a]
        vpc_azs = {"availability_zones": az_names} if az_names else {"max_azs": 2}
        vpc = ec2.Vpc(
            self, "RuntimeVpc", ip_addresses=ec2.IpAddresses.cidr("10.42.0.0/16"), nat_gateways=0,
            subnet_configuration=[ec2.SubnetConfiguration(name="runtime", cidr_mask=22,
                                                          subnet_type=ec2.SubnetType.PRIVATE_ISOLATED)],
            restrict_default_security_group=True, **vpc_azs)
        isolated = ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED)
        s3_endpoint = vpc.add_gateway_endpoint("S3Endpoint", service=ec2.GatewayVpcEndpointAwsService.S3,
                                               subnets=[isolated])
        for statement in policies.s3_endpoint_statements(bucket_arn=bucket.bucket_arn, region=self.region):
            s3_endpoint.add_to_policy(iam.PolicyStatement.from_json(statement))
        runtime_sg = ec2.SecurityGroup(self, "RuntimeSg", vpc=vpc, allow_all_outbound=False,
                                       description="AgentCore Runtime ENIs: S3 and Logs only")
        endpoint_sg = ec2.SecurityGroup(self, "EndpointSg", vpc=vpc, allow_all_outbound=False,
                                        description="Interface endpoints for the runtime VPC")
        endpoint_sg.add_ingress_rule(runtime_sg, ec2.Port.tcp(443), "runtime to interface endpoints")
        runtime_sg.add_egress_rule(endpoint_sg, ec2.Port.tcp(443), "CloudWatch Logs endpoint")
        # HTTPS egress only to S3 (the gateway endpoint) and the Logs endpoint above. The prefix
        # list ID is looked up at synth; override with -c s3PrefixListId=pl-... if lookups are off.
        s3_prefix_list = prefix_list_id or ec2.PrefixList.from_lookup(
            self, "S3PrefixList", prefix_list_name=f"com.amazonaws.{self.region}.s3").prefix_list_id
        runtime_sg.add_egress_rule(ec2.Peer.prefix_list(s3_prefix_list), ec2.Port.tcp(443), "S3 gateway endpoint")
        logs_endpoint = vpc.add_interface_endpoint(
            "LogsEndpoint", service=ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
            subnets=isolated, security_groups=[endpoint_sg], private_dns_enabled=True)
        logs_endpoint.add_to_policy(iam.PolicyStatement.from_json(
            policies.logs_endpoint_statement(account=self.account, region=self.region)))
        vpc.add_flow_log("FlowLog", traffic_type=ec2.FlowLogTrafficType.ALL,
                         destination=ec2.FlowLogDestination.to_cloud_watch_logs(
                             logs.LogGroup(self, "FlowLogs", retention=logs.RetentionDays.ONE_MONTH,
                                           removal_policy=RemovalPolicy.DESTROY)))
        dns_rule_group_id = self._dns_firewall(vpc, dns_mode, suffix)

        # ---------------------------------------------------------------- compute roles
        lambda_trust = iam.ServicePrincipal("lambda.amazonaws.com")
        basic = iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole")
        ingress_role = iam.Role(self, "IngressRole", assumed_by=lambda_trust, managed_policies=[basic])
        worker_role = iam.Role(self, "WorkerRole", assumed_by=lambda_trust, managed_policies=[basic],
                               description="Shared harness: no tenant data access of its own")
        interceptor_role = iam.Role(self, "InterceptorRole", assumed_by=lambda_trust, managed_policies=[basic])
        tools_fn_role = iam.Role(self, "ToolsFunctionRole", assumed_by=lambda_trust, managed_policies=[basic])

        harness_role = self._tenant_role("TenantHarnessRole", worker_role,
                                         "Tenant-scoped (TenantId session tag) conversation state and Gateway")
        runtime_tenant_role = self._tenant_role("TenantRuntimeRole", worker_role,
                                                "Tenant-scoped workspace credentials handed to the isolated runtime")
        tools_role = self._tenant_role("TenantToolsRole", tools_fn_role,
                                       "Tenant-scoped SaaS data access for Gateway tools")

        # The person running the demo assumes this scoped role instead of their own admin role, so
        # the sample never asks a reader to grant themselves break-glass. Created only with
        # -c demo=true. Its permissions are attached once the resources it touches exist.
        demo_operator_role = iam.Role(
            self, "DemoOperatorRole", assumed_by=iam.AccountRootPrincipal(),
            max_session_duration=Duration.hours(1),
            description="Least-privilege role for running the demo") if demo else None

        # Bucket backstop: tenant objects and listings only through the tenant roles; the bucket policy
        # and data-handling settings only through CloudFormation deployments (plus optional break-glass).
        for statement in policies.bucket_guardrail_statements(
                bucket_arn=bucket.bucket_arn,
                tenant_role_arns=[harness_role.role_arn, runtime_tenant_role.role_arn],
                deployer_role_arns=self._deployment_role_arns(),
                admin_role_arns=admin_roles,
                cleanup_role_arns=self._auto_delete_role_arns(),
                demo_operator_role_arns=[demo_operator_role.role_arn] if demo_operator_role else []):
            bucket.add_to_resource_policy(iam.PolicyStatement.from_json(statement))

        # The data key is usable only through S3, and only by the tenant roles (the SSE-KMS requester
        # is the tenant role). The HMAC key mints only through the worker and verifies only through
        # the interceptor, so holding kms:GenerateMac in an IAM policy is not enough to forge a
        # tenant context. Break-glass roles may read tenant objects (Decrypt through S3).
        mac = {"StringEquals": {"kms:MacAlgorithm": "HMAC_SHA_256"}}
        via_s3 = {"StringEquals": {"kms:ViaService": f"s3.{self.region}.amazonaws.com"}}
        crypto = ["kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:DescribeKey"]
        data_key.add_to_resource_policy(iam.PolicyStatement(
            sid="TenantRolesUseThroughS3", actions=crypto, resources=["*"], conditions=via_s3,
            principals=[iam.ArnPrincipal(harness_role.role_arn), iam.ArnPrincipal(runtime_tenant_role.role_arn)]))
        token_key.add_to_resource_policy(iam.PolicyStatement(
            sid="WorkerMintsTenantContext", actions=["kms:GenerateMac"], resources=["*"], conditions=mac,
            principals=[iam.ArnPrincipal(worker_role.role_arn)]))
        token_key.add_to_resource_policy(iam.PolicyStatement(
            sid="InterceptorVerifiesTenantContext", actions=["kms:VerifyMac"], resources=["*"], conditions=mac,
            principals=[iam.ArnPrincipal(interceptor_role.role_arn)]))
        if admin_roles:
            data_key.add_to_resource_policy(iam.PolicyStatement(
                sid="BreakGlassReadsTenantData", actions=["kms:Decrypt", "kms:DescribeKey"], resources=["*"],
                principals=[iam.ArnPrincipal(a) for a in admin_roles]))

        # ---------------------------------------------------------------- Lambda functions
        code = lambda_.Code.from_asset(str(SRC), exclude=["runtime", "runtime/**", "**/__pycache__", "**/*.pyc"])

        def function(fid: str, handler: str, role: iam.Role, timeout: Duration, memory: int = 512,
                     env: dict | None = None, layers: list | None = None,
                     reserved: int | None = None) -> lambda_.Function:
            return lambda_.Function(
                self, fid, runtime=lambda_.Runtime.PYTHON_3_13, architecture=lambda_.Architecture.ARM_64,
                handler=handler, code=code, role=role, timeout=timeout, memory_size=memory,
                reserved_concurrent_executions=reserved,
                environment=env or {}, layers=layers or [], logging_format=lambda_.LoggingFormat.JSON,
                log_group=logs.LogGroup(self, f"{fid}Logs", retention=logs.RetentionDays.ONE_MONTH,
                                        removal_policy=RemovalPolicy.DESTROY))

        tools_fn = function("ToolsFunction", "tools.handler.handler", tools_fn_role, Duration.seconds(15),
                            env={"SAAS_TABLE": saas.table_name, "TOOLS_ROLE_ARN": tools_role.role_arn})
        # Token audience = the Gateway's name, known at synth time. Using the generated gateway ID
        # would make the interceptor depend on the gateway that depends on the interceptor.
        gateway_name = f"split-harness-tools-{suffix}"
        interceptor_fn = function("InterceptorFunction", "interceptor.handler.handler", interceptor_role,
                                  Duration.seconds(10), env={"TENANT_TOKEN_KEY_ID": token_key.key_arn,
                                                             "TOKEN_AUDIENCE": gateway_name})

        # ---------------------------------------------------------------- AgentCore Gateway
        gateway_role = iam.Role(self, "GatewayRole", assumed_by=iam.ServicePrincipal(
            "bedrock-agentcore.amazonaws.com", conditions={
                "StringEquals": {"aws:SourceAccount": self.account},
                "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:gateway/*"}}))
        tools_fn.grant_invoke(gateway_role)
        interceptor_fn.grant_invoke(gateway_role)
        gateway = agentcore.CfnGateway(
            self, "ToolGateway", name=gateway_name, protocol_type="MCP",
            authorizer_type="AWS_IAM", role_arn=gateway_role.role_arn,
            description="Governed SaaS tools for the shared harness",
            # Pin the accepted MCP version instead of leaving protocolConfiguration empty, so the
            # version the worker sends is one the Gateway declares.
            protocol_configuration=agentcore.CfnGateway.GatewayProtocolConfigurationProperty(
                mcp=agentcore.CfnGateway.MCPGatewayConfigurationProperty(
                    supported_versions=[MCP_PROTOCOL_VERSION])),
            interceptor_configurations=[agentcore.CfnGateway.GatewayInterceptorConfigurationProperty(
                interceptor=agentcore.CfnGateway.InterceptorConfigurationProperty(
                    lambda_=agentcore.CfnGateway.LambdaInterceptorConfigurationProperty(arn=interceptor_fn.function_arn)),
                interception_points=["REQUEST"],
                input_configuration=agentcore.CfnGateway.InterceptorInputConfigurationProperty(pass_request_headers=True),
            )])
        gateway.node.add_dependency(gateway_role)
        target = agentcore.CfnGatewayTarget(
            self, "SaasTarget", gateway_identifier=gateway.attr_gateway_identifier, name="saas",
            description="Demo trades-business SaaS API",
            target_configuration=agentcore.CfnGatewayTarget.TargetConfigurationProperty(
                mcp=agentcore.CfnGatewayTarget.McpTargetConfigurationProperty(
                    lambda_=agentcore.CfnGatewayTarget.McpLambdaTargetConfigurationProperty(
                        lambda_arn=tools_fn.function_arn,
                        tool_schema=agentcore.CfnGatewayTarget.ToolSchemaProperty(
                            inline_payload=[self._tool_definition(*t) for t in GATEWAY_TOOLS])))),
            credential_provider_configurations=[agentcore.CfnGatewayTarget.CredentialProviderConfigurationProperty(
                credential_provider_type="GATEWAY_IAM_ROLE")])
        target.node.add_dependency(gateway_role)

        # ---------------------------------------------------------------- isolated AgentCore Runtime
        # Fixed name so the ARN is predictable. -c agentRuntimeName=... lets an independent copy
        # (see stackName) coexist in an account that already owns the canonical runtime.
        runtime_name = ctx("agentRuntimeName") or "split_harness_exec"
        runtime_code = s3_assets.Asset(self, "RuntimeCode", path=asset_dir("runtime"))
        runtime_exec_role = iam.Role(self, "RuntimeExecutionRole", assumed_by=iam.ServicePrincipal(
            "bedrock-agentcore.amazonaws.com", conditions={
                "StringEquals": {"aws:SourceAccount": self.account},
                "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:*"}}),
            description="Execution role for untrusted code: logs and its own code package only")
        exec_policy = iam.Policy(self, "RuntimeExecutionPolicy", document=iam.PolicyDocument.from_json(
            policies.runtime_execution_policy(
                account=self.account, region=self.region, runtime_name=runtime_name,
                code_object_arn=runtime_code.bucket.arn_for_objects(runtime_code.s3_object_key))))
        exec_policy.attach_to_role(runtime_exec_role)
        runtime = agentcore.CfnRuntime(
            self, "ExecutionRuntime", agent_runtime_name=runtime_name,
            description="Isolated per-tenant execution for the split harness",
            role_arn=runtime_exec_role.role_arn,
            # Pin the platform instead of taking whatever the account defaults to. The code treats
            # session storage as a warm-start cache and re-hydrates the workspace from S3, so it does
            # not depend on either platform's snapshot behaviour. Override with -c runtimePlatformVersion.
            platform_version=(ctx("runtimePlatformVersion") or "V2"),
            agent_runtime_artifact=agentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                code_configuration=agentcore.CfnRuntime.CodeConfigurationProperty(
                    code=agentcore.CfnRuntime.CodeProperty(s3=agentcore.CfnRuntime.S3LocationProperty(
                        bucket=runtime_code.s3_bucket_name, prefix=runtime_code.s3_object_key)),
                    entry_point=["main.py"], runtime="PYTHON_3_13")),
            network_configuration=agentcore.CfnRuntime.NetworkConfigurationProperty(
                network_mode="VPC", network_mode_config=agentcore.CfnRuntime.VpcConfigProperty(
                    subnets=vpc.select_subnets(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED).subnet_ids,
                    security_groups=[runtime_sg.security_group_id])),
            # Per-session (= per-tenant) managed storage only. No shared S3 Files mount: see docs.
            filesystem_configurations=[agentcore.CfnRuntime.FilesystemConfigurationProperty(
                session_storage=agentcore.CfnRuntime.SessionStorageConfigurationProperty(mount_path=SESSION_MOUNT))],
            lifecycle_configuration=agentcore.CfnRuntime.LifecycleConfigurationProperty(
                idle_runtime_session_timeout=300, max_lifetime=28800),
            environment_variables={"SESSION_ROOT": SESSION_MOUNT, "STRICT_SESSION_BINDING": "true",
                                   "RUNTIME_BIND_HOST": RUNTIME_BIND_HOST},
        )
        runtime.node.add_dependency(exec_policy)

        # ---------------------------------------------------------------- tenant role policies
        self._attach(harness_role, "TenantHarnessPolicy", policies.tenant_harness_policy(
            bucket_arn=bucket.bucket_arn, data_key_arn=data_key.key_arn, gateway_arn=gateway.attr_gateway_arn,
            region=self.region))
        self._attach(runtime_tenant_role, "TenantRuntimePolicy", policies.tenant_runtime_policy(
            bucket_arn=bucket.bucket_arn, data_key_arn=data_key.key_arn,
            s3_endpoint_id=s3_endpoint.vpc_endpoint_id, region=self.region))
        self._attach(tools_role, "TenantToolsPolicy", policies.tenant_tools_policy(table_arn=saas.table_arn))
        self._attach(worker_role, "WorkerPolicy", policies.worker_policy(
            harness_role_arn=harness_role.role_arn, runtime_role_arn=runtime_tenant_role.role_arn,
            token_key_arn=token_key.key_arn, runtime_arn=runtime.attr_agent_runtime_arn,
            model_arns=model_arns(model_id, self.region, self.account)))
        self._attach(interceptor_role, "InterceptorPolicy", policies.interceptor_policy(token_key_arn=token_key.key_arn))
        self._attach(tools_fn_role, "ToolsFunctionPolicy", policies.tools_function_policy(tools_role_arn=tools_role.role_arn))

        # ---------------------------------------------------------------- who may invoke what
        agentcore.CfnResourcePolicy(self, "RuntimeResourcePolicy", resource_arn=runtime.attr_agent_runtime_arn,
                                    policy=self.to_json_string(policies.runtime_resource_policy(
                                        runtime_arn=runtime.attr_agent_runtime_arn,
                                        worker_role_arn=worker_role.role_arn, admin_role_arns=admin_roles)))
        agentcore.CfnResourcePolicy(self, "GatewayResourcePolicy", resource_arn=gateway.attr_gateway_arn,
                                    policy=self.to_json_string(policies.gateway_resource_policy(
                                        gateway_arn=gateway.attr_gateway_arn,
                                        harness_role_arn=harness_role.role_arn, admin_role_arns=admin_roles)))

        # ---------------------------------------------------------------- queue + harness worker
        dlq = sqs.Queue(self, "TurnDlq", fifo=True, encryption=sqs.QueueEncryption.SQS_MANAGED, enforce_ssl=True,
                        retention_period=Duration.days(14))
        queue = sqs.Queue(
            self, "TurnQueue", fifo=True, content_based_deduplication=False,
            deduplication_scope=sqs.DeduplicationScope.MESSAGE_GROUP,
            fifo_throughput_limit=sqs.FifoThroughputLimit.PER_MESSAGE_GROUP_ID,
            visibility_timeout=Duration.minutes(60), encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True, dead_letter_queue=sqs.DeadLetterQueue(queue=dlq, max_receive_count=3))
        queue.add_to_resource_policy(iam.PolicyStatement(
            sid="OnlyIngressEnqueuesTurns", effect=iam.Effect.DENY, principals=[iam.AnyPrincipal()],
            actions=["sqs:SendMessage"], resources=[queue.queue_arn],
            conditions={"ArnNotEquals": {"aws:PrincipalArn": ingress_role.role_arn}}))

        layer = lambda_.LayerVersion(self, "AgentDeps", code=lambda_.Code.from_asset(asset_dir("layers/agent")),
                                     compatible_runtimes=[lambda_.Runtime.PYTHON_3_13],
                                     compatible_architectures=[lambda_.Architecture.ARM_64],
                                     description="strands-agents and boto3 for the harness worker")
        # The binding quota is the model's request rate, not Lambda. Nova Lite's default quota in
        # Sydney is 200 requests a minute in-Region (400 cross-Region). At about `modelCallsPerTurn` model calls
        # a turn, the model serves roughly modelRequestsPerMinute / modelCallsPerTurn turns a minute,
        # so the worker fleet is capped there: excess load queues (FIFO per tenant) instead of
        # spawning Lambdas that Bedrock throttles. -c workerMaxConcurrency overrides the derived value.
        model_rpm = int(ctx("modelRequestsPerMinute") or 200)
        model_calls_per_turn = max(1, int(ctx("modelCallsPerTurn") or 3))
        worker_concurrency = int(ctx("workerMaxConcurrency") or max(1, model_rpm // model_calls_per_turn))
        worker_fn = function("WorkerFunction", "worker.handler.handler", worker_role, WORKER_TIMEOUT, memory=1024,
                             reserved=worker_concurrency, layers=[layer], env={
                                 "COORDINATION_TABLE": coordination.table_name, "TENANT_BUCKET": bucket.bucket_name,
                                 "HARNESS_ROLE_ARN": harness_role.role_arn,
                                 "RUNTIME_ROLE_ARN": runtime_tenant_role.role_arn,
                                 "TENANT_TOKEN_KEY_ID": token_key.key_arn,
                                 "GATEWAY_URL": gateway.attr_gateway_url, "TOKEN_AUDIENCE": gateway_name,
                                 "AGENT_RUNTIME_ARN": runtime.attr_agent_runtime_arn, "MODEL_ID": model_id,
                                 "MAX_ATTEMPTS": "3", "MAX_MODEL_TURNS": "12", "EVENT_TURN_TOOLS": event_turn_tools})
        coordination.grant_read_write_data(worker_fn)
        worker_fn.add_event_source(event_sources.SqsEventSource(
            queue, batch_size=1, report_batch_item_failures=True, max_concurrency=worker_concurrency))
        cloudwatch.Alarm(
            self, "TurnBacklog", alarm_name=f"split-harness-turn-backlog-{suffix}",
            alarm_description=("The oldest queued turn has waited 15 minutes: the workers can't keep up, or a "
                               "tenant's turns are stuck behind a failing one."),
            metric=queue.metric_approximate_age_of_oldest_message(period=Duration.minutes(5), statistic="Maximum"),
            threshold=900, evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING)

        # A turn the queue gave up on (third receive) ends as FAILED instead of staying RUNNING: the
        # worker never marks it when Lambda stops it mid-turn.
        dead_letter_role = iam.Role(self, "DeadLetterRole", assumed_by=lambda_trust, managed_policies=[basic],
                                    description="Marks requests the turn queue gave up on as FAILED")
        dead_letter_fn = function("DeadLetterFunction", "worker.dead_letter.handler", dead_letter_role,
                                  Duration.seconds(20), memory=256, env={"COORDINATION_TABLE": coordination.table_name})
        coordination.grant(dead_letter_fn, "dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem")
        dead_letter_fn.add_event_source(event_sources.SqsEventSource(dlq, batch_size=1, report_batch_item_failures=True))

        # ---------------------------------------------------------------- authenticated ingress
        user_pool = cognito.UserPool(
            self, "Users", self_sign_up_enabled=False, sign_in_aliases=cognito.SignInAliases(email=True),
            custom_attributes={"tenant_id": cognito.StringAttribute(min_len=1, max_len=128, mutable=False)},
            password_policy=cognito.PasswordPolicy(
                min_length=12, require_lowercase=True, require_uppercase=True, require_digits=True,
                require_symbols=True, temp_password_validity=Duration.days(1)),
            removal_policy=data_removal, deletion_protection=not sandbox)
        read_attrs = cognito.ClientAttributes().with_standard_attributes(email=True).with_custom_attributes("tenant_id")
        # Not writable by users: a user cannot move themselves into another tenant.
        write_attrs = cognito.ClientAttributes().with_standard_attributes(email=True)
        client = user_pool.add_client(
            # SRP only: the browser never sends a raw password, and the admin password flow (which
            # needs IAM credentials) is not exposed here.
            "WebClient", auth_flows=cognito.AuthFlow(user_srp=True), generate_secret=False,
            prevent_user_existence_errors=True, read_attributes=read_attrs, write_attributes=write_attrs)
        client_ids = [client]
        if demo:
            # The demo driver signs in with admin_initiate_auth (it holds AWS credentials). That flow
            # lives on its own client, created only with -c demo=true, so production has SRP alone.
            demo_client = user_pool.add_client(
                "DemoClient", auth_flows=cognito.AuthFlow(admin_user_password=True), generate_secret=False,
                prevent_user_existence_errors=True, read_attributes=read_attrs, write_attributes=write_attrs)
            client_ids.append(demo_client)
        ingress_fn = function("IngressFunction", "ingress.handler.handler", ingress_role, Duration.seconds(10), env={
            "COORDINATION_TABLE": coordination.table_name, "TURN_QUEUE_URL": queue.queue_url,
            "EVENT_PRODUCER_ROLE_ARNS": producer_role_arns,
            "MAX_OPEN_REQUESTS": str(int(ctx("maxOpenRequests") or 20))})
        queue.grant_send_messages(ingress_fn)
        coordination.grant_read_write_data(ingress_fn)

        api = apigw.HttpApi(self, "Api", api_name=f"split-harness-{suffix}")
        # Stage-wide throttling in front of the per-tenant open-request limit in ingress. CDK's default
        # stage takes no throttle settings, so they go on its CloudFormation resource.
        api.default_stage.node.default_child.default_route_settings = apigw.CfnStage.RouteSettingsProperty(
            throttling_rate_limit=int(ctx("apiRateLimit") or 50), throttling_burst_limit=int(ctx("apiBurstLimit") or 100))
        jwt = authorizers.HttpUserPoolAuthorizer("Jwt", user_pool, user_pool_clients=client_ids)
        ingress = LambdaProxyIntegration("Ingress", ingress_fn)
        api.add_routes(path="/turns", methods=[apigw.HttpMethod.POST], integration=ingress, authorizer=jwt)
        api.add_routes(path="/turns/{requestId}", methods=[apigw.HttpMethod.GET], integration=ingress, authorizer=jwt)
        api.add_routes(path="/events", methods=[apigw.HttpMethod.POST], integration=ingress,
                       authorizer=authorizers.HttpIamAuthorizer())

        # Each function trusts its input to name the tenant, so nobody else in the account may invoke
        # it directly: an explicit Deny in its resource policy overrides any identity-policy grant.
        api_source = f"arn:{self.partition}:execute-api:{self.region}:{self.account}:{api.api_id}/*"
        for fn, sid, invokers in (
                (ingress_fn, "OnlyThisApiInvokesIngress", {"api_source_arn": api_source}),
                (tools_fn, "OnlyTheGatewayInvokesTools", {"invoker_role_arns": [gateway_role.role_arn]}),
                (interceptor_fn, "OnlyTheGatewayInvokesTheInterceptor", {"invoker_role_arns": [gateway_role.role_arn]}),
                (worker_fn, "OnlyTheTurnQueueInvokesTheWorker",
                 {"invoker_role_arns": [worker_role.role_arn], "lambda_service": True}),
                (dead_letter_fn, "OnlyTheDeadLetterQueueInvokesThis",
                 {"invoker_role_arns": [dead_letter_role.role_arn], "lambda_service": True})):
            lambda_.CfnResourcePolicy(self, f"{fn.node.id}InvokePolicy", resource_arn=fn.function_arn,
                                      policy_document=policies.function_invoke_policy(
                                          function_arn=fn.function_arn, sid=sid, admin_role_arns=admin_roles,
                                          **invokers))

        if demo_operator_role is not None:
            # It can set the demo up and read the bucket to show isolation, and it may ATTEMPT
            # InvokeAgentRuntime and InvokeGateway so the live run proves the resource policies refuse
            # a caller that is neither the worker nor a break-glass role. It is not in adminRoleArns.
            demo_operator_role.add_to_policy(iam.PolicyStatement(
                sid="ReadStackOutputs", actions=["cloudformation:DescribeStacks"], resources=[self.stack_id]))
            demo_operator_role.add_to_policy(iam.PolicyStatement(
                sid="SeedAndAuthenticateDemoUsers", resources=[user_pool.user_pool_arn],
                actions=["cognito-idp:AdminCreateUser", "cognito-idp:AdminSetUserPassword",
                         "cognito-idp:AdminInitiateAuth", "cognito-idp:AdminGetUser", "cognito-idp:AdminDeleteUser"]))
            demo_operator_role.add_to_policy(iam.PolicyStatement(
                sid="SeedDemoSaasData", resources=[saas.table_arn],
                actions=["dynamodb:PutItem", "dynamodb:BatchWriteItem", "dynamodb:DeleteItem",
                         "dynamodb:GetItem", "dynamodb:Query", "dynamodb:DescribeTable"]))
            demo_operator_role.add_to_policy(iam.PolicyStatement(
                # Read-only, exact-key only: the replay check reads back its own turn's record to show
                # the worker ran it once (attempts == 1).
                sid="ReadCoordinationForReplayCheck", resources=[coordination.table_arn],
                actions=["dynamodb:GetItem"]))
            demo_operator_role.add_to_policy(iam.PolicyStatement(
                # Read-only: the DNS-block check reads the runtime VPC's firewall rule group to show its
                # catch-all rule is BLOCK. The demo model refuses to run a script that reaches the network,
                # so the enforcement is shown from the firewall rather than from inside the runtime.
                sid="ReadDnsFirewallForBlockCheck",
                actions=["route53resolver:ListFirewallRules",
                         "route53resolver:ListFirewallRuleGroupAssociations"], resources=["*"]))
            demo_operator_role.add_to_policy(iam.PolicyStatement(
                sid="SeedAndInspectTenantObjects", actions=["s3:ListBucket", "s3:GetObject", "s3:PutObject",
                                                            "s3:DeleteObject"],
                resources=[bucket.bucket_arn, bucket.arn_for_objects("*")]))
            demo_operator_role.add_to_policy(iam.PolicyStatement(
                sid="SimulateTenantRolePolicies", actions=["iam:SimulatePrincipalPolicy"],
                resources=[harness_role.role_arn, runtime_tenant_role.role_arn, tools_role.role_arn]))
            demo_operator_role.add_to_policy(iam.PolicyStatement(
                # Allowed in IAM so the refusal comes from the resource policies, not a missing grant.
                sid="AttemptRuntimeAndGatewayForTheRefusalCheck",
                actions=["bedrock-agentcore:InvokeAgentRuntime", "bedrock-agentcore:InvokeGateway"],
                resources=[runtime.attr_agent_runtime_arn, f"{runtime.attr_agent_runtime_arn}/*",
                           gateway.attr_gateway_arn]))
            # Read-only, for the Cost tab's "Measure the last 24 hours" (demo/measured.py): find this
            # stack's worker, read its Lambda reports and the runtime's metrics, and count turn records.
            demo_operator_role.add_to_policy(iam.PolicyStatement(
                sid="MeasureListThisStack", actions=["cloudformation:ListStackResources"], resources=[self.stack_id]))
            demo_operator_role.add_to_policy(iam.PolicyStatement(
                sid="MeasureReadWorkerConfig", actions=["lambda:GetFunctionConfiguration"],
                resources=[worker_fn.function_arn]))
            demo_operator_role.add_to_policy(iam.PolicyStatement(
                sid="MeasureReadWorkerReports", actions=["logs:FilterLogEvents"],
                resources=[f"arn:{self.partition}:logs:{self.region}:{self.account}:log-group:"
                           f"{worker_fn.log_group.log_group_name}"]))
            demo_operator_role.add_to_policy(iam.PolicyStatement(
                sid="MeasureCountTurns", actions=["dynamodb:Scan"], resources=[coordination.table_arn]))
            demo_operator_role.add_to_policy(iam.PolicyStatement(
                # GetMetricData has no resource type for standard metrics, so it is held to this Region.
                sid="MeasureReadRuntimeMetrics", actions=["cloudwatch:GetMetricData"], resources=["*"],
                conditions={"StringEquals": {"aws:RequestedRegion": self.region}}))
            data_key.add_to_resource_policy(iam.PolicyStatement(
                sid="DemoOperatorSeedsThroughS3", actions=["kms:Decrypt", "kms:GenerateDataKey*", "kms:DescribeKey"],
                resources=["*"], conditions=via_s3, principals=[iam.ArnPrincipal(demo_operator_role.role_arn)]))

        for name, value in {
            "ApiUrl": api.api_endpoint, "UserPoolId": user_pool.user_pool_id,
            "UserPoolClientId": client.user_pool_client_id, "GatewayUrl": gateway.attr_gateway_url,
            "GatewayId": gateway.attr_gateway_identifier, "RuntimeArn": runtime.attr_agent_runtime_arn,
            "TenantBucket": bucket.bucket_name, "TurnQueueUrl": queue.queue_url,
            "CoordinationTable": coordination.table_name, "SaasTable": saas.table_name,
            "TenantHarnessRoleArn": harness_role.role_arn, "TenantRuntimeRoleArn": runtime_tenant_role.role_arn,
            "TenantToolsRoleArn": tools_role.role_arn, "TenantContextKeyArn": token_key.key_arn,
            "RuntimeS3EndpointId": s3_endpoint.vpc_endpoint_id,
            **({"DemoClientId": demo_client.user_pool_client_id,
                "DemoOperatorRoleArn": demo_operator_role.role_arn,
                "DnsRuleGroupId": dns_rule_group_id} if demo else {}),
        }.items():
            CfnOutput(self, name, value=value)

    # ------------------------------------------------------------------ helpers
    def _tenant_role(self, role_id: str, caller: iam.IRole, description: str) -> iam.Role:
        principal = iam.PrincipalWithConditions(iam.SessionTagsPrincipal(caller), policies.tenant_trust_conditions())
        return iam.Role(self, role_id, assumed_by=principal, max_session_duration=Duration.hours(1),
                        description=description)

    def _attach(self, role: iam.Role, policy_id: str, document: dict) -> None:
        iam.Policy(self, policy_id, document=iam.PolicyDocument.from_json(document)).attach_to_role(role)

    def _kms_admin_only_policy(self) -> iam.PolicyDocument:
        """A key policy that lets the account administer the key but delegates no crypto operation to
        IAM. The data-plane actions (Encrypt/Decrypt/GenerateDataKey, GenerateMac/VerifyMac) are
        granted to named principals instead, so an IAM grant alone cannot use the key."""
        return iam.PolicyDocument(statements=[iam.PolicyStatement(
            sid="KeyAdministrationByAccount", principals=[iam.AccountRootPrincipal()], resources=["*"],
            actions=["kms:Create*", "kms:Describe*", "kms:Enable*", "kms:List*", "kms:Put*", "kms:Update*",
                     "kms:Revoke*", "kms:Disable*", "kms:Get*", "kms:Delete*", "kms:TagResource",
                     "kms:UntagResource", "kms:ScheduleKeyDeletion", "kms:CancelKeyDeletion"])])

    def _deployment_role_arns(self) -> list[str]:
        """The bootstrap CloudFormation execution role: the one principal allowed to change the tenant
        bucket's policy and settings, so stack updates keep working."""
        arn = getattr(self.synthesizer, "cloud_formation_execution_role_arn", None)
        if not arn:
            Annotations.of(self).add_warning(
                "This stack synthesizer names no CloudFormation execution role, so the tenant bucket's "
                "policy and settings are not locked to deployments.")
            return []
        return [Fn.sub(arn)]

    def _auto_delete_role_arns(self) -> list[str]:
        """The S3 auto-delete function's role, present only when the bucket empties itself on deletion."""
        provider = self.node.try_find_child("Custom::S3AutoDeleteObjectsCustomResourceProvider")
        return [provider.role_arn] if provider is not None else []

    @staticmethod
    def _tool_definition(name: str, description: str, properties: dict, required: list[str]):
        schema = agentcore.CfnGatewayTarget.SchemaDefinitionProperty
        props = {key: schema(type=kind, description=text) for key, (kind, text) in properties.items()}
        props["tenantId"] = schema(type="string", description=TENANT_ARGUMENT_DOC)
        if name in MUTATING_TOOL_NAMES:
            props["operationKey"] = schema(type="string", description=OPERATION_KEY_DOC)
        return agentcore.CfnGatewayTarget.ToolDefinitionProperty(
            name=name, description=description,
            input_schema=schema(type="object", properties=props, required=required or None))

    def _dns_firewall(self, vpc: ec2.Vpc, mode: str, suffix: str) -> str:
        """Close DNS as an exfiltration channel from untrusted code: only this Region's S3 and
        CloudWatch Logs names resolve, every other lookup is blocked, and a blocked lookup alarms.

        Over a month of runs the runtime VPC looked up the tenant bucket, AgentCore's own acr-code,
        acr-storage and logs buckets, the Logs endpoint, and time.aws.com. AgentCore's bucket names
        belong to the service and can change, so the list allows the Region's S3 names instead of
        pinning them. -c dnsFirewallMode=ALERT resolves everything and only logs, for finding a
        missing name; it raises a synth warning.
        """
        if mode not in ("ALERT", "BLOCK"):
            raise ValueError("dnsFirewallMode must be ALERT or BLOCK")
        if mode == "ALERT":
            Annotations.of(self).add_warning(
                "dnsFirewallMode=ALERT: code in the runtime can resolve any name, so DNS is open as an "
                "exfiltration channel. Use it only to find a missing name, then deploy with BLOCK.")
        region = self.region
        allowed = resolver.CfnFirewallDomainList(
            self, "DnsAllowAws", name=f"split-harness-allow-{suffix}",
            domains=[f"s3.{region}.amazonaws.com", f"*.s3.{region}.amazonaws.com", f"logs.{region}.amazonaws.com"])
        # The runtime looks up time.aws.com, Amazon's public NTP name, which it can't reach anyway:
        # the security group allows only TCP 443. Blocked like everything else, but kept out of the alarm.
        expected = resolver.CfnFirewallDomainList(self, "DnsExpectedNoise", name=f"split-harness-noise-{suffix}",
                                                  domains=["time.aws.com"])
        everything = resolver.CfnFirewallDomainList(self, "DnsEverything", name=f"split-harness-all-{suffix}",
                                                    domains=["*"])
        rule = resolver.CfnFirewallRuleGroup.FirewallRuleProperty
        refuse = {"block_response": "NODATA"} if mode == "BLOCK" else {}
        rules = [
            # A bucket name is a CNAME to an S3 name outside the list (s3-r-w.<region>.amazonaws.com).
            # Trust the chain of an allowed name rather than listing S3's own host names.
            rule(action="ALLOW", firewall_domain_list_id=allowed.attr_id, priority=100,
                 firewall_domain_redirection_action="TRUST_REDIRECTION_DOMAIN"),
            rule(action=mode, firewall_domain_list_id=expected.attr_id, priority=150, **refuse),
            rule(action=mode, firewall_domain_list_id=everything.attr_id, priority=200, **refuse),
        ]
        group = resolver.CfnFirewallRuleGroup(self, "DnsRules", name=f"split-harness-dns-{suffix}", firewall_rules=rules)
        cloudwatch.Alarm(
            self, "DnsBlockedLookups", alarm_name=f"split-harness-dns-blocked-{suffix}",
            alarm_description=("Code in the runtime VPC looked up a name outside the DNS allowlist. "
                               "The Resolver query logs (DnsQueryLogs) name it."),
            metric=cloudwatch.Metric(namespace="AWS/Route53Resolver", metric_name="FirewallRuleQueryVolume",
                                     dimensions_map={"FirewallRuleGroupId": group.attr_id,
                                                     "FirewallDomainListId": everything.attr_id},
                                     statistic="Sum", period=Duration.minutes(5)),
            threshold=1, evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING)
        resolver.CfnFirewallRuleGroupAssociation(self, "DnsRulesAssociation", firewall_rule_group_id=group.attr_id,
                                                 vpc_id=vpc.vpc_id, priority=101, name=f"split-harness-dns-{suffix}")
        # ALERT and BLOCK decisions are only visible in Resolver query logs.
        query_logs = logs.LogGroup(self, "DnsQueryLogs", retention=logs.RetentionDays.ONE_MONTH,
                                   removal_policy=RemovalPolicy.DESTROY)
        query_logging = resolver.CfnResolverQueryLoggingConfig(self, "DnsQueryLogging", name=f"split-harness-dns-{suffix}",
                                                               destination_arn=query_logs.log_group_arn)
        resolver.CfnResolverQueryLoggingConfigAssociation(self, "DnsQueryLoggingAssociation",
                                                          resolver_query_log_config_id=query_logging.attr_id,
                                                          resource_id=vpc.vpc_id)
        return group.attr_id
