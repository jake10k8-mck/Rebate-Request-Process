# EC Daily Rebate Evaluation Automation

## Goal

Create a separate, daily workflow for EC rebate requests. It should evaluate only the items actually requested, present tier recommendations and customer impact for human approval, produce the exact approved rows to load, and preserve request/outcome history for representatives and audit.

This is a requirements draft based on the operator's current understanding. It is not an approved pricing policy or production design.

## Implementation Direction

Keep evaluation/reporting logic in SQL and orchestration/controlled approval writes in Python. Expose stable, secured views for current reporting tools and future GCP/Looker consumption; avoid embedding pricing or approval policy in a particular BI or workflow product.

The initial local implementation is documented in [README.md](README.md). It provides versioned evaluation records, guarded group decisions, reporting views, and synthetic tests. Production intake, pricing rules, authentication, source-freshness rechecks, load artifacts, email, and load confirmation remain gated work, not implemented production capabilities.

Read-only real-data review is now implemented in [ec_review.py](ec_review.py), using [sql/ec_review_enrichment.snowflake.sql](sql/ec_review_enrichment.snowflake.sql) and existing ETL sample outputs. It retains all ten candidate tiers per requested line, calculates sell-UOM margins and illustrative sales-based impacts, and produces internal local CSV/JSON review outputs without selecting a recommended tier, recording approvals, sending email, updating source tables, or loading prices. Annualization uses three-month activity times four; IPC quantity basis and row-grain assumptions remain explicit. See the implementation notes for sample coverage and missing account-reference exceptions.

## Current Evaluation Inputs

The current Tapestry workbook is `TAPESTRY TOOL v5.1 2.xlsx`. The operator identified two SQL-driven data inputs as necessary to make its evaluation tabs functional; other workbook tabs are believed obsolete and should be confirmed before removal.

1. **Rep/item costs:** `2. Rep Costs w Future Costs - SNOWFLAKE - works (1) 2.sql` refreshes item lookup details such as item description, MAC, acquisition cost, UOMs, and representative costs. The provided query currently filters to one item (`1074668`) and uses a fixed future period (`DIM_PERIOD_ID = '202601201'`); these must be parameterized for daily processing and limited to requested items.
2. **Tapestry contract costs:** `TAPESTRY CONTRACT COSTS 2.sql` retrieves eligible Tapestry contract item/UOM cost data. The operator says this populates the workbook's Tap Data tab, which feeds the Grid pivot table.

### Confirmed Tapestry Cost UOM

The operator confirmed that Tapestry tier costs represent the item's **sell UOM**. Use `SELL_UOM` from the item reference lookup as the cost UOM for every Tapestry tier; this basis is no longer an open decision.

- Compare the requested UOM (`UOM_PROVIDED` in the October query or `REQ_UOM` in the ETL lines) with the item's reference `SELL_UOM` before calculating requested-price margins against tier costs.
- When they match, requested price and Tapestry tier cost are on the same UOM basis; no conversion is needed for that comparison.
- If requested UOM is blank, assume the item's `SELL_UOM` and explicitly mark that assumption. If a requested UOM is present but differs from `SELL_UOM`, flag it for review; do not silently convert the price or UOM.
- Independently verify the quantity/price UOM used by `HAH_IPC` before multiplying tier-cost or sell-price differences by historical quantity. The confirmed Tapestry basis does not establish the IPC quantity basis.

The workbook's `RESULTS` sheet calculates two tier-option costs and margins, compares option margins against an intended margin, derives a selected option/tier, and displays item-level context. The formula structure has been inspected, but the calculations have not been executed or validated against approved business examples. The column T formulas include `#REF!` references that must be investigated before relying on that classification.

`#N/A` caused by a requested item with no price is a validation error: exclude that line from tier evaluation, approval recommendations, and load output. Retain the source row and error in the audit history. Other causes, such as an item or PRCA not found in lookup data, should remain explicit unmatched-data errors; confirm their handling rather than treating them as valid rows.

## Tier Assignment Decision Aid

The analyst clarified that Tapestry uses a **tier-specific declining margin target**, rather than applying the former individual-cost default margin to every tier. The active schedule is:

| Tier | Target Margin |
|---:|---:|
| 1 | 25% |
| 2 | 23% |
| 3 | 21% |
| 4 | 18% |
| 5 | 16% |
| 6 | 14% |
| 7 | 12% |
| 8 | 9% |
| 9 | 7% |
| 10 | 5% |

