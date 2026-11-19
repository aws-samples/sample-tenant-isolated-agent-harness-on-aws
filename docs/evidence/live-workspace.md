# Live evidence: workspace round trip

> **Example output.** Captured from a sandbox run, kept as a record of the workspace isolation test.

Run on 19 October 2026 at about 08:18 UTC against stack `SplitHarness` in ap-southeast-2, with
the demo client (`demo/client.py`). Each turn went through the public API with the tenant's own
ID token and ran `execute_process` in that tenant's runtime session.

| Step | Who | What happened | Observed |
| --- | --- | --- | --- |
| 1 | AnyCompany's runtime | Sum `unit_price_aud` in `uploads/prices.csv`, write the total to `total.txt` in `$WORKSPACE_DIR` | Turn completed in 13.5 s |
| 2 | Operator, S3 API | List each tenant's workspace prefix | AnyCompany: `total.txt` containing `1652.3`. Example Corp: empty |
| 3 | AnyCompany's runtime | Print `total.txt` from `$WORKSPACE_DIR` (next turn) | `1652.3`, in 4.7 s |
| 4 | Operator, S3 API | Write `operator-note.txt` straight to AnyCompany's workspace prefix in S3, bypassing the runtime | Object written |
| 5 | AnyCompany's runtime | Print `operator-note.txt` from `$WORKSPACE_DIR` | The note's exact text, including its timestamp, in 5.8 s |
| 6 | Example Corp's runtime | `find / -name total.txt; ls -la "$WORKSPACE_DIR"` | `find` printed nothing; the workspace was empty |

What this shows:

- Files a tenant's script writes to `$WORKSPACE_DIR` reach that tenant's S3 prefix (step 2), and
  only that prefix.
- Files that exist only in S3 reach the tenant's runtime (steps 4 and 5): the workspace is pulled
  from S3, not just kept in the microVM.
- Another tenant's runtime has no copy anywhere on its file system (step 6). This is the property
  a shared S3 Files mount would break.

Not shown: a cold microVM start. That uses the same S3 pull as step 5.
