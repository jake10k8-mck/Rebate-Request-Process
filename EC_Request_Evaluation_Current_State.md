# EC Rebate Request Evaluation: Current State

## Purpose and Scope

This note records the current EC request-evaluation logic shown in the shared SQL files. It distinguishes the request clarification query from the manually parameterized Tapestry cost lookup. It does not assume these scripts are yet connected to the Smartsheet notebook pipeline or to an automated load process.

## Operator-Confirmed Smartsheet Lifecycle

1. A request is submitted in Smartsheet. Its header and attachment lines do not
	enter the ETL tables or PBI until an ETL run extracts them.
2. After extraction, the request should appear as pending while the analyst runs
	the review. Tier changes, contract submission where applicable, and rep
	communication remain manual actions.
3. After the analyst has handled the request, the analyst marks it completed in
	Smartsheet. Smartsheet automation may move the row to the Completed tab before
	the next ETL run.
4. The next ETL scan should use the current Completed-stage row and attachment,
	snapshot the resulting tier and Tapestry cost per line, and expose approved or
	rejected status/reason to PBI. Completion alone must not be interpreted as a
	price approval unless the decision fields and checks support that result.

The current intake code scans Landing, Intermediate, Completed, and Rejected by
default. If a request ID appears in multiple stages, it selects the furthest
configured stage and records the duplicate stages. It fetches attachments from
the selected stage row. Thus a run can ingest a request directly from Completed
if the intermediate row moved before that run; it does not need to observe the
intermediate stage transition. Raw ingestion snapshots are append-only and the
status view selects the latest header snapshot.

The PBI status view currently omits `CMPLTD_FLG` and `CMPLTD_DT` from its final
projection. The line view exposes intake/validation fields, not final approved
tiers or approved costs. Completion status, per-line approval/rejection, and a
post-tier-change Tapestry cost snapshot still need to be added to the reporting
contract. The current intake does not trigger analysis, update tiers, send the
approved-cost email, or mark requests complete.

## Operator-Confirmed Completion and Decision Flow

The operator describes the current tracking process as moving handled requests in
Smartsheet to a new tab and marking them completed. Completion is a manual work-
tracking state; it is not itself pricing approval, a final determination, or a
load instruction.

After completion, the assigned tier is checked. For each requested item that is
not rejected for another reason, the final decision record should retain the
assigned tier and the corresponding Tapestry cost as a reference, on the verified
Tapestry cost UOM. The requested price remains the source value and is not changed
by the cost reference. A decision-status field should distinguish approved and
rejected items, with an explicit reason for rejected items. Pending review or
correction may be tracked separately from final approval/rejection; the exact
status vocabulary remains a business-owner decision.

Tier or contract updates are not automated in this phase. Any later update/load
process is separate future scope and would require an approved policy, confirmed
loader contract, authorization, and post-load reconciliation.

This is the operator-described Smartsheet process. The repository's current
intake examples and ETL code are Smartsheet/October-snapshot based, but no
completion-triggered assigned-tier evaluation is implemented here.

## Request Clarification Query

`OCTOBER 2026 MCKB REBATE REQUEST 1.sql` uses `PRD_SBX_MMS_DB.PRC.MCKB_RBT_TEST_OCT_2026` as its request source. It:

- Looks up E1 customers of types `B`, `S`, or `X` for accounts in the request set and retrieves PRCA/customer information.
- Looks up private-brand items that are not stock type `O`, including item description, UOM, cost fields, and an acquisition-cost-based buy margin.
- Joins request rows to Tapestry item/category data and `PEX_PRCA_VIEW` to add business platform, representative, and customer-size context.
- Splits clarified rows into EC (`BUS_PLTFRM = 'EC'`) and PC (`BUS_PLTFRM = 'PC'`) sets.

