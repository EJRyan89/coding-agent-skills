using namespace System.Management.Automation.Language

# The hub guard's PowerShell reader. tools/hub_guard.py starts it to parse one command with PowerShell's own parser
# and prints what the parser found. It judges nothing: every decision is made in hub_guard.py, which understands a
# command only when every node type in its tree is one POWERSHELL_UNDERSTOOD names, and refuses the rest while a
# guarded hub is in view.
#
# Input: base64-encoded UTF-8 of a JSON object on stdin, so no console code page can alter it: "command", the text.
# Output: one JSON object. {"error": message} when PowerShell would refuse to parse the command, and so would run
# none of it; otherwise:
#   "nodes": the name of every node type in the tree, nested script blocks included, each once
#   "commands": every command in the tree, in the order the text holds them, each
#       {"elements": [element], "invocation": "Unknown" | "Ampersand" | "Dot", "redirections": [redirection]}
#     or {"expression": true} for an expression standing as a command; an element is
#       {"kind": "word", "text"}, a constant or a string with no expansion, by its value,
#       {"kind": "words", "texts"}, a list of such, as a,b, each a word of its own,
#       {"kind": "parameter", "text"}, a parameter, as written, or
#       {"kind": "other", "type"}, any other element, which hub_guard.py does not settle;
#     and a redirection {"kind": "file", "target": element} or {"kind": "merge"}
#   "statements": the top-level statements, each {"pipelines": [[command index]], "operators": ["&&" | "||"],
#     "background": bool, "nested": [command index]}, nested naming each command inside the statement's own
#     expressions, which runs before it, and an assignment's also {"assigns": {"name", "qualified"}}, the variable it
#     assigns and whether a drive qualifies it, as env: does, or null when its target is no plain variable; or
#     statements is null when one of them is none of these
#
# hub_guard.py starts it with -Command rather than -File, so no execution policy applies to it.

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

function ConvertTo-Element([Ast] $Element) {
    if ($Element -is [CommandParameterAst]) {
        return [ordered]@{ kind = 'parameter'; text = $Element.Extent.Text }
    }
    if ($Element -is [ArrayLiteralAst]) {
        $texts = foreach ($item in $Element.Elements) {
            if ($item -isnot [StringConstantExpressionAst] -and $item -isnot [ConstantExpressionAst]) {
                return [ordered]@{ kind = 'other'; type = 'ArrayLiteralAst' }
            }
            "$($item.Value)"
        }
        return [ordered]@{ kind = 'words'; texts = @($texts) }
    }
    if ($Element -is [StringConstantExpressionAst] -or $Element -is [ConstantExpressionAst]) {
        return [ordered]@{ kind = 'word'; text = "$($Element.Value)" }
    }
    if ($Element -is [ExpandableStringExpressionAst] -and $Element.NestedExpressions.Count -eq 0) {
        return [ordered]@{ kind = 'word'; text = $Element.Value }
    }
    [ordered]@{ kind = 'other'; type = $Element.GetType().Name }
}

function ConvertTo-Command([CommandBaseAst] $Command) {
    if ($Command -isnot [CommandAst]) {
        return [ordered]@{ expression = $true }
    }
    $redirections = foreach ($redirection in $Command.Redirections) {
        if ($redirection -is [FileRedirectionAst]) {
            [ordered]@{ kind = 'file'; target = (ConvertTo-Element $redirection.Location) }
        } else {
            [ordered]@{ kind = 'merge' }
        }
    }
    [ordered]@{
        elements = @(foreach ($element in $Command.CommandElements) { ConvertTo-Element $element })
        invocation = "$($Command.InvocationOperator)"
        redirections = @($redirections)
    }
}

