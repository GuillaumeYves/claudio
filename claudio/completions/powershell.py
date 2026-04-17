"""PowerShell completion script generator for cld."""


def generate() -> str:
    return r'''# PowerShell completion for cld (Claudio CLI)
# Add to $PROFILE:  cld --completions powershell | Invoke-Expression

Register-ArgumentCompleter -CommandName cld -Native -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)

    $commands = @('build', 'ask', 'run', 'stats', 'setup')
    $globalFlags = @('--dry-run', '--no-cache', '--verbose', '--json', '--help', '--version', '-v', '-h')
    $buildModes = @('-refactor', '-r', '-generate', '-g')
    $askModes = @('-review', '-rv', '-question', '-q', '-debug', '-d')
    $statsFlags = @('--reset', '--json')

    $tokens = $commandAst.ToString().Trim() -split '\s+'
    $tokenCount = $tokens.Count

    # Position 1: command
    if ($tokenCount -le 1 -or ($tokenCount -eq 2 -and $wordToComplete)) {
        $candidates = $commands + $globalFlags
        $candidates | Where-Object { $_ -like "$wordToComplete*" } | ForEach-Object {
            [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_)
        }
        return
    }

    $cmd = $tokens[1]

    # @file completion
    if ($wordToComplete.StartsWith('@')) {
        $prefix = $wordToComplete.Substring(1)
        Get-ChildItem -Path "$prefix*" -ErrorAction SilentlyContinue | ForEach-Object {
            $path = "@$($_.Name)"
            if ($_.PSIsContainer) { $path += "/" }
            [System.Management.Automation.CompletionResult]::new(
                $path, $path, 'ParameterValue', $_.FullName
            )
        }
        return
    }

    # Mode and flag completion
    if ($wordToComplete.StartsWith('-')) {
        $candidates = $globalFlags
        switch ($cmd) {
            'build' { $candidates += $buildModes }
            'ask'   { $candidates += $askModes }
            'stats' { $candidates += $statsFlags }
        }
        $candidates | Where-Object { $_ -like "$wordToComplete*" } | ForEach-Object {
            [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_)
        }
        return
    }
}
'''
