_hermes_stack_location_names() {
    command hermes-stack completions bash --location-names 2>/dev/null
}

_hermes_stack() {
    local cur prev command action
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"
    command="${COMP_WORDS[1]-}"

    if (( COMP_CWORD == 1 )); then
        COMPREPLY=( $(compgen -W 'start stop restart status logs projects locations obsidian vscode completions shell update tailscale-login tailscale-status tailscale-urls' -- "$cur") )
        return
    fi

    case "$command" in
        start)
            COMPREPLY=( $(compgen -W "--add $(_hermes_stack_location_names)" -- "$cur") )
            ;;
        logs)
            COMPREPLY=( $(compgen -W 'hermes open-design obsidian vscode tailscale all' -- "$cur") )
            ;;
        completions)
            COMPREPLY=( $(compgen -W 'fish bash zsh' -- "$cur") )
            ;;
        locations)
            action="${COMP_WORDS[2]-}"
            if (( COMP_CWORD == 2 )); then
                COMPREPLY=( $(compgen -W 'list add set reset-node-modules remove' -- "$cur") )
                return
            fi
            if [[ "$prev" == --node ]]; then
                COMPREPLY=( $(compgen -W 'auto on off' -- "$cur") )
                return
            fi
            case "$action" in
                add)
                    if [[ "$cur" == --* ]]; then
                        COMPREPLY=( $(compgen -W '--node' -- "$cur") )
                    elif (( COMP_CWORD >= 4 )); then
                        COMPREPLY=( $(compgen -d -- "$cur") )
                    fi
                    ;;
                set)
                    if (( COMP_CWORD == 3 )); then
                        COMPREPLY=( $(compgen -W "$(_hermes_stack_location_names)" -- "$cur") )
                    else
                        COMPREPLY=( $(compgen -W '--node' -- "$cur") )
                    fi
                    ;;
                reset-node-modules|remove)
                    COMPREPLY=( $(compgen -W "$(_hermes_stack_location_names)" -- "$cur") )
                    ;;
            esac
            ;;
        obsidian)
            action="${COMP_WORDS[2]-}"
            if (( COMP_CWORD == 2 )); then
                COMPREPLY=( $(compgen -W 'configure login setup status disable' -- "$cur") )
                return
            fi
            case "$action" in
                configure)
                    COMPREPLY=( $(compgen -W "$(_hermes_stack_location_names)" -- "$cur") )
                    ;;
                setup)
                    COMPREPLY=( $(compgen -W '--vault --device-name' -- "$cur") )
                    ;;
            esac
            ;;
        vscode)
            COMPREPLY=( $(compgen -W 'enable disable status password reset-password' -- "$cur") )
            ;;
    esac
}

complete -F _hermes_stack hermes-stack
