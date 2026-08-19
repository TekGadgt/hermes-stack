# Hermes Stack

`hermes-stack` runs Hermes as the only model-calling runtime, Open Design as its
native design workspace, and an optional tailnet-only HTTPS gateway. Open
Design launches the existing Hermes CLI through ACP; it does not use Vela or a
separate model credential.

Hermes and Open Design run as supervised processes in one application
container. This gives OD direct access to Hermes' authenticated CLI and the
same project filesystem while keeping their web interfaces on separate ports.

## Install the launcher

The host needs Docker with the Compose plugin and Python 3.9 or newer.

The container runtime must also have enough capacity for the limits in
`compose.yaml`. Allocate at least 4 CPUs, 10 GiB of memory, and 60 GiB of disk
to the Docker VM. For concurrent Hermes profiles, Kanban workers, browser
automation, and persistent project dependencies, 6 CPUs, 12 GiB of memory, and
100 GiB of disk are recommended. These are VM-wide resources; the current
Hermes service may use up to 4 CPUs and 8 GiB, while Docker and the sidecars
need the remaining headroom.

For an existing Colima VM, stop it and restart it with the larger allocation:

```console
colima stop
colima start --cpus 6 --memory 12 --disk 100
```

Increasing an existing Colima disk preserves its Docker data, but the disk
cannot later be shrunk. Configure equivalent VM resources in Docker Desktop or
another container runtime.

After confirming the runtime capacity and cloning the repository, make the
launcher executable and link it into a user-owned binary directory:

```console
chmod +x hermes-stack
mkdir -p ~/.local/bin
ln -sfn "$PWD/hermes-stack" ~/.local/bin/hermes-stack
```

Add that directory to the host shell's persistent `PATH` configuration:

```fish
# Fish: run once; fish_add_path persists the universal variable.
fish_add_path "$HOME/.local/bin"
```

```bash
# Bash: add this line to ~/.bashrc, or ~/.bash_profile for a login shell.
export PATH="$HOME/.local/bin:$PATH"
```

```zsh
# Zsh: add this line to ~/.zshrc.
export PATH="$HOME/.local/bin:$PATH"
```

Open a new shell (or source the Bash/Zsh startup file) before invoking
`hermes-stack`. The symlink continues to work if the repository contents are
updated in place. Recreate it after moving or renaming the repository directory.

Existing installations using the old state directory should stop the running
stack and move it before issuing any other command with this version:

```console
hermes-stack stop
mv ~/.config/hermes-docker ~/.config/hermes-stack
```

The `stop` command recognizes the legacy directory specifically to make this
migration safe. No state is copied or moved automatically.

## Project mounts

Register one or more named host projects while starting the stack:

```console
hermes-stack start app=/absolute/path/to/app docs=/absolute/path/to/docs
```

The CLI stores these locations under `~/.config/hermes-stack` and generates a
Compose override there. Later starts can use the saved names without repeating
host paths:

```console
hermes-stack start app docs
```

An explicit `name=/path` adds or updates a registration. If that host path was
previously registered under a different name, the new name replaces the old
one. With no project arguments, `start`, `restart`, and `update` reuse the last
selection.

Inspect or edit the registry with:

```console
hermes-stack locations
hermes-stack locations add app /absolute/path/to/app
hermes-stack locations set app --node auto
hermes-stack locations reset-node-modules app
hermes-stack locations remove app
hermes-stack projects
```

`locations` shows every registration and marks the active selection;
`projects` shows only the selection used by the next start or restart. Registry,
selection, and runtime workspace-manifest files are human-readable JSON written
atomically. Existing `current-projects` state from the Fish wrapper is imported
automatically. The CLI uses only the Python 3.9+ standard library, so it does
not require a package manager or a separate virtual environment on the host.