For an item with tier cost $C_t$ and that tier's target margin $M_t$, the benchmark sell price is $C_t/(1-M_t)$. A candidate tier is viable only when the unchanged requested price exceeds that tier's cost. A requested price at or below Tier 10 cost is an **analyst-review exception**, not an automatic rejection.

Produce two distinct review results. First, suggest a request-price tier from the requested item prices and candidate costs without requiring customer IPC activity. Since requested quantities are not available, use one sell unit per distinct requested item as an explicitly labeled proxy; repeated snapshot rows count once and remain flagged. Compare the resulting request-item margin with each candidate tier's target, requiring every requested item's price to exceed that tier's cost. Second, evaluate the customer category using IPC quantities and sales: retain IPC sales for non-requested category items and replace current IPC revenue with requested price times IPC quantity for requested items with IPC activity. Do not assume volume for requested items without IPC sales. Compare category scenario GP divided by scenario sales with each candidate tier's target and mark the uniquely eligible closest candidate as a separate **category suggestion for review**; never auto-approve either result. Keep the tier-only margin on current IPC sales as a separate comparison, and report GP-dollar change. The acceptable GP-dollar expansion threshold is not defined and remains analyst judgment.

Former individual-cost defaults of 20% generally, 18% for Nitrile, and 10% for Vinyl are historical context. They do not override the Tapestry tier schedule. Missing sales, costs, or incompatible UOM must remain visible exceptions, not zeros. A blank requested UOM defaults to `SELL_UOM` with an assumption flag. No conversion is inferred for a populated UOM mismatch.

Historical analyst Tier 7 decisions are comparison benchmarks, not constraints on calculated suggestions. Current reference values do not reconstruct the costs available when those historical decisions were made. Projected conversion opportunity is shown separately from existing-business scenarios and is excluded unless an explicit opportunity amount and defensible quantity/volume basis are supplied.

## Approval Experience

The operator confirmed that, from a pricing perspective, approving the cost leads to loading the price as a **fixed price**. Keep the cost approval and customer sell-price load distinct; a tier recommendation alone is not the final pricing output. This does not establish whether a separate tier attachment/cost-side load is also required.

- Review and retain the approved cost and its UOM separately from the final approved fixed sell price and price UOM for each approved requested line.
- Sell-price decisions are approve or reject only. On approval, the fixed sell price must equal the requested sell price from the attachment for that approved source revision. Pricing cannot replace it with an approver-entered, calculated, cost, or tier-benchmark value. Rejected lines must not enter load output; a different requested price requires a revised request and fresh review.
- Bind the fixed sell price, customer/account scope, item, UOM, and agreed effective dates to the approved evaluation before producing load output. Any change to these approved values requires renewed review.
- Group decisions at **customer/PRCA + product category** level.
- Show the supporting item-level details within each group so approvers can inspect each requested item and its recommendation/impact.
- Allow the approver to approve, reject, or override the proposed tier. Define whether an override applies to one item or the full customer/category group.
- Keep unmatched or invalid rows out of the approved load output and present them as explicit exceptions.
- Bind each decision to an immutable evaluation revision, reference snapshot, and approved rule version. Material changes must trigger reevaluation rather than silently alter an approved result.
- Confirm category attachment/load grain before enabling item-level overrides. Detect conflicting decisions across requests for the same PRCA/category.
- Provide an assigned review queue, item drilldown, plain-language decision reasons, and a preview of the exact approved output once the loader contract is confirmed.

## Post-Approval Load Integration

Keep these three operations separate. They affect different records and must not be treated as interchangeable load files:

1. **Approved fixed sell price:** For each approved request line, prepare the requested sell price for the approved customer/account, item, UOM, and effective dates. Approval does not authorize a calculated or reviewer-entered replacement price. Produce a load artifact for the confirmed E1 fixed-price loader; do not write directly to E1 or to Tapestry cost history.
2. **Customer/category tier attachment and item archive:** The confirmed desired approval outcome is to update `TAP_CUST_ATTACHED_TIERS` for the approved PRCA/category and append an immutable archive row for each requested item in that approved group. The archive records the approved tier, the corresponding Tapestry item cost and cost UOM from the evaluated reference snapshot, and the exact requested sell price and price UOM approved from that request revision. It also retains prior/new attached tier and decision provenance. This archives the approved cost/price context; it does not change the item-level Tapestry tier-cost master. The supplied manual query is a customer-tier `MERGE`, not this complete approval workflow. Its commented contract output is a customer eligibility file, not an item-cost file.
3. **Tapestry item/contract cost update:** Item tier costs are generated from the Tapestry cost process and exposed through `MCKB_COST_TAPESTRY_CONTRACTS`. The existing infrastructure cost-load queries format supplier-contract item costs and reconcile them against E1. This is a separate operational process; do not derive these rows from an approved rebate sell price unless an owner confirms that the request is intended to change the Tapestry item cost itself.

