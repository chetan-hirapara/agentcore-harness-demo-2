$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# One-time setup for Windows / PowerShell.
# Equivalent to the bash setup.sh script, tuned for local Windows development.

# On Windows the AWS CLI strips inline double quotes from JSON arguments, which
# corrupts them (e.g. {Probabilistic: {...}}). Write JSON to a temp file and
# pass it with file:// so the CLI receives valid JSON untouched.
function New-JsonArg {
    param([Parameter(Mandatory)][string]$Json)
    $path = [System.IO.Path]::Combine([System.IO.Path]::GetTempPath(), "agentcore-$([System.Guid]::NewGuid().ToString('N')).json")
    [System.IO.File]::WriteAllText($path, $Json, (New-Object System.Text.UTF8Encoding($false)))
    return "file://$path"
}

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

# ---- 0. Shell environment (the lab standardises on us-east-1) ----
$env:AWS_REGION = "us-east-1"
$env:AWS_DEFAULT_REGION = "us-east-1"

$ACCOUNT_ID = (aws sts get-caller-identity --query Account --output text).Trim()
$ROLE_NAME = "AgentCoreHarnessLabRole"
# Braces are required: "$ACCOUNT_ID:role" parses as a scope-qualified variable.
$ROLE_ARN = "arn:aws:iam::${ACCOUNT_ID}:role/$ROLE_NAME"
$MODEL_ID = "anthropic.claude-sonnet-4-6"

Write-Host "Account $ACCOUNT_ID / Region $env:AWS_REGION"
Write-Host "Using execution role: $ROLE_ARN"

# ---- 1. Transaction Search (once per account, PER REGION) ----
# Region-scoped -- worth checking if traces have ever come up empty for
# you before. 100% sampling: you cannot afford the hero trace being
# sampled out on recording day.
$traceDestination = ""
try {
    $traceDestination = (& aws xray get-trace-segment-destination 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { $traceDestination = "" }
} catch {
    $traceDestination = ""
}

if ($traceDestination -notmatch "CloudWatchLogs") {
    # Non-fatal: destination may already be PENDING from a prior run.
    if ((Invoke-AwsSoft -AwsArgs @('xray','update-trace-segment-destination','--destination','CloudWatchLogs')) -ne 0) {
        Write-Warning "Could not update trace segment destination (may already be PENDING/enabled). Continuing."
    }
}

$ruleArg = New-JsonArg -Json '{"Probabilistic": {"DesiredSamplingPercentage": 100}}'
# Non-fatal: some IAM users lack xray:UpdateIndexingRule.
if ((Invoke-AwsSoft -AwsArgs @('xray','update-indexing-rule','--name','Default','--rule',$ruleArg)) -ne 0) {
    Write-Warning "Could not update X-Ray indexing rule (missing xray:UpdateIndexingRule?). Continuing."
}
Write-Host "Transaction Search on. First spans take ~10 min to appear."

# ---- 2. Execution role ----
# Lab 01 Step 3 has the full baseline policy (bedrock:InvokeModel*,
# ECR public pull, X-Ray put, CloudWatch logs/metrics, workload
# identity, Browser + Code Interpreter). Code Interpreter is already in
# that baseline. If you use `agentcore create`, the CLI scaffolds the
# role for you.
#
# THE TRUST POLICY IS THE WHOLE BALLGAME. The service principal is
# bedrock-agentcore.amazonaws.com -- NOT bedrock.amazonaws.com, and not
# bedrock-agentcore-control.amazonaws.com (IAM rejects that one as an
# invalid principal, and it rejects the entire document with it, so a
# valid principal listed alongside it never gets tested). Get this wrong
# and CreateHarness fails with "Role validation failed ... verify that
# the role exists and its trust policy allows assumption by this
# service" -- which reads like the role is missing. It is not.
$roleExists = $false
try {
    & aws iam get-role --role-name "$ROLE_NAME" 2>$null | Out-Null
    $roleExists = ($LASTEXITCODE -eq 0)
} catch {
    $roleExists = $false
}

if (-not $roleExists) {
    $trustPolicy = @'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
    "Action": "sts:AssumeRole"}]
}
'@
    $trustArg = New-JsonArg -Json $trustPolicy
    aws iam create-role --role-name "$ROLE_NAME" --description "AgentCore harness execution role" --assume-role-policy-document "$trustArg" | Out-Null
    aws iam wait role-exists --role-name "$ROLE_NAME"
    Write-Host "[RESOURCE] IAM role created: $ROLE_NAME ($ROLE_ARN)"
    Write-Host "Created $ROLE_NAME. Attach the Lab 01 baseline policy to it."
}

