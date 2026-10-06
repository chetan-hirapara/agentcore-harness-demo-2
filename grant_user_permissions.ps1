# Grants the CALLER's IAM user the AgentCore control-plane permissions that
# setup.ps1 needs (CreateMemory/CreateHarness/etc.) plus iam:PassRole on the
# execution role. These are permissions on YOUR identity -- separate from the
# execution role the agent assumes.
#
# Requires that whoever runs this already has iam:PutUserPolicy. If you don't,
# hand the printed policy to an admin to attach for you.
#
# Usage:
#   .\grant_user_permissions.ps1              # attach to the calling IAM user
#   .\grant_user_permissions.ps1 -UserName x  # attach to a specific IAM user

# param() must be the first statement in the script.
param([string]$UserName)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$env:AWS_REGION = "us-east-1"
$env:AWS_DEFAULT_REGION = "us-east-1"

$ACCOUNT_ID = (aws sts get-caller-identity --query Account --output text).Trim()
$ROLE_NAME = "AgentCoreHarnessLabRole"
# Braces are required: "$ACCOUNT_ID:role" parses as a scope-qualified variable.
$ROLE_ARN = "arn:aws:iam::${ACCOUNT_ID}:role/$ROLE_NAME"
$POLICY_NAME = "AgentCoreControlPlane"

# Derive the IAM user name from the caller identity when not supplied.
if ([string]::IsNullOrWhiteSpace($UserName)) {
    $callerArn = (aws sts get-caller-identity --query Arn --output text).Trim()
    if ($callerArn -match ":user/(.+)$") {
        $UserName = $Matches[1]
    } else {
        throw "Caller is not an IAM user ($callerArn). Re-run with -UserName <name>, or attach the policy to the appropriate principal manually."
    }
}

Write-Host "Account $ACCOUNT_ID / Target IAM user: $UserName"

# Function used by setup.ps1 too: write JSON to a temp file and pass via file://
# so the Windows AWS CLI does not strip the inline double quotes.
function New-JsonArg {
    param([Parameter(Mandatory)][string]$Json)
    $path = [System.IO.Path]::Combine([System.IO.Path]::GetTempPath(), "agentcore-$([System.Guid]::NewGuid().ToString('N')).json")
    [System.IO.File]::WriteAllText($path, $Json, (New-Object System.Text.UTF8Encoding($false)))
    return "file://$path"
}

# Broad on the control plane (lab convenience); PassRole scoped to the one role.
$policyDoc = @"
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AgentCoreControlPlane",
      "Effect": "Allow",
      "Action": ["bedrock-agentcore:*"],
      "Resource": "*"
    },
    {
      "Sid": "PassExecutionRole",
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": "$ROLE_ARN",
      "Condition": {
        "StringEquals": {"iam:PassedToService": "bedrock-agentcore.amazonaws.com"}
      }
    }
  ]
}
"@

Write-Host "Policy to attach ($POLICY_NAME):"
Write-Host $policyDoc

aws iam put-user-policy --user-name "$UserName" --policy-name "$POLICY_NAME" --policy-document "$(New-JsonArg -Json $policyDoc)"
Write-Host "[RESOURCE] IAM inline user policy '$POLICY_NAME' attached to user '$UserName'"
Write-Host "Wait ~10-30s for IAM propagation, then rerun: .\setup.ps1"
