# EC Workflow Foundation

Local, standard-library Python implementation of versioned EC request review and
representative decision visibility. This is an executable development foundation,
not a production pricing system, authenticated application, or daily intake job.
The core/demo makes no network calls. Optional ETL and reference readers use
SELECT-only Snowflake queries. Nothing emails or loads production pricing.

## Run

Use Python 3.10 or newer from this folder. The core/demo uses only the standard
library. Live reads require `snowflake-connector-python`; Excel exports and their
tests additionally require [requirements-workbooks.txt](requirements-workbooks.txt).

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-workbooks.txt
.\.venv\Scripts\python.exe ec_workflow.py demo
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

On systems without the Windows launcher, substitute `python` or `python3`.
The demo uses synthetic data in memory and shows a reviewer queue, a correction
decision, and the representative's summary, requested lines, and decision history.
Its unapproved pricing policy cannot be approved. It does not modify the workbook,
existing queries, or external systems.

## PowerShell Test Email

[send_ec_test_email.ps1](send_ec_test_email.ps1) uses the existing classic Outlook
COM approach. It is restricted to `jake.tenkate@mckesson.com`, no CC/BCC, and the
two analyst benchmarks: account 90586438 / PRCA 90586688 / MBR-0011485 / Diagnostics
Tier 7, and account/PRCA 62040942 / MBR-0011502 / Nitrile Tier 7. Those Tier 7 values
are user-reported historical decisions, not forced calculated recommendations.

Once a validated calculation package is available, use these command forms:

```powershell
$package = 'PATH_TO_VALIDATED_PACKAGE.json'
powershell -NoProfile -File .\send_ec_test_email.ps1 -PackagePath $package
powershell -NoProfile -File .\send_ec_test_email.ps1 -PackagePath $package -Draft
powershell -NoProfile -File .\send_ec_test_email.ps1 -PackagePath $package -Send -WhatIf
powershell -NoProfile -File .\send_ec_test_email.ps1 -PackagePath $package -Send
```

No switch validates and creates `email_preview.html` beside the package without
opening Outlook. `-Draft` saves an Outlook draft without sending. `-Send` submits
the fixed-recipient test message; `-WhatIf` avoids preview writes and Outlook
actions. Classic Outlook must be installed with a working profile. Script
execution must be permitted by organizational policy or an approved signature;
the launcher does not change execution policy or request elevation.

The input contract is a JSON object with `mode: EC_TWO_CASE_TEST`, the fixed
`recipient`, and exactly two `cases`. Every case must include `request_id`,
`account`, `prca`, canonical `category`, `analyst_tier: 7`, `current_tier`,
`suggested_tier`, `annualized_gp_change`, `annualized_internal_cost_change`,
`annualized_customer_spend_change`, `rationale`, `exceptions`, and `status`.
Unknown numeric fields must be null, not invented zeros. Status is
`CALCULATED_FOR_REVIEW`, `REVIEW_REQUIRED`, or `INPUT_UNAVAILABLE`; the latter two
cannot claim a suggested tier. HTML source text is encoded before rendering.

The sender never queries databases, infers recommendations, creates approvals or
loads prices. It records a submission receipt and prevents automatic resubmission
of the same package bytes. Outlook submission is not delivery confirmation.
Failed/ambiguous send claims require investigation rather than automatic retry.

Current process is manual review, not email automation. No test email or Outlook
draft has been created. The optional PowerShell sender's runtime tests are blocked
by Windows execution policy and the package contract has not been integrated with
it. Do not bypass policy or use it as part of the manual review process.

## Manual Analyst Benchmark Review

For the two historical Tier 7 comparison cases, create a read-only manual review
package without email or approval actions:

```powershell
.\.venv\Scripts\python.exe ec_manual_case_review.py --output-dir "Review Outputs/Analyst Benchmarks/20261022_ExactSchedule" --smartsheet-env-file "C:\path\to\approved\Smartsheet\Automation\.env"
```