# THIS DEMO ALSO NEEDS MEMORY. The Lab 01 baseline grants the event
# actions but NOT RetrieveMemoryRecords, which is what semantic recall
# uses -- without it Beat 5 goes quiet instead of failing loudly.
$memoryPolicyDoc = @"
{"Version": "2012-10-17",
 "Statement": [{
   "Sid": "AgentCoreMemory", "Effect": "Allow",
   "Action": ["bedrock-agentcore:CreateEvent",
              "bedrock-agentcore:GetEvent",
              "bedrock-agentcore:ListEvents",
              "bedrock-agentcore:RetrieveMemoryRecords"],
   "Resource": "arn:aws:bedrock-agentcore:${env:AWS_REGION}:${ACCOUNT_ID}:memory/*"}]}
"@

aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name AgentCoreMemory --policy-document "$(New-JsonArg -Json $memoryPolicyDoc)" | Out-Null
Write-Host "[RESOURCE] IAM inline role policy 'AgentCoreMemory' attached to role '$ROLE_NAME'"

# The harness calls the model as this role; the Sonnet profile is a global inference profile, so any region.
$bedrockPolicyDoc = @"
{"Version": "2012-10-17",
 "Statement": [{
   "Sid": "BedrockInvoke", "Effect": "Allow",
   "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
   "Resource": ["arn:aws:bedrock:*::foundation-model/anthropic.*",
                "arn:aws:bedrock:*:${ACCOUNT_ID}:inference-profile/*"]}]}
"@

aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name BedrockInvoke --policy-document "$(New-JsonArg -Json $bedrockPolicyDoc)" | Out-Null
Write-Host "[RESOURCE] IAM inline role policy 'BedrockInvoke' attached to role '$ROLE_NAME'"

# Without this the agent's first code_interpreter call fails and it falls back to the shell.
$ciPolicyDoc = @"
{"Version": "2012-10-17",
 "Statement": [{
   "Sid": "CodeInterpreter", "Effect": "Allow",
   "Action": ["bedrock-agentcore:CreateCodeInterpreter",
              "bedrock-agentcore:StartCodeInterpreterSession",
              "bedrock-agentcore:InvokeCodeInterpreter",
              "bedrock-agentcore:StopCodeInterpreterSession",
              "bedrock-agentcore:GetCodeInterpreter",
              "bedrock-agentcore:GetCodeInterpreterSession"],
   "Resource": "arn:aws:bedrock-agentcore:*:*:code-interpreter/*"}]}
"@

aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name CodeInterpreter --policy-document "$(New-JsonArg -Json $ciPolicyDoc)" | Out-Null
Write-Host "[RESOURCE] IAM inline role policy 'CodeInterpreter' attached to role '$ROLE_NAME'"

