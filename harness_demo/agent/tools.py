"""Tool declarations: what the harness is told the agent may use.

FIVE CAPABILITIES, TWO SIDES OF A TRUST BOUNDARY.

  Runs on AWS's microVM (we OBSERVE it in the trace):
    - code_interpreter : executes our shipped calculator
    - managed memory   : per-actor history, loaded before reasoning

  Runs in OUR process (we INTERCEPT and gate it):
    - run_sql       (inline_function, L1 read-only)
    - issue_refund  (inline_function, L3 external write)

That split is the architectural decision. Anything that can move money
or leak data is an inline_function, because it pauses the loop and hands
control back to code we own. Everything else runs server-side.
"""

# We DECLARE the interpreter as "code_interpreter"; the harness reports
# the sub-tool the model actually invoked, usually "shell". Matching on
# the declared name alone never matches a real trace, so match on the
# family and keep the raw name in the trace for the audit record.
CODE_INTERPRETER_TOOLS = frozenset({"code_interpreter", "shell",
                                    "execute_code", "read_files",
                                    "write_files"})


def build_tools(calc_remote_path: str) -> list[dict]:
    return [
        {"type": "inline_function", "name": "run_sql",
         "config": {"inlineFunction": {
             "description": ("Run a READ-ONLY SQL SELECT against the support "
                             "database (customers, orders, refund_intents). "
                             "Results are limited to the current customer. "
                             "Mutations are blocked."),
             "inputSchema": {
                 "type": "object",
                 "properties": {"query": {"type": "string"},
                                "max_rows": {"type": "integer"}},
                 "required": ["query"]}}}},
        {"type": "inline_function", "name": "issue_refund",
         "config": {"inlineFunction": {
             "description": ("Issue a refund. amount_usd MUST come from "
                             f"running {calc_remote_path} in the code "
                             "interpreter -- never from your own arithmetic. "
                             "The request is independently recomputed and "
                             "rejected on mismatch."),
             "inputSchema": {
                 "type": "object",
                 "properties": {
                     "order_id": {"type": "integer"},
                     "reason": {"type": "string",
                                "enum": ["damaged", "defective",
                                         "wrong_item", "changed_mind"]},
                     "amount_usd": {"type": "number"},
                     "days_since_delivery": {"type": "integer"}},
                 "required": ["order_id", "reason", "amount_usd",
                              "days_since_delivery"]}}}},
        {"type": "agentcore_code_interpreter", "name": "code_interpreter"},
    ]
