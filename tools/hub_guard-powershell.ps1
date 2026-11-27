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
#       {"kind": "parameter", "text"}, a parameter, as written, or, with a value attached by a colon,
#         {"kind": "parameter", "text": "-Name:", "argument": element}, or
#       {"kind": "null"}, $null, which a redirection discards its output to, or
#       {"kind": "other", "type"}, any other element, which hub_guard.py does not settle;
#     and a redirection {"kind": "file", "target": element} or {"kind": "merge"}
#   "statements": the top-level statements, each {"pipelines": [[command index]], "operators": ["&&" | "||"],
#     "stages": [[command index]], "blocks": [[command index]], "background": bool, "nested": [command index]},
#     nested naming each command inside the statement's own expressions and bodies, which runs before or within it,
#     and, beside each pipeline, the nested commands inside it, split in two: its stage, those of its arguments'
#     expressions, which PowerShell runs, left to right, once the pipelines before it have run and before any command
#     of its own does, and its blocks, those of the script blocks handed to its own commands, which run while it
#     runs, after an element before them may have moved the location; an assignment, an if, or a foreach has no
#     pipelines of its own; statements is null when one of them is none of these
#   "assigned": each variable an assignment or a foreach assigns, anywhere in the tree, {"name", "qualified"}, the
#     name and whether a drive qualifies it, as env: does, with name null for a target that is no plain variable
#   "members": each member a property access reaches, {"static": bool, "constant": bool}, whether it is a type's
#     static member and whether a constant names it
#   "types": the name of each type a cast or a type expression names, as written
#   "blocks": for each script block, the name of the command it is an argument of, or null for none
#   "fileRedirections": how many file redirections the tree holds, which hub_guard.py holds to those it read
#
# hub_guard.py starts it with -Command rather than -File, so no execution policy applies to it.

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

function ConvertTo-Element([Ast] $Element) {
    if ($Element -is [CommandParameterAst]) {
        if ($null -eq $Element.Argument) {
            return [ordered]@{ kind = 'parameter'; text = $Element.Extent.Text }
        }
        # A value attached with a colon, as -Path:env:GIT_DIR, is reported apart, for hub_guard.py to judge.
        return [ordered]@{
            kind = 'parameter'
            text = "-$($Element.ParameterName):"
            argument = (ConvertTo-Element $Element.Argument)
        }
    }
    if ($Element -is [ScriptBlockExpressionAst]) {
        return [ordered]@{ kind = 'block' }
    }
    if ($Element -is [VariableExpressionAst] -and $Element.VariablePath.UserPath -eq 'null') {
        return [ordered]@{ kind = 'null' }
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
    # Every command carries its redirections, an expression standing as one, as 'x' > out.txt, included.
    $redirections = foreach ($redirection in $Command.Redirections) {
        if ($redirection -is [FileRedirectionAst]) {
            [ordered]@{ kind = 'file'; target = (ConvertTo-Element $redirection.Location) }
        } else {
            [ordered]@{ kind = 'merge' }
        }
    }
    if ($Command -isnot [CommandAst]) {
        return [ordered]@{ expression = $true; redirections = @($redirections) }
    }
    [ordered]@{
        elements = @(foreach ($element in $Command.CommandElements) { ConvertTo-Element $element })
        invocation = "$($Command.InvocationOperator)"
        redirections = @($redirections)
    }
}

# The command a script block is handed to, through a parameter, and a calculated property's hashtable and the list
# holding it; $null for a block handed to no command.
function Get-BlockCommand([ScriptBlockExpressionAst] $Block) {
    $parent = $Block.Parent
    while ($parent -is [CommandParameterAst] -or $parent -is [CommandExpressionAst] -or
        $parent -is [PipelineAst] -or $parent -is [HashtableAst] -or $parent -is [ArrayLiteralAst]) {
        $parent = $parent.Parent
    }
    if ($parent -is [CommandAst]) { $parent } else { $null }
}

# The pipelines of a statement, the operators between them, and, beside each pipeline, the nested commands inside it:
# its stage, the commands of its arguments' expressions, which run before any command of the pipeline does, and its
# blocks, the commands of the script blocks handed to its own commands, which run while the pipeline runs, left to
# right; $null for a statement that is neither a pipeline nor a chain of them.
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
            stages = @($left.stages + $right.stages)
            blocks = @($left.blocks + $right.blocks)
        }
    }
    if ($Statement -is [AssignmentStatementAst] -or $Statement -is [IfStatementAst] -or
        $Statement -is [ForEachStatementAst]) {
        return [ordered]@{ pipelines = @(); operators = @(); stages = @(); blocks = @() }
    }
    if ($Statement -isnot [PipelineAst]) {
        return $null
    }
    $indexes = @(foreach ($element in $Statement.PipelineElements) { $Commands.IndexOf($element) })
    # A script block handed to one of the pipeline's own commands, as ForEach-Object's, runs while the pipeline runs,
    # after an element before it may have moved the location; every other nested command runs before the pipeline.
    $during = [Collections.Generic.HashSet[int]]::new()
    foreach ($block in $Statement.FindAll({ param($node) $node -is [ScriptBlockExpressionAst] }, $true)) {
        if ($indexes -contains $Commands.IndexOf((Get-BlockCommand $block))) {
            foreach ($node in $block.FindAll({ param($node) $node -is [CommandBaseAst] }, $true)) {
                [void] $during.Add($Commands.IndexOf($node))
            }
        }
    }
    $stage = [Collections.Generic.List[int]]::new()
    $blocks = [Collections.Generic.List[int]]::new()
    foreach ($node in $Statement.FindAll({ param($node) $node -is [CommandBaseAst] }, $true)) {
        $index = $Commands.IndexOf($node)
        if ($indexes -contains $index) { continue }
        if ($during.Contains($index)) { $blocks.Add($index) } else { $stage.Add($index) }
    }
    [ordered]@{
        pipelines = @(, $indexes)
        operators = @()
        stages = @(, @($stage))
        blocks = @(, @($blocks))
    }
}

