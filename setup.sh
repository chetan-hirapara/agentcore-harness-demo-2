#!/usr/bin/env bash
# One-time setup. Condensed from agentcore-harness-lab Labs 01, 02, 06, 08.
# Run line by line the first time -- several steps have propagation delays.
set -euo pipefail

# ---- 0. Shell environment (the lab standardises on us-east-1) ----
export AWS_REGION=us-east-1
export AWS_DEFAULT_REGION=us-east-1
export ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
# Use AgentCoreHarnessLabRole with proper trust policy
export ROLE_NAME=AgentCoreHarnessLabRole
export ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"
export MODEL_ID=anthropic.claude-sonnet-4-6
echo "Account ${ACCOUNT_ID} / Region ${AWS_REGION}"
echo "Using execution role: $ROLE_ARN"

# ---- 1. Transaction Search (once per account, PER REGION) ----
# Region-scoped -- worth checking if traces have ever come up empty for
# you before. 100% sampling: you cannot afford the hero trace being
# sampled out on recording day.
# add if already cloud watch logs destination exists, simply skip the update command and move on to the next step
if ! aws xray get-trace-segment-destination | grep -q CloudWatchLogs; then
  aws xray update-trace-segment-destination --destination CloudWatchLogs
fi
aws xray update-indexing-rule --name "Default" \
  --rule '{"Probabilistic": {"DesiredSamplingPercentage": 100}}'
echo "Transaction Search on. First spans take ~10 min to appear."

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
if ! aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  aws iam create-role --role-name "$ROLE_NAME" \
    --description "AgentCore harness execution role" \
    --assume-role-policy-document '{
      "Version": "2012-10-17",
      "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
        "Action": "sts:AssumeRole"}]}' >/dev/null
  echo "Created ${ROLE_NAME}. Attach the Lab 01 baseline policy to it."
fi

# THIS DEMO ALSO NEEDS MEMORY. The Lab 01 baseline grants the event
# actions but NOT RetrieveMemoryRecords, which is what semantic recall
# uses -- without it Beat 5 goes quiet instead of failing loudly.
aws iam put-role-policy --role-name "$ROLE_NAME" \
  --policy-name AgentCoreMemory \
  --policy-document "$(cat <<EOF
{"Version": "2012-10-17",
 "Statement": [{
   "Sid": "AgentCoreMemory", "Effect": "Allow",
   "Action": ["bedrock-agentcore:CreateEvent",
              "bedrock-agentcore:GetEvent",
              "bedrock-agentcore:ListEvents",
              "bedrock-agentcore:RetrieveMemoryRecords"],
   "Resource": "arn:aws:bedrock-agentcore:${AWS_REGION}:${ACCOUNT_ID}:memory/*"}]}
EOF
)"

# ---- 3. Model access agreement (once per account, per model) ----
aws bedrock get-foundation-model-availability \
  --region "$AWS_REGION" --model-id "$MODEL_ID" \
  --query agreementAvailability.status --output text
# If NOT_AVAILABLE:
#   OFFER_TOKEN="$(aws bedrock list-foundation-model-agreement-offers \
#     --region "$AWS_REGION" --model-id "$MODEL_ID" \
#     --query 'offers[0].offerToken' --output text)"
#   aws bedrock create-foundation-model-agreement \
#     --region "$AWS_REGION" --model-id "$MODEL_ID" --offer-token "$OFFER_TOKEN"
# Wait for AVAILABLE **plus 2-5 minutes** of entitlement propagation.
# The aws-marketplace:ViewSubscriptions error almost always means the
# agreement, not IAM. Do not start editing policies.

# ---- 4. Create the harness with memory + code interpreter ----
# Names reject hyphens: harness_demo, not harness-demo.
export HARNESS_NAME=support_agent

# The list-harnesses field is `arn`, NOT `harnessArn`. A JMESPath miss
# returns None rather than erroring, so the wrong key silently looks
# exactly like "the harness was never created" -- and any fallback then
# hands the demo some other account harness that has no memory
# configured, which shows up three beats later as an agent that cannot
# remember. Never fall back to harnesses[0]; a wrong harness is worse
# than no harness.
harness_arn() {
  aws bedrock-agentcore-control list-harnesses \
    --query "harnesses[?harnessName=='${HARNESS_NAME}'].arn | [0]" \
    --output text
}

if [ "$(harness_arn)" == "None" ]; then
  aws bedrock-agentcore-control create-harness \
    --harness-name "$HARNESS_NAME" \
    --execution-role-arn "$ROLE_ARN" \
    --system-prompt '[{"text": "You are a returns and refunds support agent."}]' \
    --tools '[{"type": "agentcore_code_interpreter", "name": "code_interpreter"}]' \
    --memory '{"managedMemoryConfiguration": {"strategies": ["SEMANTIC", "SUMMARIZATION", "USER_PREFERENCE"], "eventExpiryDuration": 30}}' \
    --max-iterations 25 --timeout-seconds 600 >/dev/null
  echo "Created harness ${HARNESS_NAME}."
else
  echo "Harness ${HARNESS_NAME} already exists; skipping create."
fi

export HARNESS_ARN="$(harness_arn)"

# Assert memory is actually configured. The demo's last beat depends on
# it, and a harness without it fails silently -- the agent just politely
# asks who you are.
aws bedrock-agentcore-control get-harness \
  --harness-id "${HARNESS_ARN##*/}" \
  --query 'harness.memory.managedMemoryConfiguration.strategies' \
  --output text
echo "HARNESS_ARN=${HARNESS_ARN}"

# ---- 5. Verify before you build on it ----
# setup.sh exports into its own shell, not yours. Copy this line out, or
# demo_run.py runs against an empty HARNESS_ARN.
echo "Next:"
echo "  export HARNESS_ARN=${HARNESS_ARN}"
echo "  python gates.py                       # seed the database"
echo "  python -m pytest evals/test_invariants.py -v  # 16 offline tests"
echo "  python demo_run.py                    # the recorded scenario"
echo "  agentcore traces list                 # confirm traces are flowing"