The query defines attached-tier and unpivoted Tapestry tier-cost data, including a benchmark-price calculation. However, the EC processing block that would join current attached tier and current tier cost is commented out. The active final statement is `SELECT * FROM CLARIFIED_DATA_EC`, so this script currently returns clarified EC request rows, not the commented current-tier/current-cost comparison.

The commented EC output indicates an intended evaluation shape including request/account/representative context, grouped level, current attached tier, item/UOM, requested sell price, current cost, notes, and creation date.

## Operator-Described EC Review Flow

The operator's understanding is that the October 2026 SQL table is a point-in-time snapshot of Smartsheet requests. The reviewer uses the new requests and their line-item details in an Excel review, checking that requested prices and costs are not materially out of bounds and that the requested UOM is appropriate. If a request appears valid, the reviewer compares the customer's current attached tier with the cost and business impact of assigning a different tier.

The Smartsheet attachment Excel file is the source of line-level request detail: item number, item description, requested UOM, and requested price. The notebook reads these values from columns C:F. The tier evaluation is performed in a separate workbook, `TAPESTRY TOOL v5.1 2.xlsx`, in the Automation folder.

### Tier Evaluation Workbook

The workbook has a `WORKSHEET` input/calculation sheet and a formula-driven `RESULTS` sheet. Formula inspection confirms that it:

- Pulls item descriptions and sell UOMs, requested and last sell prices, converted costs, acquisition cost, and MAC for result rows.
- Looks up costs for two tier options and calculates the corresponding margins against the requested sell price (or last sell price where no new price is entered).
- Compares the two option margins with an intended margin and selects the cost option whose margin is closer. It then derives the selected tier from the selected cost option and exposes current/input tier and category fields.
- Calculates the resulting new-contract margin and includes source/load classification and item flags in the results.

This confirms that tier/cost impact evaluation is embedded in the review workbook, rather than in the active October EC SQL query. The workbook’s formula structure was inspected, but its calculations were not executed or validated against approved business examples. In particular, `RESULTS` column T formulas contain `#REF!` references in one classification lookup branch; verify and repair that branch before relying on the classification output.

The operator's interpretation is that displayed `#N/A` values usually indicate the item or PRCA could not be found in the lookup data. Treat these as unmatched-data cases that should be identified and routed for review, rather than assuming they are calculation/formula defects; verify the exact lookup and source for each occurrence. This is distinct from the literal `#REF!` broken-reference tokens present in the column T formulas.

The workbook shows the item's sell UOM as a reference value, but the inspected formulas do not establish that the requested UOM is automatically checked against it. The out-of-bounds thresholds and the human approval/override action also remain to be confirmed.

This interpretation is consistent with the roles of the shared SQL and workbook files:

- The October query enriches request rows with customer/PRCA, item, business-platform, representative, grouped-level, requested-price, and UOM context. Its active final select returns the clarified EC rows.
- The Tapestry tool workbook performs the tier-option, cost, and margin comparison used for review. The separate manual cost lookup accepts proposed price and tier inputs and can support additional what-if analysis, but is not confirmed as the normal evaluation step.

The shared active SQL does not enforce the review checks, and the October query's current-tier/current-cost EC comparison block is commented out. The Tapestry tool workbook contains the tier evaluation, but the thresholds for “out of bounds,” UOM acceptance rules, and expected outcomes still need business validation before its formulas are replicated in an automated process.

## Manual Tapestry Cost Lookup

`CURRENT TAPESTRY COST LOOKUP 4.sql` is a separate what-if/detail query with manually entered `INPUT_DT` rows (customer, item, UOM, proposed sell price, and usage) and `INPUT_TIER` rows (grouped level and proposed tier).

It combines customer and private-brand item data with:

- Current attached tiers from `TAP_CUST_ATTACHED_TIERS`.
- Tapestry costs for tiers 1 through 10, unpivoted to one row per item/tier.
- Current pricing/activity data from `HAH_IPC`.
- Optional proposed input price and manually selected tier.