Locations use `node: auto` by default. A root `package.json` enables a
persistent Linux-only `node_modules` volume, mounted over `node_modules` at
both container path representations. The host's macOS dependencies remain
untouched and hidden inside the container; install once inside Hermes and
reuse that Linux dependency tree across restarts and image rebuilds. Override
detection with `--node on` or `--node off`. Resetting dependencies removes only
that location's Linux volume and requires the stack not to be using it.

Only `node_modules` is isolated. Framework caches and outputs such as `.next`,
`.nuxt`, `.vite`, and `dist` remain in the shared project, and simultaneous
host/container servers must still use different ports.

Each selected directory is mounted read-write at two synchronized paths:

- `/workspace/<name>` is the operational path used by the dashboard and normal
  interactive file work.
- Its original absolute host path is also present inside the container. Use
  that portable identity when generated code or automation must record an
  absolute path that works both inside and outside the container.

Both paths are bind mounts of the same directory, not copies; an edit or upload
through either path is immediately visible through the other. Prefer relative
paths inside a project. Hermes receives an always-on workspace contract and a
`workspace-paths` skill that select the portable path when an absolute reference
must persist. The selected mappings are available inside the container at
`/run/hermes-stack/workspaces.json` and on the host at
`~/.config/hermes-stack/workspaces.json`.

Only names in the current selection are mounted or included in that runtime
manifest. Other saved locations remain unavailable to the container. Open
Design's own state remains writable at
`~/.config/hermes-stack/open-design`, so its web workspace, previews, and
settings continue to work. The wrapper marks OD onboarding complete and selects
Hermes as its agent after startup.

The launcher refuses workspace paths that would defeat or destabilize this
boundary: the filesystem root, the home directory or its parents, known
credential and configuration trees (including `.ssh`, `.config`, `.docker`,
and `.hermes`), Docker runtime directories, system pseudo-filesystems, and
paths overlapping reserved container locations. It also rejects path-list
separator and control characters before constructing the write-safe-root
environment. Ordinary project directories and separately located output
directories such as Obsidian vaults remain valid.

Avoid running a Discord-driven Hermes edit and an OD-driven Hermes run against
the same project at the same time. They are independent sessions with shared
filesystem access and no cross-session write lease.

Open an interactive shell as the unprivileged `hermes` user with:

```console
hermes-stack shell
```

Fish is that user's login shell. Supervised services and automated tool calls
continue to use their explicitly configured interpreters.

## Obsidian Headless mirror

Obsidian Headless can act as a third Sync client alongside independent Windows
and Mac desktop vaults. Create a dedicated, initially empty host directory for
the headless mirror; do not reuse either desktop client's local vault folder.
Register and configure it without placing account credentials in this
repository or an environment file:

```console
mkdir -p /absolute/path/to/dev-vault-headless
hermes-stack locations add dev-vault /absolute/path/to/dev-vault-headless
hermes-stack obsidian configure dev-vault
hermes-stack obsidian login
hermes-stack obsidian setup --vault "Dev-Vault"
```

`login` prompts interactively for the Obsidian account, password, and MFA.
`setup` accepts a remote vault name or ID and prompts for an end-to-end
encryption password when required. The authentication token and headless sync
database persist beneath `~/.config/hermes-stack/obsidian-headless`, which is
mounted only into the sidecar.

After the next user-initiated stack restart, the sidecar runs bidirectional
continuous sync with all attachment and Obsidian configuration categories,
including community plugins and plugin data. It keeps syncing its mirror even
when the saved location is not selected for Hermes. Select `dev-vault` in the
normal `start` command when Hermes should access its notes.

When selected, the mirror uses the same dual workspace mounts as other
locations. Nested read-only mounts protect `.obsidian` at both Hermes paths,
while the sidecar retains read-write access. Hermes can edit notes and
attachments but cannot distribute changed plugin JavaScript or configuration
to desktop clients. Use `hermes-stack obsidian status`, `logs obsidian`, or
`obsidian disable` to inspect or disable the integration. Disabling does not
delete credentials, unlink the remote vault, or change the running stack.