# Harness telemetry is delivered through this role; without it the Observability dashboard stays at zero.
$obsPolicyDoc = @"
{"Version": "2012-10-17",
 "Statement": [
  {"Sid": "XRay", "Effect": "Allow",
   "Action": ["xray:PutTraceSegments", "xray:PutTelemetryRecords",
              "xray:GetSamplingRules", "xray:GetSamplingTargets"],
   "Resource": "*"},
  {"Sid": "LogsDescribe", "Effect": "Allow",
   "Action": ["logs:DescribeLogGroups"],
   "Resource": "arn:aws:logs:*:${ACCOUNT_ID}:log-group:*"},
  {"Sid": "LogsWrite", "Effect": "Allow",
   "Action": ["logs:CreateLogGroup", "logs:DescribeLogStreams",
              "logs:CreateLogStream", "logs:PutLogEvents"],
   "Resource": ["arn:aws:logs:*:${ACCOUNT_ID}:log-group:/aws/bedrock-agentcore/runtimes/*",
                "arn:aws:logs:*:${ACCOUNT_ID}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*"]},
  {"Sid": "Metrics", "Effect": "Allow",
   "Action": "cloudwatch:PutMetricData", "Resource": "*",
   "Condition": {"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}}}]}
"@

aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name AgentCoreObservability --policy-document "$(New-JsonArg -Json $obsPolicyDoc)" | Out-Null
Write-Host "[RESOURCE] IAM inline role policy 'AgentCoreObservability' attached to role '$ROLE_NAME'"

# ---- 3. Model access agreement (once per account, per model) ----
aws bedrock get-foundation-model-availability --region "$env:AWS_REGION" --model-id "$MODEL_ID" --query agreementAvailability.status --output text
# If NOT_AVAILABLE:
#   $offerToken = aws bedrock list-foundation-model-agreement-offers --region "$env:AWS_REGION" --model-id "$MODEL_ID" --query 'offers[0].offerToken' --output text
#   aws bedrock create-foundation-model-agreement --region "$env:AWS_REGION" --model-id "$MODEL_ID" --offer-token "$offerToken"
# Wait for AVAILABLE plus 2-5 minutes of entitlement propagation.
# The aws-marketplace:ViewSubscriptions error almost always means the
# agreement, not IAM. Do not start editing policies.

# ---- 4. Create the AgentCore Memory resource ----
# AWS CLI 2.34+ models memory as a standalone resource that the harness
# references by ARN -- the older inline managedMemoryConfiguration is gone.
# list-memories exposes no name field, so match on the id prefix (AgentCore
# ids are "<name>-<suffix>").
$MEMORY_NAME = "support_agent_memory"
$MEMORY_ACTOR_ID = "support_user"

function Get-MemoryField {
    param([Parameter(Mandatory)][ValidateSet('arn','id','status')][string]$Field)
    $value = ""
    try {
        $value = (& aws bedrock-agentcore-control list-memories --query "memories[?starts_with(id, '${MEMORY_NAME}-')].$Field | [0]" --output text 2>$null | Out-String).Trim()
        if ($LASTEXITCODE -ne 0) { return "None" }
    } catch {
        return "None"
    }
    if ([string]::IsNullOrWhiteSpace($value)) { return "None" }
    return $value
}

$MEMORY_ARN = Get-MemoryField -Field arn
if ($MEMORY_ARN -eq "None") {
    $memoryInput = @"
{
  "name": "$MEMORY_NAME",
  "description": "Memory for the returns and refunds support agent.",
  "eventExpiryDuration": 30,
  "memoryExecutionRoleArn": "$ROLE_ARN",
  "memoryStrategies": [
    {"semanticMemoryStrategy": {"name": "semantic"}},
    {"summaryMemoryStrategy": {"name": "summary"}},
    {"userPreferenceMemoryStrategy": {"name": "userPreference"}}
  ]
}
"@
    # Fail loudly: the caller's own IAM identity needs control-plane permissions.
    $rc = Invoke-AwsSoft -AwsArgs @('bedrock-agentcore-control','create-memory','--cli-input-json',(New-JsonArg -Json $memoryInput))
    if ($rc -ne 0) {
        throw @"
create-memory failed (exit $rc). This usually means the IAM identity running
this script lacks AgentCore control-plane permissions -- NOT the execution role.
Grant your user/role these actions on this account, then rerun:
  bedrock-agentcore:CreateMemory, GetMemory, ListMemories, DeleteMemory,
  bedrock-agentcore:CreateHarness, GetHarness, ListHarnesses, DeleteHarness
Also needed for memory to assume the role: iam:PassRole on $ROLE_ARN.
An admin can attach a policy granting "bedrock-agentcore:*" (control plane) and
"iam:PassRole" for the role above. See grant_user_permissions.ps1 in this folder.
"@
    }
    Write-Host "Created memory $MEMORY_NAME. Waiting for it to become ACTIVE..."
}

$MEMORY_ID = Get-MemoryField -Field id
if ($MEMORY_ID -eq "None") { throw "Memory resource was not found after create." }
# Strategies extract via the LLM; the resource stays CREATING until ready.
$deadline = (Get-Date).AddMinutes(10)
do {
    $status = (& aws bedrock-agentcore-control get-memory --memory-id "$MEMORY_ID" --query 'memory.status' --output text 2>$null | Out-String).Trim()
    Write-Host "Memory status: $status"
    if ($status -eq "ACTIVE") { break }
    if ($status -eq "FAILED") { throw "Memory creation failed; check get-memory for failureReason." }
    Start-Sleep -Seconds 10
} while ((Get-Date) -lt $deadline)

if ($status -ne "ACTIVE") { throw "Memory did not reach ACTIVE within the timeout." }
$MEMORY_ARN = Get-MemoryField -Field arn
Write-Host "[RESOURCE] AgentCore Memory ready: id=$MEMORY_ID arn=$MEMORY_ARN"
Write-Host "Memory ARN=$MEMORY_ARN"

# ---- 5. Create the harness with memory + code interpreter ----
# Names reject hyphens: harness_demo, not harness-demo.
$HARNESS_NAME = "support_agent"

function Get-HarnessArn {
    $result = ""
    try {
        $result = (& aws bedrock-agentcore-control list-harnesses --query "harnesses[?harnessName=='$HARNESS_NAME'].arn | [0]" --output text 2>$null | Out-String).Trim()
        if ($LASTEXITCODE -ne 0) { return "None" }
    } catch {
        return "None"
    }
    if ([string]::IsNullOrWhiteSpace($result)) { return "None" }
    return $result
}

$HarnessArn = Get-HarnessArn
if ($HarnessArn -eq "None") {
    $systemPromptArg = New-JsonArg -Json '[{"text": "You are a returns and refunds support agent."}]'
    # New tools shape: code interpreter lives under config.agentCoreCodeInterpreter.
    # Empty config => the built-in Code Interpreter ARN is used.
    $toolsArg = New-JsonArg -Json '[{"type": "agentcore_code_interpreter", "name": "code_interpreter", "config": {"agentCoreCodeInterpreter": {}}}]'
    # New memory shape: reference the standalone Memory resource by ARN.
    $memoryArg = New-JsonArg -Json "{`"agentCoreMemoryConfiguration`": {`"arn`": `"$MEMORY_ARN`", `"actorId`": `"$MEMORY_ACTOR_ID`", `"messagesCount`": 20}}"
    aws bedrock-agentcore-control create-harness --harness-name "$HARNESS_NAME" --execution-role-arn "$ROLE_ARN" --system-prompt "$systemPromptArg" --tools "$toolsArg" --memory "$memoryArg" --max-iterations 25 --timeout-seconds 600 | Out-Null
    Write-Host "Created harness $HARNESS_NAME."
} else {
    Write-Host "Harness $HARNESS_NAME already exists; skipping create."
}

$HARNESS_ARN = Get-HarnessArn
if ($HARNESS_ARN -eq "None" -or [string]::IsNullOrWhiteSpace($HARNESS_ARN)) {
    throw "Harness creation did not return a valid ARN."
}
Write-Host "[RESOURCE] AgentCore Harness ready: name=$HARNESS_NAME arn=$HARNESS_ARN"

# Assert memory is actually wired. The demo's last beat depends on it, and a
# harness without it fails silently -- the agent just politely asks who you are.
$harnessId = $HARNESS_ARN.Split('/')[-1]
$memoryArnOnHarness = aws bedrock-agentcore-control get-harness --harness-id "$harnessId" --query 'harness.memory.agentCoreMemoryConfiguration.arn' --output text
Write-Host "Harness memory ARN: $memoryArnOnHarness"
Write-Host "HARNESS_ARN=$HARNESS_ARN"
$env:HARNESS_ARN = $HARNESS_ARN

# ---- 6. Verify before you build on it ----
# setup.ps1 exports into its own shell, not yours. Copy this line out, or
# demo_run.py runs against an empty HARNESS_ARN.
Write-Host ""
Write-Host "==== Resources created by this script (see cleanup_aws.ps1 to remove) ===="
Write-Host "  IAM role          : $ROLE_NAME"
Write-Host "  IAM inline policy : AgentCoreMemory (on $ROLE_NAME)"
Write-Host "  AgentCore Memory  : $MEMORY_ID"
Write-Host "  AgentCore Harness : $HARNESS_NAME ($HARNESS_ARN)"
Write-Host "========================================================================="
Write-Host ""
Write-Host "Next:"
Write-Host "  $env:HARNESS_ARN"
Write-Host "  python -m harness_demo.db              # seed the database (the demo also reseeds)"
Write-Host "  python -m pytest evals/test_invariants.py -v  # offline tests, no model calls"
Write-Host "  python demo_run.py                    # the recorded scenario (--pause to step)"
Write-Host "  agentcore traces list                 # confirm traces are flowing"
