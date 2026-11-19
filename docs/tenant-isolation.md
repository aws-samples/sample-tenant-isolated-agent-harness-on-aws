# Tenant isolation boundary for the split harness

This document covers how the decoupled ("split") agent harness keeps tenants apart. The control
plane (a shared Strands harness on Lambda), the direct tool path (AgentCore Gateway) and the lazy
execution path (AgentCore Runtime) are introduced in the top-level README; here the focus is the
isolation boundary between tenants.

## Summary

Every tenant data access runs on short-lived credentials that carry the tenant's storage ID as
an STS session tag (`TenantId`). The IAM policies reference `${aws:PrincipalTag/TenantId}` in S3
key ARNs, KMS encryption-context conditions and DynamoDB leading keys. A credential minted for
tenant A cannot reach tenant B's objects, items or keys, even if application code builds the
wrong key. The same three tenant roles serve every tenant, so nothing in IAM grows with the
tenant count.

The shared harness role itself holds no tenant data access. It can only mint tenant-scoped
sessions, call the pinned model and invoke the execution runtime.

**The one change from the usual pattern: the permanent workspace does not use a shared S3 Files
mount.** AgentCore mounts a bring-your-own S3 Files access point "into every session at the path
you configure", the data is "shared across sessions and agents", and all file operations run as
the access point's single POSIX identity
([File system configurations](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-filesystem-configurations.html)).
A script running in tenant A's session could list `/mnt/persistent/customers/` and read every
other tenant's files. The sample keeps the workspace as a tenant prefix in the shared bucket and
syncs it into AgentCore session storage, which AgentCore isolates per session (one session per
tenant here). See [Requirement 4](#requirement-4-runtime-to-workspace).

What was verified, without deploying anything:

| Check | Result |
| --- | --- |
| Unit, integration and template tests (`pytest`) | 285 passed |
| IAM policy simulator on the exact deployed policy documents | 49 of 49 cases matched ([evidence](evidence/iam-simulation.md)) |
| `cdk synth` and `cfn-lint` on the full stack (95 resources) | Clean, apart from 7 W3005 "redundant DependsOn" warnings from CDK constructs |
| Runtime package served on 127.0.0.1: `/ping`, session check | `Healthy`; a task without a matching session header got 403 |

Deployed and run live in ap-southeast-2, first on 19 October 2026 and most recently on 11 November
2026: all ten isolation checks pass, and the Gateway header pass-through, the runtime's
endpoint-bound access from inside its network, the workspace round trip through S3, and the
runtime and Gateway resource policies refusing a caller other than the worker are now proven.
Still not observed live: the same credentials being refused outside the runtime's network. See
[Live results](#live-results).

## The eight requirements

| # | Requirement | How it is met | Proof |
| --- | --- | --- | --- |
| 1 | Event to Lambdas | Cognito JWT for users, SigV4 plus a producer allowlist for system events, queue policy, tenant-ordered FIFO | `tests/test_ingress.py`, `tests/test_stack.py` |
| 2 | Harness Lambda to S3 (session state) | Per-turn `TenantHarnessRole` session; prefix ARNs plus KMS encryption context; bucket backstop | Simulator cases 1 to 11, 24 and 32; `tests/test_worker_turn.py` |
| 3 | Harness Lambda to Gateway | IAM inbound auth, Gateway resource policy, KMS-signed tenant context verified by a REQUEST interceptor, leading-key ABAC in the tool | `tests/test_interceptor.py`, `tests/test_gateway_client.py`; cases 12, 21 to 23, 25, 48 and 49 |
| 4 | Runtime to workspace | Tenant prefix plus per-session storage, VPC-bound 15-minute credentials, session-to-tenant check, runtime resource policy | `tests/test_runtime.py`; cases 13 to 20, 28 to 31 and 44 to 47 |
| 5 | 10k+ tenants (partial) | Isolation primitives are constant per tenant, but the model request rate binds first: at Nova Lite's default in-Region quota the sample serves about 66 turns a minute per account and Region | [Scale and quotas](#scale-and-quotas) |
| 6 | IAM policies and variables for tenant ID | `${aws:PrincipalTag/TenantId}` everywhere; trust policies require exactly one well-formed tag | `infra/policies.py` |
| 7 | Shared bucket and prefixes | One bucket, one prefix tree per tenant | [Storage layout](#storage-layout) |
| 8 | Simple, AWS primitives | STS session tags, IAM, KMS, endpoint policies, AgentCore resource policies, a Gateway interceptor, Resolver DNS Firewall | Custom code on the security path: the interceptor and the runtime's task checks |

## Requirement 1: event to Lambdas

- **Users** call `POST /turns` on an HTTP API with a Cognito JWT authorizer. The tenant comes
  from the token's `custom:tenant_id` claim. The attribute is immutable and not writable by the
  app client, so a user cannot move themselves into another tenant. A `customerId` in the body
  that disagrees with the token is rejected with 403 rather than ignored, so attempts are visible.
- **System events** (webhooks, schedulers) call `POST /events` with SigV4. Only full role ARNs on
  the `eventProducerRoleArns` allowlist (account included) may assert a tenant, and the event text
  reaches the model as data, not instructions. By default an event turn cannot create quotes or run
  scripts; `-c eventTurnTools=all` opts in.
- `GET /turns/{requestId}` reads only the caller's own partition and returns the same 404 for
  "not yours" and "does not exist", so it is not an existence oracle across tenants.
- The FIFO queue policy denies `sqs:SendMessage` to every principal except the ingress role and
  requires TLS. The message group is the tenant hash, so a tenant's turns stay ordered while
  other tenants run in parallel.
- The deduplication ID is a hash of tenant and request ID. With a bare `requestId`, tenant A
  reusing tenant B's idempotency key inside the five-minute window would silently drop B's turn
  if deduplication were ever queue-scoped. The queue also uses message-group deduplication scope.

## Requirement 2: harness to S3

The worker's role has no S3 permissions at all. At the start of each turn it assumes
`TenantHarnessRole` with `Tags=[TenantId=<storage id>]`, and the Strands session
(`SnapshotSessionManager` on `S3Storage`) reads and writes with that session only.

`TenantHarnessRole` allows:

- `s3:GetObject`, `PutObject`, `DeleteObject` on `tenants/${aws:PrincipalTag/TenantId}/harness/*`
  and `transfer/${aws:PrincipalTag/TenantId}/*`; read-only on the tenant's `uploads/`;
- `s3:ListBucket` only with `s3:prefix` under those paths;
- `kms:Decrypt` and `kms:GenerateDataKey` only via S3 and only when
  `kms:EncryptionContext:aws:s3:arn` is one of the tenant's object ARNs. S3 Bucket Keys are off
  so the object ARN reaches KMS. This is a second lock that does not depend on the S3 policy
  being right.

The bucket policy adds a backstop: `GetObject` and `PutObject` under `tenants/` and `transfer/`
are denied to every principal except the tenant roles (plus any break-glass roles you pass with
`-c adminRoleArns=...`). A broad `s3:*` grant added to some other role later does not open
tenant data. Admins can still change the bucket policy, and that change is logged.

The DynamoDB tables (coordination and SaaS data) have no such resource-policy backstop: the
`TENANT#` leading-key scoping constrains only the tenant roles' sessions, so a principal with
account-level DynamoDB permissions, an administrator among them, can read the raw tables directly.
Add a table resource policy if that direct path must be closed as well.

Conversation state uses one snapshot object per tenant, overwritten after every message. A
retried turn resumes from the last model or tool step, and restore cost does not grow with
history length.

## Requirement 3: harness to Gateway

1. The Gateway uses `AWS_IAM` inbound auth. A Gateway resource policy denies `InvokeGateway` to
   every principal except `TenantHarnessRole` sessions, so the shared worker role, and account
   admins without break-glass, cannot call tools directly.
2. Each call is SigV4-signed with the turn's tenant session and carries `X-Tenant-Context`: a
   token holding tenant, request, audience and expiry, MAC'd with a KMS HMAC key
   (`GenerateMac`). The worker mints one token per turn.
3. A Gateway REQUEST interceptor, configured with `passRequestHeaders: true`, verifies the token
   with `VerifyMac` and overwrites `arguments.tenantId` on every `tools/call`. Anything it cannot
   verify is rejected before a target runs (fail closed).
4. The model never sees `tenantId`. The worker removes it from each tool's schema and strips it
   from any arguments the model sends anyway; the interceptor would overwrite it regardless.
5. The tool Lambda does not trust its own key building. It uses a `TenantToolsRole` session for
   the verified tenant, and that role only allows DynamoDB items whose partition key is
   `TENANT#${aws:PrincipalTag/TenantId}` (`dynamodb:LeadingKeys`).

Why a signed header: a Gateway Lambda target receives the tool arguments plus Gateway metadata
(gateway, target, tool name, request IDs) and no caller identity
([Lambda input format](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-add-target-lambda.html)).
Tenant context has to travel in-band, so it has to be verifiable. KMS HMAC keeps the key inside
KMS; minting (`kms:GenerateMac`, worker only) and verifying (`kms:VerifyMac`, interceptor only)
are separate permissions; and HMAC requests count against the shared symmetric KMS quota
([KMS request quotas](https://docs.aws.amazon.com/kms/latest/developerguide/requests-per-second.html)).

The alternative is `CUSTOM_JWT` inbound auth with a tenant claim in each token. That suits
user-delegated calls, but for this internal service-to-service hop it adds an identity-provider
token exchange per tenant per turn.

## Requirement 4: runtime to workspace

**Why not the shared S3 Files mount.** A bring-your-own S3 Files access point on an AgentCore
Runtime is mounted into every session of that runtime and runs as one POSIX identity, with mount
authorisation from the one execution role. Per-tenant access points would need per-tenant
runtimes: a runtime takes at most two S3 Files access points, and an account holds 1,000 runtimes
by default
([quotas](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/bedrock-agentcore-limits.html)).
That would turn the shared execution plane back into per-tenant infrastructure.

**What the sample does instead.**

- The permanent workspace is `tenants/{sid}/workspace/` in the shared bucket. Uploads live in
  `tenants/{sid}/uploads/`.
- Each `execute_process` task carries credentials for `TenantRuntimeRole`, minted for this tenant
  and this request, valid for 15 minutes. They allow read and write on the tenant's workspace,
  read-only on its uploads, nothing in `harness/`, and only through the runtime VPC's S3 gateway
  endpoint (`aws:SourceVpce`). Stolen credentials do not work from anywhere else.
- The runtime pulls the workspace into session storage at `/mnt/session` before the script and
  pushes changes after it. The sync is incremental (ETag and mtime manifest), so a warm or resumed
  session only moves what changed. Session storage is isolated per session; the session ID is
  `tenant-{sid}`, one per tenant.
- The runtime refuses a task unless its tenant matches the session it is serving
  (`X-Amzn-Bedrock-AgentCore-Runtime-Session-Id`), and refuses workspace, uploads or output keys
  outside that tenant's prefixes. A routing bug in the harness fails closed.
- Scripts run with a minimal environment: no AWS variables, a killed process group on timeout,
  bounded stdout and stderr.
- The execution role holds only CloudWatch Logs and read access to its own code object. Lifting
  it from the microVM gets nothing useful: no tenant data, no models, no Gateway, no `AssumeRole`.
- A runtime resource policy denies `InvokeAgentRuntime` and `StopRuntimeSession` to everyone but
  the worker role. Whoever can call `InvokeAgentRuntime` chooses the session ID, so this is the
  control that keeps other principals out of tenant sessions. The interactive actions
  (`InvokeAgentRuntimeCommand`, `InvokeAgentRuntimeCommandShell`, the WebSocket variants) are
  denied to everyone, the worker included; they would give a shell into a live tenant session.

**Network.** Isolated subnets with no internet gateway or NAT. Runtime egress is TCP 443 only, to
the S3 prefix list and the Logs interface endpoint. The S3 gateway endpoint policy allows the
tenant bucket plus AgentCore's own code and session-storage buckets (service principal only). The
Logs endpoint policy allows only this account's principals writing this account's log groups;
without it, code in the runtime could ship data out with its own credentials through the
endpoint. Route 53 Resolver DNS Firewall resolves only this Region's S3 and CloudWatch Logs names
and blocks everything else. A blocked lookup raises the `DnsBlockedLookups` alarm, and the Resolver
query logs (`DnsQueryLogs`) name it. `-c dnsFirewallMode=ALERT` resolves every name and only logs,
for finding a missing name.

## Threat model

Assumed hostile: a tenant's own users and anything they send (messages, uploads, webhook
payloads), model output, and any code running in the runtime microVM. Assume that code can read
everything in the microVM, including the task's workspace credentials and the execution role.

What a hostile script gets: its own tenant's workspace and uploads, from inside the runtime VPC,
for up to 15 minutes, which is what it could already touch through the filesystem. It cannot
reach other tenants, conversation state, models, the Gateway, other sessions or the internet. It
can leave background processes running in its own tenant's session until that microVM ends.

Prompt injection cannot change the tenant: the tenant is fixed at ingress and carried in the
queue message, never derived from model output. Injection can still misuse a tenant's own tools
inside that tenant. That needs confirmation steps for irreversible tools, Bedrock Guardrails and
model invocation logging, which are outside this boundary.

Trusted: the shared harness code. It can mint credentials for any tenant by design, so it is
reviewed code with a small surface, and its role has no data access of its own.

No long-lived secrets: the model is reached through IAM roles, so there are no inference API keys,
and the tenant-context signing key is a KMS HMAC key that never leaves KMS. Every function's
environment variables hold only resource names and ARNs, so a leak of the functions'
configuration exposes no credentials. The worker and runtime pin the AWS SDK at 1.43.123. AWS security bulletin
2026-150-AWS asks for 1.43.117 or later, whose bundled CA certificates drop the affected roots.
The runtime package has no `certifi`, so botocore's own bundle is the one it uses.

## Storage layout

| Prefix | Contents | Who can reach it |
| --- | --- | --- |
| `tenants/{sid}/harness/` | Strands snapshot: conversation and agent state | `TenantHarnessRole` for `{sid}` |
| `tenants/{sid}/workspace/` | Durable execution workspace | `TenantRuntimeRole` for `{sid}`, runtime VPC only |
| `tenants/{sid}/uploads/` | Validated user uploads | Read: harness and runtime roles for `{sid}` |
| `transfer/{sid}/{requestId}/` | Large scripts and results; expires after one day | `TenantHarnessRole` for `{sid}`; presigned URLs for the runtime |

`{sid}` is the first 32 hex characters of a namespaced SHA-256 of the customer ID: stable, not
secret, and spread across S3 partitions.

## Scale and quotas

Rates use an illustrative cost model: one event per tenant every 10 minutes, 1.5 Gateway calls per
event, 10% of events run a script (30 seconds), 5-second turns, a 300-second runtime idle
timeout. 10,000 tenants is 16.7 turns per second.

The binding quota is the model's request rate, not any isolation primitive. The worker fleet is
capped at about `modelRequestsPerMinute / modelCallsPerTurn` turns a minute (66 by default:
in-Region 200 requests a minute, 3 model calls a turn), so excess load queues per tenant instead
of spawning workers that Bedrock throttles. `-c workerMaxConcurrency` overrides the derived cap.

| Quota (default) | At 10,000 tenants | At 100,000 tenants | Action |
| --- | --- | --- | --- |
| **Bedrock Nova Lite requests a minute (in-Region 200, cross-Region 400 in Sydney)** | about 1,000 a minute at one model call an event, more with tool loops: already over the cap | about 10,000 | **The first limit reached.** Request an increase, use the cross-Region profile, or shard tenants across accounts and Regions. Requirement 5 is partial because of this. |
| **Bedrock Nova Lite tokens a minute (in-Region 200,000, cross-Region 400,000 adjustable)** | at about 7,000 tokens a turn, 400,000 covers about 570 concurrent turns a minute | raise the token quota | Ask Support to raise the token-per-minute quota. |
| IAM roles per account (1,000; max 10,000) | 12 roles, constant | 12 | None. Per-tenant roles would stop at 10,000 tenants. |
| STS `AssumeRole` and related calls (600 per second per account per Region) | about 45 per second, worst case | about 430 | At 100k, cache tenant sessions in warm workers or ask Support for more. |
| KMS symmetric requests (10,000 per second shared in most Regions) | about 185 per second | about 1,850 | None |
| Gateway tool-call and tool-list rate (200 TPS per gateway and per account) | 25 to 42 TPS | 250 to 420 TPS | Raise before about 50,000 tenants. This applies to this design with or without the boundary. |
| AgentCore Runtime active sessions (2,500 in Sydney; 5,000 in N. Virginia and Oregon) | about 550 | about 5,500 | At 100k, shorten the idle timeout or raise the quota. |
| New runtime session creation (25 TPS) | about 1.6 TPS | about 16 TPS | Watch above 100k. |
| Lambda concurrency (1,000 per Region) | worker about 84 | about 835 | Raise account concurrency; set `-c workerMaxConcurrency`. |
| S3 requests per partitioned prefix (3,500 writes, 5,500 reads per second) | about 105 writes per second in total, spread over tenant prefixes | about 1,050 in total | None |
| Session storage per session (1 GB, fixed) | per-tenant workspace capped at 900 MB in this sample | same | Larger tenants need direct S3 access from scripts. |

Sessions counted as active assume idle sessions count until the idle timeout ends them. Runtime
ENIs are shared by sessions that use the same subnets and security group, so they do not scale
with tenants.

Sources: [AgentCore quotas](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/bedrock-agentcore-limits.html),
[Amazon Bedrock quotas](https://docs.aws.amazon.com/general/latest/gr/bedrock.html),
[IAM and STS quotas](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_iam-quotas.html),
[KMS request quotas](https://docs.aws.amazon.com/kms/latest/developerguide/requests-per-second.html).

## Cost of the boundary

Per tenant per month, with 4,320 events (one per tenant every 10 minutes) and Sydney prices from
the AWS Price List API (October 2026):

| Item | Calculation | Per tenant-month |
| --- | --- | --- |
| KMS requests | about 11 per event (1 `GenerateMac`, up to 2.5 `VerifyMac`, about 7 for SSE-KMS on snapshot reads and writes, about 0.3 for workspace sync) x 4,320 x $0.03 per 10,000 | $0.14 |
| Interceptor Lambda | up to 2.5 invocations per event x 4,320 x ($0.20 per million + 50 ms at 512 MB x $0.0000133334 per GB-second) | $0.01 |
| STS | no charge | $0.00 |
| **Total** | | **about $0.15** |

That moves the split-harness estimate from $1.67 to about $1.81 per tenant-month, or about $1,500
a month at 10,000 tenants. The snapshot write count comes from the worker test:
one read plus three writes for a turn with no tools, and two more writes per tool call.

Turning on S3 Bucket Keys removes most of the SSE-KMS requests (about $0.06 per tenant-month in
total) but drops the per-object encryption-context check, the second lock in Requirement 2. The
IAM prefix scoping still holds.

Fixed costs that do not grow with tenants: the Logs interface endpoint in two Availability Zones
(2 x $0.013 per hour, about $19 a month), two customer managed KMS keys ($1 per key version per
month), and usage-based DNS Firewall queries and VPC flow logs. There is no NAT gateway.

The cost model also bills every event as 5 seconds of agent loop in both designs. A code event
takes longer in both: the split's worker Lambda waits for the runtime (the invocation is
synchronous), and the all-on microVM stays busy while the script runs. Counting a 30-second script
adds about $0.17 to the split and $0.14 to all on AgentCore. With this and the boundary, the
comparison is about $1.99 against $4.24 per tenant-month. [demo/cost_model.py](../demo/cost_model.py)
reproduces these figures and applies either correction; the demo page's Cost tab drives it.

## Alternatives considered

| Option | Why not |
| --- | --- |
| A role per tenant | Hits the roles-per-account quota; onboarding needs IAM changes; ABAC gives the same isolation with three roles. |
| S3 Files access point per tenant | Needs a runtime per tenant (two access points per runtime, 1,000 runtimes per account by default). |
| Bucket per tenant | More resources per tenant and no stronger isolation than ABAC on prefixes for this access pattern. |
| `CUSTOM_JWT` on the Gateway with per-tenant tokens | Workable; adds an identity-provider round trip per tenant per turn for an internal hop. |
| Lambda tenant isolation mode for the worker | Isolates execution environments per tenant ([docs](https://docs.aws.amazon.com/lambda/latest/dg/tenant-isolation.html)), which addresses leftovers in warm containers, but most turns would start cold at 10,000 tenants, against the shared-pool cost goal. Worth revisiting if warm-container leakage is in scope. |

## Related work

[Infinity](https://aws.amazon.com/blogs/opensource/introducing-infinity-a-runtime-for-extreme-agent-concurrency/),
AWS's open-source research runtime, uses the same pattern on AWS: an SQS FIFO input queue with
one message group per conversation, one Lambda invocation per unit of work, stable input IDs with
deduplication state in DynamoDB, and each turn saved before its tool call is dispatched. Two
differences matter here:

- **Infinity never waits on a tool.** Its unit of work ends at every tool call, and the result
  comes back as a new message on the queue. This harness's worker waits while a script runs in
  the runtime. That wait is the $0.17 per tenant-month in the corrected cost model, and it means
  a script can't outlast the worker's own timeout. Infinity's approach would need the runtime to
  post the script's result back to the queue, with a tenant context the worker can verify.
- **The post doesn't cover tenant isolation.** One of its open questions is how a runtime should
  establish where an event came from. Here, ingress accepts webhook events only from allowlisted
  producer roles signing with SigV4, user turns only with a Cognito token, and every Gateway call
  carries a tenant context signed with a KMS key and bound to one request.

[Vercel's durable-harness](https://vercel.com/blog/making-every-harness-durable) records an intent
before each model or tool call and the outcome after it. After a crash it re-runs only tools
marked replay-safe, and reports any other interrupted tool to the model as possibly run. This
harness does the same. Strands saves the turn after every message and, after a crash, re-runs the
cut-off message's tool calls with the same IDs. The operation key makes `create_quote` safe to
repeat, and each script call is recorded in the coordination table before it goes to the runtime
and when the runtime answers, so a script cut off by a worker crash is reported to the model as
possibly run instead of being run again ([execute_process.py](../src/worker/execute_process.py),
tested in [test_worker_turn.py](../tests/test_worker_turn.py)).

## Live results

Deployed with `cdk deploy` to a sandbox account in ap-southeast-2 on 19 October 2026 (88
resources, about 12 minutes), then driven by `python -m demo.run_demo` as two tenants. Full
output: [evidence/live-demo.md](evidence/live-demo.md).

| Check | Requirement | Observed |
| --- | --- | --- |
| No sign-in token | 1 | 401 from the JWT authoriser, with `WWW-Authenticate: Bearer` |
| Tenant named in the request body | 1 | 403 `customerId does not match the authenticated tenant` |
| Tenant B reads tenant A's request | 1 | B 404 `request not found`; A 200 |
| Same `requestId` again, and with a different message | 1 | One worker run; different message 409; repeat after completion 200 |
| Gateway call with no tenant context, as the operator role | 3 | Gateway resource policy: HTTP 403, JSON-RPC -32002 `Authorization error - Insufficient permissions` |
| Gateway call with a forged tenant A context, as the operator role | 3 | The same resource-policy refusal; the forged context never reaches the interceptor |
| Operator invokes the runtime directly, not through the worker | 4 | `AccessDeniedException` from the runtime resource policy |
| IAM evaluation of the deployed tenant roles as tenant B | 2, 6 | Own S3 prefix and DynamoDB partition allowed; tenant A's denied |
| Tenant B tells the agent to act for tenant A | 3, 6 | Only `list_jobs` ran, returning B's jobs; none of A's job IDs or addresses |
| DNS Firewall for the runtime VPC | 4 | Catch-all rule BLOCK, one ALLOW rule, rule group associated with the runtime VPC |

The rows for the two Gateway checks, the direct runtime invoke and the DNS Firewall show the 11
November 2026 run, in which all ten checks passed; the other rows show the 19 October run. The demo
signs its Gateway calls as the operator role, which the Gateway resource policy refuses before the
interceptor runs, so the interceptor's own refusals (`missing tenant context`, and `tenant context
is not authentic` from the KMS MAC check) are covered by the unit tests in `tests/test_interceptor.py`,
`tests/test_tenant_token.py` and `tests/test_demo_checks.py`, not by the live run.

Before any tenant data was loaded, anonymous calls were refused: `POST /turns` and `GET /turns`
401, unsigned `POST /events` 403, unsigned Gateway `tools/list` 401.

The four behaviours the summary listed as deployment-only:

| Behaviour | Status |
| --- | --- |
| Gateway passes `X-Tenant-Context` to the interceptor | **Proven** by real turns: their tools received the verified tenant, which only the interceptor writes into the tool arguments. The interceptor's refusals of a missing or forged context are unit-tested, because the demo's operator role is refused one layer earlier, by the resource policy. |
| `aws:SourceVpce` as seen from the runtime | **Proven from inside:** the price-list turn staged the tenant's upload with endpoint-bound credentials and returned the right total. Refusal of the same credentials from outside the network is evaluated by the IAM simulator only. |
| Session storage and workspace sync | **Proven** ([evidence](evidence/live-workspace.md)): a file AnyCompany's script wrote to the workspace landed under AnyCompany's S3 prefix only and was read back in the next turn; a file written only to S3 reached AnyCompany's runtime; Example Corp's runtime found no copy anywhere on its file system (`find /`). A cold microVM start, which uses the same S3 pull, was not forced. |
| AgentCore resource policies | **Proven.** Unsigned calls were refused, and the demo's operator role, which IAM allows to call `InvokeAgentRuntime` and `InvokeGateway`, is refused by both resource policies (checks `runtime-direct-invoke`, `gateway-no-context` and `gateway-forged`). |

The live run also found three defects that local tests could not see, all fixed and redeployed:

- AgentCore Gateway accepts MCP protocol version `2025-03-26` only (JSON-RPC -32600 otherwise); the
  Gateway now declares it in `supportedVersions` and the client sends the same version.
- On the FIFO queue, one failed turn held the tenant's later turns for the 60-minute visibility
  timeout. The worker now returns a failed turn after 30 seconds, then 2 minutes.
- Nova Lite wrote `INPUTS_DIR/...` without the `$`. The worker refuses such a script with the fix.

The runtime's own server binds `0.0.0.0` (`RUNTIME_BIND_HOST`) inside its microVM, which
AgentCore requires. It has no listener outside the microVM: the security group has no ingress
rules, and the only way in is `InvokeAgentRuntime`, which IAM and the resource policy gate.

## Open items

- **Verify live** the behaviour still marked unproven in [Live results](#live-results):
  credentials refused outside the runtime's network. That needs the runtime's tenant credentials
  used from outside its VPC.
- **The runtime is pinned to platform V2** (`-c runtimePlatformVersion` overrides). Session storage
  is in Preview: if it resets (platform update, 14 days idle) the workspace re-hydrates from S3, so
  the design works on either platform and only the warm-start speed-up is lost.
- **The model is in-Region by default** (`amazon.nova-lite-v1:0`), so inference stays in the Region.
  At the default quotas in Sydney that halves the per-minute rate (200 against the cross-Region
  400); a deploy with `-c modelId=apac.amazon.nova-lite-v1:0` takes the higher rate and leaves the
  Region. Synth refuses a cross-Region profile whose geography does not cover the deployment Region.
- **AgentCore VPC availability zones.** CDK picks two AZs by name. AgentCore VPC mode is offered in
  all three Sydney AZ IDs (`apse2-az1/az2/az3`) but only `use1-az1/az2/az4` in us-east-1, and an AZ
  name maps to a different AZ ID per account, so in such Regions pin `-c availabilityZones` to names
  whose AZ IDs are supported.
- **DNS Firewall blocks by default.** A runtime dependency that needs another name fails until the
  name is added to the allowlist; a deploy with `-c dnsFirewallMode=ALERT` shows which name.
- **Break-glass.** Without `-c adminRoleArns=...` even account admins are denied tenant objects,
  tenant sessions and direct Gateway calls until they change the policies (which CloudTrail
  records). Decide who holds break-glass.
- **Replay safety** covers `create_quote`, the demo's one mutating tool: an operation key from
  `requestId` and `toolUseId`, with the outcome kept in an execution ledger. Other mutating tools
  need the same, and scripts with external side effects need their own contract.
  `execute_process` disables SDK retries, and a script cut off by a worker crash is reported to
  the model as possibly run rather than run again; the model decides whether to check and rerun.
- **Script calls hold the worker.** The worker waits for each script (see
  [Related work](#related-work)). Ending the turn at the call and resuming when the runtime posts
  the result, as Infinity does, would remove that cost and the timeout cap on scripts.
