using namespace System.Management.Automation.Language

# The hub guard's PowerShell reader. tools/hub_guard.py starts it to read one command with PowerShell's own parser
# and prints what the guard needs, in the order PowerShell would run it. It judges nothing: every decision is made
# in hub_guard.py, where tests/tools/test_hub_guard.py drives it.
#
# Input: base64-encoded UTF-8 of a JSON object on stdin, so no console code page can alter it: "command", the text,
# and "limit", how deep Invoke-Expression and pwsh -Command are followed, which hub_guard.py passes from its own
# nesting limit.
# Output: one JSON object. {"error": message} when PowerShell would refuse to parse the command, and so would run
# none of it; otherwise {"events": [...]}, each one of:
#   {"kind": "command", "words": [{"text", "dynamic"}],  a command, its name first; dynamic means the text does
#    "parameters": {name: [{"text", "dynamic"}]},       not settle the word's value, unless the word names the
#    "failed": bool, "whatIf": bool}                     variables it holds, as "variable" or "expansions", for
#                                                        hub_guard.py to read in the session. parameters are the
#                                                        words PowerShell's own binder gives each parameter, by
#                                                        the name the command declares, an array's elements
#                                                        apart; failed means the binder refused the command and
#                                                        whatIf that -WhatIf is set, so that it runs nothing
#   {"kind": "assigned", "path": path, "removed": bool,  a variable on a drive or provider, as in $env:NAME or
#    "value": word}                                      ${Environment::NAME}, was assigned, as a foreach loop's
#                                                        variable, or through PSVariable.Set; removed when $null
#                                                        was; value, the word a plain assignment gives it
#   {"kind": "setVariable", "name": name|null,           [Environment]::SetEnvironmentVariable set or, given
#    "removed": bool}                                     $null, removed a process variable; null when the name is
#                                                        not settled by the text
#   {"kind": "opaque"}                                   a variable was set where the text does not settle which
#   {"kind": "open", "region": kind, "name": name}       around commands PowerShell may not run once: a "branch"
#   ... {"kind": "close"}                                runs once or not at all, a "loop" any number of times, a
#                                                        "function" body only where a command calls it by name, and
#                                                        a "child" pwsh in a process of its own
#   {"kind": "unread", "text": text}                     a command another one runs that could not be read
#   {"kind": "note", "text": text}                       a nested command that does not parse, so runs nothing
#
# hub_guard.py starts it with -Command rather than -File, so no execution policy applies to it.

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

function ConvertTo-Word([string] $Text, [bool] $Dynamic = $false) {
    [ordered]@{ text = $Text; dynamic = $Dynamic }
}

# The words one command element passes. `-Name:value` passes two. A variable's value is the session's, which the
# command's own assignments change, so a word that holds variables is dynamic here and names them for hub_guard.py:
# "variable", the path of the one it is, or "expansions", each {"token", "variable"} its text holds.
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
        $word = ConvertTo-Word '' $true
        $word.variable = $Element.VariablePath.UserPath
        $words.Add($word)
    } elseif ($Element -is [ExpandableStringExpressionAst]) {
        # Settled only when every expansion in it is a variable.
        $word = ConvertTo-Word $Element.Value $true
        $expansions = [Collections.Generic.List[object]]::new()
        foreach ($nested in $Element.NestedExpressions) {
            if ($nested -isnot [VariableExpressionAst]) {
                $words.Add((ConvertTo-Word '' $true))
                return , $words.ToArray()
            }
            $expansions.Add([ordered]@{ token = $nested.Extent.Text; variable = $nested.VariablePath.UserPath })
        }
        $word.expansions = $expansions.ToArray()
        $words.Add($word)
    } else {
        $words.Add((ConvertTo-Word '' $true))
    }
    , $words.ToArray()
}

# What PowerShell's own binder makes of a command: the words it gives each parameter, keyed by the name the command
# declares, with abbreviations, aliases, and positions resolved, and an array's elements as separate words; whether
# it refuses to bind the command, which then runs nothing; and whether -WhatIf is set. Nothing is bound when the
# command's name is computed or the binder cannot be run.
function Get-Binding([CommandAst] $Command) {
    $binding = [ordered]@{ parameters = [ordered]@{}; failed = $false; whatIf = $false }
    if ($null -eq $Command.GetCommandName()) {
        return $binding
    }
    try {
        $result = [StaticParameterBinder]::BindCommand($Command, $true)
    } catch {
        return $binding
    }
    $binding.failed = $result.BindingExceptions.Count -gt 0
    $bound = $result.BoundParameters
    $binding.whatIf = $bound.ContainsKey('WhatIf') -and $bound['WhatIf'].ConstantValue -eq $true
    foreach ($name in $bound.Keys) {
        $value = $bound[$name].Value
        $elements = if ($value -is [ArrayLiteralAst]) { $value.Elements } else { @($value) }
        $words = [Collections.Generic.List[object]]::new()
        foreach ($element in $elements) {
            if ($element -is [CommandElementAst]) {
                $words.AddRange([object[]] (Get-ElementWord $element))
            }
        }
        $binding.parameters[$name] = $words.ToArray()
    }
    $binding
}

