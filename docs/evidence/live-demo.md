# Live demo evidence

> **Example output.** Captured from a sandbox run, with the sandbox's resource IDs redacted and its
> stack name replaced by the sample's default, `SplitHarness`; a fresh `python -m demo.run_demo`
> writes to `.build/evidence/live-demo.md` and leaves this committed copy in place.

Run at 2026-11-20 03:54 UTC against stack `SplitHarness` in ap-southeast-2, by `python -m demo.run_demo`.

## Turns

| Tenant | Message | State | Path | Seconds | Result (first 300 characters) |
| --- | --- | --- | --- | --- | --- |
| anycompany-plumbing | Which jobs are open? | COMPLETED | model only | 7.4 | Here are the open jobs: 1. Job ID: J7, Title: Hot water system replacement, 14 Rose St 2. Job ID: J9, Title: Leaking mixer tap, 88 Hill Ave  If you need more information about any of these jobs, please let me know. |
| anycompany-plumbing | uploads/prices.csv is our supplier price list. It has a header row and the columns sku, description, unit_price_aud. Add up the unit_price_aud column and tell me the total. | COMPLETED | runtime session started | 21.2 | The total sum of the 'unit_price_aud' column in the 'uploads/prices.csv' file is AUD 1652.30. |
| example-corp-electrical | Create a quote for job E1: switchboard upgrade, $640. | COMPLETED | Gateway tools only | 4.7 | <response>The quote for job E1: switchboard upgrade has been successfully created with the quote ID befbe1efb391. The amount for this quote is $640.</response> |

## Isolation checks

10 of 10 passed.

| Check | Requirement | Expected | Observed | Result |
| --- | --- | --- | --- | --- |
| Turn without a sign-in token | 1 (Event to Lambdas) | 401 from the API's JWT authoriser; nothing is queued | 401 Unauthorized | PASS |
| Tenant B names tenant A in the request body | 1 (Event to Lambdas) | 403: the tenant comes from B's ID token, never from the body | 403 customerId does not match the authenticated tenant | PASS |
| Tenant B reads tenant A's request | 1 (Event to Lambdas) | B gets 404, the same answer as for a request that does not exist; A gets 200 | A submit 202; B read 404 request not found; A read 200 | PASS |
| Same requestId sent again, and with a different message | 1 (idempotency) | One worker run; repeat while queued 202 (FIFO drops the copy); different message 409; repeat after completion 200 COMPLETED | first 202; repeat 202; different message 409 requestId was already used for a different request; turn COMPLETED; repeat after 200 COMPLETED; attempts recorded 1 | PASS |
| Operator calls the Gateway with no tenant context | 3 (Gateway resource policy) | HTTP 403 with JSON-RPC error -32002 from the Gateway resource policy, which admits only tenant harness sessions (IAM grants the operator InvokeGateway); any other answer fails | refused by the resource policy: Gateway HTTP 403: {"jsonrpc":"2.0","id":1,"error":{"code":-32002,"message":"Authorization error - Insufficient permissions"}} | PASS |
| Operator calls the Gateway with a forged tenant A context | 3 (Gateway resource policy) | HTTP 403 with JSON-RPC error -32002 from the Gateway resource policy, which admits only tenant harness sessions (IAM grants the operator InvokeGateway); any other answer fails | refused by the resource policy: Gateway HTTP 403: {"jsonrpc":"2.0","id":1,"error":{"code":-32002,"message":"Authorization error - Insufficient permissions"}} | PASS |
| Operator invokes the runtime directly, not through the worker | 4 (runtime resource policy) | AccessDenied from the runtime resource policy, even though IAM grants the operator InvokeAgentRuntime | refused: AccessDeniedException | PASS |
| IAM evaluation of the deployed tenant roles as tenant B | 2, 6 (ABAC) | Own prefix and partition allowed; tenant A's denied | harness S3, own prefix: allowed; harness S3, tenant A prefix: denied; tools DynamoDB, own partition: allowed; tools DynamoDB, tenant A partition: denied | PASS |
| Tenant B tells the agent to act for tenant A | 3, 6 (tenant on every tool call) | Only Example Corp data or a refusal: the interceptor sets the tenant on every tool call | state COMPLETED; tools []; leaked nothing | PASS |
| DNS Firewall blocks a non-allowlisted name for the runtime VPC | 4 (DNS Firewall BLOCK) | The catch-all rule is BLOCK with only a narrow ALLOW list, bound to the runtime VPC, so a script in the runtime can't resolve a name off the list | catch-all priority 200 action BLOCK; 1 ALLOW rule(s); associated to VPC (ID redacted) | PASS |