### Proposed Controlled Handoff

1. Start only from an `APPROVE` decision tied to the latest immutable evaluation revision. Include only valid requested lines; rejected, expired, superseded, unmatched, or UOM-invalid lines must not enter the output.
2. Recheck the approved source revision, exact requested price, item, PRCA/account mapping, UOM, approval, and effective dates immediately before creating the artifact. Any changed value requires a new evaluation and approval.
3. In the approval handoff, create one idempotent operation keyed by the approved evaluation/decision revision. It must carry the approved PRCA/category/tier plus only the validated requested lines and their approved requested prices. Re-read or validate the exact source and reference hashes before applying it.
4. In Snowflake, update the current attachment row and append one archive row per approved requested line from the same validated operation. Use a transaction for these DML actions where supported. If approval storage and Snowflake cannot participate in one transaction, use a durable outbox/dispatch state and reconciliation so a retry cannot duplicate archive rows or hide a partial failure. Do not mark the approval as applied until both attachment and archive writes are verified.
5. Generate the fixed-price load artifact separately using the confirmed E1 loader schema. Preserve request ID, line ID, evaluation ID/revision, approver, decision time, source/reference hashes, row count, and file hash. Include only approved, valid lines. File creation or email delivery is not proof that E1 applied the price; reconcile against loader acknowledgement or a defined post-load query.
6. Track `APPROVED_NOT_APPLIED`, `ATTACHMENT_AND_ARCHIVE_APPLIED`, `LOAD_READY`, `SUBMITTED`, and `CONFIRMED_LOADED` or `LOAD_FAILED` as distinct states. Make each operation idempotent; do not automatically resubmit an ambiguous or failed update/load before reconciliation.

### Item-Level Approval Archive And Access

Store one append-only record per approved request line, not a synthesized row for every catalog item in the category. At minimum, retain request/source line IDs and revision, evaluation/decision ID, PRCA and approved customer scope, category, item and description, requested and reference UOMs, requested/approved fixed sell price, approved tier, prior and new attached tier, the selected Tapestry item tier cost and cost UOM, effective dates, approver and decision timestamp, source/reference/rule versions or hashes, and attachment/archive/load outcome identifiers. Preserve the extracted cost/price values as approval-time snapshots; later reference changes must not rewrite the audit row.

**RLS is deferred for the initial sandbox workflow.** Build and validate the approval/archive lifecycle without implementing row-access policies. Do not expose the sandbox archive as a generally accessible representative-facing report. Before production or broader reporting access, define the identity-to-account mapping, row entitlements, cost/margin column visibility, policy owner, and any required secured views or masking. The local workspace contains no verified forced-sub row-access-policy implementation to copy.

The supplied Forced Subs workflow analysis is a useful **lifecycle and representative-status reference**: its status view unions active requests, loading rows, and archived outcomes so reps can see both work in progress and prior decisions. Apply that pattern with explicit EC request states and a secured status view over the approval/archive history. The Forced Subs analysis does not describe its RLS policy, so it is not evidence for the EC archive's authorization rules. The EC archive also has a different retention point: this workflow must preserve the approval-time item snapshot even while the corresponding attachment update and any fixed-price load are still pending.

### Current Integration Gap

The local `ec_workflow` foundation is not a production approval or load implementation. Its decision table records `approved_tier`, action, actor, reason, and decision time; request lines retain item, requested UOM, requested price, and validation errors. It does not yet persist the approved tier-attachment mutation, item-level approval-cost snapshot, fixed-price approval state, E1 account scope, effective dates, load batch/file identity, submission, or load confirmation. It uses local SQLite and cannot atomically commit an approval together with Snowflake attachment/archive writes. Its code and review runners do not generate or submit load files. Add a durable, idempotent handoff and these fields/transitions after the approval authority, price loader owner, target schema, customer scope, date rules, and acknowledgement mechanism are confirmed. RLS can be layered on later before production or representative-facing access.