# The commands inside a statement's expressions and bodies: every command in it but the elements of its own
# pipelines, which Get-Chain listed. A chain is the statement's own only where it is the statement; one inside an
# expression, a body, or an assignment's value runs within the statement, so each of its commands is nested.
function Get-Nested([Ast] $Statement, $Chain, [Collections.Generic.List[CommandBaseAst]] $Commands) {
    $own = [Collections.Generic.HashSet[int]]::new()
    foreach ($pipeline in $Chain.pipelines) {
        foreach ($index in $pipeline) { [void] $own.Add($index) }
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
        $chain['nested'] = @(Get-Nested $statement $chain $commands)
        $statements.Add($chain)
    }
}
function ConvertTo-Assigned([Ast] $Target) {
    if ($Target -is [VariableExpressionAst]) {
        return [ordered]@{
            name = $Target.VariablePath.UserPath -replace '^(?i:global|local|script|private):', ''
            qualified = [bool] $Target.VariablePath.IsDriveQualified
        }
    }
    [ordered]@{ name = $null; qualified = $false }
}
$assigned = foreach ($node in $all) {
    if ($node -is [AssignmentStatementAst]) { ConvertTo-Assigned $node.Left }
    elseif ($node -is [ForEachStatementAst]) { ConvertTo-Assigned $node.Variable }
}
$members = foreach ($node in $all) {
    if ($node -is [MemberExpressionAst] -and $node -isnot [InvokeMemberExpressionAst]) {
        [ordered]@{ static = [bool] $node.Static; constant = $node.Member -is [StringConstantExpressionAst] }
    }
}
$types = foreach ($node in $all) {
    if ($node -is [ConvertExpressionAst]) { $node.Type.TypeName.Name }
    elseif ($node -is [TypeExpressionAst]) { $node.TypeName.Name }
}
$blocks = foreach ($node in $all) {
    if ($node -is [ScriptBlockExpressionAst]) {
        $command = Get-BlockCommand $node
        if ($null -ne $command) { , "$($command.GetCommandName())" } else { , $null }
    }
}
[ordered]@{
    nodes = $nodes
    fileRedirections = @($all | Where-Object { $_ -is [FileRedirectionAst] }).Count
    assigned = @($assigned)
    members = @($members)
    types = @($types)
    blocks = @($blocks)
    commands = @(foreach ($command in $commands) { ConvertTo-Command $command })
    # The comma keeps a single statement a list: an if statement's output is otherwise unrolled.
    statements = if ($null -eq $statements) { $null } else { , $statements.ToArray() }
} | ConvertTo-Json -Depth 10 -Compress