At item level, it reports current tier/cost and current-price margin, proposed-price margin, sales and usage measures, and cost-change information. It also calculates the cost and buy-side impact for the manually selected input tier. A `QUALIFY ROW_NUMBER()` chooses one candidate tier row per PRCA/item: closest benchmark price to the proposed price when one is entered, otherwise closest tier number to the current tier. Optional commented output blocks calculate portfolio/category margin and negative-margin counts.

This is an analyst-driven lookup, not evidence of an automated per-request process: the customer/item/price inputs and category tiers are edited in the SQL `VALUES` clauses.

This SQL is separate from the formula-driven `TAPESTRY TOOL v5.1 2.xlsx` workbook and should be treated as a supplemental what-if tool unless the operator confirms it is part of the regular run.

## Relationship to the Smartsheet Pipeline

The shared Smartsheet notebook reads request headers from landing, intermediate, completed, and rejected sheets. It compares request row IDs with existing Snowflake headers, filters to a recent date window, and processes attachments for rows considered new. It recognizes three Excel template markers in cell B2, reads line data from columns C:F, and maps those columns to item number, item description, requested UOM, and requested price.

The notebook's visible line validation checks for a numeric item number and a positive numeric requested price, strips digits from the UOM text, removes exact duplicate lines, and rejects overlength text fields. It does not visibly compare requested UOM with the item's sell UOM, check the requested price against cost bounds, or perform tier evaluation. Accepted headers and line items are written to separate `MCKB_REBATE_REQUEST_HEADER` and `MCKB_REBATE_REQUEST_LINES` tables; it also synchronizes completion fields from Smartsheet to existing Snowflake headers.

The notebook's final email call sends a run manifest/log and optional rejected-line or attachment-failure artifacts through a PowerShell report script. In the shown code, it does not create or email an evaluated load-ready row file. That is distinct from the required load-data email for the new workflow.

The October SQL instead reads `MCKB_RBT_TEST_OCT_2026`, and the manual lookup uses manually entered values. The files shared do not establish how that monthly request table is populated from the notebook tables, or whether the manual lookup is run for each request. Confirm this source/handoff before automating the EC evaluation.

## Implications for the New Workflow

The new EC workflow should keep these responsibilities explicit:

1. Ingest and validate a request header and its attached line items.
2. Enrich each line with customer/PRCA, representative, business platform, item/UOM, current attached tier, Tapestry current cost, and current price/activity as applicable.
3. Calculate the requested-price and recommended-tier/cost comparisons using approved business rules. The tier-selection rule and benchmark-price formula should be validated with business owners before production use.
4. Persist row-level request status and evaluation outcome so representatives can see progress/results in Power BI.
5. Produce an auditable, deterministic load-ready output and automatically email the exact rows to load, with agreed columns, format, recipients, and approval gate.

## Open Questions

- Is `MCKB_RBT_TEST_OCT_2026` populated from the Smartsheet header/line tables, and what process creates it?
- Should EC requests use the commented current attached-tier/current-cost logic, the manual lookup logic, or a formally revised rule?
- Which cost and price fields are authoritative for EC, and how should missing customer, item, attached-tier, cost, or pricing data be handled?
- What determines a recommended tier, including the benchmark-price equation, tie handling, and any portfolio-level constraints?
- What should the load email contain, who receives it, what file format is required, and does a person approve it before sending/loading?
- Which request statuses and outcome details should be exposed to representatives in Power BI, and what is the report's source of truth?

## Source Files Reviewed

- `OCTOBER 2026 MCKB REBATE REQUEST 1.sql`
- `CURRENT TAPESTRY COST LOOKUP 4.sql`
- `TAPESTRY TOOL v5.1 2.xlsx` (formula structure inspected; calculations not executed)
- `SmartsheetJupyter v2.ipynb` (shared notebook excerpt)
- `Header_line_data.sql`