## Raw Intake And PBI Read Views

Keep the Smartsheet ETL as an append-only data pull. It should not update header rows with processing status or approval outcomes. Store raw request headers and line values plus a separate raw attachment-metadata row per file, with source row/attachment identifiers, file name/type, content hash, and ingestion run ID/time. Preserve raw requested prices separately from any parsed numeric value so blanks or malformed values remain inspectable.

Retain header records even when required PRCA/account data or attachments are missing. If the header fails those intake prerequisites, do not insert its item rows into the eligible line table; retain the header and attachment metadata so the reason remains reportable. A view should derive PRCA presence, attachment presence/parsing status, header intake outcome, failure reason, and line counts from the raw tables. A second view should expose line details only for headers that pass the intake checks. Views are read models; do not repair or edit raw rows to change a status.

Record approvals and line-level outcomes in a separate append-only decision/archive source. The PBI approval view should join that source to the immutable request line and reference snapshot to show the approved tier, cost/UOM, exact requested/approved price/UOM, decision reason, approver, and timestamp. A missing approval record means pending review; it must not be inferred as approved from a header status.

### Confirmed Point-in-Time Validation Rules

- The request account must resolve to an active E1 customer of type `B`, `S`, or `X`. Record missing, invalid, and ambiguous E1-account outcomes separately.
- The resolved PRCA must be present in `PRD_SBX_MMS_DB.PRC.PEX_PRCA_VIEW`. Missing PEX mapping is a separate failure flag; retain the raw header either way.
- Item validation uses the current item reference and Tapestry item record. Record missing/ambiguous items, non-private-brand items, ineligible stock types, missing Tapestry records, and unavailable floor costs as distinct outcomes.
- Tapestry tier costs use `SELL_UOM`. When requested UOM is blank, use reference `SELL_UOM` as the effective UOM and set `UOM_ASSUMED = TRUE`. When a populated requested UOM differs, flag `UOM_MISMATCH` and do not compare price to cost as if the units matched.
- The price floor is the item’s current Tapestry `TIER_10` cost in `SELL_UOM`. Flag a numeric requested price at or below that floor as `AT_OR_BELOW_TIER_10_FLOOR`, including when the UOM was assumed. Preserve the raw price and separately classify blank, nonnumeric, and nonpositive values.
- Each audit result records the reference values used, `VALIDATED_AT`, `RULE_VERSION`, and a reference snapshot hash. Revalidation against changed reference values creates a new audit record; it does not rewrite the prior result.

The initial three-month sandbox run on October 26, 2026 captured 124 landing headers, 5,847 item lines, and 113 attachment records. Derived header validation found 92 ready for line validation, 15 missing attachments, 12 attachment parse failures, 3 missing PRCA values, and 2 invalid E1 account mappings. For the six specifically requested headers, all six had active E1 matches and PRCAs present in PEX. Their 174 lines included 134 blank prices, 16 nonpositive prices, 12 prices at or below the Tapestry Tier 10 floor, 2 populated UOM mismatches, and 10 lines above the floor; every target item matched the expected item/Tapestry reference checks. These are intake/audit flags, not approvals or load authorization.

## Daily Workflow

1. Pull and append raw request headers, attachment metadata, and extracted line values from the confirmed intake source (SharePoint and/or Smartsheet; source-of-truth handoff still needs confirmation). Keep status and approval state out of the raw intake writes.
2. Derive PRCA/attachment/intake status and failure reasons in SQL views. Retain failed headers; expose eligible item lines only when header prerequisites pass. Validate item number, UOM, price, and attachment/template integrity without rewriting the raw values.
3. Refresh only the two required cost/reference data inputs, using a current effective period instead of the hard-coded date and requested-item scope instead of a test item filter.
4. Enrich only the requested items with item facts, current prices/costs, applicable Tapestry contract costs, and tier options.
5. Calculate tier options, margins, and customer impact. Route missing data and rule exceptions for human review.
6. Produce a review package grouped by customer/category, with item-level details and approve/reject actions for requested sell prices. Any proposed tier adjustment is a separate cost-side decision, not permission to change the sell price.
7. After cost and final sell-price approval, generate the exact fixed-price load-ready rows for approved requested items. Use the confirmed delivery method; email is optional and remains undecided. Confirm any separate customer-tier or item-cost output independently. Do not perform the production load automatically unless separately authorized.
8. Record approval/override, approved cost and price values, reasons, email/load artifact, and final outcome in a separate append-only decision/archive source linked to immutable raw request rows.
9. Publish current intake status, line details, decisions, and outcomes through SQL reporting views for Power BI/current BI and future Looker, with an agreed refresh cadence and source links where appropriate. Derive intake failures from raw data and reconcile pending approvals, attachment/archive application, load-ready/submitted work, confirmed outcomes, and failures without inferring state from free-text notes.

