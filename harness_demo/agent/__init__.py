"""The harness runtime: everything that talks to AgentCore.

  client     HarnessClient.run_episode -- the invoke/stream/gate loop
  stream     turns the raw event stream into text, trace and pending calls
  episode    the audit record of one run
  memory     long-term, per-actor memory: count, wait for, namespace
  sandbox    puts OUR calculator on the AWS microVM
  prompt     the system prompt, built fresh for every episode
  tools      the tool declarations the harness is told about
  observer   how a caller watches an episode without the library printing
"""
