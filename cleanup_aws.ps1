# Cleanup for resources created by setup.ps1.
# Deletes in reverse dependency order: harness -> code interpreters -> memory -> role policy -> role.
# X-Ray Transaction Search / indexing-rule changes are account/region settings,
# not deletable resources, so they are left as-is.
#
# Usage:
#   .\cleanup_aws.ps1            # prompts before deleting
#   .\cleanup_aws.ps1 -Force     # deletes without prompting
param([switch]$Force)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# Run an aws command that is allowed to fail. ErrorActionPreference is relaxed
# locally so native-command stderr never raises a terminating NativeCommandError
# under Stop mode; the exit code is returned for the caller to inspect.
function Invoke-AwsSoft {
    param([Parameter(Mandatory)][string[]]$AwsArgs)
    $ErrorActionPreference = "Continue"
    $output = & aws @AwsArgs 2>&1
    $code = $LASTEXITCODE
    if ($output) { $output | ForEach-Object { Write-Host $_ } }
    return $code
}

# ---- 0. Shell environment (must match setup.ps1) ----
$env:AWS_REGION = "us-east-1"
$env:AWS_DEFAULT_REGION = "us-east-1"

$ACCOUNT_ID = (aws sts get-caller-identity --query Account --output text).Trim()
$ROLE_NAME = "AgentCoreHarnessLabRole"
$POLICY_NAME = "AgentCoreMemory"
$MEMORY_NAME = "support_agent_memory"
$HARNESS_NAME = "support_agent"

Write-Host "Account $ACCOUNT_ID / Region $env:AWS_REGION"

# ---- Discover resources ----
function Get-HarnessArn {
    $arn = ""
    try {
        $arn = (& aws bedrock-agentcore-control list-harnesses --query "harnesses[?harnessName=='$HARNESS_NAME'].arn | [0]" --output text 2>$null | Out-String).Trim()
        if ($LASTEXITCODE -ne 0) { return "None" }
    } catch { return "None" }
    if ([string]::IsNullOrWhiteSpace($arn)) { return "None" }
    return $arn
}

function Get-MemoryId {
    $id = ""
    try {
        $id = (& aws bedrock-agentcore-control list-memories --query "memories[?starts_with(id, '${MEMORY_NAME}-')].id | [0]" --output text 2>$null | Out-String).Trim()
        if ($LASTEXITCODE -ne 0) { return "None" }
    } catch { return "None" }
    if ([string]::IsNullOrWhiteSpace($id)) { return "None" }
    return $id
}

$harnessArn = Get-HarnessArn
$harnessId = if ($harnessArn -ne "None") { $harnessArn.Split('/')[-1] } else { "None" }
$memoryId = Get-MemoryId

# Custom code interpreters under AgentCore built-in tools. The AWS-managed
# default (id starts with "aws.") is not deletable and is skipped.
# NOTE: this targets EVERY custom interpreter in the account/region.
function Get-CodeInterpreterIds {
    try {
        $ids = (& aws bedrock-agentcore-control list-code-interpreters --query "codeInterpreterSummaries[?!starts_with(codeInterpreterId, 'aws.')].codeInterpreterId" --output text 2>$null | Out-String).Trim()
        if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($ids) -or $ids -eq "None") { return @() }
    } catch { return @() }
    return @($ids -split '\s+' | Where-Object { $_ })
}

# @() because a function returning an empty array yields $null, which has no .Count under strict mode.
$codeInterpreterIds = @(Get-CodeInterpreterIds)

$roleExists = $false
try {
    & aws iam get-role --role-name "$ROLE_NAME" 2>$null | Out-Null
    $roleExists = ($LASTEXITCODE -eq 0)
} catch { $roleExists = $false }

Write-Host ""
Write-Host "==== Resources targeted for deletion ===="
Write-Host "  AgentCore Harness : $(if ($harnessArn -ne 'None') { $harnessArn } else { '(none found)' })"
Write-Host "  Code interpreters : $(if ($codeInterpreterIds.Count) { $codeInterpreterIds -join ', ' } else { '(none found)' })"
Write-Host "  AgentCore Memory  : $(if ($memoryId -ne 'None') { $memoryId } else { '(none found)' })"
Write-Host "  IAM inline policy : $(if ($roleExists) { "$POLICY_NAME (on $ROLE_NAME)" } else { '(role not found)' })"
Write-Host "  IAM role          : $(if ($roleExists) { $ROLE_NAME } else { '(none found)' })"
Write-Host "========================================="
Write-Host ""