# The lower-cased name a command is called by, without its directory or extension; $null when it is computed.
function Get-CommandLeaf([CommandAst] $Command) {
    $name = $Command.GetCommandName()
    if ($null -eq $name) {
        return $null
    }
    [IO.Path]::GetFileNameWithoutExtension(($name -split '[\\/]')[-1]).ToLowerInvariant()
}

# The variables an assignment's left side assigns. A cast or attribute, as in [string]$env:NAME = ..., still assigns
# the variable it wraps, and so do parentheses, as in ($env:NAME) = ...; a multiple assignment, as in
# $env:NAME, $other = ..., assigns each of its targets, whatever value PowerShell gives each.
function Get-AssignedVariable([ExpressionAst] $Left) {
    while ($true) {
        if ($Left -is [AttributedExpressionAst]) {
            $Left = $Left.Child
        } elseif ($Left -is [ParenExpressionAst] -and $Left.Pipeline -is [PipelineAst] -and
            $Left.Pipeline.PipelineElements.Count -eq 1 -and
            $Left.Pipeline.PipelineElements[0] -is [CommandExpressionAst]) {
            $Left = $Left.Pipeline.PipelineElements[0].Expression
        } else {
            break
        }
    }
    if ($Left -is [VariableExpressionAst]) {
        , $Left
    } elseif ($Left -is [ArrayLiteralAst]) {
        foreach ($element in $Left.Elements) {
            Get-AssignedVariable $element
        }
    }
}

# Whether an expression is $null, which removes an environment variable it is assigned to.
function Test-Null([Ast] $Ast) {
    $Ast -is [CommandExpressionAst] -and $Ast.Expression -is [VariableExpressionAst] -and
    $Ast.Expression.VariablePath.UserPath -eq 'null'
}

# The parameter name a pwsh argument spells, lower-cased, as pwsh reads its command line, which hub_guard.py's
# powershell_switch reads the same way: trimmed, without a leading dash (a hyphen, an en dash, an em dash, or a
# horizontal bar) and then a second dash the same as the first, or without a leading slash.
# $null for a word that is not a parameter.
function Get-PwshParameterName([string] $Text) {
    $trimmed = $Text.Trim()
    if ($trimmed.Length -eq 0) {
        return $null
    }
    $first = $trimmed[0]
    $name = $trimmed.Substring(1)
    if ("-$([char] 0x2013)$([char] 0x2014)$([char] 0x2015)".IndexOf($first) -ge 0) {
        if ($name.Length -gt 0 -and $name[0] -eq $first) {
            $name = $name.Substring(1)
        }
    } elseif ($first -ne '/') {
        return $null
    }
    $name.ToLowerInvariant()
}

# The script pwsh runs inline: every word after -Command, joined, or the one word after -CommandWithArgs or -cwa,
# each spelled as Get-PwshParameterName reads it. $null when there is no such switch, '' when what follows it is
# missing or not static.
function Get-ChildScript([object[]] $Words) {
    for ($index = 1; $index -lt $Words.Count; $index++) {
        if ($Words[$index].dynamic) {
            continue
        }
        $name = Get-PwshParameterName $Words[$index].text
        $withArguments = $name -cmatch '^(cwa|commandw(i(t(h(a(r(g(s)?)?)?)?)?)?)?)$'
        if ($withArguments -or $name -cmatch '^c(o(m(m(a(n(d)?)?)?)?)?)?$') {
            # Checked before slicing: a range whose start is past its end counts down instead of coming back empty.
            if ($index + 1 -ge $Words.Count) {
                return ''
            }
            $last = if ($withArguments) { $index + 1 } else { $Words.Count - 1 }
            $rest = @($Words[($index + 1)..$last])
            if (@($rest | Where-Object { $_.dynamic }).Count -gt 0) {
                return ''
            }
            return ($rest | ForEach-Object { $_.text }) -join ' '
        }
    }
    $null
}

# Whether a script block runs once where it stands: invoked by & or . as the command itself.
function Test-InvokedOnce([ScriptBlockExpressionAst] $Block) {
    $parent = $Block.Parent
    $parent -is [CommandAst] -and [object]::ReferenceEquals($parent.CommandElements[0], $Block) -and
    $parent.InvocationOperator -in @([TokenKind]::Ampersand, [TokenKind]::Dot)
}