The command reads `MBR-0011502` from the October snapshot and gets the verified
`MBR-0011485` request and its workbook attachment from Smartsheet. It combines
these with current item, Tapestry, attachment, and IPC data, scoped to the
Diagnostics and Nitrile categories only. It never emails, writes to Snowflake,
approves a tier, or loads pricing. Outputs are `manual_summary.html`,
`MBR-0011485_Manual_Review.xlsx`, `MBR-0011502_Manual_Review.xlsx`, and
`review_manifest.json`.

Each workbook compares the existing category tier with ten candidate tier
scenarios, includes requested and existing category items, separates tier-only
effects from the requested-price scenario, and shows analyst-judgment and data
exceptions. SQL, Python and workbook benchmarks use the exact discrete schedule:
Tier 1 25%, 2 23%, 3 21%, 4 18%, 5 16%, 6 14%, 7 12%, 8 9%, 9 7%, and 10 5%.
The review reports two distinct results. The request-price tier suggestion is
based on requested prices and candidate costs, even when customer IPC activity
is unavailable. Because requested quantities are not present, it uses one sell
unit per distinct requested item as a labeled proxy; duplicate source lines
count once and remain flagged. The separate customer-category scenario retains
IPC sales for non-requested items and substitutes requested price at observed
quantity for requested items with IPC activity. It does not assume volume for
requested items without IPC activity. The category suggestion is the unique
eligible tier whose category-wide requested-price scenario margin is closest
to target; tier-only margin remains a separate comparison. Neither suggestion
is an approval. Historical Tier 7 is a comparison benchmark, not a constraint
imposed on the calculation. The historical individual-cost defaults
(20% generally, 18% Nitrile, 10% Vinyl) are context only and do not override the
Tapestry schedule.

Diagnostics also contains requested glove categories. This package deliberately
excludes them from the Diagnostics benchmark and lists them as excluded request
categories. A missing requested UOM is defaulted to `SELL_UOM` and marked as an
assumption. Prices at or below candidate cost are flagged for review. Per-candidate
nonviability is labeled by tier, not treated as a blanket request rejection.

The category baseline currently covers mapped Tapestry MCKB items with IPC
activity. Customer-wide MCKB/national-brand totals can be unavailable; those are
not replaced with zero. Projected conversion opportunity was not provided and is
not included in existing sales. IPC row grain/nonoverlap and quantity-in-sell-UOM
remain assumptions. Annualization is a 3-month run-rate times four, not a forecast.
Current reference costs/tier attachments do not reconstruct the historical
analyst-decision date. These are manual review examples, not an approval record
or rep-facing report.

## Customer Excel Reviews

[ec_customer_workbooks.py](ec_customer_workbooks.py) creates one internal Excel
workbook per resolved PRCA and a clickable customer index, combining requests for
the same customer. It uses an existing review snapshot and makes no network calls,
database changes, approvals or price loads. Existing output folders are never
overwritten. Customer partitioning, requested-line counts and ten-tier coverage
must reconcile before exporting; unresolved customers stop the export.

```powershell
.\.venv\Scripts\python.exe ec_customer_workbooks.py --input "Review Outputs/October 2026/20261021_123859/review.json" --output-dir "Review Outputs/October 2026/20261021_123859/Customers"
```

The October snapshot contains 31 resolved customers across 33 requests. Open
[Review Outputs/October 2026/20261021_123859/Customers/Customer_Index.xlsx](Review%20Outputs/October%202026/20261021_123859/Customers/Customer_Index.xlsx)
and follow a workbook link. Each file contains a customer summary, requested items,
tier-cost and margin matrices, tier-impact scenarios, requested-category scenarios,
exceptions, analyst notes, and source assumptions. Numeric margins are displayed
as percentages; exact source-price strings are retained separately. Workbook notes
are not written back as pipeline approvals. Treat these as internal pricing files,
not rep-facing deliverables. Source text is written as literal text, not Excel
formulas. Generated outputs are excluded from source control, not from OneDrive
sync or existing folder access: maintain approved storage permissions.

