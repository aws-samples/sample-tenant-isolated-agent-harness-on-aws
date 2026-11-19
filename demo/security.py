# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The eight tenant-isolation requirements, how each is met, and the live checks that prove it.

Served to the page's Security tab through /api/info. The wording follows
docs/tenant-isolation.md; every check id must exist in demo.checks.CHECKS (a test enforces it).
"""

REQUIREMENTS = [
    {
        "number": 1,
        "title": "Secure connectivity from event to Lambdas",
        "how": "Cognito JWT for users, SigV4 plus a producer allowlist for system events. The tenant comes "
               "from the token, never the request body. Turns queue in a FIFO lane per tenant, and the "
               "dedup ID includes the tenant.",
        "checks": ["no-token", "body-spoof", "cross-tenant-read", "replay"],
    },
    {
        "number": 2,
        "title": "Secure connectivity from harness Lambda to S3 session state",
        "how": "Each turn uses a TenantHarnessRole session tagged with the tenant. IAM limits it to that "
               "tenant's prefix and KMS encryption context; the worker role itself has no data access.",
        "checks": ["abac"],
    },
    {
        "number": 3,
        "title": "Secure connectivity from harness Lambda to Gateway",
        "how": "IAM inbound auth and a Gateway resource policy. The worker signs the tenant context with KMS; "
               "an interceptor verifies it and sets the tenant on every tool call. The tool's DynamoDB "
               "access is limited to the tenant's leading keys.",
        "checks": ["gateway-no-context", "gateway-forged", "prompt-injection"],
    },
    {
        "number": 4,
        "title": "Secure connectivity from Runtime to the persistent workspace",
        "how": "The tenant's workspace prefix syncs into that tenant's own session storage with 15-minute "
               "credentials that S3 accepts only through the runtime's VPC endpoint. The runtime checks the "
               "task's tenant against its session, and its resource policy admits only the worker.",
        "checks": [],
        "evidence": "Proven live on 19 Oct 2026: a file AnyCompany saved stayed under AnyCompany's prefix and Example Corp's "
                    "runtime found no copy (docs/evidence/live-workspace.md).",
    },
    {
        "number": 5,
        "title": "Scales to 10,000+ tenants",
        "how": "The same three tenant roles serve every tenant, so IAM does not grow with tenants. Gateway "
               "rate, runtime sessions, STS calls and Lambda concurrency need raising past about 50,000.",
        "checks": [],
        "evidence": "Quota table in docs/tenant-isolation.md, Scale and quotas.",
    },
    {
        "number": 6,
        "title": "IAM policies use variables for the tenant ID",
        "how": "${aws:PrincipalTag/TenantId} appears in S3 key ARNs, KMS encryption-context conditions and "
               "DynamoDB leading keys. Trust policies require exactly one well-formed TenantId tag.",
        "checks": ["abac"],
    },
    {
        "number": 7,
        "title": "Shared bucket with a prefix per tenant",
        "how": "One bucket with one prefix tree per tenant for harness state, uploads and the workspace.",
        "checks": ["abac"],
    },
    {
        "number": 8,
        "title": "As simple as possible, using AWS primitives",
        "how": "STS session tags, IAM, KMS, endpoint policies, AgentCore resource policies, Resolver DNS "
               "Firewall and one Gateway interceptor. The only custom code on the security path is the "
               "interceptor and the runtime's task checks.",
        "checks": [],
        "evidence": "infra/policies.py and infra/stack.py.",
    },
]

NOT_YET_LIVE = [
    "The runtime's credentials refused when used outside its VPC.",
]
