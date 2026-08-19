---
name: workspace-paths
description: Keep Hermes Stack filesystem references portable between its /workspace aliases and the host. Use when creating scripts, scheduled jobs, configuration, documentation, generated commands, or cross-workspace automation that records or communicates workspace paths.
---

# Workspace Paths

Read the JSON file named by `HERMES_STACK_WORKSPACE_MANIFEST` before recording a
workspace path. Each entry maps a dashboard-friendly `workspace_path` to a
portable `host_path`. Both paths address the same bind-mounted files.

## Choose paths

- Perform interactive file work through `/workspace/<name>`.
- Prefer paths relative to the relevant project root in code, scripts, and
  configuration.
- When an absolute path must survive outside the container, translate it to the
  entry's `host_path`. That exact path is also mounted inside the container.
- For a path below a workspace, preserve its relative suffix during translation.
- Treat every manifest entry as independent unless the task explicitly connects
  them.
- Never treat the two paths in one entry as separate copies, and never dispatch
  duplicate agents against both representations.
- When `node_project` is true, both container paths share a persistent Linux
  `node_modules` overlay. Never record paths into that directory as portable or
  replace it with dependencies from the host.
- When `obsidian_vault` is true, `.obsidian` is intentionally read-only to
  Hermes. Work with vault content without changing synchronized plugins or
  configuration.

## Example

Given:

```json
{
  "name": "chat_suspects",
  "workspace_path": "/workspace/chat_suspects",
  "host_path": "/Users/example/projects/whodunchat-with-filters"
}
```

Edit `/workspace/chat_suspects/scripts/report.py` interactively. Prefer
`scripts/report.py` when the project can resolve it. If a scheduled job requires
an absolute path, write
`/Users/example/projects/whodunchat-with-filters/scripts/report.py`.

If no manifest entry contains a requested path, stop and report that the
directory is not exposed to this container. Do not infer access through a
parent or sibling directory.
