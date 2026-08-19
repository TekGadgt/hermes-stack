function __hermes_stack_location_names
    command hermes-stack completions fish --location-names 2>/dev/null
end

function __hermes_stack_needs_location_action
    not __fish_seen_subcommand_from list add set reset-node-modules remove
end

function __hermes_stack_needs_obsidian_action
    not __fish_seen_subcommand_from configure login setup status disable
end

complete -c hermes-stack -f

complete -c hermes-stack -n '__fish_use_subcommand' -a start -d 'Start with saved project locations'
complete -c hermes-stack -n '__fish_use_subcommand' -a stop -d 'Stop all stack services'
complete -c hermes-stack -n '__fish_use_subcommand' -a restart -d 'Restart with the current selection'
complete -c hermes-stack -n '__fish_use_subcommand' -a status -d 'Show service status'
complete -c hermes-stack -n '__fish_use_subcommand' -a logs -d 'Follow service logs'
complete -c hermes-stack -n '__fish_use_subcommand' -a projects -d 'Show the current project selection'
complete -c hermes-stack -n '__fish_use_subcommand' -a locations -d 'List or edit saved locations'
complete -c hermes-stack -n '__fish_use_subcommand' -a obsidian -d 'Configure the Obsidian mirror'
complete -c hermes-stack -n '__fish_use_subcommand' -a completions -d 'Print shell completion definitions'
complete -c hermes-stack -n '__fish_use_subcommand' -a shell -d 'Open the Hermes container shell'
complete -c hermes-stack -n '__fish_use_subcommand' -a update -d 'Pull, rebuild, and restart'
complete -c hermes-stack -n '__fish_use_subcommand' -a tailscale-login -d 'Authorize Tailscale'
complete -c hermes-stack -n '__fish_use_subcommand' -a tailscale-status -d 'Show Tailscale status'
complete -c hermes-stack -n '__fish_use_subcommand' -a tailscale-urls -d 'Show local and tailnet URLs'

complete -c hermes-stack -n '__fish_seen_subcommand_from start' -a '(__hermes_stack_location_names)' -d 'Saved location'
complete -c hermes-stack -n '__fish_seen_subcommand_from logs' -a 'hermes open-design obsidian tailscale all'
complete -c hermes-stack -n '__fish_seen_subcommand_from completions' -a 'fish bash zsh'

complete -c hermes-stack -n '__fish_seen_subcommand_from locations; and __hermes_stack_needs_location_action' -a list -d 'List saved locations'
complete -c hermes-stack -n '__fish_seen_subcommand_from locations; and __hermes_stack_needs_location_action' -a add -d 'Add or update a location'
complete -c hermes-stack -n '__fish_seen_subcommand_from locations; and __hermes_stack_needs_location_action' -a set -d 'Change location options'
complete -c hermes-stack -n '__fish_seen_subcommand_from locations; and __hermes_stack_needs_location_action' -a reset-node-modules -d 'Reset Linux dependencies'
complete -c hermes-stack -n '__fish_seen_subcommand_from locations; and __hermes_stack_needs_location_action' -a remove -d 'Remove a saved location'
complete -c hermes-stack -n '__fish_seen_subcommand_from locations; and __fish_seen_subcommand_from add set' -l node -xa 'auto on off' -d 'Node project detection mode'
complete -c hermes-stack -n '__fish_seen_subcommand_from locations; and __fish_seen_subcommand_from set reset-node-modules remove' -a '(__hermes_stack_location_names)' -d 'Saved location'
complete -c hermes-stack -n '__fish_seen_subcommand_from locations; and __fish_seen_subcommand_from add' -F

complete -c hermes-stack -n '__fish_seen_subcommand_from obsidian; and __hermes_stack_needs_obsidian_action' -a configure -d 'Choose the mirror location'
complete -c hermes-stack -n '__fish_seen_subcommand_from obsidian; and __hermes_stack_needs_obsidian_action' -a login -d 'Log in to Obsidian'
complete -c hermes-stack -n '__fish_seen_subcommand_from obsidian; and __hermes_stack_needs_obsidian_action' -a setup -d 'Connect the remote vault'
complete -c hermes-stack -n '__fish_seen_subcommand_from obsidian; and __hermes_stack_needs_obsidian_action' -a status -d 'Show sync status'
complete -c hermes-stack -n '__fish_seen_subcommand_from obsidian; and __hermes_stack_needs_obsidian_action' -a disable -d 'Disable continuous sync'
complete -c hermes-stack -n '__fish_seen_subcommand_from obsidian; and __fish_seen_subcommand_from configure' -a '(__hermes_stack_location_names)' -d 'Saved location'
complete -c hermes-stack -n '__fish_seen_subcommand_from obsidian; and __fish_seen_subcommand_from setup' -l vault -r -d 'Remote vault name or ID'
complete -c hermes-stack -n '__fish_seen_subcommand_from obsidian; and __fish_seen_subcommand_from setup' -l device-name -r -d 'Sync device name'
