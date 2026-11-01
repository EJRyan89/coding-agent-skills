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
#    "failed": bool, "whatIf": bool,                     variables it holds, as "variable" or "expansions", for
#    "fromPipeline": [name]}                             hub_guard.py to read in the session. parameters are the
#                                                        words PowerShell's own binder gives each parameter, by
#                                                        the name the command declares, an array's elements
#                                                        apart; failed means the binder refused the command and
#                                                        whatIf that -WhatIf is set, so that it runs nothing,
#                                                        unless it calls a function the command defines;
#                                                        fromPipeline, for a command after the first in its
#                                                        pipeline, each unbound parameter it may take from there
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
# it refuses to bind the command, which then runs nothing; whether -WhatIf is set; and, for a command after the first
# in its pipeline, each parameter its metadata lets it take from the pipeline, by value or by property name, that its
# arguments leave unbound. Nothing is bound when the command's name is computed or the binder cannot be run.
function Get-Binding([CommandAst] $Command) {
    $binding = [ordered]@{ parameters = [ordered]@{}; failed = $false; whatIf = $false; fromPipeline = @() }
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
    if (($binding.failed -or $binding.whatIf) -and -not (Test-Bindable $Command)) {
        # A native program takes its words as they are: no binder refuses them, and -WhatIf is one of them.
        $binding.failed = $false
        $binding.whatIf = $false
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
        $binding.parameters[$name] = $words.ToArray()
    }
    $pipeline = $Command.Parent
    if ($pipeline -is [PipelineAst] -and $pipeline.PipelineElements.IndexOf($Command) -gt 0) {
        # A parameter the pipeline supplies is one the binder sees missing, so its refusal says nothing here.
        $binding.failed = $false
        $binding.fromPipeline = @(Get-PipelineParameter $Command @($bound.Keys))
    }
    $binding
}

# The command a command's name resolves to in this session, through any alias; $null for one it does not have.
function Get-ResolvedCommand([CommandAst] $Command) {
    $resolved = Get-Command -Name $Command.GetCommandName() -ErrorAction Ignore | Select-Object -First 1
    while ($resolved -is [Management.Automation.AliasInfo]) {
        $resolved = $resolved.ResolvedCommand
    }
    $resolved
}

# Whether PowerShell's binder decides what a command runs: one that does not resolve, through any alias, to a native
# program. The binder neither refuses nor gives -WhatIf to a command the session does not have.
function Test-Bindable([CommandAst] $Command) {
    (Get-ResolvedCommand $Command) -isnot [Management.Automation.ApplicationInfo]
}