Obsidian Headless is still beta. Concurrent edits to the same note can conflict
like edits from any independent Sync clients, so keep the dev vault backed up.

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

```console
hermes-stack tailscale-login
hermes-stack tailscale-urls
```

Tailscale Serve exposes Open Design on standard HTTPS port `443` and Hermes on
HTTPS port `9119`, only to the tailnet; Funnel is disabled. Its node identity
is stored at
`~/.config/hermes-stack/tailscale-state`, outside Docker's managed volume
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
`~/.config/hermes-stack/open-design-amr` directory is left untouched during
migration but is no longer mounted or read.

The Hermes dashboard OAuth callback configured in Hermes must exactly match:

```text
https://<tailscale-fqdn>:9119/auth/callback
```

The wrapper does not inspect or change Hermes' OAuth or other dashboard
settings. After login it recreates Open Design with its exact remote HTTPS
origin allowed and refreshes the Tailscale proxy's application upstream.

## Security model

The stack assumes a single-user host and a trusted tailnet:

- Hermes and Open Design publish host ports only on `127.0.0.1`. They are not
  directly reachable from the LAN or public internet.
- Tailscale Serve is the remote access boundary. Funnel is explicitly disabled;
  tailnet membership and Tailscale grants determine who can connect.
- Open Design's bearer-token middleware is deliberately disabled because its
  browser and API traffic passes through the trusted Tailscale/Caddy boundary.
  Any process running as the local user can still reach OD through its loopback
  port, so this is not isolation from other local processes or users.
- Hermes authentication under `~/.hermes`, OD state, Tailscale node state, and
  Obsidian Headless credentials, and the project-location registry are
  sensitive runtime data stored outside this repository. Obsidian credentials
  are available only to its sidecar. Do not copy this state into the repository
  or publish a snapshot made from a running container with `docker commit`.
- Selected host projects are mounted read-write at both their `/workspace`
  alias and original absolute path. These are two names for the same files and
  do not expose either path's parent or sibling directories. Hermes sessions
  started from Discord and OD are independent writers, so do not run both
  against the same project concurrently.

Use narrow Tailscale grants for ports `443` and `9119`, periodically review
tailnet membership, and treat anyone with access to either UI as able to invoke
the authenticated Hermes runtime.

## Reproducible dependencies

External container and build images use readable release tags plus immutable
multi-platform SHA-256 digests. The Open Design source revision and CLI tool
versions are pinned separately in `compose.yaml` and the Dockerfiles. The
locally built Obsidian sidecar pins the official `obsidian-headless` npm package
and is not published as a redistributed image.

`hermes-stack update` rebuilds and restarts the reviewed versions in this
repository; it does not silently advance pinned dependencies. On GitHub,
Dependabot checks Dockerfiles, Compose, and workflow actions weekly and proposes
updates as pull requests. Review and merge an update, then run
`hermes-stack update` locally.

CI runs the Python CLI tests, syntax and configuration checks, and a full Git
history scan for committed secrets. Workflow actions are themselves pinned to
immutable commits.

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
   `~/.config/hermes-stack/caddy-data`) and provide narrowly scoped DNS API
   credentials without committing them to this repository.
5. Allow `https://design.example.com` in `OD_ALLOWED_ORIGINS`, and change the
   Hermes dashboard OAuth callback to
   `https://hermes.example.com/auth/callback`.
6. Verify both names from an authorized tailnet device, confirm Funnel remains
   disabled, then remove the `*.ts.net` Serve routes.

DNS availability does not depend on the laptop being online because the DNS
provider remains authoritative. The laptop must come online often enough for
the persisted ACME client to renew its certificates before expiry.

## License

Hermes Stack is licensed under the Apache License, Version 2.0. See `LICENSE`
for the terms and `NOTICE` for the Open Design attribution associated with the
temporary source backport.
