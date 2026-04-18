"""Zsh completion script generator for claudio."""


def generate() -> str:
    return r'''#compdef claudio
# Zsh completion for claudio (Claudio CLI)
# Add to ~/.zshrc:  eval "$(claudio --completions zsh)"

_claudio() {
    local -a commands global_flags build_modes ask_modes

    commands=(
        'build:Create or modify code'
        'ask:Ask Claude a question'
        'run:Execute plan from claudio-task.json'
        'setup:Configure PATH and verify installation'
    )

    global_flags=(
        '--dry-run[Show optimized prompt without sending]'
        '--verbose[Show token estimates and details]'
        '--json[Output in JSON format]'
        '--help[Show help]'
        '--version[Show version]'
    )

    build_modes=(
        '-refactor[Refactor existing code]'
        '-r[Refactor (short)]'
        '-generate[Generate new code]'
        '-g[Generate (short)]'
    )

    ask_modes=(
        '-review[Code review]'
        '-rv[Review (short)]'
        '-question[General question]'
        '-q[Question (short)]'
        '-debug[Debug an issue]'
        '-d[Debug (short)]'
    )

    # No subcommand yet
    if (( CURRENT == 2 )); then
        _describe 'command' commands
        return
    fi

    local cmd=${words[2]}

    case "$cmd" in
        build)
            _arguments \
                '1:mode:_describe "mode" build_modes' \
                '*:file:_claudio_files' \
                $global_flags
            ;;
        ask)
            _arguments \
                '1:mode:_describe "mode" ask_modes' \
                '*:file:_claudio_files' \
                $global_flags
            ;;
        run)
            _arguments \
                '*:file:_claudio_files' \
                $global_flags
            ;;
        setup)
            ;;
    esac
}

_claudio_files() {
    # Complete @path with workspace files
    if [[ "$PREFIX" == @* ]]; then
        local fileprefix="${PREFIX#@}"
        local -a files
        files=( ${fileprefix}*(N) )
        compadd -p "@" -S "" -- ${files[@]}
    fi
}

_claudio "$@"
'''