These files reorganize the current requested-item review. They are not yet a full
replica of the Tapestry tool: whole-category existing business, customer-wide MCKB
and national-brand context, separate projected opportunities and closest-to-20%
category ranking still require implementation. Those gaps are labeled in each
workbook; do not interpret requested-category totals as a full-category decision.

## Isolated Pipeline Test

[sql/ec_pipeline_test_tables.snowflake.sql](sql/ec_pipeline_test_tables.snowflake.sql)
creates two transient tables in `PRD_SBX_MMS_DB.PRC`:
`EC_REBATE_REQUEST_HEADER_PIPELINE_TEST` and
`EC_REBATE_REQUEST_LINES_PIPELINE_TEST`. The setup seeds a bounded September
24-30, 2026 Extended Care sample and associated lines. Existing test tables are
preserved on reruns, not replaced or refreshed. Original ETL tables are only read.
Creating the tables requires an authorized role with CREATE TABLE privileges.

After setup, run the current database-to-review pipeline against the test inputs:

```powershell
$run = Join-Path $env:LOCALAPPDATA ('ECRebateWorkflow\pipeline_test_' + (Get-Date -Format yyyyMMdd_HHmmss))
.\.venv\Scripts\python.exe ec_etl_preview.py --table-set pipeline-test --since 2026-09-24 --until 2026-10-01 --limit 500 --segment "Extended Care" --output "$run\preview.json"
.\.venv\Scripts\python.exe ec_review.py --input "$run\preview.json" --output-dir "$run\review"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The preview records the selected table names. Enrichment still reads current
customer, item, Tapestry, and IPC reference tables; those references are not frozen
test copies. Review outputs remain local and non-loadable. The copied inputs retain
source blanks and UOM discrepancies, so a successful execution is not a data-quality
approval. This tests database extraction through pricing review, not direct
Smartsheet/workbook extraction or production fixed-price loading.

## Append October Test Inputs

[ec_test_inputs.py](ec_test_inputs.py) imports the verified flat
`PRD_SBX_MMS_DB.PRC.MCKB_RBT_TEST_OCT_2026` source into the existing pipeline-test
header/line pair. It never writes the original ETL tables or any pricing table.

```powershell
$run = Join-Path $env:LOCALAPPDATA ('ECRebateWorkflow\october_' + (Get-Date -Format yyyyMMdd_HHmmss))
.\.venv\Scripts\python.exe ec_test_inputs.py --since 2026-10-01 --until 2026-10-22 --output-dir "$run\dry_run"
.\.venv\Scripts\python.exe ec_test_inputs.py --since 2026-10-01 --until 2026-10-22 --apply --output-dir "$run\applied"
.\.venv\Scripts\python.exe ec_etl_preview.py --table-set pipeline-test --since 2026-10-01 --until 2026-10-22 --limit 500 --segment "Extended Care" --output "$run\preview.json"
.\.venv\Scripts\python.exe ec_review.py --input "$run\preview.json" --output-dir "$run\review"
```

The default importer run is a rollback-only preview. `--apply` appends headers and
lines in one transaction after checking existing data. Rerunning an unchanged
request is a no-op; conflicting existing requests stop the import without an
overwrite. Identical-looking source lines retain their multiplicity. Other test
requests are compared before and after writing to verify they remain unchanged.
Run this as a single writer; ordinary Snowflake tables do not enforce unique IDs.

## Smartsheet ETL Intake

[`ETL/ec_smartsheet_intake.py`](ETL/ec_smartsheet_intake.py) reads request rows
from the landing, intermediate, completed, and rejected Smartsheet sheets, parses
their `.xlsx` or `.csv` line-item attachments, and appends raw header, line, and
attachment records to separate ETL-only sandbox tables. PEX, item, UOM, and floor
checks run later as an append-only audit snapshot. It defaults to a five-day
Created-date window; use repeated `--request REQUEST_ID=ACCOUNT_NUMBER` options to
target specific requests. It does not write status/approval fields or read/write
the previously populated `*_PIPELINE_TEST` tables, standard ETL tables, or
production tables.

Place `SMARTSHEET_API_TOKEN` and the approved Snowflake connection variables in
a private `ETL/.env` file. The script loads that file beside itself. Keep it out
of source control and follow company secret-storage policy; OneDrive may still
sync ignored files. Install dependencies with
`python -m pip install -r requirements-workbooks.txt`, create the three raw ETL
tables using [`ETL/sql/create_smartsheet_intake_test_tables.snowflake.sql`](ETL/sql/create_smartsheet_intake_test_tables.snowflake.sql),
create the validation audit table using [`ETL/sql/create_validation_audit_table.snowflake.sql`](ETL/sql/create_validation_audit_table.snowflake.sql),
and create the read-only PBI views using [`ETL/sql/create_smartsheet_intake_views.snowflake.sql`](ETL/sql/create_smartsheet_intake_views.snowflake.sql).
After raw import, run [`ETL/sql/snapshot_smartsheet_validation.snowflake.sql`](ETL/sql/snapshot_smartsheet_validation.snowflake.sql).
The raw tables are distinct from the existing test tables.

Run from the `Automation` directory. The first command is a dry run and does not
write Snowflake tables:

```powershell
python -m ETL.ec_smartsheet_intake --request MBR-0011568=20046631 --request MBR-0011567=20046631 --output-dir "$env:LOCALAPPDATA\ECRebateWorkflow\intake_dry_run"
```

Review `source_snapshot.json` and `manifest.json`. To append the validated rows
to the separate `*_ETL_TEST` sandbox tables, choose a new output directory and add `--apply`.
The script fails closed for missing or conflicting requests, repeated request
rows/stages, duplicate attachment content, and changed data on replay. It
preserves duplicate line items and does not resolve PEX/item eligibility, set
status, approve prices, send email, or load pricing. PRCA/PEX/item/UOM/floor
results are stored separately in the validation audit. Full source header and
attachment metadata remain in the local snapshot.

Run its offline tests with:

```powershell
python -m unittest discover -s ETL/tests -v
```

Source fields map explicitly: `REQUEST_ID` to `ROW_ID`, `ACCOUNT_NUMBER` to
`CUST_NUM_PRCA`, `CREATED_DATE` to `CREATED_DT`, `UOM_PROVIDED` to `REQ_UOM`, and
`FUTURE_SELL_PRICE` to `REQ_PRICE`. One derived header is created only if its source
rows agree on account/date/platform. Analyst, requester, completion and other
unavailable header metadata stay null; these are not refreshed Smartsheet headers.
Notes, rationale, exact source prices and source hashes remain in the private
`source_snapshot.json` beside the import `manifest.json`.

Customer/PEX mapping identifies EC requests. Known other-platform requests are
excluded; unresolved mappings are retained with an unset segment for investigation,
not falsely labeled EC. The example preview's segment filter excludes unresolved
records, so inspect the importer manifest before treating it as complete coverage.
Source UOMs and prices are preserved, including missing UOMs; no conversion or
approval occurs during importing. The current review may flag those missing UOMs;
the newly discussed sell-UOM assumption/ranking policy is not implemented here.

On October 21, the standard ETL header table had no October records, while the
monthly snapshot had 658 rows across 47 requests dated October 1-19. This import
selected 33 confirmed EC requests and 334 lines, excluding 14 other-platform
requests, and preserved the 17 September headers and 725 September lines. October
enrichment produced 3,340 tier scenarios: 282 lines with candidate margins and 37
with historical-impact estimates. These are sample results, not approval metrics.

Reference costs and tier attachments are current at review extraction. The review
records the evaluated time and exact reference values/hash; the October submission
date alone does not establish which cost-change version is represented. Do not
interpret the output as a reconstruction of the original assigned-tier decision.

## Real-Data Pricing Review

[ec_etl_preview.py](ec_etl_preview.py) selects existing ETL request headers and
associated lines without running the ETL notebook. [ec_review.py](ec_review.py)
then enriches that local snapshot using
[sql/ec_review_enrichment.snowflake.sql](sql/ec_review_enrichment.snowflake.sql).
The query reuses the customer/item restrictions, PEX platform mapping, current
category attachment, ten Tapestry tiers, benchmark formula, and IPC sources from
the two supplied queries. The supplied queries and workbook remain unchanged.

Run in the configured ETL environment or workspace virtual environment with the
approved Snowflake environment variables already set. Browser SSO is supported by
`SNOWFLAKE_AUTHENTICATOR=externalbrowser` and `SNOWFLAKE_USER`; credentials are
never stored in this repository. Prefer a SELECT-only source role.

```powershell
.\.venv\Scripts\python.exe -m pip install snowflake-connector-python
.\.venv\Scripts\python.exe ec_etl_preview.py --since 2026-09-24 --until 2026-10-01 --limit 25 --segment "Extended Care" --output "$env:LOCALAPPDATA\ECRebateWorkflow\sample.json"
.\.venv\Scripts\python.exe ec_review.py --input "$env:LOCALAPPDATA\ECRebateWorkflow\sample.json" --output-dir "$env:LOCALAPPDATA\ECRebateWorkflow\review_01"
```

Choose a new preview filename and review directory for every run; existing
outputs are never overwritten. The preview limits header rows, not associated
line count. Dates are an inclusive start and exclusive end. The segment filter
only selects intake rows; enrichment independently checks `PEX.BUS_PLTFRM = EC`.

The private local output directory contains:

| Artifact | Contents |
| --- | --- |
| `item_tier_review.csv` | Ten tier scenarios per requested source line, reference UOM/costs, margins, sales/activity, impact and explicit exceptions |
| `customer_category_tier_review.csv` | Request/PRCA/category/tier scenarios, line/exception/coverage counts and complete-coverage impact totals |
| `review.json` | Source/reference hashes, original header exceptions, exact queried reference values, detailed scenarios and assumptions |

To create a browser-viewable static report from a review JSON, use a new local
output directory:

```powershell
.\.venv\Scripts\python.exe ec_html_review.py --input "<review.json>" --output-dir "<new-html-directory>"
```

The exporter writes an `index.html` plus one HTML page per request. Pages include
positive-price analysis and excluded unpriced lines, use no external scripts or
network assets, and remain internal review material. Store and share them only
with approved access; this command does not publish or host them online.

These are **internal pricing-review outputs**, not rep-facing reports or loader
files. They contain customer, representative, internal cost and margin data; use
approved local storage and permissions. No approvals are created by this runner.
There is no final tier recommendation while tier-selection policy is provisional.
Requested price is preserved, not replaced by the benchmark or a calculated price.

### Calculation Basis

- Tapestry costs use item `SELL_UOM`, as confirmed. Missing/mismatched requested
  UOMs are explicit exceptions; no UOM conversion is invented. Raw UOMs are
  retained; comparison ignores surrounding whitespace and case only.
- Candidate margin is `(requested price - candidate cost) / requested price`.
  Valid item-level margins remain visible even when customer/account data is
  missing; this does not make those lines approved or loadable.
- IPC is aggregated by PRCA/item/sell UOM across its rows, rather than discarding
  activity through the original single-row tier-selection step. Mixed sell UOMs,
  negative activity, incomplete quantities/sales and nonpositive quantities block
  impact estimates. Current price ranges and price dates remain visible.
- IPC quantity is assumed to use IPC `SELL_UOM`, following the original query.
  The schema exposes that UOM but does not independently prove quantity semantics
  or that multiple IPC rows contain nonoverlapping activity. Verify both before
  relying on the estimates. `PRICE_DATE` is not treated as a sales-period end date.
- Three-month internal cost change is `(candidate cost - current tier cost) *
  quantity`. Positive means higher internal cost.
- Three-month customer spend change is `requested price * quantity - historical
  sales`. Positive means increased spend. This uses realized historical sales,
  not an arbitrarily selected current IPC unit price.
- Annualized values multiply the three-month result by four. They are run-rate
  scenarios, not forecasts. Gross-profit change is a proxy combining the spend
  and cost changes; current tier cost is not historical actual cost.
- No activity or missing current-tier data remains unavailable, not zero.
  Group totals remain blank with incomplete impact coverage or repeated requested
  items in that group, avoiding silent partial totals or duplicated usage.
- Do not sum alternative tiers or overlapping requests. Group scenarios cover
  requested items only, not category-wide effects on unrequested items.
- Reference data is current at extraction, not reconstructed as of submission.
  Missing or ambiguous matches remain exceptions; no customer is guessed.

### Verified Sample Coverage

The September 24-30, 2026 sample contains 17 Extended Care-labeled headers and
725 lines. Enrichment produced all 7,250 line/tier scenarios and 540 grouped tier
scenarios. Candidate margins are available for 714 lines; 11 requested-UOM
mismatches were flagged. Sixteen source headers lack `cust_num_prca`, affecting
724 lines; only one line currently supports customer/tier/sales impact. Three
original headers have no stored lines. This is sample-specific source coverage,
not a general conclusion about all ETL requests.

Recover missing account references from authoritative intake data or repair the
ETL mapping in a separately reviewed change. This workflow does not patch source
tables. ETL-discarded rows, attachment line IDs, or original decimal precision
from FLOAT prices cannot be reconstructed by these readers.

## Implemented

- Immutable source/evaluation snapshots identified by SHA-256 of canonical JSON.
- Durable source-line IDs; duplicate-looking rows remain distinct source lines.
- Positive finite prices, item-number checks, exact UOM comparison, upstream
  validation errors, and conflicting per-item recommendations block group approval.
- Customer/category decisions tied to a specific request revision and snapshot.
- Assigned-reviewer checks, UTC expiry, superseded-revision rejection, and
  idempotent decision command IDs with payload mismatch detection.
- Append-only approval, rejection, and correction decisions with rep-safe reasons.
- Transactional category conflict checks across requests and concurrent reviewers.
- Scoped rep summaries, requested-line details, and decision history. A rep can
  see their own requests or requests for explicitly assigned accounts.
- Approval is reported as `Approved - not sent for loading`, never as loaded.

All requested lines in a group must be valid for that group to be approved in
this initial implementation. Different category groups can have different outcomes.
An override requires a new evaluated snapshot; arbitrary tier changes at decision
time are blocked. Approved groups cannot be revised or released for another
approval until a future amendment/load-reconciliation workflow is implemented.

## Code and SQL Contracts

- [ec_workflow.py](ec_workflow.py): local storage adapter and approval/query methods.
- [sql/ec_workflow.sqlite.sql](sql/ec_workflow.sqlite.sql): tables, immutable-record
  triggers, and reporting views for the local adapter.
- [tests/test_ec_workflow.py](tests/test_ec_workflow.py): synthetic regression tests.

`Workflow()` uses an in-memory database. `Workflow(database_path)` persists locally;
call `close()` when finished. `add_evaluation(snapshot)` accepts a JSON-compatible
dictionary; the demo provides an example. Prices should be decimal strings, not
floating-point values. Keep source/reference identities, rule version, and raw
requested values in the snapshot. Upstream unmatched-data errors must be supplied
in each line's `validation_errors`; this module does not query item or cost sources.

Reviewer methods are `review_queue(actor)`, `review_evaluation(actor, evaluation_id)`,
and `decide(actor, evaluation_id, command_id, action, reason, approved_tier)`.
Rep methods are `rep_requests(actor)`, `rep_lines(actor, evaluation_id)`, and
`rep_history(actor, request_id)`.

| View | Intended Consumer |
| --- | --- |
| `v_ec_review_queue` | Internal group status, counts, proposed tier, assignment |
| `v_ec_review_lines` | Internal requested-line details and validation errors |
| `v_ec_rep_requests` | Rep-safe column projection of latest group outcomes |
| `v_ec_decision_history` | Internal immutable decisions, actors, reasons and revisions |
| `v_ec_latest_evaluation` | Internal latest-revision projection |

Views contain multiple users' rows. **They are not row-level security boundaries.**
The rep methods apply entitlement predicates, but database-file access bypasses
them. Do not publish the SQLite database or these unrestricted views to reps.
Production BI must use identity-enforced secured views or an authenticated service.
Internal history exposes actor IDs; the rep history method excludes them.

## Trust and Production Gates

`Actor` represents an identity already verified by a trusted caller. Constructing
one is not authentication. Account entitlements and reviewer assignments must come
from managed sources, never an editable request or browser-supplied role.

The snapshot's `rule_approved` flag defaults to false. Its use here is a testable
integration contract, not proof that a business owner approved a policy. Before
production, a controlled policy registry must supply that value and rule version.
Likewise, source/reference hashes are recorded inputs; this module does not verify
their authenticity or re-read current attachments, costs, or source files.

Before production approval or distribution, implement and verify:

1. Confirmed intake adapter, durable attachment revisions, and daily scheduling.
2. Business-approved tier selection, handling of requested/sell-UOM mismatches, customer impact, reference
   snapshots, and effective-date rules, validated against expected examples.
3. Current-source/attachment rechecks and material-change invalidation at approval
   and dispatch; snapshot expiry alone does not prove freshness.
4. Organization authentication, managed entitlements, secured BI access, and an
   authenticated review interface. No web interface exists yet.
5. Confirmed loader grain and file schema, approved artifact/hash, email outbox,
   delivery reconciliation, and explicit downstream load acknowledgement.
6. Amendment/cancellation and category-conflict release after verified loading.
7. Monitoring, retention, migrations, backups, and approved deployment runtime.

Tapestry tier costs are confirmed to represent the item's reference `SELL_UOM`.
Use that as the cost UOM and compare it with the requested UOM before calculating
margin. Matching UOMs need no conversion; missing/mismatched UOMs remain exceptions
unless an approved conversion rule is available. IPC quantity UOM still needs
independent verification before sales-based cost or price impact calculations.

The operator confirmed that approving the cost leads to a fixed sell-price load.
Sell-price decisions are approve or reject only: an approved fixed sell price is
the unchanged requested price from the attachment. Pricing cannot override it.
Rejected lines are excluded from load output; a different requested price requires
a revised request and fresh review. Tier adjustments are separate cost-side decisions.
The current prototype records tier-oriented evaluation decisions only; it does
not yet record separate approved cost and final approved fixed sell-price values.
Confirm account scope, UOM, effective dates, and any separate cost/tier load before extending the
decision schema. Do not treat the tier decision as an approved fixed-price file.

No load-ready view/file is provided because the fixed-price loader contract is
unresolved. Approval records are not authorization to perform an automatic
production load.

## GCP and Looker Migration

Keep pricing transformations in SQL and orchestration/decision validation in Python.
Looker should consume secured reporting contracts rather than implement pricing
logic. Confirm the target GCP database before choosing its adapter.

SQLite connection handling, placeholders, UTC functions, DDL and immutability
triggers are local-adapter specifics, not scripts to deploy unchanged to Snowflake
or a GCP warehouse. A production adapter must preserve atomic decision/conflict
checks and replay tests against the target system; reporting SQL alone does not
provide safe concurrent approval writes. This prototype initializes a fresh local
schema and has no versioned database migrations yet.