# The parameters a command's metadata lets it take from the pipeline, by value or by property name, by the names it
# declares: each one $Bound, the parameters its arguments bind, leaves unbound, in a parameter set that holds every
# one of those.
function Get-PipelineParameter([CommandAst] $Command, [string[]] $Bound) {
    $resolved = Get-ResolvedCommand $Command
    if ($null -eq $resolved -or $resolved -is [Management.Automation.ApplicationInfo]) {
        return
    }
    $all = '__AllParameterSets'
    $sets = $null
    foreach ($name in $Bound) {
        $parameter = $resolved.Parameters[$name]
        if ($null -ne $parameter -and -not $parameter.ParameterSets.ContainsKey($all)) {
            $own = @($parameter.ParameterSets.Keys)
            $sets = if ($null -eq $sets) { $own } else { @($sets | Where-Object { $_ -in $own }) }
        }
    }
    foreach ($parameter in $resolved.Parameters.Values) {
        if ($parameter.Name -in $Bound) {
            continue
        }
        foreach ($set in $parameter.ParameterSets.GetEnumerator()) {
            $open = $set.Key -eq $all -or $null -eq $sets -or $set.Key -in $sets
            if ($open -and ($set.Value.ValueFromPipeline -or $set.Value.ValueFromPipelineByPropertyName)) {
                $parameter.Name
                break
            }
        }
    }
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
# process, and PSVariable.Set on a provider path. $Member is the method's name, as Get-MemberName reads it.
function Add-MethodEvent([InvokeMemberExpressionAst] $Node, [string] $Member, [Collections.Generic.List[object]] $Events) {
    $arguments = @($Node.Arguments)
    $name = $arguments | Select-Object -First 1
    if ($Member -eq 'SetEnvironmentVariable') {
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
    } elseif ($Member -eq 'Set' -and $Node.Expression.Extent.Text -match 'PSVariable$') {
        if ($name -is [StringConstantExpressionAst]) {
            $Events.Add([ordered]@{ kind = 'assigned'; path = $name.Value; removed = $false })
        } else {
            $Events.Add([ordered]@{ kind = 'opaque' })
        }
    }
}

# An expression with the parentheses around it taken off, as in ([scriptblock]) or ('Create').
function Get-Unwrapped([Ast] $Expression) {
    while ($Expression -is [ParenExpressionAst] -and $Expression.Pipeline -is [PipelineAst] -and
        $Expression.Pipeline.PipelineElements.Count -eq 1 -and
        $Expression.Pipeline.PipelineElements[0] -is [CommandExpressionAst]) {
        $Expression = $Expression.Pipeline.PipelineElements[0].Expression
    }
    $Expression
}

# The name of the member a member expression reaches: a constant, in parentheses or not, as in .Create or
# .('Create'); $null when the text does not settle it.
function Get-MemberName([MemberExpressionAst] $Node) {
    $member = Get-Unwrapped $Node.Member
    if ($member -is [StringConstantExpressionAst]) {
        $member.Value
    }
}

# The type a type literal names, as in [scriptblock]; $null for one this session cannot resolve, or one that names
# its assembly, which is never loaded to read a command.
function Resolve-TypeName([ITypeName] $TypeName) {
    if (-not $TypeName.AssemblyName) {
        $TypeName.GetReflectionType()
    }
}

# The type an expression names, for a static member it reaches: a type literal, or a constant cast to a type, as in
# [type]'scriptblock', through parentheses; $null when the text does not settle one.
function Get-NamedType([Ast] $Expression) {
    $Expression = Get-Unwrapped $Expression
    if ($Expression -is [TypeExpressionAst]) {
        Resolve-TypeName $Expression.TypeName
    } elseif ($Expression -is [ConvertExpressionAst] -and (Resolve-TypeName $Expression.Type.TypeName) -eq [type]) {
        $name = Get-Unwrapped $Expression.Child
        if ($name -is [StringConstantExpressionAst] -and -not $name.Value.Contains(',')) {
            $name.Value -as [type]
        }
    }
}

# The type of an automatic variable whose value is part of PowerShell's engine, by its name with any scope or the
# variable: drive; $null for any other.
function Get-AutomaticType([VariableExpressionAst] $Variable) {
    $path = $Variable.VariablePath
    if ($path.IsDriveQualified -and $path.DriveName -ne 'variable') {
        return $null
    }
    switch ($path.UserPath -replace '^(global|script|local|private|variable):', '') {
        'ExecutionContext' { $ExecutionContext.GetType() }
        'Host' { $Host.GetType() }
        'MyInvocation' { [Management.Automation.InvocationInfo] }
        'PSCmdlet' { [Management.Automation.PSCmdlet] }
    }
}

# The members a member expression reaches, looked up by its name on the type of its receiver, static or not.
function Get-ReachedMember([MemberExpressionAst] $Node, [type] $Receiver, [string] $Name) {
    $flags = [Reflection.BindingFlags]'Public, IgnoreCase, FlattenHierarchy'
    $flags = $flags -bor $(if ($Node.Static) { [Reflection.BindingFlags]::Static } else { [Reflection.BindingFlags]::Instance })
    @($Receiver.GetMember($Name, $flags))
}

# The type of the receiver a member expression reaches its member on: the type it names for a static member, and the
# type of its value otherwise; $null when the text does not settle it.
function Get-ReceiverType([MemberExpressionAst] $Node) {
    if ($Node.Static) {
        Get-NamedType $Node.Expression
    } else {
        Get-ValueType $Node.Expression
    }
}

# The type of an expression's value as the text settles it, through parentheses, casts, the engine's automatic
# variables, and each property, field, or method whose type reflection settles; $null otherwise.
function Get-ValueType([Ast] $Expression) {
    $Expression = Get-Unwrapped $Expression
    if ($Expression -is [TypeExpressionAst]) {
        return [type]
    }
    if ($Expression -is [ConvertExpressionAst]) {
        return Resolve-TypeName $Expression.Type.TypeName
    }
    if ($Expression -is [VariableExpressionAst]) {
        return Get-AutomaticType $Expression
    }
    if ($Expression -isnot [MemberExpressionAst]) {
        return $null
    }
    $receiver = Get-ReceiverType $Expression
    $name = Get-MemberName $Expression
    if ($null -eq $receiver -or $null -eq $name) {
        return $null
    }
    $members = Get-ReachedMember $Expression $receiver $name
    $types = @(foreach ($member in $members) {
            if ($Expression -is [InvokeMemberExpressionAst]) {
                if ($member -is [Reflection.MethodInfo]) { $member.ReturnType } else { $null }
            } elseif ($member -is [Reflection.PropertyInfo]) {
                $member.PropertyType
            } elseif ($member -is [Reflection.FieldInfo]) {
                $member.FieldType
            } else {
                $null
            }
        }) | Select-Object -Unique
    if ($types.Count -eq 1) {
        $types[0]
    }
}

# Whether a type is part of PowerShell's engine or of reflection, whose members can run text as commands or reach
# one that does: a type in System.Management.Automation, Microsoft.PowerShell, or System.Reflection, a Type, and
# Activator and AppDomain.
function Test-EngineType([type] $Type) {
    $Type.Namespace -match '^(System\.Management\.Automation|Microsoft\.PowerShell|System\.Reflection)(\.|$)' -or
    [type].IsAssignableFrom($Type) -or $Type -eq [Activator] -or $Type -eq [AppDomain]
}

# Whether an expression is the receiver of the member expression around it, as $ExecutionContext is in
# $ExecutionContext.SessionState.
function Test-Receiver([Ast] $Expression) {
    $Expression.Parent -is [MemberExpressionAst] -and [object]::ReferenceEquals($Expression.Parent.Expression, $Expression)
}

# The events of a member a command reaches, called or not, as in [scriptblock]::Create.Invoke(...), read by one
# resolver that fails closed: the member's name and its receiver's type, through parentheses and casts. On a type of
# the engine or of reflection (Test-EngineType), each method, and each property whose value is such a type that the
# command keeps rather than reaching a member of, is unread, since it may run text the parser never saw as a command
# or hand on what does; PSVariable.Set goes on to Add-MethodEvent, which reads it. A member whose name the text does
# not settle, on a receiver of such a type or one it does not settle, and a static member of a type it does not
# settle, are unread too. On a receiver the text does not type, as a variable's value, the members that build a
# script block from a string or run a string as commands are unread by name. A method call on any other receiver
# goes on to Add-MethodEvent.
function Add-MemberEvent([MemberExpressionAst] $Node, [Collections.Generic.List[object]] $Events) {
    $name = Get-MemberName $Node
    $receiver = Get-ReceiverType $Node
    $unread = $null
    if ($null -eq $name) {
        if ($null -eq $receiver -or (Test-EngineType $receiver)) {
            $unread = "a member the text does not name ($($Node.Member.Extent.Text))"
        }
    } elseif ($null -eq $receiver) {
        $fromText = @(
            'NewScriptBlock', 'InvokeScript', 'ExpandString', 'AddScript', 'AddCommand', 'GetScriptBlock',
            'CreateNestedPipeline', 'CreatePipeline', 'ParseInput', 'ParseFile'
        )
        if ($Node.Static) {
            $unread = "a member of a type the text does not settle ($name)"
        } elseif ($name -in $fromText) {
            $unread = "a script block built from a string ($name)"
        } elseif ($Node -is [InvokeMemberExpressionAst]) {
            Add-MethodEvent $Node $name $Events
        }
    } elseif (Test-EngineType $receiver) {
        $members = Get-ReachedMember $Node $receiver $name
        $data = @($members | Where-Object { $_ -is [Reflection.PropertyInfo] -or $_ -is [Reflection.FieldInfo] })
        $kept = -not (Test-Receiver $Node) -and ($null -eq ($type = Get-ValueType $Node) -or (Test-EngineType $type))
        if ($receiver -eq [Management.Automation.PSVariableIntrinsics] -and $name -eq 'Set' -and
            $Node -is [InvokeMemberExpressionAst]) {
            Add-MethodEvent $Node $name $Events
        } elseif ($Node -is [InvokeMemberExpressionAst] -or $data.Count -eq 0 -or $data.Count -ne $members.Count) {
            $unread = "a method of PowerShell's engine ($($receiver.Name).$name)"
        } elseif ($kept) {
            $unread = "an object of PowerShell's engine the command keeps ($($receiver.Name).$name)"
        }
    } elseif ($Node -is [InvokeMemberExpressionAst]) {
        Add-MethodEvent $Node $name $Events
    }
    if ($null -ne $unread) {
        $Events.Add([ordered]@{ kind = 'unread'; text = $unread })
    }
}

# The events of a reference to an automatic variable whose value is part of PowerShell's engine, as $ExecutionContext,
# that is not the receiver of a member, whose own events read that: the command keeps or hands on the engine object.
function Add-VariableEvent([VariableExpressionAst] $Node, [Collections.Generic.List[object]] $Events) {
    if (-not (Test-Receiver $Node)) {
        $Events.Add([ordered]@{ kind = 'unread'; text = "an object of PowerShell's engine the command keeps (`$$($Node.VariablePath.UserPath))" })
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
            failed = $binding.failed; whatIf = $binding.whatIf; fromPipeline = $binding.fromPipeline
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
        # Its words go with it, so hub_guard.py reads where -WorkingDirectory starts it.
        $Events.Add([ordered]@{ kind = 'open'; region = 'child'; name = $leaf; words = $words.ToArray() })
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
            $node -is [CommandAst] -or $node -is [AssignmentStatementAst] -or $node -is [MemberExpressionAst] -or
            ($node -is [VariableExpressionAst] -and $node.Parent -is [ForEachStatementAst] -and
            [object]::ReferenceEquals($node.Parent.Variable, $node)) -or
            ($node -is [VariableExpressionAst] -and $null -ne (Get-AutomaticType $node))
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
        } elseif ($node -is [MemberExpressionAst]) {
            Add-MemberEvent $node $Events
        } elseif ($node -is [VariableExpressionAst] -and $node.Parent -isnot [ForEachStatementAst]) {
            Add-VariableEvent $node $Events
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
