"""Provider names, defined once.

A provider name is an implicit contract between the runner that creates a task,
the registry that stores its binding, the adapter that reads it back, and the
adapter manifest that declares it.  Spelling it inline in each module is how
those four drift apart: every file looks correct on its own, and the mismatch
only shows up at dispatch time.  Import from here instead.
"""

# The visible Codex control plane, matching adapters/manifests/codex.yaml.
CODEX_PROVIDER = "codex-app-visible"

# The visible Claude Code control plane, matching adapters/manifests/claude-code.yaml.
CLAUDE_CODE_PROVIDER = "claude-code-visible"

# The visible WorkBuddy control plane, matching adapters/manifests/workbuddy.yaml.
# Serviced by the WorkBuddy desktop session through an MCP server that wraps the
# daemon Jobs HTTP API (POST /api/v1/jobs and friends).  Nothing dispatches until
# that MCP server is installed and trusted: the request is written to the mailbox
# with a native_tool name and waits for the session to call it.
WORKBUDDY_VISIBLE_PROVIDER = "workbuddy-visible"
