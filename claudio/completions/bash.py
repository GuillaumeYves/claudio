"""Bash completion script generator for cld."""


def generate() -> str:
    return r'''# Bash completion for cld (Claudio CLI)
# Add to ~/.bashrc:  eval "$(cld --completions bash)"

_cld_complete() {
    local cur prev words cword
    _init_completion || return

    local commands="build ask run stats setup"
    local global_flags="--dry-run --no-cache --verbose --json --help --version"
    local build_modes="-refactor -r -generate -g"
    local ask_modes="-review -rv -question -q -debug -d"
    local stats_flags="--reset"

    # Position 1: command
    if [[ $cword -eq 1 ]]; then
        COMPREPLY=($(compgen -W "$commands $global_flags -v -h" -- "$cur"))
        return
    fi

    local cmd="${words[1]}"

    # After command: modes, flags, @files
    case "$cmd" in
        build)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "$build_modes $global_flags" -- "$cur"))
            elif [[ "$cur" == @* ]]; then
                _cld_complete_files
            fi
            ;;
        ask)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "$ask_modes $global_flags" -- "$cur"))
            elif [[ "$cur" == @* ]]; then
                _cld_complete_files
            fi
            ;;
        run)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "$global_flags" -- "$cur"))
            elif [[ "$cur" == @* ]]; then
                _cld_complete_files
            fi
            ;;
        stats)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "$stats_flags --json" -- "$cur"))
            fi
            ;;
        setup)
            COMPREPLY=()
            ;;
    esac
}

_cld_complete_files() {
    # Complete @path with workspace files
    local prefix="${cur#@}"
    local files
    files=$(compgen -f -- "$prefix" 2>/dev/null)
    COMPREPLY=()
    while IFS= read -r f; do
        if [[ -d "$f" ]]; then
            COMPREPLY+=("@${f}/")
        else
            COMPREPLY+=("@${f}")
        fi
    done <<< "$files"
    compopt -o nospace
}

complete -o default -F _cld_complete cld
'''
