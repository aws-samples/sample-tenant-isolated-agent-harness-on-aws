# IAM policy simulation: tenant isolation boundary

> **Example output.** Captured from a sandbox run; a fresh `scripts/simulate_isolation.py --out
> .build/evidence/iam-simulation.md` leaves this committed copy in place. Tenant IDs are hashes of
> the demo tenant names.

Run 2026-11-06 04:14 UTC with `iam:SimulateCustomPolicy` (read-only). Tenant A = `73679ad749907f076d69b4aacc255dc3`, tenant B = `cf2a93fc75360f3de51242dd000ec233`.
Documents: `infra/policies.py`, the same functions CDK deploys, with placeholder ARNs.

**49/49 cases matched the expected decision.**

| Case | Action | Expected | Simulator | Result |
| --- | --- | --- | --- | --- |
| harness A reads its own conversation | `s3:GetObject` | allowed | allowed | PASS |
| harness A reads tenant B's conversation | `s3:GetObject` | implicitDeny | implicitDeny | PASS |
| harness A writes its own transfer object | `s3:PutObject` | allowed | allowed | PASS |
| harness A writes tenant B's transfer prefix | `s3:PutObject` | implicitDeny | implicitDeny | PASS |
| harness A reads tenant A's workspace | `s3:GetObject` | implicitDeny | implicitDeny | PASS |
| harness A lists its own harness prefix | `s3:ListBucket` | allowed | allowed | PASS |
| harness A lists tenant B's prefix | `s3:ListBucket` | implicitDeny | implicitDeny | PASS |
| harness A lists the bucket root | `s3:ListBucket` | implicitDeny | implicitDeny | PASS |
| harness A decrypts its own object via S3 | `kms:Decrypt` | allowed | allowed | PASS |
| harness A decrypts tenant B's object via S3 | `kms:Decrypt` | implicitDeny | implicitDeny | PASS |
| harness A calls KMS directly (not via S3) | `kms:Decrypt` | implicitDeny | implicitDeny | PASS |
| harness A invokes the Gateway | `bedrock-agentcore:InvokeGateway` | allowed | allowed | PASS |
| runtime A writes its workspace from the runtime VPC | `s3:PutObject` | allowed | allowed | PASS |
| runtime A writes its workspace from elsewhere (stolen creds) | `s3:PutObject` | implicitDeny | implicitDeny | PASS |
| runtime A uses its creds outside any VPC endpoint | `s3:GetObject` | implicitDeny | implicitDeny | PASS |
| runtime A reads tenant B's workspace | `s3:GetObject` | implicitDeny | implicitDeny | PASS |
| runtime A reads its own conversation state | `s3:GetObject` | implicitDeny | implicitDeny | PASS |
| runtime A reads its uploads | `s3:GetObject` | allowed | allowed | PASS |
| runtime A overwrites an upload | `s3:PutObject` | implicitDeny | implicitDeny | PASS |
| runtime A lists tenant B's workspace | `s3:ListBucket` | implicitDeny | implicitDeny | PASS |
| tools A queries its own partition | `dynamodb:Query` | allowed | allowed | PASS |
| tools A queries tenant B's partition | `dynamodb:Query` | implicitDeny | implicitDeny | PASS |
| tools A scans the whole table | `dynamodb:Scan` | implicitDeny | implicitDeny | PASS |
| worker reads any tenant object directly | `s3:GetObject` | implicitDeny | implicitDeny | PASS |
| worker calls the Gateway with its own role | `bedrock-agentcore:InvokeGateway` | implicitDeny | implicitDeny | PASS |
| worker vends a tenant harness session | `sts:AssumeRole` | allowed | allowed | PASS |
| worker invokes an unpinned model | `bedrock:InvokeModel` | implicitDeny | implicitDeny | PASS |
| execution role reads tenant data | `s3:GetObject` | implicitDeny | implicitDeny | PASS |
| execution role invokes a model (LLMjacking) | `bedrock:InvokeModel` | implicitDeny | implicitDeny | PASS |
| execution role invokes another runtime session | `bedrock-agentcore:InvokeAgentRuntime` | implicitDeny | implicitDeny | PASS |
| execution role assumes a tenant role | `sts:AssumeRole` | implicitDeny | implicitDeny | PASS |
| an admin with s3:* reads tenant data (bucket backstop) | `s3:GetObject` | explicitDeny | explicitDeny | PASS |
| an admin with s3:* reads an earlier version of a tenant object | `s3:GetObjectVersion` | explicitDeny | explicitDeny | PASS |
| an admin with s3:* deletes a tenant object | `s3:DeleteObject` | explicitDeny | explicitDeny | PASS |
| an admin with s3:* reads a transfer object | `s3:GetObject` | explicitDeny | explicitDeny | PASS |
| an admin with s3:* lists the bucket | `s3:ListBucket` | explicitDeny | explicitDeny | PASS |
| an admin with s3:* lists object versions | `s3:ListBucketVersions` | explicitDeny | explicitDeny | PASS |
| an admin with s3:* rewrites the bucket policy | `s3:PutBucketPolicy` | explicitDeny | explicitDeny | PASS |
| an admin with s3:* suspends versioning | `s3:PutBucketVersioning` | explicitDeny | explicitDeny | PASS |
| an admin with s3:* replicates the bucket elsewhere | `s3:PutReplicationConfiguration` | explicitDeny | explicitDeny | PASS |
| CloudFormation updates the bucket policy | `s3:PutBucketPolicy` | allowed | allowed | PASS |
| CloudFormation reads tenant data | `s3:GetObject` | explicitDeny | explicitDeny | PASS |
| harness A reads its own conversation through the bucket policy | `s3:GetObject` | allowed | allowed | PASS |
| worker invokes a tenant's execution session | `bedrock-agentcore:InvokeAgentRuntime` | allowed | allowed | PASS |
| an admin with bedrock-agentcore:* invokes a tenant session | `bedrock-agentcore:InvokeAgentRuntime` | explicitDeny | explicitDeny | PASS |
| an admin opens a shell in a tenant session | `bedrock-agentcore:InvokeAgentRuntimeCommandShell` | explicitDeny | explicitDeny | PASS |
| the worker itself runs a command in a tenant session | `bedrock-agentcore:InvokeAgentRuntimeCommand` | explicitDeny | explicitDeny | PASS |
| an admin calls the Gateway directly | `bedrock-agentcore:InvokeGateway` | explicitDeny | explicitDeny | PASS |
| a tenant harness session calls the Gateway | `bedrock-agentcore:InvokeGateway` | allowed | allowed | PASS |