# The pipelines of a statement and the operators between them, left to right; $null for a statement that is neither
# a pipeline nor a chain of them.
function Get-Chain([Ast] $Statement, [Collections.Generic.List[CommandBaseAst]] $Commands) {
    if ($Statement -is [PipelineChainAst]) {
        $left = Get-Chain $Statement.LhsPipelineChain $Commands
        $right = Get-Chain $Statement.RhsPipeline $Commands
        if ($null -eq $left -or $null -eq $right) {
            return $null
        }
        $operator = if ($Statement.Operator -eq [TokenKind]::AndAnd) { '&&' } else { '||' }
        return [ordered]@{
            pipelines = @($left.pipelines + $right.pipelines)
            operators = @($left.operators + $operator + $right.operators)
        }
    }
    if ($Statement -is [AssignmentStatementAst]) {
        $target = $Statement.Left
        $assigns = if ($target -is [VariableExpressionAst]) {
            [ordered]@{
                name = $target.VariablePath.UserPath -replace '^(?i:global|local|script|private):', ''
                qualified = [bool] $target.VariablePath.IsDriveQualified
            }
        } else { $null }
        return [ordered]@{ pipelines = @(); operators = @(); assigns = $assigns }
    }
    if ($Statement -isnot [PipelineAst]) {
        return $null
    }
    $indexes = foreach ($element in $Statement.PipelineElements) { $Commands.IndexOf($element) }
    [ordered]@{ pipelines = @(, @($indexes)); operators = @() }
}

# The commands inside a statement's own expressions, as the arguments of its commands and an assignment's value
# hold them, which are not the statement's pipeline elements.
function Get-Nested([Ast] $Statement, [Collections.Generic.List[CommandBaseAst]] $Commands) {
    $own = [Collections.Generic.HashSet[int]]::new()
    # An assignment's value is all nested: the commands it runs come before the assignment.
    $pipelines = if ($Statement -is [AssignmentStatementAst]) { @() } else {
        $Statement.FindAll({ param($node) $node -is [PipelineAst] }, $false)
    }
    foreach ($pipeline in $pipelines) {
        if ($pipeline -eq $Statement -or $pipeline.Parent -is [PipelineChainAst]) {
            foreach ($element in $pipeline.PipelineElements) { [void] $own.Add($Commands.IndexOf($element)) }
        }
    }
    foreach ($node in $Statement.FindAll({ param($node) $node -is [CommandBaseAst] }, $true)) {
        $index = $Commands.IndexOf($node)
        if (-not $own.Contains($index)) { $index }
    }
}

$payload = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String([Console]::In.ReadToEnd().Trim())) |
    ConvertFrom-Json
$tokens = $null
$errors = $null
$ast = [Parser]::ParseInput([string] $payload.command, [ref] $tokens, [ref] $errors)
if ($errors.Count -gt 0) {
    [ordered]@{ error = $errors[0].Message } | ConvertTo-Json -Compress
    return
}
$all = $ast.FindAll({ $true }, $true)
$nodes = @($all | ForEach-Object { $_.GetType().Name } | Sort-Object -Unique)
$commands = [Collections.Generic.List[CommandBaseAst]]::new()
foreach ($node in $all) {
    if ($node -is [CommandBaseAst]) {
        $commands.Add($node)
    }
}
$statements = $null
$blocks = @($ast.BeginBlock, $ast.ProcessBlock, $ast.DynamicParamBlock, $ast.ParamBlock) | Where-Object { $null -ne $_ }
if ($blocks.Count -eq 0 -and $null -ne $ast.EndBlock) {
    $statements = [Collections.Generic.List[object]]::new()
    foreach ($statement in $ast.EndBlock.Statements) {
        $chain = Get-Chain $statement $commands
        if ($null -eq $chain) {
            $statements = $null
            break
        }
        $chain['background'] = $statement -is [PipelineBaseAst] -and [bool] $statement.Background
        $chain['nested'] = @(Get-Nested $statement $commands)
        $statements.Add($chain)
    }
}
[ordered]@{
    nodes = $nodes
    commands = @(foreach ($command in $commands) { ConvertTo-Command $command })
    # The comma keeps a single statement a list: an if statement's output is otherwise unrolled.
    statements = if ($null -eq $statements) { $null } else { , $statements.ToArray() }
} | ConvertTo-Json -Depth 10 -Compress
