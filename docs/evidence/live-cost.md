# Live cost evidence

> **Example output.** Captured from a sandbox run; the demo's **Measure the last 24 hours** button
> and `demo/measured.py` produce current figures for your own stack.

Measured on 2026-10-19 against stack `SplitHarness` in ap-southeast-2, from CloudWatch: the
worker's Lambda `platform.report` records, and the runtime's `MemoryUsed-GBHours`,
`CPUUsed-vCPUHours` and `Invocations` metrics in `AWS/Bedrock-AgentCore` at one-minute periods.
Prices are the Sydney on-demand rates in the AWS Price List API: Lambda on Arm $0.0000133334 per
GB-second; AgentCore Runtime $0.0895 per vCPU-hour and $0.00945 per GB-hour. This is demo volume
in a sandbox, not a load test.

## Worker Lambda (1 GB, Arm)

| Runs | Cold starts | Billed time, average | Billed time, median | Range | Peak memory used |
| --- | --- | --- | --- | --- | --- |
| 33 | 7 | 4.2 s | 3.1 s | 2.2 to 10.1 s | 278 MB |

At the average, one run costs $0.000056. For the cost model's 4,320 events per tenant per month
that is about $0.24, against the model's $0.29 for 5-second runs.

## Runtime usage

Usage was recorded only in the minutes around turns that ran a script:

| Window (UTC) | Invocations | What ran | Metered usage | Cost |
| --- | --- | --- | --- | --- |
| 07:43 to 07:48 | 1 | One price-list script | 0.091 GB-hours (about 1 GB until the 300-second idle timeout ended), 0.0012 vCPU-hours | $0.00097 |
| 07:50 to 08:04 | 4 | Price-list turns while the script guard was fixed, then the clean demo run; the session stayed warm between calls | 0.201 GB-hours, 0.0023 vCPU-hours | $0.00210 |
| 08:17 to 08:23 | 4 | Workspace round trip, one session per tenant | 0.178 GB-hours, 0.0026 vCPU-hours | $0.00192 |

In the minutes when the worker ran only Gateway turns (09:36 UTC, 5 runs; 09:57 UTC, 3 runs), the
runtime recorded no usage. The day's runtime total was $0.0050.

The cost model bills idle memory as Runtime v2 does: 1 GB for 120 seconds, then the 128 MB
floor until the idle timeout ends. This runtime held about 1 GB for the whole idle period. Which
billing version applies to it has not been checked.
