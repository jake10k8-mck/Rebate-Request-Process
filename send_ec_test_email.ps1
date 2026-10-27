[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)][string]$PackagePath,
    [switch]$Draft,
    [switch]$Send
)

$ErrorActionPreference = 'Stop'
if ($Draft -and $Send) { throw 'Choose either -Draft or -Send, not both.' }
$packageFile = Get-Item -LiteralPath $PackagePath
$package = Get-Content -LiteralPath $packageFile.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
$recipient = 'jake.tenkate@mckesson.com'
$expected = @{
    'MBR-0011485' = @{ account = '90586438'; prca = '90586688'; category = 'DIAGNOSTIC' }
    'MBR-0011502' = @{ account = '62040942'; prca = '62040942'; category = 'NITRILE' }
}
if ($package.mode -cne 'EC_TWO_CASE_TEST' -or $package.recipient -cne $recipient -or @($package.cases).Count -ne 2) {
    throw 'Package must be the authorized two-request test addressed only to Jake.'
}
$seen = @{}
foreach ($case in $package.cases) {
    $requestId = [string]$case.request_id
    if (-not $expected.ContainsKey($requestId) -or $seen.ContainsKey($requestId)) {
        throw 'Unexpected or duplicate request in test package.'
    }
    $seen[$requestId] = $true
    $scope = $expected[$requestId]
    if ([string]$case.account -cne $scope.account -or [string]$case.prca -cne $scope.prca -or
        ([string]$case.category).ToUpperInvariant() -notlike ('*' + $scope.category + '*') -or
        [string]$case.analyst_tier -ne '7') {
        throw 'Account, resolved PRCA, category or analyst benchmark does not match the authorized test.'
    }
    foreach ($field in @('current_tier', 'suggested_tier')) {
        if ($null -ne $case.$field -and [string]$case.$field -ne '' -and
            [string]$case.$field -notmatch '^(10|[1-9])$') { throw 'Tier values must be 1-10 or null.' }
    }
    if ($case.status -notin @('CALCULATED_FOR_REVIEW', 'REVIEW_REQUIRED', 'INPUT_UNAVAILABLE')) {
        throw 'Each case must have an explicit review status.'
    }
    if ($null -ne $case.suggested_tier -and $case.status -ne 'CALCULATED_FOR_REVIEW') {
        throw 'An incomplete calculation cannot claim a suggested tier.'
    }
    foreach ($required in @('current_tier', 'suggested_tier', 'annualized_gp_change',
                           'annualized_internal_cost_change', 'annualized_customer_spend_change',
                           'rationale', 'exceptions')) {
        if ($case.PSObject.Properties.Name -notcontains $required) { throw "Missing case field: $required" }
    }
}

function Encode-Value($Value) {
    if ($null -eq $Value -or [string]$Value -eq '') { return 'Unavailable' }
    return [System.Net.WebUtility]::HtmlEncode([string]$Value)
}

$sections = foreach ($case in $package.cases) {
    $fields = [ordered]@{
        'Account / PRCA' = ([string]$case.account + ' / ' + [string]$case.prca)
        'Category' = $case.category
        'Current tier (now)' = $case.current_tier
        'Suggested tier (current-cost replay)' = $case.suggested_tier
        'Historical analyst benchmark' = 'Tier 7 (user-provided; not forced into calculations)'
        'Calculation status' = $case.status
        'Annualized GP change' = $case.annualized_gp_change
        'Annualized internal cost change' = $case.annualized_internal_cost_change
        'Annualized customer spend change' = $case.annualized_customer_spend_change
        'Rationale' = $case.rationale
        'Exceptions' = (@($case.exceptions) -join '; ')
    }
    $rows = foreach ($field in $fields.Keys) {
        '<tr><th>' + (Encode-Value $field) + '</th><td>' + (Encode-Value $fields[$field]) + '</td></tr>'
    }
    '<h2>' + (Encode-Value $case.request_id) + '</h2><table>' + ($rows -join '') + '</table>'
}
$body = '<html><head><meta charset="utf-8"><style>body{font-family:Calibri,Arial,sans-serif;color:#222}' +
    'table{border-collapse:collapse}th,td{padding:6px;border:1px solid #bbb;text-align:left;vertical-align:top}' +
    'th{background:#edf2f5}h2{font-size:17px}</style></head><body>' +
    '<h1>TEST: Analyst tier comparison</h1><p>Internal review only. Not an approval or pricing-load instruction.</p>' +
    ($sections -join '') + '<p>Unknown values are unavailable, not zero. Annualized values must be calculated ' +
    'from three-month activity times four and labeled as estimates. Suggested tiers require analyst review. ' +
    'Historical Tier 7 decisions are compared with current reference costs, not reconstructed historical costs. ' +
    'Projected opportunities must remain separate from existing-business impacts.</p></body></html>'
