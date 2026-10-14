using namespace System.Management.Automation.Language

# The hub guard's PowerShell reader. tools/worktrees.py starts it to read one command with PowerShell's own parser
# and prints what the guard needs, in the order PowerShell would run it. It judges nothing: every decision is made
# in worktrees.py, where tests/tools/test_worktrees.py drives it.
#
# Input: the command as base64-encoded UTF-8 on stdin, so no console code page can alter it.
# Output: one JSON object. {"error": message} when PowerShell would refuse to parse the command, and so would run
# none of it; otherwise {"events": [...]}, each one of:
#   {"kind": "command", "words": [{"text", "dynamic"}],  a command, its name first; dynamic means the text does
#    "parameters": {name: [{"text", "dynamic"}]}}        not settle the word's value. parameters are the words
#                                                        PowerShell's own binder gives each parameter, by the
#                                                        name the command declares, an array's elements apart
#   {"kind": "gitEnv", "name": name}                     a location variable (GIT_DIR, GIT_WORK_TREE, or
#                                                        GIT_COMMON_DIR) was assigned through $env: or
#                                                        [Environment]::SetEnvironmentVariable
#   {"kind": "push"} ... {"kind": "pop"}                 around a child pwsh's commands, whose location dies with it
#   {"kind": "note", "text": text}                       a nested command that could not be read
#
# worktrees.py starts it with -Command rather than -File, so no execution policy applies to it.

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

# How deep Invoke-Expression and pwsh -Command are followed; deeper nesting is left unread.
$MaxDepth = 4
# The variables that choose the repository or work tree git writes; worktrees.py names the same three.
$LocationVariable = '^(GIT_DIR|GIT_WORK_TREE|GIT_COMMON_DIR)$'

function ConvertTo-Word([string] $Text, [bool] $Dynamic = $false) {
    [ordered]@{ text = $Text; dynamic = $Dynamic }
}

# The value of $env:NAME when the session has it set; $null for any other variable.
function Get-EnvironmentValue([VariableExpressionAst] $Variable) {
    if (-not $Variable.VariablePath.IsDriveQualified -or $Variable.VariablePath.DriveName -ne 'env') {
        return $null
    }
    [Environment]::GetEnvironmentVariable($Variable.VariablePath.UserPath.Substring(4))
}

# The words one command element passes. `-Name:value` passes two.
function Get-ElementWord([CommandElementAst] $Element) {
    $words = [Collections.Generic.List[object]]::new()
    if ($Element -is [CommandParameterAst]) {
        if ($null -eq $Element.Argument) {
            $words.Add((ConvertTo-Word $Element.Extent.Text))
        } else {
            $words.Add((ConvertTo-Word "-$($Element.ParameterName)"))
            $words.AddRange([object[]] (Get-ElementWord $Element.Argument))
        }
    } elseif ($Element -is [StringConstantExpressionAst] -or $Element -is [ConstantExpressionAst]) {
        $words.Add((ConvertTo-Word "$($Element.Value)"))
    } elseif ($Element -is [VariableExpressionAst]) {
        $value = Get-EnvironmentValue $Element
        $words.Add((ConvertTo-Word "$value" ($null -eq $value)))
    } elseif ($Element -is [ExpandableStringExpressionAst]) {
        # Settled only when every expansion in it is an environment variable the session has set.
        $text = $Element.Value
        foreach ($nested in $Element.NestedExpressions) {
            $value = if ($nested -is [VariableExpressionAst]) { Get-EnvironmentValue $nested }
            if ($null -eq $value) {
                $words.Add((ConvertTo-Word '' $true))
                return , $words.ToArray()
            }
            $text = $text.Replace($nested.Extent.Text, $value)
        }
        $words.Add((ConvertTo-Word $text))
    } else {
        $words.Add((ConvertTo-Word '' $true))
    }
    , $words.ToArray()
}

# The words PowerShell's own binder gives each of a command's parameters, keyed by the name the command declares,
# with abbreviations, aliases, and positions resolved, and an array's elements as separate words. Empty when the
# command's name is computed or the binder fails.
function Get-BoundParameter([CommandAst] $Command) {
    $parameters = [ordered]@{}
    if ($null -eq $Command.GetCommandName()) {
        return $parameters
    }
    try {
        $bound = [StaticParameterBinder]::BindCommand($Command, $true).BoundParameters
    } catch {
        return $parameters
    }
    foreach ($name in $bound.Keys) {
        $value = $bound[$name].Value
        $elements = if ($value -is [ArrayLiteralAst]) { $value.Elements } else { @($value) }
        $words = [Collections.Generic.List[object]]::new()
        foreach ($element in $elements) {
            if ($element -is [CommandElementAst]) {
                $words.AddRange([object[]] (Get-ElementWord $element))
            }
        }
        $parameters[$name] = $words.ToArray()
    }
    $parameters
}

# The lower-cased name a command is called by, without its directory or extension; $null when it is computed.
function Get-CommandLeaf([CommandAst] $Command) {
    $name = $Command.GetCommandName()
    if ($null -eq $name) {
        return $null
    }
    [IO.Path]::GetFileNameWithoutExtension(($name -split '[\\/]')[-1]).ToLowerInvariant()
}

