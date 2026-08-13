# Hermes Stack

`hermes-stack` runs Hermes as the only model-calling runtime, Open Design as its
native design workspace, and an optional tailnet-only HTTPS gateway. Open
Design launches the existing Hermes CLI through ACP; it does not use Vela or a
separate model credential.

Hermes and Open Design run as supervised processes in one application
container. This gives OD direct access to Hermes' authenticated CLI and the
same project filesystem while keeping their web interfaces on separate ports.

## Project mounts

Start with one or more named host projects:

```fish
./hermes-stack start app=/absolute/path/to/app
```

The wrapper stores the selection under `~/.config/hermes-docker` and generates a
Compose override there. Hermes receives each project read-write at
`/workspace/<name>`; OD-launched Hermes runs use that same writable path. Open
Design's own state remains writable at
`~/.config/hermes-docker/open-design`, so its web workspace, previews, and
settings continue to work. The wrapper marks OD onboarding complete and selects
Hermes as its agent after startup.

Avoid running a Discord-driven Hermes edit and an OD-driven Hermes run against
the same project at the same time. They are independent sessions with shared
filesystem access and no cross-session write lease.

## Native Open Design agent

Open Design invokes the local agent using its supported Hermes adapter:

```text
hermes acp --accept-hooks
```

OD owns skill, design-system, craft, plugin, prompt, preview, continuation, and
run lifecycle composition. Hermes owns model calls and tool execution using its
existing state under `~/.hermes`. The old Open Design MCP entry is removed at
container startup so Hermes cannot recursively invoke an OD run that launches
another Hermes process.

The pinned Open Design v0.19.0 source receives one temporary upstream backport:
signed-out Open Design Cloud state redirects to onboarding only when the AMR
cloud agent is selected, so it does not block Hermes. Remove
`patches/open-design-v0.19-local-agent-cloud-gate.patch` and its Dockerfile
application after `OPEN_DESIGN_REV` advances to a release containing upstream
commit [`85d2e4893c`](https://github.com/nexu-io/open-design/commit/85d2e4893c).

## Tailscale

The stack starts locally without Tailscale authorization. Authorize its
persistent container identity once, then display the tailnet URLs:

```fish
./hermes-stack tailscale-login
./hermes-stack tailscale-urls
```

Tailscale Serve exposes Open Design on standard HTTPS port `443` and Hermes on
HTTPS port `9119`, only to the tailnet; Funnel is disabled. Its node identity
is stored at
`~/.config/hermes-docker/tailscale-state`, outside Docker's managed volume
storage. Local access remains available at
`http://127.0.0.1:9119` and `http://127.0.0.1:7456`.

The resulting remote URLs are:

```text
https://<tailscale-fqdn>
https://<tailscale-fqdn>:9119
```

Open Design disables its API-token middleware because Tailscale and Caddy are
the trusted authentication boundary for remote browser access. Its published
host port remains loopback-only, and the exact remote origin is allow-listed
after Tailscale login. This lets the remote UI save OD settings without exposing
OD directly on the LAN.

## Authentication

Open Design does not install or use Vela. Its native agent inherits
`HERMES_HOME=/opt/data` and uses the same host-persisted Hermes authentication
as the Discord gateway. The former
`~/.config/hermes-docker/open-design-amr` directory is left untouched during
migration but is no longer mounted or read.

The Hermes dashboard OAuth callback configured in Hermes must exactly match:

```text
https://<tailscale-fqdn>:9119/auth/callback
```

The wrapper does not inspect or change Hermes' OAuth or other dashboard
settings. After login it recreates Open Design with its exact remote HTTPS
origin allowed and refreshes the Tailscale proxy's application upstream.

## Future custom-domain migration

Custom domains are not enabled by this stack. A future migration can use two
hostnames, such as `design.example.com` and `hermes.example.com`, without
making either service public:

1. Keep the domain's authoritative DNS at the existing provider. Public `A`
   records can point both names at this node's persisted Tailscale `100.x`
   address. Publishing that private address does not provide a public route;
   clients still need tailnet access and permission under Tailscale grants.
2. Replace Tailscale Serve's HTTPS termination with tailnet-only raw TCP `443`
   forwarding to Caddy. Let Caddy route by hostname to OD on `7456` and Hermes
   on `9119`.
3. Obtain certificates through ACME DNS-01. Use a Caddy build containing the
   DNS module for the authoritative provider, or use Certbot/acme.sh with that
   provider's DNS API. HTTP-01 cannot validate a tailnet-only origin.
4. Persist Caddy's `/data` outside Docker-managed volumes (for example,
   `~/.config/hermes-docker/caddy-data`) and provide narrowly scoped DNS API
   credentials without committing them to this repository.
5. Allow `https://design.example.com` in `OD_ALLOWED_ORIGINS`, and change the
   Hermes dashboard OAuth callback to
   `https://hermes.example.com/auth/callback`.
6. Verify both names from an authorized tailnet device, confirm Funnel remains
   disabled, then remove the `*.ts.net` Serve routes.

DNS availability does not depend on the laptop being online because the DNS
provider remains authoritative. The laptop must come online often enough for
the persisted ACME client to renew its certificates before expiry.