$previewPath = Join-Path $packageFile.DirectoryName 'email_preview.html'
if (-not $WhatIfPreference) {
    if (Test-Path -LiteralPath $previewPath) {
        if ([System.IO.File]::ReadAllText($previewPath) -cne $body) {
            throw 'Existing email preview differs; use a new package folder rather than overwriting it.'
        }
    } else {
        $stream = [System.IO.File]::Open($previewPath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write)
        try {
            $bytes = [System.Text.Encoding]::UTF8.GetBytes($body)
            $stream.Write($bytes, 0, $bytes.Length)
        } finally { $stream.Dispose() }
    }
}
if (-not $Draft -and -not $Send) {
    Write-Output "VALIDATED_PREVIEW_ONLY: $previewPath; no Outlook message created or sent."
    exit 0
}
$action = if ($Send) { 'Submit two-case test email through Outlook' } else { 'Save two-case Outlook draft' }
if (-not $PSCmdlet.ShouldProcess($recipient, $action)) { exit 0 }

$bytes = [System.IO.File]::ReadAllBytes($packageFile.FullName)
$sha = [System.Security.Cryptography.SHA256]::Create()
try { $packageHash = ([System.BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant() }
finally { $sha.Dispose() }
$receiptPath = Join-Path $packageFile.DirectoryName 'outlook_receipt.json'
$ledgerPath = Join-Path $PSScriptRoot 'Review Outputs\Email Tests\.send-ledger'
if ($Send) {
    [System.IO.Directory]::CreateDirectory($ledgerPath) | Out-Null
    $claimPath = Join-Path $ledgerPath ($packageHash + '.json')
    $claim = [System.IO.File]::Open($claimPath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write)
    $claim.Dispose()
}
$outlook = $null
$mail = $null
$status = 'FAILED_BEFORE_SUBMISSION'
try {
    $outlook = New-Object -ComObject Outlook.Application
    $mail = $outlook.CreateItem(0)
    $mail.To = $recipient
    $mail.CC = ''
    $mail.BCC = ''
    $mail.Subject = '[TEST - NO LOAD] MBR-0011485 Diagnostics / MBR-0011502 Nitrile'
    $mail.HTMLBody = $body
    if (-not $mail.Recipients.ResolveAll() -or $mail.Recipients.Count -ne 1) {
        throw 'Could not resolve the single authorized recipient.'
    }
    if ($Send) {
        $status = 'UNKNOWN_SUBMISSION_RESULT'
        $mail.Send()
        $status = 'SUBMITTED_TO_OUTLOOK_DELIVERY_NOT_CONFIRMED'
    } else {
        $mail.Save()
        $status = 'DRAFT_SAVED_NOT_SENT'
    }
    Write-Output $status
} finally {
    $receipt = [ordered]@{ package_sha256 = $packageHash; recipient = $recipient;
        status = $status; timestamp_utc = [DateTime]::UtcNow.ToString('o') }
    $encoded = $receipt | ConvertTo-Json
    [System.IO.File]::WriteAllText($receiptPath, $encoded)
    if ($Send) { [System.IO.File]::WriteAllText($claimPath, $encoded) }
    if ($mail) { [System.Runtime.InteropServices.Marshal]::ReleaseComObject($mail) | Out-Null }
    if ($outlook) { [System.Runtime.InteropServices.Marshal]::ReleaseComObject($outlook) | Out-Null }
}