# The script pwsh -Command runs: every word after the switch, joined. $null when there is no -Command, '' when
# what follows it is missing or not static.
function Get-ChildScript([object[]] $Words) {
    for ($index = 1; $index -lt $Words.Count; $index++) {
        if (-not $Words[$index].dynamic -and $Words[$index].text -match '^-c(o(m(m(a(n(d)?)?)?)?)?)?$') {
            # Checked before slicing: a range whose start is past its end counts down instead of coming back empty.
            if ($index + 1 -ge $Words.Count) {
                return ''
            }
            $rest = $Words[($index + 1)..($Words.Count - 1)]
            if (@($rest | Where-Object { $_.dynamic }).Count -gt 0) {
                return ''
            }
            return ($rest | ForEach-Object { $_.text }) -join ' '
        }
    }
    $null
}

# Appends the events of $Text to $Events and returns $null, or returns the parse error of an outermost command.
# A nested command that does not parse runs none of itself, but the command around it still runs.
function Read-CommandEvent([string] $Text, [int] $Depth, [Collections.Generic.List[object]] $Events) {
    $tokens = $null
    $errors = $null
    $ast = [Parser]::ParseInput($Text, [ref] $tokens, [ref] $errors)
    if ($errors.Count -gt 0) {
        if ($Depth -eq 0) {
            return $errors[0].Message
        }
        $Events.Add([ordered]@{ kind = 'note'; text = "a nested command does not parse ($($errors[0].Message))" })
        return $null
    }

    # A command's arguments, $( ) included, run before it does, so execution order is the order the nodes end
    # in; of two that end together, the inner one starts later and runs first.
    $nodes = $ast.FindAll({
            param($node)
            $node -is [CommandAst] -or $node -is [AssignmentStatementAst] -or $node -is [InvokeMemberExpressionAst]
        }, $true) |
        Sort-Object { $_.Extent.EndOffset }, @{ Expression = { $_.Extent.StartOffset }; Descending = $true }

    foreach ($node in $nodes) {
        if ($node -is [AssignmentStatementAst]) {
            if ($node.Left -is [VariableExpressionAst] -and $node.Left.VariablePath.IsDriveQualified -and
                $node.Left.VariablePath.DriveName -eq 'env') {
                $name = $node.Left.VariablePath.UserPath.Substring(4)
                if ($name -match $LocationVariable) {
                    $Events.Add([ordered]@{ kind = 'gitEnv'; name = $name.ToUpperInvariant() })
                }
            }
            continue
        }
        if ($node -is [InvokeMemberExpressionAst]) {
            # [Environment]::SetEnvironmentVariable('GIT_DIR', ...) sets the variable as $env: does.
            $name = $node.Arguments | Select-Object -First 1
            if ($node.Member -is [StringConstantExpressionAst] -and
                $node.Member.Value -eq 'SetEnvironmentVariable' -and
                $name -is [StringConstantExpressionAst] -and $name.Value -match $LocationVariable) {
                $Events.Add([ordered]@{ kind = 'gitEnv'; name = $name.Value.ToUpperInvariant() })
            }
            continue
        }

        $leaf = Get-CommandLeaf $node
        $words = [Collections.Generic.List[object]]::new()
        if ($null -eq $leaf) {
            $words.Add((ConvertTo-Word '' $true))
        } else {
            $words.Add((ConvertTo-Word $node.GetCommandName()))
        }
        $elements = $node.CommandElements
        for ($index = 1; $index -lt $elements.Count; $index++) {
            $words.AddRange([object[]] (Get-ElementWord $elements[$index]))
        }
        $Events.Add([ordered]@{ kind = 'command'; words = $words.ToArray(); parameters = Get-BoundParameter $node })

        if ($leaf -eq 'iex' -or $leaf -eq 'invoke-expression') {
            # Invoke-Expression runs its string in this session, so its location changes outlive it.
            $script = @($words | Select-Object -Skip 1 | Where-Object { $_.dynamic -or -not $_.text.StartsWith('-') })
            if ($script.Count -ne 1 -or $script[0].dynamic -or $Depth + 1 -ge $MaxDepth) {
                $Events.Add([ordered]@{ kind = 'note'; text = "could not read the command passed to $leaf" })
            } else {
                $null = Read-CommandEvent $script[0].text ($Depth + 1) $Events
            }
        } elseif ($leaf -eq 'pwsh' -or $leaf -eq 'powershell') {
            $child = Get-ChildScript $words.ToArray()
            if ($null -eq $child) {
                continue
            }
            if ($child -eq '' -or $Depth + 1 -ge $MaxDepth) {
                $Events.Add([ordered]@{ kind = 'note'; text = "could not read the command passed to $leaf" })
                continue
            }
            $Events.Add([ordered]@{ kind = 'push' })
            $null = Read-CommandEvent $child ($Depth + 1) $Events
            $Events.Add([ordered]@{ kind = 'pop' })
        }
    }
    $null
}

$command = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String([Console]::In.ReadToEnd().Trim()))
$events = [Collections.Generic.List[object]]::new()
$parseError = Read-CommandEvent $command 0 $events
if ($null -ne $parseError) {
    [ordered]@{ error = $parseError } | ConvertTo-Json -Compress
} else {
    [ordered]@{ events = $events.ToArray() } | ConvertTo-Json -Depth 8 -Compress
}