# The parts of a script PowerShell may not run exactly once where they stand, outermost first.
function Get-Region([Ast] $Ast) {
    $regions = [Collections.Generic.List[object]]::new()
    $add = {
        param([Ast] $Body, [string] $Kind, [string] $Name = '')
        if ($null -ne $Body) {
            $regions.Add([pscustomobject]@{
                    Start = $Body.Extent.StartOffset; End = $Body.Extent.EndOffset; Kind = $Kind; Name = $Name
                })
        }
    }
    foreach ($node in $Ast.FindAll({ $true }, $true)) {
        if ($node -is [IfStatementAst]) {
            foreach ($clause in $node.Clauses) { & $add $clause.Item2 'branch' }
            & $add $node.ElseClause 'branch'
        } elseif ($node -is [LoopStatementAst]) {
            & $add $node.Body 'loop'
        } elseif ($node -is [SwitchStatementAst]) {
            foreach ($clause in $node.Clauses) { & $add $clause.Item2 'branch' }
            & $add $node.Default 'branch'
        } elseif ($node -is [TryStatementAst]) {
            foreach ($catch in $node.CatchClauses) { & $add $catch.Body 'branch' }
        } elseif ($node -is [TrapStatementAst]) {
            & $add $node.Body 'branch'
        } elseif ($node -is [PipelineChainAst]) {
            & $add $node.RhsPipeline 'branch'
        } elseif ($node -is [FunctionDefinitionAst]) {
            & $add $node.Body 'function' $node.Name
        } elseif ($node -is [ScriptBlockExpressionAst] -and -not (Test-InvokedOnce $node)) {
            & $add $node 'loop'
        }
    }
    , @($regions | Sort-Object Start, @{ Expression = { $_.End }; Descending = $true })
}

# The events of a method call that sets an environment variable: [Environment]::SetEnvironmentVariable in this
# process, and PSVariable.Set on a provider path.
function Add-MethodEvent([InvokeMemberExpressionAst] $Node, [Collections.Generic.List[object]] $Events) {
    if ($Node.Member -isnot [StringConstantExpressionAst]) {
        return
    }
    $arguments = @($Node.Arguments)
    $name = $arguments | Select-Object -First 1
    if ($Node.Member.Value -eq 'SetEnvironmentVariable') {
        $target = if ($arguments.Count -ge 3) { $arguments[2] }
        if ($null -ne $target -and $target.Extent.Text -notmatch 'Process') {
            if ($target -is [StringConstantExpressionAst] -or $target -is [MemberExpressionAst]) {
                return
            }
        }
        $removed = $arguments.Count -ge 2 -and $arguments[1] -is [VariableExpressionAst] -and
        $arguments[1].VariablePath.UserPath -eq 'null'
        $settled = $name -is [StringConstantExpressionAst]
        $Events.Add([ordered]@{ kind = 'setVariable'; name = if ($settled) { $name.Value }; removed = $removed })
    } elseif ($Node.Member.Value -eq 'Set' -and $Node.Expression.Extent.Text -match 'PSVariable$') {
        if ($name -is [StringConstantExpressionAst]) {
            $Events.Add([ordered]@{ kind = 'assigned'; path = $name.Value; removed = $false })
        } else {
            $Events.Add([ordered]@{ kind = 'opaque' })
        }
    }
}

# The events of an assignment, or of a foreach loop's variable, to a variable on a drive or provider.
function Add-AssignmentEvent([Ast] $Node, [Collections.Generic.List[object]] $Events) {
    if ($Node -is [AssignmentStatementAst]) {
        $plain = $Node.Operator -eq [TokenKind]::Equals
        $removed = $plain -and (Test-Null $Node.Right)
        # A plain assignment of one word to the variable itself sets that word; any other sets what the text does not
        # settle.
        $value = $null
        if ($plain -and -not $removed -and $Node.Left -is [VariableExpressionAst] -and
            $Node.Right -is [CommandExpressionAst]) {
            $values = [object[]] (Get-ElementWord $Node.Right.Expression)
            if ($values.Count -eq 1) {
                $value = $values[0]
            }
        }
        foreach ($variable in Get-AssignedVariable $Node.Left) {
            if ($variable.VariablePath.IsDriveQualified) {
                $assignment = [ordered]@{
                    kind = 'assigned'; path = $variable.VariablePath.UserPath; removed = $removed
                }
                if ($null -ne $value) {
                    $assignment.value = $value
                }
                $Events.Add($assignment)
            }
        }
    } elseif ($Node.VariablePath.IsDriveQualified) {
        $Events.Add([ordered]@{ kind = 'assigned'; path = $Node.VariablePath.UserPath; removed = $false })
    }
}