## Audit and Rep Visibility

The audit history should retain header and line-item detail for **only the items requested**, together with the costs and determinations used. At minimum, capture:

- Request ID, source row/file identifiers, requester/representative, submitted and processed timestamps, and run ID.
- Customer/PRCA, category, requested item, description, requested UOM, and requested price.
- Reference UOM, current tier/current price, cost-source values and effective date, candidate tiers/costs/margins, selected recommendation, and calculated customer impact.
- Approved cost and cost UOM, final approved fixed sell price and price UOM, fixed-price effective dates, and approved customer/account scope. Link the fixed sell price to the unchanged requested price and its exact source revision; retain any rejection reason and subsequent request revisions.
- Validation/exceptions, approval status, approver, approval time, override tier/reason, and final disposition.
- Attachment/archive operation ID and outcome, exact load-file identifier/hash, optional email delivery status/recipients, and any downstream load confirmation or failure.

Representatives should see an understandable status and reason, not only raw internal status codes. The audit record should be the reporting source of truth or feed a clearly defined BI-neutral reporting model.

Use a BI-neutral reporting model over that history. Representatives should see their own requests and assigned accounts, including partial group outcomes, requested versus approved values, the rep-safe reason, decision/effective timestamps, current owner/next action, and reporting freshness. Enforce identity-based access outside dashboard filters and keep internal cost/margin fields and reviewer-only notes separately restricted.

For approved pricing, show the representative the final fixed sell price, item/UOM, effective dates, and whether that price has been confirmed loaded. Do not expose the internal approved cost merely because a fixed sell price was approved.

Distinguish approved, sent for loading, confirmed loaded, and load failed. Email delivery is not proof that an approved change is active. Define a loader acknowledgement or manual reconciliation owner before publishing a confirmed-loaded status.

## Operational Controls

- Run once daily using an approved scheduler/runtime and managed SharePoint/Snowflake access.
- Make processing idempotent so reruns do not create duplicate headers, line details, approvals, or load emails.
- Record run-level counts and failures; retry transient SharePoint/Snowflake errors safely; quarantine invalid files/rows without losing their audit trail.
- Keep a human approval gate before sending final load rows. Separate test and production data, destinations, recipients, and permissions.
- Store credentials in an approved secret store; never in notebooks, SQL, workbook cells, or email.

## Open Decisions

1. Confirm the intake source and how the October snapshot table relates to the Smartsheet header/line tables and SharePoint request site.
2. Confirm the tier-selection and customer-impact rules with business owners; the intake price-floor check is the current item Tapestry `TIER_10` cost in `SELL_UOM`.
3. Identify the exact customer/category aggregation and approval behavior, including item-level overrides.
4. Confirm the fixed-price loader and output schema, customer/account scope, UOM, effective dates, file type, and downstream acknowledgement. The sell price is the requested price, approved or rejected without price overrides. Email remains an optional delivery choice. Item/contract Tapestry cost-master changes remain separate from the approved customer-tier update and cost snapshot in the archive.
5. Confirm BI-neutral source views, current/future reporting adapters, refresh schedule, and source linking. Initial validation requires an active E1 `B`/`S`/`X` account and a PRCA present in `PEX_PRCA_VIEW`. Defer archive RLS and representative-facing security design until before production/reporting rollout.
6. Confirm the current/future cost effective-date selection logic and resolve the workbook `#REF!` references.
7. Provide a sanitized end-to-end example with input request, refreshed lookup data, expected recommendation/impact, approval, and expected load output.

## Reference Notes

See [EC_Request_Evaluation_Current_State.md](EC_Request_Evaluation_Current_State.md) for the current SQL, workbook, and Smartsheet notebook analysis. The described manual review is based on shared files and operator context; no production run has been verified.
