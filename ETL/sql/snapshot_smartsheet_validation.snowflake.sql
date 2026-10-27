-- Point-in-time validation snapshot over immutable raw Smartsheet intake.
-- Requires create_validation_audit_table.snowflake.sql first.
-- Re-running unchanged source/reference values is idempotent by AUDIT_RECORD_KEY.

MERGE INTO PRD_SBX_MMS_DB.PRC.EC_REBATE_REQUEST_VALIDATION_AUDIT_ETL_TEST target
USING (
    WITH run_context AS (
        SELECT CURRENT_TIMESTAMP()::TIMESTAMP_NTZ AS VALIDATED_AT,
               'EC_REQUEST_VALIDATION_V1' AS RULE_VERSION
    ),
    raw_headers AS (
        SELECT header.*
        FROM PRD_SBX_MMS_DB.PRC.EC_REBATE_REQUEST_HEADER_ETL_TEST header
    ),
    raw_lines AS (
        SELECT line.*
        FROM PRD_SBX_MMS_DB.PRC.EC_REBATE_REQUEST_LINES_ETL_TEST line
    ),
    customer_candidates AS (
        SELECT DISTINCT
            customer.CUST_E1_NUM,
            customer.PRCA_NUM,
            customer.CUST_TYPE_CD,
            customer.ACTV_FLG
        FROM PRD_PL_MMS_EDW_DB.EDWRPT.V_DIM_CUST_CURR customer
        INNER JOIN (
            SELECT DISTINCT TRY_TO_NUMBER(CUST_NUM_PRCA) AS CUST_E1_NUM
            FROM raw_headers
            WHERE TRY_TO_NUMBER(CUST_NUM_PRCA) IS NOT NULL
        ) requested
            ON requested.CUST_E1_NUM = customer.CUST_E1_NUM
        WHERE customer.SYS_PLTFRM = 'E1'
    ),
    customer_refs AS (
        SELECT
            CUST_E1_NUM,
            COUNT(DISTINCT IFF(
                ACTV_FLG = 'Y' AND CUST_TYPE_CD IN ('B', 'S', 'X'), PRCA_NUM, NULL
            )) AS E1_PRCA_MATCH_COUNT,
            MIN(IFF(
                ACTV_FLG = 'Y' AND CUST_TYPE_CD IN ('B', 'S', 'X'), PRCA_NUM, NULL
            )) AS RESOLVED_PRCA
        FROM customer_candidates
        GROUP BY CUST_E1_NUM
    ),
    pex_refs AS (
        SELECT PRCA_NUM, COUNT(*) AS PEX_MATCH_COUNT,
               MIN(BUS_PLTFRM) AS PEX_BUS_PLTFRM
        FROM (
            SELECT DISTINCT PRCA_NUM, BUS_PLTFRM
            FROM PRD_SBX_MMS_DB.PRC.PEX_PRCA_VIEW
        ) pex
        GROUP BY PRCA_NUM
    ),
    attachment_refs AS (
        SELECT
            ROW_ID,
            INGESTION_RUN_ID,
            COUNT(*) AS ATTACHMENT_COUNT,
            COUNT_IF(LOWER(FILE_EXTENSION) IN ('.xlsx', '.csv')) AS SUPPORTED_ATTACHMENT_COUNT,
            COUNT_IF(DOWNLOAD_STATUS = 'FAILED') AS DOWNLOAD_FAILURE_COUNT,
            COUNT_IF(PARSE_STATUS IN ('FAILED', 'UNSUPPORTED', 'DUPLICATE_CONTENT')) AS PARSE_FAILURE_COUNT,
            SUM(COALESCE(PARSED_LINE_COUNT, 0)) AS PARSED_LINE_COUNT
        FROM PRD_SBX_MMS_DB.PRC.EC_REBATE_REQUEST_ATTACHMENTS_ETL_TEST
        GROUP BY ROW_ID, INGESTION_RUN_ID
    ),
    line_refs AS (
        SELECT ROW_ID, INGESTION_RUN_ID, COUNT(*) AS RAW_LINE_COUNT
        FROM raw_lines
        GROUP BY ROW_ID, INGESTION_RUN_ID
    ),
    header_facts AS (
        SELECT
            header.*,
            customer.E1_PRCA_MATCH_COUNT,
            customer.RESOLVED_PRCA,
            pex.PEX_MATCH_COUNT,
            pex.PEX_BUS_PLTFRM,
            COALESCE(attachment.ATTACHMENT_COUNT, 0) AS ATTACHMENT_RECORD_COUNT,
            COALESCE(attachment.SUPPORTED_ATTACHMENT_COUNT, 0) AS SUPPORTED_ATTACHMENT_COUNT,
            COALESCE(attachment.DOWNLOAD_FAILURE_COUNT, 0) AS DOWNLOAD_FAILURE_COUNT,
            COALESCE(attachment.PARSE_FAILURE_COUNT, 0) AS PARSE_FAILURE_COUNT,
            COALESCE(attachment.PARSED_LINE_COUNT, 0) AS ATTACHMENT_PARSED_LINE_COUNT,
            COALESCE(line.RAW_LINE_COUNT, 0) AS RAW_LINE_COUNT,
            CASE
                WHEN NULLIF(TRIM(header.CUST_NUM_PRCA), '') IS NULL THEN 'MISSING_E1_ACCOUNT'
                WHEN COALESCE(customer.E1_PRCA_MATCH_COUNT, 0) = 0 THEN 'INVALID_E1_ACCOUNT'
                WHEN customer.E1_PRCA_MATCH_COUNT > 1 THEN 'AMBIGUOUS_E1_ACCOUNT'
                ELSE 'VALID_E1_ACCOUNT'
            END AS E1_ACCOUNT_STATUS,
            CASE
                WHEN customer.E1_PRCA_MATCH_COUNT = 1 AND COALESCE(pex.PEX_MATCH_COUNT, 0) > 0
                    THEN 'VALID_PRCA_IN_PEX'
                WHEN customer.E1_PRCA_MATCH_COUNT = 1 THEN 'PRCA_NOT_FOUND_IN_PEX'
                ELSE 'NOT_CHECKED'
            END AS PEX_STATUS
        FROM raw_headers header
        LEFT JOIN customer_refs customer
            ON customer.CUST_E1_NUM = TRY_TO_NUMBER(header.CUST_NUM_PRCA)
        LEFT JOIN pex_refs pex
            ON pex.PRCA_NUM = customer.RESOLVED_PRCA
        LEFT JOIN attachment_refs attachment
            ON attachment.ROW_ID = header.ROW_ID
           AND attachment.INGESTION_RUN_ID = header.INGESTION_RUN_ID
        LEFT JOIN line_refs line
            ON line.ROW_ID = header.ROW_ID
           AND line.INGESTION_RUN_ID = header.INGESTION_RUN_ID
    ),
    header_results AS (
        SELECT
            SHA2_HEX(CONCAT_WS('|', header.INGESTION_RUN_ID, header.ROW_ID,
                'HEADER', context.RULE_VERSION, ref.REFERENCE_SNAPSHOT_HASH), 256) AS AUDIT_RECORD_KEY,
            SHA2_HEX(CONCAT_WS('|', header.INGESTION_RUN_ID,
                context.RULE_VERSION, ref.REFERENCE_SNAPSHOT_HASH), 256) AS VALIDATION_RUN_ID,
            header.INGESTION_RUN_ID,
            header.ROW_ID AS REQUEST_ID,
            'HEADER' AS RECORD_TYPE,
            NULL::VARCHAR AS SOURCE_LINE_KEY,
            NULL::VARCHAR AS SOURCE_ATTACHMENT_ID,
            NULL::NUMBER AS SOURCE_ROW_NUMBER,
            context.VALIDATED_AT,
            context.RULE_VERSION,
            header.CUST_NUM_PRCA::VARCHAR AS RAW_ACCOUNT_NUMBER,
            header.E1_ACCOUNT_STATUS,
            COALESCE(header.E1_PRCA_MATCH_COUNT, 0) AS E1_PRCA_MATCH_COUNT,
            header.RESOLVED_PRCA::VARCHAR AS RESOLVED_PRCA,
            header.PEX_STATUS,
            header.PEX_MATCH_COUNT,
            header.PEX_BUS_PLTFRM,
            NULL::VARCHAR AS ITEM_E1_NUM,
            NULL::VARCHAR AS ITEM_STATUS,
            NULL::NUMBER AS ITEM_MATCH_COUNT,
            NULL::VARCHAR AS ITEM_DSC,
            NULL::VARCHAR AS ITEM_PRIVATE_BRAND_FLAG,
            NULL::VARCHAR AS ITEM_STOCK_TYPE,
            NULL::VARCHAR AS GROUPED_LVL2,
            NULL::VARCHAR AS REQUESTED_UOM_RAW,
            NULL::VARCHAR AS REFERENCE_SELL_UOM,
            NULL::VARCHAR AS EFFECTIVE_UOM,
            NULL::BOOLEAN AS UOM_ASSUMED,
            NULL::VARCHAR AS UOM_STATUS,
            NULL::VARCHAR AS REQUESTED_PRICE_RAW,
            NULL::FLOAT AS REQUESTED_PRICE,
            NULL::FLOAT AS TIER_10_COST,
            NULL::VARCHAR AS PRICE_FLOOR_STATUS,
            CASE
                WHEN header.E1_ACCOUNT_STATUS = 'MISSING_E1_ACCOUNT' THEN 'FAILED_MISSING_E1_ACCOUNT'
                WHEN header.E1_ACCOUNT_STATUS = 'INVALID_E1_ACCOUNT' THEN 'FAILED_INVALID_E1_ACCOUNT'
                WHEN header.E1_ACCOUNT_STATUS = 'AMBIGUOUS_E1_ACCOUNT' THEN 'FAILED_AMBIGUOUS_E1_ACCOUNT'
                WHEN header.PEX_STATUS = 'PRCA_NOT_FOUND_IN_PEX' THEN 'FAILED_PRCA_NOT_IN_PEX'
                WHEN header.ATTACHMENT_RECORD_COUNT = 0 THEN 'FAILED_MISSING_ATTACHMENT'
                WHEN header.SUPPORTED_ATTACHMENT_COUNT = 0 THEN 'FAILED_UNSUPPORTED_ATTACHMENT'
                WHEN header.DOWNLOAD_FAILURE_COUNT > 0 THEN 'FAILED_ATTACHMENT_DOWNLOAD'
                WHEN header.PARSE_FAILURE_COUNT > 0 THEN 'FAILED_ATTACHMENT_PARSE'
                WHEN header.ATTACHMENT_PARSED_LINE_COUNT = 0 OR header.RAW_LINE_COUNT = 0
                    THEN 'FAILED_NO_LINE_ITEMS'
                ELSE 'READY_FOR_LINE_VALIDATION'
            END AS HEADER_INTAKE_STATUS,
            NULL::VARCHAR AS LINE_VALIDATION_STATUS,
            CASE
                WHEN header.E1_ACCOUNT_STATUS = 'MISSING_E1_ACCOUNT' THEN 'MISSING_E1_ACCOUNT'
                WHEN header.E1_ACCOUNT_STATUS = 'INVALID_E1_ACCOUNT' THEN 'INVALID_E1_ACCOUNT'
                WHEN header.E1_ACCOUNT_STATUS = 'AMBIGUOUS_E1_ACCOUNT' THEN 'AMBIGUOUS_E1_ACCOUNT'
                WHEN header.PEX_STATUS = 'PRCA_NOT_FOUND_IN_PEX' THEN 'PRCA_NOT_FOUND_IN_PEX'
                WHEN header.ATTACHMENT_RECORD_COUNT = 0 THEN 'MISSING_ATTACHMENT'
                WHEN header.SUPPORTED_ATTACHMENT_COUNT = 0 THEN 'UNSUPPORTED_ATTACHMENT'
                WHEN header.DOWNLOAD_FAILURE_COUNT > 0 THEN 'ATTACHMENT_DOWNLOAD_FAILED'
                WHEN header.PARSE_FAILURE_COUNT > 0 THEN 'ATTACHMENT_PARSE_FAILED'
                WHEN header.ATTACHMENT_PARSED_LINE_COUNT = 0 OR header.RAW_LINE_COUNT = 0
                    THEN 'NO_LINE_ITEMS_EXTRACTED'
                ELSE NULL
            END AS FAILURE_REASON,
            ref.REFERENCE_SNAPSHOT_HASH
        FROM header_facts header
        CROSS JOIN run_context context
        CROSS JOIN LATERAL (
            SELECT SHA2_HEX(TO_JSON(OBJECT_CONSTRUCT_KEEP_NULL(
                'RAW_ACCOUNT', header.CUST_NUM_PRCA,
                'E1_STATUS', header.E1_ACCOUNT_STATUS,
                'E1_MATCH_COUNT', header.E1_PRCA_MATCH_COUNT,
                'RESOLVED_PRCA', header.RESOLVED_PRCA,
                'PEX_STATUS', header.PEX_STATUS,
                'PEX_PLATFORM', header.PEX_BUS_PLTFRM,
                'ATTACHMENT_COUNT', header.ATTACHMENT_RECORD_COUNT,
                'SUPPORTED_ATTACHMENT_COUNT', header.SUPPORTED_ATTACHMENT_COUNT,
                'DOWNLOAD_FAILURE_COUNT', header.DOWNLOAD_FAILURE_COUNT,
                'PARSE_FAILURE_COUNT', header.PARSE_FAILURE_COUNT,
                'LINE_COUNT', header.RAW_LINE_COUNT,
                'RULE_VERSION', context.RULE_VERSION
            )), 256) AS REFERENCE_SNAPSHOT_HASH
        ) ref
    ),
    item_source AS (
        SELECT DISTINCT
            ITEM_E1_NUM, ITEM_DSC, SELL_UOM, PRVT_BRND_FLG, STOCK_TYPE_CD
        FROM PRD_PL_MMS_EDW_DB.EDWRPT.V_DIM_ITEM_E1_CURR
        WHERE ITEM_E1_NUM IN (
            SELECT DISTINCT TRY_TO_NUMBER(ITEM_E1_NUM)
            FROM raw_lines WHERE TRY_TO_NUMBER(ITEM_E1_NUM) IS NOT NULL
        )
    ),
    item_refs AS (
        SELECT
            ITEM_E1_NUM,
            COUNT(*) AS ITEM_MATCH_COUNT,
            MIN(ITEM_DSC) AS ITEM_DSC,
            MIN(SELL_UOM) AS SELL_UOM,
            MIN(PRVT_BRND_FLG) AS PRVT_BRND_FLG,
            MIN(STOCK_TYPE_CD) AS STOCK_TYPE_CD
        FROM item_source
        GROUP BY ITEM_E1_NUM
    ),
    tapestry_source AS (
        SELECT DISTINCT ITEM_E1_NUM, GROUPED_LVL2, TIER_10
        FROM PRD_SBX_MMS_DB.VALUEPLUS.MCKB_COST_TAPESTRY
        WHERE ITEM_E1_NUM IN (
            SELECT DISTINCT TRY_TO_NUMBER(ITEM_E1_NUM)
            FROM raw_lines WHERE TRY_TO_NUMBER(ITEM_E1_NUM) IS NOT NULL
        )
    ),
    tapestry_refs AS (
        SELECT ITEM_E1_NUM, COUNT(*) AS TAPESTRY_MATCH_COUNT,
               MIN(GROUPED_LVL2) AS GROUPED_LVL2,
               MIN(TIER_10) AS TIER_10_COST
        FROM tapestry_source
        GROUP BY ITEM_E1_NUM
    ),
    line_facts AS (
        SELECT
            line.*,
            header.CUST_NUM_PRCA AS RAW_ACCOUNT_NUMBER,
            header.E1_ACCOUNT_STATUS,
            header.E1_PRCA_MATCH_COUNT,
            header.RESOLVED_PRCA,
            header.PEX_STATUS,
            header.PEX_MATCH_COUNT,
            header.PEX_BUS_PLTFRM,
            CASE
                WHEN header.E1_ACCOUNT_STATUS = 'MISSING_E1_ACCOUNT' THEN 'FAILED_MISSING_E1_ACCOUNT'
                WHEN header.E1_ACCOUNT_STATUS = 'INVALID_E1_ACCOUNT' THEN 'FAILED_INVALID_E1_ACCOUNT'
                WHEN header.E1_ACCOUNT_STATUS = 'AMBIGUOUS_E1_ACCOUNT' THEN 'FAILED_AMBIGUOUS_E1_ACCOUNT'
                WHEN header.PEX_STATUS = 'PRCA_NOT_FOUND_IN_PEX' THEN 'FAILED_PRCA_NOT_IN_PEX'
                WHEN header.ATTACHMENT_RECORD_COUNT = 0 THEN 'FAILED_MISSING_ATTACHMENT'
                WHEN header.SUPPORTED_ATTACHMENT_COUNT = 0 THEN 'FAILED_UNSUPPORTED_ATTACHMENT'
                WHEN header.DOWNLOAD_FAILURE_COUNT > 0 THEN 'FAILED_ATTACHMENT_DOWNLOAD'
                WHEN header.PARSE_FAILURE_COUNT > 0 THEN 'FAILED_ATTACHMENT_PARSE'
                WHEN header.ATTACHMENT_PARSED_LINE_COUNT = 0 OR header.RAW_LINE_COUNT = 0
                    THEN 'FAILED_NO_LINE_ITEMS'
                ELSE 'READY_FOR_LINE_VALIDATION'
            END AS HEADER_INTAKE_STATUS,
            item.ITEM_MATCH_COUNT,
            item.ITEM_DSC AS REFERENCE_ITEM_DSC,
            item.SELL_UOM AS REFERENCE_SELL_UOM,
            item.PRVT_BRND_FLG,
            item.STOCK_TYPE_CD,
            tapestry.TAPESTRY_MATCH_COUNT,
            tapestry.GROUPED_LVL2,
            tapestry.TIER_10_COST
        FROM raw_lines line
        INNER JOIN header_facts header
            ON header.ROW_ID = line.ROW_ID
           AND header.INGESTION_RUN_ID = line.INGESTION_RUN_ID
        LEFT JOIN item_refs item
            ON item.ITEM_E1_NUM = TRY_TO_NUMBER(line.ITEM_E1_NUM)
        LEFT JOIN tapestry_refs tapestry
            ON tapestry.ITEM_E1_NUM = TRY_TO_NUMBER(line.ITEM_E1_NUM)
    ),
    line_evaluated AS (
        SELECT
            facts.*,
            TRY_TO_DOUBLE(facts.REQ_PRICE_RAW) AS PARSED_REQUESTED_PRICE,
            CASE
                WHEN facts.ITEM_MATCH_COUNT IS NULL THEN 'ITEM_NOT_FOUND'
                WHEN facts.ITEM_MATCH_COUNT > 1 THEN 'ITEM_AMBIGUOUS'
                WHEN facts.PRVT_BRND_FLG <> 'Y' THEN 'NOT_PRIVATE_BRAND'
                WHEN facts.STOCK_TYPE_CD = 'O' THEN 'INELIGIBLE_STOCK_TYPE'
                WHEN facts.TAPESTRY_MATCH_COUNT IS NULL THEN 'ITEM_NOT_IN_TAPESTRY'
                WHEN facts.TAPESTRY_MATCH_COUNT > 1 THEN 'TAPESTRY_ITEM_AMBIGUOUS'
                WHEN facts.TIER_10_COST IS NULL THEN 'TIER_10_FLOOR_MISSING'
                ELSE 'ITEM_VALID'
            END AS ITEM_STATUS,
            IFF(NULLIF(TRIM(facts.REQ_UOM), '') IS NULL, TRUE, FALSE) AS UOM_ASSUMED,
            CASE
                WHEN facts.ITEM_MATCH_COUNT IS NULL OR facts.ITEM_MATCH_COUNT <> 1
                    THEN 'REFERENCE_ITEM_UNAVAILABLE'
                WHEN NULLIF(TRIM(facts.REQ_UOM), '') IS NULL AND NULLIF(TRIM(facts.REFERENCE_SELL_UOM), '') IS NULL
                    THEN 'REFERENCE_SELL_UOM_MISSING'
                WHEN NULLIF(TRIM(facts.REQ_UOM), '') IS NULL THEN 'ASSUMED_SELL_UOM'
                WHEN UPPER(TRIM(facts.REQ_UOM)) = UPPER(TRIM(facts.REFERENCE_SELL_UOM))
                    THEN 'MATCH'
                ELSE 'MISMATCH'
            END AS UOM_STATUS,
            CASE
                WHEN NULLIF(TRIM(facts.REQ_UOM), '') IS NULL THEN facts.REFERENCE_SELL_UOM
                ELSE facts.REQ_UOM
            END AS EFFECTIVE_UOM
        FROM line_facts facts
    ),
    line_results AS (
        SELECT
            SHA2_HEX(CONCAT_WS('|', line.INGESTION_RUN_ID, line.ROW_ID,
                'LINE', line.SOURCE_ATTACHMENT_ID, line.SOURCE_SHEET_NAME,
                TO_VARCHAR(line.SOURCE_ROW_NUMBER), context.RULE_VERSION,
                ref.REFERENCE_SNAPSHOT_HASH), 256) AS AUDIT_RECORD_KEY,
            SHA2_HEX(CONCAT_WS('|', line.INGESTION_RUN_ID,
                context.RULE_VERSION, ref.REFERENCE_SNAPSHOT_HASH), 256) AS VALIDATION_RUN_ID,
            line.INGESTION_RUN_ID,
            line.ROW_ID AS REQUEST_ID,
            'LINE' AS RECORD_TYPE,
            SHA2_HEX(CONCAT_WS('|', line.ROW_ID, line.INGESTION_RUN_ID,
                line.SOURCE_ATTACHMENT_ID, line.SOURCE_SHEET_NAME,
                TO_VARCHAR(line.SOURCE_ROW_NUMBER)), 256) AS SOURCE_LINE_KEY,
            line.SOURCE_ATTACHMENT_ID,
            line.SOURCE_ROW_NUMBER,
            context.VALIDATED_AT,
            context.RULE_VERSION,
            line.RAW_ACCOUNT_NUMBER::VARCHAR AS RAW_ACCOUNT_NUMBER,
            line.E1_ACCOUNT_STATUS,
            COALESCE(line.E1_PRCA_MATCH_COUNT, 0) AS E1_PRCA_MATCH_COUNT,
            line.RESOLVED_PRCA::VARCHAR AS RESOLVED_PRCA,
            line.PEX_STATUS,
            line.PEX_MATCH_COUNT,
            line.PEX_BUS_PLTFRM,
            line.ITEM_E1_NUM::VARCHAR AS ITEM_E1_NUM,
            line.ITEM_STATUS,
            COALESCE(line.ITEM_MATCH_COUNT, 0) AS ITEM_MATCH_COUNT,
            COALESCE(line.REFERENCE_ITEM_DSC, line.ITEM_DSC) AS ITEM_DSC,
            line.PRVT_BRND_FLG::VARCHAR AS ITEM_PRIVATE_BRAND_FLAG,
            line.STOCK_TYPE_CD::VARCHAR AS ITEM_STOCK_TYPE,
            line.GROUPED_LVL2,
            line.REQ_UOM AS REQUESTED_UOM_RAW,
            line.REFERENCE_SELL_UOM,
            line.EFFECTIVE_UOM,
            line.UOM_ASSUMED,
            line.UOM_STATUS,
            line.REQ_PRICE_RAW AS REQUESTED_PRICE_RAW,
            line.PARSED_REQUESTED_PRICE AS REQUESTED_PRICE,
            line.TIER_10_COST,
            CASE
                WHEN line.PARSED_REQUESTED_PRICE IS NULL THEN 'MISSING_OR_INVALID_PRICE'
                WHEN line.PARSED_REQUESTED_PRICE <= 0 THEN 'NONPOSITIVE_PRICE'
                WHEN line.UOM_STATUS IN ('MISMATCH', 'REFERENCE_SELL_UOM_MISSING', 'REFERENCE_ITEM_UNAVAILABLE')
                    THEN 'NOT_COMPARABLE_UOM'
                WHEN line.TIER_10_COST IS NULL THEN 'FLOOR_UNAVAILABLE'
                WHEN line.PARSED_REQUESTED_PRICE <= line.TIER_10_COST THEN 'AT_OR_BELOW_TIER_10_FLOOR'
                ELSE 'ABOVE_TIER_10_FLOOR'
            END AS PRICE_FLOOR_STATUS,
            line.HEADER_INTAKE_STATUS,
            CASE
                WHEN line.HEADER_INTAKE_STATUS <> 'READY_FOR_LINE_VALIDATION' THEN 'BLOCKED_HEADER'
                WHEN line.ITEM_STATUS <> 'ITEM_VALID' THEN line.ITEM_STATUS
                WHEN line.UOM_STATUS = 'MISMATCH' THEN 'UOM_MISMATCH'
                WHEN line.UOM_STATUS = 'REFERENCE_SELL_UOM_MISSING' THEN 'MISSING_REFERENCE_SELL_UOM'
                WHEN line.PARSED_REQUESTED_PRICE IS NULL THEN 'MISSING_OR_INVALID_PRICE'
                WHEN line.PARSED_REQUESTED_PRICE <= 0 THEN 'NONPOSITIVE_PRICE'
                WHEN line.TIER_10_COST IS NULL THEN 'FLOOR_UNAVAILABLE'
                WHEN line.PARSED_REQUESTED_PRICE <= line.TIER_10_COST THEN 'REVIEW_AT_OR_BELOW_TIER_10_FLOOR'
                ELSE 'VALID_FOR_REVIEW'
            END AS LINE_VALIDATION_STATUS,
            CASE
                WHEN line.HEADER_INTAKE_STATUS <> 'READY_FOR_LINE_VALIDATION' THEN line.HEADER_INTAKE_STATUS
                WHEN line.ITEM_STATUS <> 'ITEM_VALID' THEN line.ITEM_STATUS
                WHEN line.UOM_STATUS = 'MISMATCH' THEN 'REQUESTED_UOM_DIFFERS_FROM_SELL_UOM'
                WHEN line.PARSED_REQUESTED_PRICE IS NULL THEN 'MISSING_OR_INVALID_PRICE'
                WHEN line.PARSED_REQUESTED_PRICE <= 0 THEN 'NONPOSITIVE_PRICE'
                WHEN line.TIER_10_COST IS NULL THEN 'TIER_10_FLOOR_UNAVAILABLE'
                WHEN line.PARSED_REQUESTED_PRICE <= line.TIER_10_COST THEN 'PRICE_AT_OR_BELOW_TIER_10_FLOOR'
                ELSE NULL
            END AS FAILURE_REASON,
            ref.REFERENCE_SNAPSHOT_HASH
        FROM line_evaluated line
        CROSS JOIN run_context context
        CROSS JOIN LATERAL (
            SELECT SHA2_HEX(TO_JSON(OBJECT_CONSTRUCT_KEEP_NULL(
                'RAW_ACCOUNT', line.RAW_ACCOUNT_NUMBER,
                'E1_STATUS', line.E1_ACCOUNT_STATUS,
                'RESOLVED_PRCA', line.RESOLVED_PRCA,
                'PEX_STATUS', line.PEX_STATUS,
                'PEX_PLATFORM', line.PEX_BUS_PLTFRM,
                'ITEM_MATCH_COUNT', line.ITEM_MATCH_COUNT,
                'SELL_UOM', line.REFERENCE_SELL_UOM,
                'PRIVATE_BRAND', line.PRVT_BRND_FLG,
                'STOCK_TYPE', line.STOCK_TYPE_CD,
                'GROUPED_LVL2', line.GROUPED_LVL2,
                'TIER_10_COST', line.TIER_10_COST,
                'REQUESTED_UOM', line.REQ_UOM,
                'REQUESTED_PRICE_RAW', line.REQ_PRICE_RAW,
                'RULE_VERSION', context.RULE_VERSION
            )), 256) AS REFERENCE_SNAPSHOT_HASH
        ) ref
    ),
    results AS (
        SELECT * FROM header_results
        UNION ALL
        SELECT * FROM line_results
    )
    SELECT * FROM results
) source
ON target.AUDIT_RECORD_KEY = source.AUDIT_RECORD_KEY
WHEN NOT MATCHED THEN INSERT (
    AUDIT_RECORD_KEY, VALIDATION_RUN_ID, INGESTION_RUN_ID, REQUEST_ID,
    RECORD_TYPE, SOURCE_LINE_KEY, SOURCE_ATTACHMENT_ID, SOURCE_ROW_NUMBER,
    VALIDATED_AT, RULE_VERSION, RAW_ACCOUNT_NUMBER, E1_ACCOUNT_STATUS,
    E1_PRCA_MATCH_COUNT, RESOLVED_PRCA, PEX_STATUS, PEX_MATCH_COUNT,
    PEX_BUS_PLTFRM, ITEM_E1_NUM, ITEM_STATUS, ITEM_MATCH_COUNT, ITEM_DSC,
    ITEM_PRIVATE_BRAND_FLAG, ITEM_STOCK_TYPE, GROUPED_LVL2,
    REQUESTED_UOM_RAW, REFERENCE_SELL_UOM, EFFECTIVE_UOM, UOM_ASSUMED,
    UOM_STATUS, REQUESTED_PRICE_RAW, REQUESTED_PRICE, TIER_10_COST,
    PRICE_FLOOR_STATUS, HEADER_INTAKE_STATUS, LINE_VALIDATION_STATUS,
    FAILURE_REASON, REFERENCE_SNAPSHOT_HASH
) VALUES (
    source.AUDIT_RECORD_KEY, source.VALIDATION_RUN_ID, source.INGESTION_RUN_ID,
    source.REQUEST_ID, source.RECORD_TYPE, source.SOURCE_LINE_KEY,
    source.SOURCE_ATTACHMENT_ID, source.SOURCE_ROW_NUMBER, source.VALIDATED_AT,
    source.RULE_VERSION, source.RAW_ACCOUNT_NUMBER, source.E1_ACCOUNT_STATUS,
    source.E1_PRCA_MATCH_COUNT, source.RESOLVED_PRCA, source.PEX_STATUS,
    source.PEX_MATCH_COUNT, source.PEX_BUS_PLTFRM, source.ITEM_E1_NUM,
    source.ITEM_STATUS, source.ITEM_MATCH_COUNT, source.ITEM_DSC,
    source.ITEM_PRIVATE_BRAND_FLAG, source.ITEM_STOCK_TYPE, source.GROUPED_LVL2,
    source.REQUESTED_UOM_RAW, source.REFERENCE_SELL_UOM, source.EFFECTIVE_UOM,
    source.UOM_ASSUMED, source.UOM_STATUS, source.REQUESTED_PRICE_RAW,
    source.REQUESTED_PRICE, source.TIER_10_COST, source.PRICE_FLOOR_STATUS,
    source.HEADER_INTAKE_STATUS, source.LINE_VALIDATION_STATUS,
    source.FAILURE_REASON, source.REFERENCE_SNAPSHOT_HASH
);