# The events of one command, and of the command it hands Invoke-Expression or a child pwsh.
function Add-CommandEvent([CommandAst] $Node, [int] $Depth, [int] $Limit, [Collections.Generic.List[object]] $Events) {
    $leaf = Get-CommandLeaf $Node
    $words = [Collections.Generic.List[object]]::new()
    if ($null -eq $leaf) {
        $words.Add((ConvertTo-Word '' $true))
    } else {
        $words.Add((ConvertTo-Word $Node.GetCommandName()))
    }
    $elements = $Node.CommandElements
    for ($index = 1; $index -lt $elements.Count; $index++) {
        $words.AddRange([object[]] (Get-ElementWord $elements[$index]))
    }
    $binding = Get-Binding $Node
    $Events.Add([ordered]@{
            kind = 'command'; words = $words.ToArray(); parameters = $binding.parameters
            failed = $binding.failed; whatIf = $binding.whatIf
        })
    if ($binding.failed -or $binding.whatIf) {
        return
    }
    if ($leaf -eq 'iex' -or $leaf -eq 'invoke-expression') {
        # Invoke-Expression runs its string in this session, so its location changes outlive it.
        $script = @($words | Select-Object -Skip 1 | Where-Object { $_.dynamic -or -not $_.text.StartsWith('-') })
        if ($script.Count -ne 1 -or $script[0].dynamic -or $Depth + 1 -ge $Limit) {
            $Events.Add([ordered]@{ kind = 'unread'; text = "could not read the command passed to $leaf" })
        } else {
            $null = Read-CommandEvent $script[0].text ($Depth + 1) $Limit $Events
        }
    } elseif ($leaf -eq 'pwsh' -or $leaf -eq 'powershell') {
        $child = Get-ChildScript $words.ToArray()
        if ($null -eq $child) {
            return
        }
        if ($child -eq '' -or $Depth + 1 -ge $Limit) {
            $Events.Add([ordered]@{ kind = 'unread'; text = "could not read the command passed to $leaf" })
            return
        }
        $Events.Add([ordered]@{ kind = 'open'; region = 'child'; name = $leaf })
        $null = Read-CommandEvent $child ($Depth + 1) $Limit $Events
        $Events.Add([ordered]@{ kind = 'close' })
    }
}

# Appends the events of $Text to $Events and returns $null, or returns the parse error of an outermost command.
# A nested command that does not parse runs none of itself, but the command around it still runs.
function Read-CommandEvent([string] $Text, [int] $Depth, [int] $Limit, [Collections.Generic.List[object]] $Events) {
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
            $node -is [CommandAst] -or $node -is [AssignmentStatementAst] -or $node -is [InvokeMemberExpressionAst] -or
            ($node -is [VariableExpressionAst] -and $node.Parent -is [ForEachStatementAst] -and
            [object]::ReferenceEquals($node.Parent.Variable, $node))
        }, $true) |
        Sort-Object { $_.Extent.EndOffset }, @{ Expression = { $_.Extent.StartOffset }; Descending = $true }
    $regions = Get-Region $ast
    $open = [Collections.Generic.List[int]]::new()

    foreach ($node in $nodes) {
        # Close each open region that does not hold this node, then open each that does, outermost first.
        $holding = @(for ($index = 0; $index -lt $regions.Count; $index++) {
                if ($regions[$index].Start -le $node.Extent.StartOffset -and
                    $node.Extent.EndOffset -le $regions[$index].End) {
                    $index
                }
            })
        while ($open.Count -gt 0 -and $holding -notcontains $open[$open.Count - 1]) {
            $Events.Add([ordered]@{ kind = 'close' })
            $open.RemoveAt($open.Count - 1)
        }
        foreach ($index in $holding) {
            if (-not $open.Contains($index)) {
                $region = $regions[$index]
                $Events.Add([ordered]@{ kind = 'open'; region = $region.Kind; name = $region.Name })
                $open.Add($index)
            }
        }

        if ($node -is [CommandAst]) {
            Add-CommandEvent $node $Depth $Limit $Events
        } elseif ($node -is [InvokeMemberExpressionAst]) {
            Add-MethodEvent $node $Events
        } else {
            Add-AssignmentEvent $node $Events
        }
    }
    foreach ($index in $open) {
        $Events.Add([ordered]@{ kind = 'close' })
    }
    $null
}

$payload = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String([Console]::In.ReadToEnd().Trim())) |
    ConvertFrom-Json
$events = [Collections.Generic.List[object]]::new()
$parseError = Read-CommandEvent $payload.command 0 ([int] $payload.limit) $events
if ($null -ne $parseError) {
    [ordered]@{ error = $parseError } | ConvertTo-Json -Compress
} else {
    [ordered]@{ events = $events.ToArray() } | ConvertTo-Json -Depth 10 -Compress
}
