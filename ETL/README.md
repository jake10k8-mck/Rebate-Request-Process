# Smartsheet Rebate Intake

This package performs a raw, append-only pull from the four configured Smartsheet stages into its isolated sandbox tables:

- `PRD_SBX_MMS_DB.PRC.EC_REBATE_REQUEST_HEADER_ETL_TEST`
- `PRD_SBX_MMS_DB.PRC.EC_REBATE_REQUEST_LINES_ETL_TEST`
- `PRD_SBX_MMS_DB.PRC.EC_REBATE_REQUEST_ATTACHMENTS_ETL_TEST`

It never writes to standard ETL or production tables and does not update request status or approval fields. The default is a dry run. `--apply` is required to append all three tables in one transaction. Invalid price text is retained in `REQ_PRICE_RAW`; missing-PRCA headers are retained but their line rows are not extracted.

## Setup

1. Install the Automation dependencies from the parent directory:

   ```powershell
   python -m pip install -r requirements-workbooks.txt
   ```

2. Create or extend this package's isolated tables using [`sql/create_smartsheet_intake_test_tables.snowflake.sql`](sql/create_smartsheet_intake_test_tables.snowflake.sql). The script uses `CREATE ... LIKE` for header/line schemas and does not copy rows from or modify the older pipeline-test tables.
3. Create the append-only validation audit table using [`sql/create_validation_audit_table.snowflake.sql`](sql/create_validation_audit_table.snowflake.sql).
4. Create the read-only status and eligible-line views using [`sql/create_smartsheet_intake_views.snowflake.sql`](sql/create_smartsheet_intake_views.snowflake.sql).
5. Create a private `.env` beside `ec_smartsheet_intake.py` with `SMARTSHEET_API_TOKEN`, `SNOWFLAKE_USER`, and approved Snowflake connection settings such as `SNOWFLAKE_AUTHENTICATOR=externalbrowser`. Never commit or share the token. Git ignore does not prevent OneDrive synchronization.

## Target Specific Requests

Run from `Rebate Requests/Automation`. A targeted run scans all four stages by default and verifies the account number. Use `--stage` to scope the search when the request stage is known. Date filters still apply to targeted requests.

```powershell
python -m ETL.ec_smartsheet_intake `
  --request 'MBR-0011568=20046631' `
  --request 'MBR-0011567=20046631' `
  --output-dir "$env:LOCALAPPDATA\ECRebateWorkflow\intake_dry_run"
```

For requests known to be in the landing sheet, add `--stage landing` to avoid scanning other sheets:

```powershell
python -m ETL.ec_smartsheet_intake --stage landing --since 2026-07-26 --until 2026-10-27 --request 'MBR-0011568=20046631' --output-dir "$env:LOCALAPPDATA\ECRebateWorkflow\intake_dry_run"
```

Review `source_snapshot.json` and `manifest.json`. To append only to the separate raw ETL tables, choose a new output directory and add `--apply` only after the dry-run counts and source rows are verified. The older `EC_REBATE_REQUEST_*_PIPELINE_TEST` tables are not read or written by this package.

After a successful raw import, run [`sql/snapshot_smartsheet_validation.snowflake.sql`](sql/snapshot_smartsheet_validation.snowflake.sql) to append point-in-time customer, PRCA/PEX, item, UOM, and Tier 10 floor checks to the audit table. It uses a stable audit key to avoid repeating unchanged validations. Refresh the views by running their setup SQL again; they remain read-only projections over the raw and validation tables.

## Recent Requests

Without `--request`, the tool scans requests created within the last five days. Override the window with `--since YYYY-MM-DD --until YYYY-MM-DD`; the start is inclusive and the end exclusive. Supported attachment types are `.xlsx` and `.csv`, up to 20 MB each. Duplicate request rows/stages, duplicate attachment content, conflicting account pairs, missing attachments, or an existing changed request stop the run.

Headers and lines preserve the source fields and lineage. The attachment table captures one row per source attachment, including its ID, filename, type, content hash, and extraction result. The audit table stores point-in-time checks; the status view displays header-level E1/PRCA/PEX/attachment outcomes, and the line view shows item/Sell-UOM/Tier 10 floor results only for headers that pass. Full source headers and attachment details also remain in the private local `source_snapshot.json`.

Run offline parser tests from the parent Automation directory:

```powershell
python -m unittest discover -s ETL/tests -v
```