if (($harnessArn -eq "None") -and ($codeInterpreterIds.Count -eq 0) -and ($memoryId -eq "None") -and (-not $roleExists)) {
    Write-Host "Nothing to clean up."
    return
}

if (-not $Force) {
    $answer = Read-Host "Delete these resources? Type 'yes' to proceed"
    if ($answer -ne "yes") {
        Write-Host "Aborted. No resources were deleted."
        return
    }
}

# ---- 1. Delete the harness (depends on the memory resource) ----
if ($harnessId -ne "None") {
    if ((Invoke-AwsSoft -AwsArgs @('bedrock-agentcore-control','delete-harness','--harness-id',$harnessId)) -eq 0) {
        Write-Host "[DELETED] AgentCore Harness: $HARNESS_NAME ($harnessId)"
    } else {
        Write-Warning "Failed to delete harness $harnessId. Continuing."
    }
} else {
    Write-Host "[SKIP] No harness named $HARNESS_NAME found."
}

# ---- 2. Delete custom code interpreters ----
foreach ($ciId in $codeInterpreterIds) {
    if ((Invoke-AwsSoft -AwsArgs @('bedrock-agentcore-control','delete-code-interpreter','--code-interpreter-id',$ciId)) -eq 0) {
        Write-Host "[DELETED] Code interpreter: $ciId"
    } else {
        Write-Warning "Failed to delete code interpreter $ciId (active sessions?). Continuing."
    }
}
if ($codeInterpreterIds.Count -eq 0) {
    Write-Host "[SKIP] No custom code interpreters found."
}

# ---- 3. Delete the memory resource ----
if ($memoryId -ne "None") {
    if ((Invoke-AwsSoft -AwsArgs @('bedrock-agentcore-control','delete-memory','--memory-id',$memoryId)) -eq 0) {
        Write-Host "[DELETED] AgentCore Memory: $memoryId"
    } else {
        Write-Warning "Failed to delete memory $memoryId. Continuing."
    }
} else {
    Write-Host "[SKIP] No memory with prefix ${MEMORY_NAME}- found."
}

# ---- 4. Delete the inline role policy ----
if ($roleExists) {
    if ((Invoke-AwsSoft -AwsArgs @('iam','delete-role-policy','--role-name',$ROLE_NAME,'--policy-name',$POLICY_NAME)) -eq 0) {
        Write-Host "[DELETED] IAM inline policy: $POLICY_NAME (on $ROLE_NAME)"
    } else {
        Write-Warning "Could not delete inline policy $POLICY_NAME (may not exist). Continuing."
    }
}

# ---- 5. Delete the IAM role ----
# A role cannot be deleted while it still has attached managed policies or
# remaining inline policies, so detach/remove everything first.
if ($roleExists) {
    $attached = (& aws iam list-attached-role-policies --role-name $ROLE_NAME --query 'AttachedPolicies[].PolicyArn' --output text 2>$null | Out-String).Trim()
    if ($attached) {
        foreach ($policyArn in ($attached -split '\s+')) {
            if ($policyArn) {
                Invoke-AwsSoft -AwsArgs @('iam','detach-role-policy','--role-name',$ROLE_NAME,'--policy-arn',$policyArn) | Out-Null
                Write-Host "[DETACHED] Managed policy: $policyArn"
            }
        }
    }

    $inline = (& aws iam list-role-policies --role-name $ROLE_NAME --query 'PolicyNames' --output text 2>$null | Out-String).Trim()
    if ($inline) {
        foreach ($inlineName in ($inline -split '\s+')) {
            if ($inlineName) {
                Invoke-AwsSoft -AwsArgs @('iam','delete-role-policy','--role-name',$ROLE_NAME,'--policy-name',$inlineName) | Out-Null
                Write-Host "[DELETED] Remaining inline policy: $inlineName"
            }
        }
    }

    if ((Invoke-AwsSoft -AwsArgs @('iam','delete-role','--role-name',$ROLE_NAME)) -eq 0) {
        Write-Host "[DELETED] IAM role: $ROLE_NAME"
    } else {
        Write-Warning "Failed to delete role $ROLE_NAME (it may have instance profiles or other dependencies)."
    }
} else {
    Write-Host "[SKIP] IAM role $ROLE_NAME not found."
}

Write-Host ""
Write-Host "Cleanup complete."
Write-Host "Note: X-Ray Transaction Search and the Default indexing rule are"
Write-Host "account/region settings, not deletable resources; left unchanged."
Write-Host "Note: the AgentCoreControlPlane user policy from grant_user_permissions.ps1"
Write-Host "is not removed here. To remove it: aws iam delete-user-policy --user-name <you> --policy-name AgentCoreControlPlane"
