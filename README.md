# Hermes Stack

`hermes-stack` runs Hermes as the only model-calling runtime, Open Design as an
authenticated design workspace and MCP server, and an optional tailnet-only
HTTPS gateway. Open Design receives no OpenAI or Hermes credentials.

## Project mounts

Start with one or more named host projects:

```fish
./hermes-stack start app=/absolute/path/to/app
```

The wrapper stores the selection under `~/.config/hermes-docker` and generates a
Compose override there. Hermes receives each project read-write at
`/workspace/<name>`; Open Design receives the same mount read-only. Open
Design's own `open_design_data` volume remains writable so its web workspace and
previews continue to work.

Avoid editing the same Open Design-managed artifact in the UI while Hermes is
writing it through MCP. Open Design's write API is last-write-wins.

## MCP

After the stack is healthy, configure only Hermes' `open-design` MCP entry:

```fish
./hermes-stack mcp-setup
```

Use `--replace` only when that named entry already points somewhere else. The
configuration excludes OD's run, cancellation, deletion, and project deletion
tools. The MCP bridge is reachable only on the Compose network at
`http://open-design:8000/mcp`; it has no host port and reaches OD through the
daemon's loopback interface.

## Tailscale

The stack starts locally without Tailscale authorization. Authorize its
persistent container identity once, then display the tailnet URLs:

```fish
./hermes-stack tailscale-login
./hermes-stack tailscale-urls
```

Tailscale Serve exposes HTTPS on ports `9119` and `7456` only to the tailnet;
Funnel is disabled. Local access remains available at
`http://127.0.0.1:9119` and `http://127.0.0.1:7456`.

The Hermes dashboard OAuth callback configured in Hermes must exactly match:

```text
https://<tailscale-fqdn>:9119/auth/callback
```

The wrapper does not inspect or change Hermes' OAuth or other dashboard
settings. After login it recreates Open Design with its exact remote HTTPS
origin allowed.
