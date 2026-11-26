"""Bash command text read by Bash's own grammar: the repository's one Bash grammar, which the hub guard in
tools/hub_guard.py reads a command through, git_subcommands and audit-skill a skill's script, and validation's
policies every script, fence, and template they read Bash in.

`parse` splits a command into words and operators by Bash's quoting rules (single and double quotes, `$'...'`,
backslash escapes and line continuations, comments, and here-documents, whose bodies are data and never commands)
and builds the tree Bash runs: lists, `&&` and `||`, pipelines, subshells, groups, `if`, `while`, `until`, `for`,
`select`, `case`, `[[ ]]`, `(( ))`, function definitions, and simple commands with the assignments before their name and
their redirections, each of which may stand before the name, among those assignments, or after it: a command read
with a redirection first, such as `2>/dev/null git status`, is the one read with it last. Every assignment Bash
reads before a name is one: NAME=value and NAME+=value, an element's, NAME[SUBSCRIPT]=value and
NAME[SUBSCRIPT]+=value, whose subscript is read whole, blanks, quotes, and nested brackets and all, as Bash reads it
where an assignment may stand, and an array's, NAME=( ), NAME+=( ), and the same to a subscript, which Bash also
reads among a declaration builtin's arguments. An assignment may stand at a command's start, after an assignment
that stood there, and after redirections that are all the command holds so far, but not after a redirection that
follows a word: there Bash still assigns NAME=value, yet reads a subscript's blank as the word's end and refuses
NAME=( ), as the reader does. A reserved word is one only as a command's first word, never after an assignment or a
redirection. A word keeps its parts apart, so a reader
can tell literal text from a `$NAME` it must look up and from what only running the command settles: a command or
process substitution, arithmetic, a special parameter, a glob, or a brace expansion. The text of every command and
process substitution, and of each one in an unquoted here-document's body, is kept for a reader to read as the
subshell Bash runs it in. A `$( )` body is read as a command list, as Bash 4 and later read it, so a case pattern's
`)` inside it closes the pattern, not the substitution. Bash 3.2, macOS's /bin/bash, ends the substitution at a
pattern's `)` that no `(` opened and runs the rest of the line in the shell around it, as when a `#` after it comments
out the rest, so such a substitution's text says so, `bare_case`, as does a refusal raised in one and a here-document
whose body holds one, and `holds_bare_case` says one stood at any depth of a refused text or a body, for a reader to
refuse what the two Bashes run differently, and `parse` given `bash_3_2` reads
as Bash 3.2 does, for a reader that judges both readings. Each substitution's text keeps the form that opened it.

Every word, operator, and simple command keeps the line it starts on, counted from 1 at each LF alone, as Bash counts
its lines, so a reader can place what it finds on the script's own line. The text of a substitution or of a
here-document's body is a `Source`, which keeps the script line of each of its characters, so a command read from it,
`parse` given it, keeps its line in the script too, after a continuation or a here-document. Bash's own `$LINENO`
gives a command in a substitution, or in an unquoted here-document's body, the line where the substitution or the
command holding the body ends or starts; the reader gives the line its text stands on.

`simple_commands` lists every simple command Bash may run for a text, each substitution's included, read as the
subshell Bash runs it in, and `command_words` gives one's words from its program's name, for a reader that judges
what a script runs; `every_command` lists every command, compound ones and function definitions too, and
`command_bodies` gives the lists a compound command runs, the walk the hub guard reads a command by too.

The reader reads text as a non-interactive Bash started with every `set -o` and `shopt` option at its default reads it,
and decides each option once: OPTIONS_READ, under which Bash reads the same commands whatever its setting;
OPTIONS_REFUSED, under which Bash reads the text after the command that turns it on otherwise, such as extglob,
expand_aliases, or POSIX mode; and NOT_READ, each with why. `parse` reads a command that turns one of OPTIONS_REFUSED
on as the grammar does any other, and `simple_commands` and `every_command` raise NotRead at it (`option_refusal`), so a
reader that judges what a text runs fails closed there, while the hub guard, which reads by `parse` and follows no
option's effect, reads the tree as before.

Every expansion and quoting form Bash's manual names is decided once too: EXPANSIONS_READ, which the reader ends where
Bash ends them, reading every command substitution in them Bash may run; EXPANSIONS_REFUSED, Bash 5.3's `${ command; }`
and `${| command; }`; and NOT_READ, `${!parameter}` and `${parameter@P}`, which run what a variable's value holds. A
form the reader refuses is kept on its word (`Word.refusal`), or an unquoted here-document's body
(`HereDocument.refusal`), and `simple_commands` and `every_command` raise NotRead at the command holding it, as for an
option, while `parse` reads the tree on. A `${ }` ends at the first `}` outside its quotes, its escapes, and the
expansions and substitutions it holds, its single quotes and `$'...'` quoting there in double quotes too, as Bash's
parser reads it. Where Bash expands a `${ }`'s word as a double-quoted string, as `${x:-word}`'s in double quotes, or a
substring's offset and length or a parameter's subscript anywhere, a single quote is text and a substitution between
single quotes runs, so the reader reads a substitution wherever one starts in that word; an array's subscript in an
assignment, and arithmetic, `$(( ))`, `(( ))`, and the deprecated `$[ ]`, which Bash expands the same way, keep their
quoted parentheses and brackets apart and read each substitution in their quotes. A `$((` whose first `)` another does
not follow at once is a command substitution of a subshell, as Bash reads it. Bash puts the text an ANSI-C string spells
into such a word or arithmetic as it reads it, so one there that spells `$` or a backquote is refused, and, in a
double-quoted `${ }`, where Bash puts it unquoted, one spelling any character Bash reads as syntax there. A `$$` there,
the shell's process ID, is read whole, as Bash 4 and later read it; one before a single-quoted string holding a
backslash is refused, since Bash 3.2, and Bash 5.3 in a here-document's body, read the second `$` and the quote as an
ANSI-C string.

Nothing here runs or judges anything. Text Bash itself would refuse, or that this grammar does not cover, raises
BashSyntaxError, and each reader fails closed on it as its own contract says: the guard does not read such a command,
which it allows with a note, as "The guard's contract" in docs/parallel-sessions.md states, and git_subcommands
raises it for its caller to fail.

tests/tools/test_bash_syntax.py holds the reader to Bash's own syntax check, `bash -n`, on every fixture, as the hub
guard's suite does on every Bash command it reads and the deployer's rendering suite on every rendered fragment it
checks for quoting. Where they differ is a limit stated here and pinned there:
- Inside `[[ ]]` the expression's own grammar is not read, so text Bash refuses there, such as `[[ -n ]]`, is read;
  Bash runs none of a command it refuses.
- A coprocess, `coproc [NAME] command`, is read as Bash 4 and later read it, its command as a subshell's, where Bash
  3.2 reads `coproc` as a program's name and refuses a compound command after it.
- Text nested deeper than Python's recursion limit, such as a few hundred `$( )` one inside another, which Bash reads,
  raises NotRead.
- A CR outside quotes is read as a blank, where Bash reads it as a character of the word it stands in, so
  `git push<CR>` is read as git's push and `a<CR>b` as two words.
- A here-document's delimiter line is compared without the CRs that end it, as Git Bash on Windows compares it, so
  `EOF<CR>` closes the body there, and the lines after it are read as the commands Git Bash runs; Bash elsewhere keeps
  the CR and reads them as body text, running none of them. Where those lines do not parse, the whole command goes
  unread, the git call after the real delimiter line included. A delimiter word written before a CR, as in a script with
  CRLF line ends, is read without it, so its `EOF<CR>` line closes the body too, as Bash elsewhere reads it.
- A `for` or `select` loop whose body is a `{ }` group rather than `do ... done`, arithmetic or not, which Bash reads,
  raises NotRead.
- A function's name holding a tilde, as `~f () { :; }`, which Bash defines, is read as one Bash refuses (None).
- A case pattern holding a parenthesis, as `case y in ((a)) :;; esac` or `(a|(b))`, which Bash refuses, is read.
- Bash 3.2 ends a `${ }` at a `}` inside a command substitution it holds, as in `${x:-$(: })}`, and a `$( )` in
  double quotes at a `)` inside a `${ }` it holds, as in `"$(echo ${x+ )})"`, each of which Bash 4 and later read
  whole, as the reader does.
- A substitution in a `${ }`'s word that Bash expands as a double-quoted string is read wherever one starts, so one
  Bash does not run there, as after a backslash, is read too.
- A backslash and newline that split `$(` inside double quotes, a `${ }`'s word, or arithmetic, which Bash joins into
  a command substitution, leave the substitution unread.
- A value Bash runs as code is not seen: a variable's value `${!parameter}` or `${parameter@P}` expands (NOT_READ),
  and one Bash evaluates as arithmetic, in `$(( ))`, `(( ))`, `$[ ]`, a substring's offset or length, or a subscript,
  whose array subscript holds a command substitution, as `y='a[$(cmd)]'; echo $((y))` runs `cmd`.
- A reserved word right after `]]`, as `if [[ -n x ]] then git commit -m x; fi` or `{ [[ -n x ]] }; git commit -m x`,
  which Bash 5.3 runs and Bash 3.2 refuses, raises BashSyntaxError.
- A here-document body's substitution the grammar refuses, or an expansion the body never closes, raises NotRead,
  whether Bash refuses it too or not: in an unquoted body Bash runs whatever a substitution holds, so text the reader
  cannot read there is unread, never text that runs nothing.
- A `]]` word inside the parentheses of an `=~` regex, as `[[ x =~ ( ]] ) ]]; git commit -m x`, which Bash reads, ends
  the expression there, so the text raises BashSyntaxError.
- An option the text does not turn on itself is not seen: one the environment's SHELLOPTS, BASHOPTS, POSIXLY_CORRECT,
  or BASH_COMPAT turns on, one a shell is started with, as `bash -O extglob` or `--posix`, or on a script's first line,
  one a startup file sets, and an interactive shell's, which expands aliases and history by default.
- A set or shopt behind a name the text does not settle, as `"$x" -o posix`, or behind a glob or a brace expansion in
  the command's name or in command's options, as `{set,-k}` or `command -[p] set -k`, or in a text eval or another
  shell runs, which each reader reads as its own text, is read where that text is; and POSIXLY_CORRECT or BASH_COMPAT
  assigned other than before a command, as a declaration builtin's NAME=value, or by ${NAME:=word}, as by a
  declaration that gives no value, a `{NAME}>` redirection, `read`, `printf -v`, a loop, a name reference, or an
  arithmetic assignment, or by ${NAME:=word} in an array assignment's element or inside `$(( ))`, as
  `x=( ${POSIXLY_CORRECT:=1} )`, is not seen.
- A brace expansion whose quoted or escaped text or inner braces split it, as `shopt -s {'extglob',x}`,
  `{x\\,y,extglob}`, or `{{a},extglob}`, is read as the one word its text spells, so set or shopt given one is read as
  given that word.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field

# Characters that end an unquoted word.
METACHARACTERS = frozenset(" \t\r\n|&;()<>")
# Every control and redirection operator, longest first so each is matched whole.
OPERATORS = (
    ";;&",
    "&>>",
    "<<<",
    "<<-",
    ";;",
    ";&",
    "&&",
    "&>",
    "||",
    "|&",
    "<<",
    "<>",
    "<&",
    ">>",
    ">&",
    ">|",
    ";",
    "&",
    "|",
    "<",
    ">",
    "(",
    ")",
    "\n",
)
REDIRECTIONS = frozenset({"<", ">", ">>", ">|", "<>", "<&", ">&", "&>", "&>>", "<<", "<<-", "<<<"})
HERE_DOCUMENTS = frozenset({"<<", "<<-"})
# A word written right before a redirection that names the file descriptor it redirects, as 2 in 2>&1.
DESCRIPTOR = re.compile(r"[0-9]+|\{[A-Za-z_][A-Za-z0-9_]*\}")
CASE_ENDS = frozenset({";;", ";&", ";;&"})
# Words Bash reads as reserved at the start of a command, where each one that does not start a compound command is
# refused. tests/tools/test_bash_syntax.py holds every word Bash's `compgen -k` prints to being one of these, refused
# where a command starts, or named in NOT_READ with why.
RESERVED = frozenset(
    {"!", "{", "}", "if", "then", "elif", "else", "fi", "while", "until", "for", "select", "do", "done", "case"}
    | {"esac", "in", "function", "time", "[[", "]]", "coproc"}
)
# The reserved word of a coprocess, `coproc [NAME] command`, whose command is read as the subshell Bash runs it in, in
# the background; Bash takes a NAME only before a compound command.
COPROCESS = "coproc"
# Every `set -o` and `shopt` option is decided here, keyed as `set -o NAME` or `shopt NAME`, in exactly one of
# OPTIONS_READ, OPTIONS_REFUSED, and NOT_READ, as tests/tools/test_bash_syntax.py holds to each option the validated
# Bash's `set -o` and `shopt -p` print and to pinned lists of the newest Bash's and Git Bash's. The reader reads text as
# a non-interactive Bash started with every option at its default reads it.
# The options under which Bash reads the same words and commands from a text whatever their setting, and runs no
# command but those: each changes only what an expansion produces, what a command does, prints, or exports, when the
# shell stops, or an interactive prompt, its history, or its completion. The test holds Bash's own reading of a corpus,
# as `declare -f` prints it, to be the same with each on and off.
OPTIONS_READ = frozenset(
    {"set -o allexport", "set -o braceexpand", "set -o emacs", "set -o errexit", "set -o errtrace", "set -o functrace"}
    | {"set -o hashall", "set -o history", "set -o ignoreeof", "set -o interactive-comments", "set -o monitor"}
    | {"set -o noclobber", "set -o noexec", "set -o noglob", "set -o nolog", "set -o notify", "set -o nounset"}
    | {"set -o onecmd", "set -o physical", "set -o pipefail", "set -o privileged", "set -o verbose", "set -o vi"}
    | {"shopt array_expand_once", "shopt assoc_expand_once", "shopt autocd", "shopt bash_source_fullpath"}
    | {"shopt cdable_vars", "shopt cdspell", "shopt checkhash", "shopt checkjobs", "shopt checkwinsize"}
    | {"shopt cmdhist", "shopt complete_fullquote", "shopt completion_strip_exe", "shopt direxpand", "shopt dirspell"}
    | {"shopt dotglob", "shopt execfail", "shopt extdebug", "shopt failglob", "shopt force_fignore"}
    | {"shopt globasciiranges", "shopt globskipdots", "shopt globstar", "shopt gnu_errfmt", "shopt histappend"}
    | {"shopt histreedit", "shopt histverify", "shopt hostcomplete", "shopt huponexit", "shopt inherit_errexit"}
    | {"shopt interactive_comments", "shopt lastpipe", "shopt lithist", "shopt localvar_inherit"}
    | {"shopt localvar_unset", "shopt login_shell", "shopt mailwarn", "shopt no_empty_cmd_completion"}
    | {"shopt nocaseglob", "shopt nocasematch", "shopt noexpand_translation", "shopt nullglob"}
    | {"shopt patsub_replacement", "shopt progcomp", "shopt progcomp_alias", "shopt promptvars"}
    | {"shopt restricted_shell", "shopt shift_verbose", "shopt sourcepath", "shopt varredir_close", "shopt xpg_echo"}
)
# The options under which Bash reads the text after the command that turns them on otherwise than the reader does, each
# with how; simple_commands and every_command raise NotRead at a command that turns one on (option_refusal). Each is
# off by default, and turning it off is read.
COMPATIBILITY = "Bash then reads and runs text as the earlier release whose compatibility level it names did"
OPTIONS_REFUSED: dict[str, str] = {
    "set -o histexpand": (
        "with history on too, Bash then replaces each `!` history designator in a line, as `!!`, with an earlier "
        "line's text before it reads the line"
    ),
    "set -o keyword": (
        "Bash then takes every word shaped as an assignment for one, wherever it stands, so `git x=1 push` runs "
        "git's push"
    ),
    "set -o posix": "Bash then reads in POSIX mode, in which it expands aliases, as expand_aliases does",
    "set -o igncr": (
        "Git Bash's: Bash then drops the carriage returns it reads, where the reader reads one outside quotes as a "
        "blank and one inside them as text"
    ),
    "shopt expand_aliases": (
        "Bash then replaces a command's first word that names an alias with the alias's text, which the reader reads "
        "as the program the word names"
    ),
    "shopt extglob": (
        "Bash then reads `?(`, `*(`, `+(`, `@(`, and `!(` patterns as parts of words, so `!(cmd)` is a pattern a "
        "command's name is expanded from, not a negated subshell"
    ),
    **dict.fromkeys((f"shopt compat{level}" for level in (31, 32, 40, 41, 42, 43, 44)), COMPATIBILITY),
}
# The letters by which set turns on an option OPTIONS_REFUSED names, as -k or within a bundle such as -ek.
SET_LETTERS = {"H": "histexpand", "k": "keyword"}
# The variables whose assignment changes how Bash reads as an option OPTIONS_REFUSED does, each with what it does:
# POSIXLY_CORRECT turns on POSIX mode, and BASH_COMPAT sets the compatibility level the compat options set.
OPTION_VARIABLES = {
    "POSIXLY_CORRECT": f"turns on posix: {OPTIONS_REFUSED['set -o posix']}",
    "BASH_COMPAT": f"sets the compatibility level the compat options set: {COMPATIBILITY}",
}
# Every expansion and quoting form Bash's manual names, keyed as the manual writes it, is decided here, in exactly one
# of EXPANSIONS_READ, EXPANSIONS_REFUSED, and NOT_READ, as tests/tools/test_bash_syntax.py holds to a pinned list of the
# newest Bash's and to the `@` transformations the validated Bash takes, running each form with a command substitution
# in each of its places, and in each quote that may hold a `}`, on that Bash.
# The forms the reader ends where Bash ends them, reading every command substitution in them Bash may run: each
# `${ }` form, whose word Bash expands as a double-quoted string in double quotes, or for a substring's offset and
# length anywhere, so a substitution between its single quotes runs there too; the substitutions and arithmetic; a
# process substitution; and the brace, tilde, and pathname expansions and the quoting forms, which run nothing
# themselves.
EXPANSIONS_READ = frozenset(
    {"$parameter", "${parameter}", "${parameter:-word}", "${parameter-word}", "${parameter:=word}"}
    | {"${parameter=word}", "${parameter:?word}", "${parameter?word}", "${parameter:+word}", "${parameter+word}"}
    | {"${parameter:offset}", "${parameter:offset:length}", "${!prefix*}", "${!prefix@}", "${!name[@]}"}
    | {"${!name[*]}", "${#parameter}", "${parameter#word}", "${parameter##word}", "${parameter%word}"}
    | {"${parameter%%word}", "${parameter/pattern/string}", "${parameter//pattern/string}"}
    | {"${parameter/#pattern/string}", "${parameter/%pattern/string}", "${parameter^pattern}"}
    | {"${parameter^^pattern}", "${parameter,pattern}", "${parameter,,pattern}", "${parameter@U}"}
    | {"${parameter@u}", "${parameter@L}", "${parameter@Q}", "${parameter@E}", "${parameter@A}", "${parameter@K}"}
    | {"${parameter@a}", "${parameter@k}", "$(command)", "`command`", "$(< file)", "$((expression))"}
    | {"$[expression]", "<(list)", ">(list)", "{string,string}", "{x..y}", "{x..y..incr}", "~", "~login", "~+"}
    | {"~-", "~N", "~+N", "~-N", "*", "?", "[...]", "$'string'", '$"string"'}
)
# The forms whose commands the reader does not read, each with why; reading text that holds one raises NotRead.
EXPANSIONS_REFUSED: dict[str, str] = {
    "${ command;}": (
        "Bash 5.3 runs the command after `${` and a blank in the current shell, where earlier releases refuse it as a "
        "bad substitution, and the reader does not read its commands"
    ),
    "${| command;}": (
        "Bash 5.3 runs the command after `${|` in the current shell and expands to REPLY, where earlier releases "
        "refuse it as a bad substitution, and the reader does not read its commands"
    ),
}
# The characters after `${` that open the forms EXPANSIONS_REFUSED names.
CURRENT_SHELL_OPENERS = frozenset(" \t\n|")
# The start of a `${ }` whose word is a pattern or that takes none, `${parameter#word}`, `%`, `^`, `,`, their doubled
# forms, and `@`, after a parameter: a name, with a subscript or not, a positional or a special one, after an optional
# `!`. Bash reads the single quotes of such a word as quotes in double quotes too, so a substitution between them does
# not run.
QUOTED_PATTERN = re.compile(r"!?(?:[A-Za-z_]\w*(?:\[[^]]*\])?|[0-9]+|[-*@#?$!])(?:##?|%%?|\^\^?|,,?|@)", re.ASCII)
# A `:` that opens no `:-`, `:=`, `:?`, or `:+`: a substring's offset, which Bash expands as a double-quoted string
# wherever the `${ }` stands, so a substitution between its single quotes runs.
SUBSTRING = re.compile(r":(?![-=?+])")
# A parameter with a subscript, after an optional `!` or `#`: Bash 5.3 expands an indexed array's subscript as
# arithmetic, as a double-quoted string, wherever the `${ }` stands, so a substitution between its single quotes runs.
SUBSCRIPT = re.compile(r"[!#]?[A-Za-z_]\w*\[", re.ASCII)
# An escape of an ANSI-C string, `$'...'`: by a hexadecimal, Unicode, or octal number, or any other; and the letters
# whose escape spells a control character, and `?`, which spells itself, `c` taking the character after it.
ANSI_C_ESCAPE = re.compile(
    r"\\(?:x([0-9A-Fa-f]{1,2})|u([0-9A-Fa-f]{1,4})|U([0-9A-Fa-f]{1,8})|([0-7]{1,3})|.)", re.DOTALL
)
ANSI_C_LETTERS = frozenset("abeEfnrtv?c")
# The characters Bash reads as syntax in a double-quoted `${ }`'s word: with extquote on, as by default, Bash puts the
# text an ANSI-C string there spells into the word as it reads it, so an ANSI-C string spelling one of them is refused.
TRANSLATED_SYNTAX = frozenset("$`\"\\}'")
TRANSLATED_REASON = (
    "an ANSI-C string inside a double-quoted `${ }` spells a character Bash reads as syntax there, as `$`, a quote, or "
    "`}`: with extquote on, as by default, Bash puts the text it spells into the word as it reads it, unquoted, so "
    "what it spells is expanded and may end the expansion"
)
# Each reserved word, `set -o` option, `shopt` option, and expansion form the reader neither reads nor refuses, with
# why.
NOT_READ: dict[str, str] = {
    "${!parameter}": (
        "expands the variable whose name the parameter's value holds, evaluating an array subscript in that value, so "
        "a command substitution the value holds runs, which no reader of the text sees"
    ),
    "${parameter@P}": (
        "expands the parameter's value as a prompt string, running each command substitution the value holds, which "
        "no reader of the text sees"
    ),
    "set -o xtrace": (
        "expands PS4 before each command it traces, running any command substitution PS4's value holds, which the "
        "reader reads as the text of an assignment"
    ),
    "shopt extquote": (
        "decides whether Bash puts the text an ANSI-C or locale string inside a double-quoted `${ }` spells into the "
        "word as it reads it, as by default, or leaves the string to expand as text; the reader ends the expansion "
        "where Bash does under either setting and refuses an ANSI-C string there that spells a character Bash reads "
        "as syntax, but follows neither setting's text"
    ),
}
NAME = re.compile(r"[A-Za-z_]\w*", re.ASCII)
# The start of an expansion that assigns, ${NAME:=word} or ${NAME=word}, to a subscript or not, or through ${!NAME.
ASSIGNING = re.compile(r"\$\{(!?)([A-Za-z_]\w*(?:\[.*?\])?):?=", re.ASCII)
# A subscript after a name where Bash reads no assignment, whose `[ ]` holds only plain characters of one word.
PLAIN_SUBSCRIPT = re.compile(r"\[[^]\s|&;()<>'\"\\$`]*\]")
# The reserved words after which the next word starts a command, where Bash reads an assignment.
COMMAND_OPENERS = frozenset({"!", "{", "if", "then", "elif", "else", "while", "until", "do", "time", "coproc"})
# The options Bash reads after the `time` keyword, each once, unquoted, and in this order, before the pipeline it times,
# with the words each may follow: -p right after `time`, and -- right after `time` or its -p. Any other word there, a
# second -p or -- among them, is the timed command's own.
TIME_OPTIONS = {"-p": ("time",), "--": ("time", "-p")}
# Unquoted text Bash expands into other words when the command runs: a glob, which names the files it matches, and a
# brace expansion, `{a,b}` or `{1..3}`.
EXPANDS_TO_WORDS = re.compile(r"[*?]|\[.*\]|\{[^{}]*(?:,|\.\.)[^{}]*\}")
# The builtins whose arguments Bash reads as assignments, so NAME=( ) among them is an array.
DECLARATION_BUILTINS = frozenset({"alias", "declare", "eval", "export", "let", "local", "readonly", "typeset"})
# The declaration builtins that assign the variable a NAME=value argument names, as Bash assigns one before a command.
ASSIGNING_BUILTINS = DECLARATION_BUILTINS - {"alias", "eval", "let"}
# What may follow a function's name and `()`: a compound command, as Bash requires of a function's body.
FUNCTION_BODIES = frozenset({"{", "if", "while", "until", "for", "select", "case", "[["})
SPECIAL_PARAMETERS = frozenset("@*#?$!-0123456789")
# The texts of the "dynamic" parts that name only their form, never the text they stand for: a command or process
# substitution, arithmetic, the deprecated `$[ ]` among it, an ANSI-C string holding an escape, and an array
# assignment's value.
FORM_ONLY = frozenset({"$(", "`", "<(", ">(", "$((", "$[", "$'", "(", "(("})
# What a double quote's backslash escapes; before any other character the backslash is kept.
DOUBLE_QUOTED_ESCAPES = frozenset('$`"\\\n')


class BashSyntaxError(ValueError):
    """Text Bash would refuse, or that this grammar does not read; `bare_case` when a `$( )` it refuses holds a case
    pattern whose `)` no `(` opened, which Bash 3.2 takes for the substitution's end and so may run as written."""

    bare_case = False
    # Whether any substitution the text holds, at any depth, held such a pattern, read or not before the refusal.
    holds_bare_case = False


class NotRead(BashSyntaxError):
    """Text Bash may read and run that this grammar does not read, such as a loop whose body is a `{ }` group or a
    here-document body's substitution the grammar refuses: never text a reader may take to run nothing."""


class Source(str):
    """Text the reader takes from a script, a substitution's or a here-document's body, which keeps the script line
    each of its characters stands on, and one more for its end: `lines[index]` is the line of `self[index]`, and
    `lines[len(self)]` the line its end stands on, and, for a substitution's text, the `form` that opened it: `$(`,
    `` ` ``, `<(`, or `>(`, and whether it is a `$( )` or process substitution's body holding a case pattern whose `)`
    no `(` opened, `bare_case`, which Bash 3.2 reads otherwise. It compares and hashes as the plain text it holds."""

    lines: tuple[int, ...]
    form: str
    bare_case: bool

    def __new__(cls, text: str, lines: Sequence[int], form: str = "") -> Source:
        source = super().__new__(cls, text)
        source.lines = tuple(lines)
        source.form = form
        source.bare_case = False
        return source


def line_numbers(text: str) -> tuple[int, ...]:
    """The line of each character of text, and of its end, counted from 1 at each LF alone, as Bash counts them; an LF
    stands on the line it ends."""
    numbers: list[int] = []
    line = 1
    for character in text:
        numbers.append(line)
        line += character == "\n"
    numbers.append(line)
    return tuple(numbers)


@dataclass(frozen=True)
class Part:
    """A piece of a word: literal text, quoted or not, a `$NAME` to look up, a leading `~`, or what only running
    settles. A dynamic part's text names its form, for a reader to decide on: `$(`, `` ` ``, `<(`, or `>(` for a
    command or process substitution, `$((` for arithmetic, `$[` for the deprecated arithmetic `$[ ]`, `$` and the
    character for a special parameter, `${` and the whole of its braces for any other braced expansion, `$'` for an
    ANSI-C string holding an escape, `~` for `~user`, `~+`, or `~-`, and `(` for an array assignment's value."""

    kind: str  # "text", "parameter", "home", or "dynamic"
    text: str = ""
    quoted: bool = False


@dataclass(frozen=True)
class Assignment:
    """What a word that starts as an assignment assigns: the variable's name, whether it names one element,
    NAME[SUBSCRIPT]=, whether it appends, +=, and the parts of its value."""

    name: str
    element: bool
    appends: bool
    value: tuple[Part, ...]


@dataclass(frozen=True)
class Word:
    raw: str
    parts: tuple[Part, ...]
    # The text of each command or process substitution in the word, which Bash runs before the command.
    substitutions: tuple[str, ...] = ()
    # Whether the word is an array assignment, NAME=( ) or NAME+=( ), to a subscript or not, whose value is its
    # "dynamic" part.
    array: bool = False
    # What the word assigns when it starts as an assignment does, which Bash reads as one before a command's name.
    assignment: Assignment | None = None
    # The variables its expansions assign, ${NAME:=word} and ${NAME=word}, each as NAME or NAME[SUBSCRIPT], or as
    # !NAME for ${!NAME:=word}, which assigns the variable NAME's value names.
    assigns: tuple[str, ...] = ()
    # Whether the word is the whole of an arithmetic command's (( )), or an arithmetic for's header, whose `((` Bash
    # reads where a reserved word may stand; its one part is the "dynamic" `((`.
    arithmetic: bool = False
    # The script line the word starts on; 0 for a word a reader builds.
    line: int = field(default=0, compare=False)
    # Why the reader does not read a form the word holds, as Bash 5.3's `${ command; }`, which simple_commands and
    # every_command raise as NotRead at the command holding the word, while parse reads on; "" for none.
    refusal: str = field(default="", compare=False)

    @property
    def literal(self) -> str | None:
        """The word's text when it is one unquoted literal, as a reserved word or a name must be; else None."""
        if len(self.parts) == 1 and self.parts[0].kind == "text" and not self.parts[0].quoted:
            return self.parts[0].text
        return None


@dataclass
class HereDocument:
    """A here-document's body, filled in when the line that opened it ends."""

    quoted: bool
    strip_tabs: bool
    delimiter: str
    body: str = ""
    substitutions: tuple[str, ...] = ()
    # The variables the body's expansions assign, as a word's assigns holds them.
    assigns: tuple[str, ...] = ()
    # Each expansion in an unquoted body that is not a plain `$NAME`, as a word's dynamic parts hold it.
    expansions: tuple[Part, ...] = ()
    # Whether a delimiter line closed the body, rather than the text's end, which Bash also takes for its end.
    closed: bool = False
    # Whether an unquoted body holds an expansion whose end it never reaches, as `$(echo "a)`, which runs to the body's
    # end, so neither it nor anything after it is among the substitutions, and whether that expansion holds a case
    # pattern whose `)` no `(` opened, as a substitution's text says by its bare_case.
    unclosed: bool = False
    bare_case: bool = False
    # Whether any substitution the body holds, at any depth, holds a case pattern whose `)` no `(` opened.
    holds_bare_case: bool = False
    # Why the reader does not read an expansion of an unquoted body that Bash reads, as Bash 5.3's `${ command; }`, read
    # no further than unclosed is; "" for none.
    refusal: str = ""


@dataclass(frozen=True)
class Operator:
    text: str
    # The descriptor word written right before a redirection, as 2 in 2>&1 or {fd} in {fd}>out, which Bash assigns
    # the descriptor it opens to the variable NAME names.
    descriptor: str = ""
    # The script line the operator, or its descriptor word, starts on.
    line: int = field(default=0, compare=False)


Token = Word | Operator | HereDocument


@dataclass
class Redirect:
    operator: str
    target: Word | None
    here: HereDocument | None = None
    # The descriptor word written before the operator, as its Operator token keeps it; "" for none.
    descriptor: str = ""


@dataclass
class Simple:
    words: list[Word]
    redirects: list[Redirect] = field(default_factory=list)
    # The script line its first word or redirection starts on.
    line: int = 0


@dataclass
class Subshell:
    body: CommandList
    redirects: list[Redirect] = field(default_factory=list)


@dataclass
class Group:
    body: CommandList
    redirects: list[Redirect] = field(default_factory=list)


@dataclass
class If:
    clauses: list[tuple[CommandList, CommandList]]
    otherwise: CommandList | None
    redirects: list[Redirect] = field(default_factory=list)


@dataclass
class Loop:
    """while and until, whose condition runs before each pass, and for and select, which assign name each pass."""

    kind: str
    condition: CommandList | None
    name: str | None
    words: list[Word] | None
    body: CommandList
    redirects: list[Redirect] = field(default_factory=list)
    # An arithmetic for's header, whose words are expanded once, before the first pass.
    header: list[Word] = field(default_factory=list)


@dataclass
class Case:
    word: Word
    branches: list[CommandList]
    redirects: list[Redirect] = field(default_factory=list)
    # Each word of every pattern, which Bash expands, its substitutions and arithmetic included, as it tests it.
    patterns: list[Word] = field(default_factory=list)


@dataclass
class Arithmetic:
    """An arithmetic command, (( )), whose expression is one word: Bash expands its substitutions and runs them."""

    expression: Word
    redirects: list[Redirect] = field(default_factory=list)


@dataclass
class FunctionDefinition:
    """A function's definition: the name Bash defines it by, and its body. Bash reads any word as the name, but when
    it runs the definition it refuses one quoted, escaped, or holding an expansion as not a valid identifier, as in
    `'f' () { :; }` or `function $x { :; }`, and defines nothing; such a name is None, and so is one holding a tilde,
    which Bash defines, a limit the module states."""

    name: str | None
    body: Command


Command = Simple | Subshell | Group | If | Loop | Case | Arithmetic | FunctionDefinition


@dataclass
class Pipeline:
    commands: list[Command]
    negated: bool = False


@dataclass
class AndOr:
    first: Pipeline
    rest: list[tuple[str, Pipeline]] = field(default_factory=list)


@dataclass
class CommandList:
    items: list[tuple[AndOr, bool]] = field(default_factory=list)  # each with whether it runs in the background


# ---------------------------------------------------------------------------------------------------------------
# Words and operators


@dataclass
class OpenCase:
    """A `case` the lexer is inside, so a pattern's `)` closes the pattern, not a `$( )` around it, as Bash 4 and
    later read it: the parenthesis depth it opened at, what it reads next, its "subject" word, then "in", its
    "pattern" list, or a branch's "commands", whether the pattern being read has begun, and whether a `(` opened it."""

    depth: int
    state: str = "subject"
    begun: bool = False
    opened: bool = False


class Lexer:
    """Splits text into tokens; with nested set it reads a `$( )` body as a command list and stops at the parenthesis
    that closes it, which a case pattern's `)` never is. `lines` is the script line of each character of text and of
    its end, as a Source keeps them; by default, text's own."""

    def __init__(
        self,
        text: str,
        start: int = 0,
        nested: bool = False,
        lines: Sequence[int] | None = None,
        bash_3_2: bool = False,
    ) -> None:
        self.text = text
        # Whether a case pattern's `)` inside a `$( )` ends the substitution, as Bash 3.2 reads it.
        self.bash_3_2 = bash_3_2
        self.lines = line_numbers(text) if lines is None else lines
        self.index = start
        self.nested = nested
        self.tokens: list[Token] = []
        self.pending: list[HereDocument] = []
        # Whether the next word is where Bash reads an assignment: a command's first word, or one after its
        # assignments, or after redirections that are all the command holds so far; whether it is where Bash reads a
        # reserved word, which is only a command's first; whether the command so far is only redirections; and
        # whether the next word is a redirection's target, which leaves all that as it was.
        self.command_start = True
        self.reserved = True
        self.redirections_only = True
        self.redirecting = False
        self.naming_function = False
        # Whether the last word read is the `for` keyword, after which `((` opens an arithmetic for's header.
        self.after_for = False
        # Whether the last operator read is a pipe, or the one newline right after a pipe, after which `time` is the
        # time program rather than Bash's keyword, as Bash reads it.
        self.after_pipe = False
        # The descriptor word before the redirection being read, kept for its Operator token, and the line it starts
        # on.
        self.descriptor = ""
        self.descriptor_line = 0
        # The variables the expansions read since the word being read began assign, and why the reader does not read
        # a form among them, as a word's refusal keeps it.
        self.assigned: list[str] = []
        self.refusals: list[str] = []
        # The parentheses open here, which a `)` closes before the one that closes a `$( )` body, and each case open
        # here, innermost last.
        self.depth = 0
        self.cases: list[OpenCase] = []
        # Whether a `$( )` body holds a case pattern whose `)` no `(` opened, which Bash 3.2 takes for the body's end,
        # and whether this text or any substitution in it, at any depth, does.
        self.bare_case = False
        self.holds_bare_case = False

    def peek(self, offset: int = 0) -> str:
        position = self.index + offset
        return self.text[position] if position < len(self.text) else ""

    def run(self) -> list[Token]:
        while self.index < len(self.text):
            character = self.peek()
            if character == "\\" and self.peek(1) == "\n":
                self.index += 2
            elif character in " \t\r":
                self.index += 1
            elif character == "#":
                while self.index < len(self.text) and self.peek() != "\n":
                    self.index += 1
            elif character in "<>" and self.peek(1) == "(":
                self.tokens.append(self.word())
            elif self.text.startswith("((", self.index) and (self.reserved or self.after_for) and self.double_paren():
                pass
            elif character in METACHARACTERS:
                if self.operator():
                    return self.tokens
            else:
                word = self.word(subscripts=self.command_start and not self.redirecting)
                if self.peek() and self.peek() in "<>" and DESCRIPTOR.fullmatch(word.raw):
                    self.descriptor, self.descriptor_line = word.raw, word.line
                else:
                    self.case_word(word)
                    self.follow_word(word)
                    self.tokens.append(word)
        if self.nested:
            raise BashSyntaxError("a command substitution is not closed")
        self.read_here_documents()
        return self.tokens

    def operator(self) -> bool:
        """Reads the operator at the index; True, reading nothing, for the `)` that closes a nested `$( )` body."""
        operator = next(item for item in OPERATORS if self.text.startswith(item, self.index))
        pattern = self.case_operator(operator)
        if self.nested and operator == ")" and self.depth == 0 and not pattern:
            return True
        if not pattern:
            self.depth += {"(": 1, ")": -1}.get(operator, 0)
        line = self.descriptor_line if self.descriptor else self.lines[self.index]
        self.index += len(operator)
        self.tokens.append(Operator(operator, self.descriptor, line))
        self.descriptor = ""
        if operator in REDIRECTIONS:
            self.redirecting = operator not in HERE_DOCUMENTS
            self.command_start = self.command_start and self.redirections_only
            self.reserved = False
        else:
            self.command_start = self.reserved = operator not in CASE_ENDS
            self.redirections_only = True
            piped = len(self.tokens) > 1 and self.tokens[-2] in (Operator("|"), Operator("|&"))
            self.after_pipe = operator in ("|", "|&") or (operator == "\n" and piped)
        if operator == "\n":
            self.read_here_documents()
        elif operator in HERE_DOCUMENTS:
            self.open_here_document(operator)
        return False

    def open_case(self) -> OpenCase | None:
        """The innermost case open at the parenthesis depth being read, or None."""
        return self.cases[-1] if self.cases and self.cases[-1].depth == self.depth else None

    def case_operator(self, operator: str) -> bool:
        """Follows a case through an operator; True for a pattern's own parenthesis, the `(` that may open it or the
        `)` that closes it, which opens or closes no subshell."""
        case = None if self.bash_3_2 else self.open_case()
        if case is None:
            return False
        if case.state == "pattern" and (operator == ")" or (operator == "(" and not case.begun)):
            # Bash 3.2 ends a `$( )` at a pattern's `)` that no `(` opened, so the two Bashes read the body apart.
            self.bare_case = self.bare_case or (operator == ")" and self.nested and not case.opened)
            self.holds_bare_case = self.holds_bare_case or self.bare_case
            case.opened = operator == "("
            case.state, case.begun = ("commands", False) if operator == ")" else ("pattern", True)
            return True
        if case.state == "commands" and operator in CASE_ENDS:
            case.state = "pattern"
        return False

    def case_word(self, word: Word) -> None:
        """Follows a case through a word: `case` where Bash reads a reserved word opens one, and `esac` where a
        pattern or a command may start closes it."""
        case = self.open_case()
        state = case.state if case is not None else ""
        if case is not None and state in ("subject", "in"):
            case.state = "in" if state == "subject" or word.literal != "in" else "pattern"
        elif case is not None and state == "pattern":
            if not case.begun and word.literal == "esac":
                self.cases.pop()
            case.begun = True
        elif self.reserved and not self.redirecting and word.literal in ("case", "esac"):
            if word.literal == "case":
                self.cases.append(OpenCase(self.depth))
            elif case is not None:
                self.cases.pop()

    def follow_word(self, word: Word) -> None:
        """Whether the word after this one starts a command, after a reserved word that opens one, an option of the
        `time` keyword, or a function's name, each where Bash reads a reserved word; or stands where Bash reads an
        assignment, after one that did."""
        if self.redirecting:
            self.redirecting = False
            return
        previous = self.tokens[-1] if self.tokens else None
        follows = TIME_OPTIONS.get(word.literal or "", ())
        keyword = word.literal in COMMAND_OPENERS and not (word.literal == "time" and self.after_pipe)
        opens = keyword or (isinstance(previous, Word) and previous.literal in follows)
        self.after_pipe = False
        started, reserved, naming = self.command_start, self.reserved, self.naming_function
        self.after_for = reserved and word.literal == "for"
        self.reserved = self.redirections_only = naming or (reserved and opens)
        self.command_start = self.reserved or (started and word.assignment is not None)
        # A function's name follows `function`, and a coprocess's may follow `coproc`: the word after either is read
        # where Bash reads a reserved word, as a compound command after a coprocess's NAME is.
        self.naming_function = reserved and not naming and word.literal in ("function", COPROCESS)

    def double_paren(self) -> bool:
        """Reads the `(( ))` at the index, where Bash reads a reserved word or right after `for`, as the one word Bash
        reads it as, an arithmetic command or a for's header, whose text holds no operator, so a `<<` in it shifts;
        False, reading nothing, where its `((` closes with a `)` that another does not follow at once, which Bash
        reads as two subshells' parentheses."""
        start = self.index
        refused = len(self.refusals)
        try:
            found = self.arithmetic(start + 2, quotes=True)
        except BashSyntaxError:
            found = None
        if found is None:
            # Read as two subshells instead, the arithmetic reading's refusals go with it.
            del self.refusals[refused:]
            self.index = start
            return False
        text = self.text[start : self.index]
        assigns = tuple(match.group(1) + match.group(2) for match in ASSIGNING.finditer(text))
        refusal = next(iter(self.refusals[refused:]), "")
        word = Word(
            text,
            (Part("dynamic", "(("),),
            tuple(found),
            assigns=assigns,
            arithmetic=True,
            line=self.lines[start],
            refusal=refusal,
        )
        self.follow_word(word)
        self.tokens.append(word)
        return True

    def open_here_document(self, operator: str) -> None:
        while self.peek() in (" ", "\t"):
            self.index += 1
        if not self.peek() or self.peek() in METACHARACTERS:
            raise BashSyntaxError(f"{operator} names no delimiter")
        start = self.index
        delimiter = self.word()
        raw = self.text[start : self.index]
        quoted = any(character in raw for character in "'\"\\")
        text = "".join(part.text for part in delimiter.parts)
        document = HereDocument(quoted, operator == "<<-", text)
        self.tokens.extend([delimiter, document])
        self.pending.append(document)

    def read_here_documents(self) -> None:
        """Reads each pending body, line by line up to its delimiter; one the text never closes runs to its end, as
        Bash reads it. An unquoted body joins each line its backslash continues to the next, as Bash removes that
        backslash and the LF before it compares a line with the delimiter, so a delimiter after one is body text."""
        for document in self.pending:
            body: list[str] = []
            numbers: list[int] = []
            while self.index < len(self.text):
                line, places = self.next_line()
                while not document.quoted and escapes_its_end(line) and self.index < len(self.text):
                    following, following_places = self.next_line()
                    line, places = line[:-1] + following, places[:-2] + following_places
                compared = line.lstrip("\t") if document.strip_tabs else line
                delimiter = document.delimiter
                if compared == delimiter or compared.rstrip("\r") == delimiter.rstrip("\r"):
                    document.closed = True
                    break
                body.append(compared)
                # The line of each character the body keeps, and of the LF that ends it, which joins it to the next.
                numbers += places[len(line) - len(compared) :]
            document.body = Source("\n".join(body), numbers if body else [self.lines[self.index]])
            if not document.quoted:
                substitutions, assigns, parts, unclosed, holds = expansions(document.body, self.bash_3_2)
                document.unclosed, document.bare_case = unclosed is not None, bool(unclosed and unclosed.bare_case)
                document.refusal = str(unclosed) if isinstance(unclosed, NotRead) else ""
                document.holds_bare_case = holds
                self.holds_bare_case = self.holds_bare_case or holds
                document.substitutions, document.assigns = tuple(substitutions), tuple(assigns)
                document.expansions = tuple(part for part in parts if part.kind == "dynamic")
        self.pending = []

    def next_line(self) -> tuple[str, list[int]]:
        """The text up to the next LF, or to the text's end, read past that LF, with the script line of each of its
        characters and of the LF or the end after it."""
        end = self.text.find("\n", self.index)
        end = len(self.text) if end < 0 else end
        line = self.text[self.index : end]
        numbers = list(self.lines[self.index : end + 1])
        self.index = min(end + 1, len(self.text))
        return line, numbers

    def word(self, subscripts: bool = False) -> Word:
        """One word; with subscripts set it stands where Bash reads an assignment, so a subscript after a name at its
        start is read whole."""
        outer, self.assigned = self.assigned, []
        outer_refusals, self.refusals = self.refusals, []
        try:
            return self.word_parts(subscripts)
        finally:
            self.assigned = outer
            # A word nested in another, as an array's element, refuses the word around it too.
            self.refusals = outer_refusals + self.refusals

    def word_parts(self, subscripts: bool) -> Word:
        start = self.index
        parts: list[Part] = []
        substitutions: list[str] = []
        array = False
        if self.peek() == "~":
            self.tilde(parts)
        head = None if parts else self.assignment_head(subscripts, substitutions)
        value_start = self.index
        if head is None and self.index > start and not parts:
            parts.append(Part("text", self.text[start : self.index]))  # a subscript Bash reads whole, with no = after
        while self.index < len(self.text):
            character = self.peek()
            if character == "(" and head is not None and self.index == value_start:
                self.array_elements(substitutions)
                parts.append(Part("dynamic", "("))
                array = True
            elif character in "<>" and self.peek(1) == "(":
                parts.append(Part("dynamic", character + "("))
                substitutions.append(self.balanced(self.index + 2, character + "("))
            elif character in METACHARACTERS:
                break
            elif character == "\\":
                self.escape(parts)
            elif character == "'":
                parts.append(Part("text", self.single_quoted(self.index + 1), quoted=True))
            elif character == '"':
                self.index += 1
                self.double_quoted(parts, substitutions)
            elif character == "$":
                self.dollar(parts, substitutions, quoted=False)
            elif character == "`":
                substitutions.append(self.backquoted())
                parts.append(Part("dynamic", "`"))
            else:
                add_text(parts, character, quoted=False)
                self.index += 1
        raw = self.text[start : self.index]
        assigns = tuple(self.assigned)
        line = self.lines[start]
        refusal = next(iter(self.refusals), "")
        if head is None:
            return Word(raw, tuple(parts), tuple(substitutions), array, assigns=assigns, line=line, refusal=refusal)
        whole = [Part("text", self.text[start:value_start])]
        for part in parts:
            if part.kind == "text" and len(whole) == 1:
                add_text(whole, part.text, part.quoted)
            else:
                whole.append(part)
        assignment = Assignment(*head, tuple(parts))
        return Word(raw, tuple(whole), tuple(substitutions), array, assignment, assigns, line=line, refusal=refusal)

    def assignment_head(self, subscripts: bool, substitutions: list[str]) -> tuple[str, bool, bool] | None:
        """Reads the start of an assignment, NAME or NAME[SUBSCRIPT] and the = or += after it, and returns the name,
        whether it names an element, and whether it appends; else None, having read only a subscript Bash reads
        whole where an assignment may stand, as Bash reads it there whatever follows."""
        start = self.index
        name = NAME.match(self.text, start)
        if name is None:
            return None
        self.index = name.end()
        element = self.peek() == "["
        if element and subscripts:
            self.subscript(substitutions)
        elif element:
            plain = PLAIN_SUBSCRIPT.match(self.text, self.index)
            if plain is None:
                self.index = start
                return None
            self.index = plain.end()
        appends = self.text.startswith("+=", self.index)
        if appends or self.peek() == "=":
            self.index += 2 if appends else 1
            return name.group(), element, appends
        if not (element and subscripts):
            self.index = start
        return None

    def subscript(self, substitutions: list[str]) -> None:
        """The `[ ]` after a name where Bash reads an assignment, up to the `]` that closes it; blanks and operators
        inside are its own, and quotes, expansions, and nested brackets are read as Bash reads them. Bash 5.3 expands
        an indexed array's subscript as arithmetic, as a double-quoted string, so a substitution between its single
        quotes runs, as one in a `${ }` inside it does."""
        depth = 0
        self.index += 1
        while True:
            character = self.peek()
            if not character:
                raise BashSyntaxError("an array subscript is not closed")
            if character == "\\":
                self.index += 2
            elif character in "'\"":
                substitutions.extend(self.arithmetic_quote(character))
            elif character == "$":
                self.dollar([], substitutions, quoted=True)
            elif character == "`":
                substitutions.append(self.backquoted())
            elif character == "]" and depth == 0:
                self.index += 1
                return
            else:
                depth += {"[": 1, "]": -1}.get(character, 0)
                self.index += 1

    def array_elements(self, substitutions: list[str]) -> None:
        """The ( ) of an array assignment: words apart by blanks, newlines, and comments, up to its close."""
        self.index += 1
        while True:
            character = self.peek()
            if not character:
                raise BashSyntaxError("an array assignment is not closed")
            if character == "\\" and self.peek(1) == "\n":
                self.index += 2
            elif character in " \t\r\n":
                self.index += 1
                if character == "\n":
                    self.read_here_documents()  # a body pending from before the array starts at its first newline
            elif character == "#":
                while self.peek() not in ("", "\n"):
                    self.index += 1
            elif character == ")":
                self.index += 1
                return
            elif character in METACHARACTERS and not (character in "<>" and self.peek(1) == "("):
                raise BashSyntaxError(f"`{character}` inside an array assignment")
            else:
                substitutions.extend(self.word().substitutions)

    def tilde(self, parts: list[Part]) -> None:
        end = self.index + 1
        while end < len(self.text) and self.text[end] not in METACHARACTERS and self.text[end] not in "/'\"\\$`":
            end += 1
        if end == self.index + 1:
            parts.append(Part("home"))
        else:
            parts.append(Part("dynamic", "~"))  # ~user, ~+, and ~- name what only running settles
        self.index = end

    def escape(self, parts: list[Part]) -> None:
        following = self.peek(1)
        self.index += 2
        if following and following != "\n":
            add_text(parts, following, quoted=True)

    def single_quoted(self, start: int) -> str:
        end = self.text.find("'", start)
        if end < 0:
            raise BashSyntaxError("a single quote is not closed")
        self.index = end + 1
        return self.text[start:end]

    def double_quoted(self, parts: list[Part], substitutions: list[str]) -> None:
        while True:
            character = self.peek()
            if not character:
                raise BashSyntaxError("a double quote is not closed")
            if character == '"':
                self.index += 1
                add_text(parts, "", quoted=True)
                return
            if character == "\\" and self.peek(1) in DOUBLE_QUOTED_ESCAPES:
                if self.peek(1) != "\n":
                    add_text(parts, self.peek(1), quoted=True)
                self.index += 2
            elif character == "$":
                self.dollar(parts, substitutions, quoted=True)
            elif character == "`":
                substitutions.append(self.backquoted())
                parts.append(Part("dynamic", "`", quoted=True))
            else:
                add_text(parts, character, quoted=True)
                self.index += 1

    def dollar(self, parts: list[Part], substitutions: list[str], quoted: bool) -> None:
        following = self.peek(1)
        start = self.index
        refused = len(self.refusals)
        found = self.arithmetic(start + 3, quotes=True) if self.text.startswith("$((", start) else None
        if found is None:
            del self.refusals[refused:]  # a substitution of a subshell, its arithmetic reading's refusals going with it
        if found is not None:
            substitutions.extend(found)
            parts.append(Part("dynamic", "$((", quoted=quoted))
        elif following == "(":
            # A `$((` whose first `)` another does not follow at once is a command substitution of a subshell, as
            # Bash reads it.
            substitutions.append(self.balanced(start + 2))
            parts.append(Part("dynamic", "$(", quoted=quoted))
        elif following == "[":
            substitutions.extend(self.arithmetic(start + 2, quotes=True, close="]") or [])
            parts.append(Part("dynamic", "$[", quoted=quoted))
        elif following == "{":
            self.braced(parts, substitutions, quoted)
        elif following == "'" and not quoted:
            text = self.ansi_c_quoted(self.index + 2)
            parts.append(Part("dynamic", "$'", quoted=True) if "\\" in text else Part("text", text, quoted=True))
        elif following == '"' and not quoted:
            self.index += 2
            self.double_quoted(parts, substitutions)
        elif following and NAME.match(following):
            end = self.index + 1
            while (
                end < len(self.text)
                and (self.text[end].isalnum() or self.text[end] == "_")
                and self.text[end].isascii()
            ):
                end += 1
            parts.append(Part("parameter", self.text[self.index + 1 : end], quoted=quoted))
            self.index = end
        elif following and following in SPECIAL_PARAMETERS:
            parts.append(Part("dynamic", "$" + following, quoted=quoted))
            self.index += 2
        else:
            add_text(parts, "$", quoted=quoted)
            self.index += 1

    def balanced(self, start: int, form: str = "$(") -> Source:
        """The body of a `$( )`, `<( )`, or `>( )`, the form that opens before start, read as commands up to its
        close."""
        inner = Lexer(self.text, start, nested=True, lines=self.lines, bash_3_2=self.bash_3_2)
        try:
            inner.run()
        except BashSyntaxError as error:
            error.bare_case = error.bare_case or inner.bare_case
            error.holds_bare_case = error.holds_bare_case or inner.holds_bare_case
            raise
        finally:
            self.holds_bare_case = self.holds_bare_case or inner.holds_bare_case
        self.index = inner.index + 1
        source = Source(self.text[start : inner.index], self.lines[start : inner.index + 1], form)
        source.bare_case = inner.bare_case
        return source

    def arithmetic(self, start: int, quotes: bool = False, close: str = ")") -> list[str] | None:
        """The substitutions in arithmetic from start to the `))` that closes it, or the `]` that closes a `$[ ]`,
        read past; None, where a `)` closes the first parenthesis and another does not follow it at once, which Bash
        reads as a subshell's. With quotes set, as Bash reads `(( ))`, `$(( ))`, and `$[ ]`, a quoted string's
        parentheses are text, so `((echo "))"; x) )` closes nothing, while each substitution in it, single-quoted or
        double-quoted, still runs, as Bash expands the expression as a double-quoted string. A `${ }` inside is read as
        one in double quotes is, so a `)` inside its quotes closes nothing either."""
        found: list[str] = []
        depth = 0
        opening = "(" if close == ")" else "["
        self.index = start
        while True:
            character = self.peek()
            if not character:
                raise BashSyntaxError("an arithmetic expansion is not closed")
            if quotes and character in "'\"":
                found.extend(self.arithmetic_quote(character))
            elif character == "$" and self.peek(1) == "'":
                self.refuse(spelled_expansion([self.ansi_c_quoted(self.index + 2)]))
            elif not self.arithmetic_expansion(found):
                if character == opening:
                    depth += 1
                elif character == close and depth == 0:
                    if close == ")" and self.peek(1) != ")":
                        return None
                    self.index += 2 if close == ")" else 1
                    return found
                elif character == close:
                    depth -= 1
                self.index += 2 if character == "\\" else 1

    def arithmetic_expansion(self, found: list[str]) -> bool:
        """Reads the substitution, `${ }`, or backquote at the index inside arithmetic, adding the commands it runs to
        found; False, reading nothing, where none stands there. `$$`, the shell's process ID, is read whole, so the
        character after it opens nothing, as Bash 4 and later read it."""
        if self.text.startswith("$$", self.index):
            self.index += 2
            self.quote_after_process_id()
            return True
        if self.peek() == "$" and self.peek(1) and self.peek(1) in "({[":
            self.dollar([], found, quoted=True)
            return True
        if self.peek() == "`":
            found.append(self.backquoted())
            return True
        return False

    def quote_after_process_id(self) -> str | None:
        """The text of the single-quoted string at the index, right after a `$$` inside a `${ }` or arithmetic, read
        nothing past, for its caller to read as an ANSI-C string too: Bash 3.2, and Bash 5.3 in a here-document's body,
        read the second `$` and the quote as one, where Bash 4 and later read `$$` whole elsewhere. Refused (refuse)
        where it holds a backslash, by which the two readings may end it apart; None where no closed quote stands
        there."""
        end = self.text.find("'", self.index + 1) if self.peek() == "'" else -1
        if end < 0:
            return None
        text = self.text[self.index + 1 : end]
        if "\\" in text:
            self.refuse(
                "a `$$` before a single-quoted string holding a backslash, inside a `${ }` or arithmetic, which Bash "
                "3.2, and Bash 5.3 in a here-document's body, read as `$` and an ANSI-C string, ending it elsewhere"
            )
        return text

    def refuse(self, reason: str | None) -> None:
        """Records why the reader does not read a form of the word being read, which simple_commands and
        every_command raise as NotRead at the command holding it, while parse reads on; nothing for None."""
        if reason is not None:
            self.refusals.append(reason)

    def arithmetic_quote(self, quote: str) -> list[str]:
        """The substitutions in the quoted string at the index inside arithmetic, read past its closing quote: Bash
        takes the quotes there for text that keeps parentheses apart, and expands the `$( )`, `$(( ))`, `${ }`, and
        backquotes inside either kind; a backslash escapes the next character only between double quotes."""
        found: list[str] = []
        self.index += 1
        while True:
            character = self.peek()
            if not character:
                raise BashSyntaxError("a quote in an arithmetic command is not closed")
            if character == quote:
                self.index += 1
                return found
            if character == "\\" and quote == '"':
                self.index += 2
            elif not self.arithmetic_expansion(found):
                self.index += 1

    def braced(self, parts: list[Part], substitutions: list[str], quoted: bool, nested: bool = False) -> None:
        """The `${ }` at the index, ended where Bash's parser ends it: at the first `}` outside a quoted string, an
        escape, and a nested expansion or substitution. Its quotes quote there in double quotes too, `$'...'` as an
        ANSI-C string; and a word Bash expands as a double-quoted string, as `${x:-word}`'s in double quotes or a
        substring's offset or a subscript anywhere (double_quoted_word), runs each substitution its single quotes hold
        too. Bash 5.3's `${ command; }` and `${| command; }` are refused (refuse), read on to the first `}` as any
        other. With nested set, the variables it assigns are left to the expansion around it."""
        start = self.index + 2
        # Bash joins the lines a backslash continues before it reads the character after `${`.
        opener = start
        while self.text.startswith("\\\n", opener):
            opener += 2
        if self.text[opener : opener + 1] and self.text[opener] in CURRENT_SHELL_OPENERS:
            self.refuse(EXPANSIONS_REFUSED["${| command;}" if self.text[opener] == "|" else "${ command;}"])
        self.index = start
        found: list[str] = []
        ansi_c: list[str] = []
        while self.peek() != "}":
            self.brace_part(found, ansi_c, quoted)
        content = self.text[start : self.index]
        self.index += 1
        if quoted and any(set(ansi_c_text(text)) & TRANSLATED_SYNTAX for text in ansi_c):
            self.refuse(TRANSLATED_REASON)
        if double_quoted_word(content, quoted):
            found = self.double_quoted_expansions(start, ansi_c)
        substitutions.extend(found)
        if not nested:
            # ${NAME:=word} and ${NAME=word} assign, and so does each such expansion nested in the word.
            self.assigned.extend(match.group(1) + match.group(2) for match in ASSIGNING.finditer("${" + content))
        if NAME.fullmatch(content):
            parts.append(Part("parameter", content, quoted=quoted))
        else:
            parts.append(Part("dynamic", "${" + content + "}", quoted=quoted))

    def brace_part(self, found: list[str], ansi_c: list[str], quoted: bool) -> None:
        """Reads the next part of a `${ }` up to its `}`, as braced reads it: a quoted string, an escape, or an
        expansion or substitution, adding the commands a substitution runs to found and each ANSI-C string's text to
        ansi_c, or one plain character."""
        character = self.peek()
        if not character:
            raise BashSyntaxError("a ${ } expansion is not closed")
        if character == "\\":
            self.index += 2
        elif character == "'":
            self.single_quoted(self.index + 1)
        elif character == '"':
            self.index += 1
            self.double_quoted([], found)
        elif self.text.startswith("$$", self.index):
            self.index += 2  # the shell's process ID, read whole as in arithmetic
            if (quote := self.quote_after_process_id()) is not None:
                ansi_c.append(quote)
        elif character == "$" and self.peek(1) == "'":
            ansi_c.append(self.ansi_c_quoted(self.index + 2))
        elif character == "$" and self.peek(1) == "{":
            self.braced([], found, quoted, nested=True)
        elif character == "$" and self.peek(1) and self.peek(1) in "([":
            self.dollar([], found, quoted=True)
        elif character == "`":
            found.append(self.backquoted())
        else:
            self.index += 1

    def double_quoted_expansions(self, start: int, ansi_c: list[str]) -> list[str]:
        """The substitutions of the `${ }` whose text runs from start to the index's `}`, read as Bash expands a word
        there, as a double-quoted string, in which a single quote is text, so each substitution it holds runs. An
        ANSI-C string there that spells `$` or a backquote by an escape is refused (refuse), since Bash puts the text
        it spells there, single-quoted, as it reads the word, and then expands it; and so is an expansion it does not
        close."""
        self.refuse(spelled_expansion(ansi_c))
        end = self.index - 1
        source = Source(self.text[start:end], self.lines[start : end + 1])
        lexer = Lexer(source, lines=source.lines, bash_3_2=self.bash_3_2)
        found: list[str] = []
        # Bash reads the word again as it expands it, where a quote, a backslash, or a `$$` before a substitution may
        # not keep it from running, so a substitution is read wherever one starts.
        try:
            while lexer.index < len(lexer.text):
                if lexer.peek() == "`":
                    found.append(lexer.backquoted())
                elif lexer.peek() == "$" and lexer.peek(1) in ("(", "{", "["):
                    lexer.dollar([], found, quoted=True)
                else:
                    lexer.index += 1
        except BashSyntaxError as error:
            self.refuse(f"a `${{ }}` Bash expands as a double-quoted string is not read ({error})")
        self.holds_bare_case = self.holds_bare_case or lexer.holds_bare_case
        self.refusals.extend(lexer.refusals)
        return found

    def ansi_c_quoted(self, start: int) -> str:
        index = start
        while index < len(self.text):
            if self.text[index] == "\\":
                index += 2
            elif self.text[index] == "'":
                self.index = index + 1
                return self.text[start:index]
            else:
                index += 1
        raise BashSyntaxError("a $' quote is not closed")

    def backquoted(self) -> Source:
        """The command a backquote pair runs, with the backslashes that escape a backquote, `$`, or `\\` removed."""
        index = self.index + 1
        body: list[str] = []
        numbers: list[int] = []
        while index < len(self.text):
            character = self.text[index]
            if character == "\\" and index + 1 < len(self.text) and self.text[index + 1] in "`$\\":
                body.append(self.text[index + 1])
                numbers.append(self.lines[index + 1])
                index += 2
            elif character == "`":
                self.index = index + 1
                return Source("".join(body), [*numbers, self.lines[index]], "`")
            else:
                body.append(character)
                numbers.append(self.lines[index])
                index += 1
        raise BashSyntaxError("a backquote is not closed")


def double_quoted_word(content: str, quoted: bool) -> bool:
    """Whether Bash expands the word of the `${ }` whose text is content as a double-quoted string, where a single
    quote is text: a substring's offset and length, and a parameter's subscript, wherever it stands (SUBSTRING,
    SUBSCRIPT), and in double quotes any word that is not a pattern (QUOTED_PATTERN). Each is taken wherever the text
    may hold one, so a `:` anywhere is read as a substring's."""
    if SUBSTRING.search(content) is not None or SUBSCRIPT.match(content) is not None:
        return True
    return quoted and QUOTED_PATTERN.match(content) is None


def ansi_c_text(text: str) -> str:
    """The text an ANSI-C string spells, as Bash reads `$'...'`, as far as it may hold a character of TRANSLATED_SYNTAX:
    an escape by a hexadecimal, Unicode, or octal number is that character, and `\\\\`, `\\'`, and `\\"` the character
    they escape; any other escape spells a character none of them is, a letter's a control character, or, unknown to
    Bash, its backslash and the character after it, which is left as written."""

    def spelled(match: re.Match[str]) -> str:
        for group, base in ((1, 16), (2, 16), (3, 16), (4, 8)):
            if match.group(group) is not None:
                value = int(match.group(group), base)
                return chr(value) if value < 0x110000 else ""
        escaped = match.group()[1]
        return escaped if escaped in "\\'\"" else "\x00" if escaped in ANSI_C_LETTERS else match.group()

    return ANSI_C_ESCAPE.sub(spelled, text)


def spelled_expansion(ansi_c: list[str]) -> str | None:
    """Why the reader does not read text holding an ANSI-C string of ansi_c, standing where Bash expands it as a
    double-quoted string, in arithmetic or a `${ }`'s word, that spells `$` or a backquote: Bash puts the text it
    spells there, single-quoted, as it reads it, and then expands it, single quotes being text there; None for none."""
    if any(set(ansi_c_text(text)) & set("$`") for text in ansi_c):
        return (
            "an ANSI-C string Bash expands as a double-quoted string, in arithmetic or a `${ }`'s word, spells `$` or "
            "a backquote, which Bash then expands"
        )
    return None


def escapes_its_end(line: str) -> bool:
    """Whether a line ends in an odd count of backslashes, the last of which escapes the LF after it."""
    return (len(line) - len(line.rstrip("\\"))) % 2 == 1


def add_text(parts: list[Part], text: str, quoted: bool) -> None:
    """Appends literal text, joining it to the part before when that is literal text quoted the same way."""
    if parts and parts[-1].kind == "text" and parts[-1].quoted == quoted:
        parts[-1] = Part("text", parts[-1].text + text, quoted)
    else:
        parts.append(Part("text", text, quoted))


def expansions(
    text: str, bash_3_2: bool = False
) -> tuple[list[str], list[str], list[Part], BashSyntaxError | None, bool]:
    """The command substitutions in text that expands as a double-quoted string does, such as an unquoted
    here-document's body, where a double quote is an ordinary character but quotes inside a `$( )` or a `${ }`, as
    Bash reads it there, the variables its expansions assign, as a word's assigns holds them, the parts its
    expansions make, as a word's parts hold them, the refusal of an expansion whose end it never reaches, or None, and
    whether any substitution in it, at any depth, holds a case pattern whose `)` no `(` opened. Each substitution keeps
    its script line, from text's when it is a Source. bash_3_2 reads as parse says."""
    lexer = Lexer(text, lines=source_lines(text), bash_3_2=bash_3_2)
    found: list[str] = []
    parts: list[Part] = []
    while lexer.index < len(lexer.text):
        character = lexer.peek()
        try:
            if character == "\\":
                lexer.index += 2
            elif character == "$":
                lexer.dollar(parts, found, quoted=True)
            elif character == "`":
                found.append(lexer.backquoted())
                parts.append(Part("dynamic", "`", quoted=True))
            else:
                lexer.index += 1
        except BashSyntaxError as error:
            # An expansion whose end the body never reaches, as `$(echo "a)` is, runs to the body's end, as Bash
            # reads it, so no expansion stands after it.
            error.holds_bare_case = error.holds_bare_case or lexer.holds_bare_case
            return found, lexer.assigned, parts, error, error.holds_bare_case
    if lexer.refusals:
        # A form the reader does not read refuses the body as an expansion it does not close does.
        refused = NotRead(lexer.refusals[0])
        refused.holds_bare_case = lexer.holds_bare_case
        return found, lexer.assigned, parts, refused, lexer.holds_bare_case
    return found, lexer.assigned, parts, None, lexer.holds_bare_case


# ---------------------------------------------------------------------------------------------------------------
# The command tree


class Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = tokens
        self.index = 0

    def peek(self) -> Token | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def advance(self) -> Token:
        if self.index >= len(self.tokens):
            raise BashSyntaxError("the text ends where a word should stand")
        token = self.tokens[self.index]
        self.index += 1
        return token

    def operator(self) -> str:
        token = self.advance()
        if not isinstance(token, Operator):
            raise BashSyntaxError("an operator is missing")
        return token.text

    def at_operator(self, *texts: str) -> bool:
        token = self.peek()
        return isinstance(token, Operator) and token.text in texts

    def at_reserved(self, *words: str) -> bool:
        token = self.peek()
        return isinstance(token, Word) and token.literal in words

    def expect_reserved(self, word: str) -> None:
        self.skip_separators()
        if not self.at_reserved(word):
            raise BashSyntaxError(f"expected `{word}`")
        self.index += 1

    def skip_newlines(self) -> None:
        while self.at_operator("\n"):
            self.index += 1

    def skip_separators(self) -> None:
        while self.at_operator("\n", ";"):
            self.index += 1

    def command_list(self, terminators: frozenset[str] = frozenset()) -> CommandList:
        """Commands up to the end, a `)`, a case branch's end, or one of the reserved words that ends this list."""
        result = CommandList()
        while True:
            self.skip_newlines()
            token = self.peek()
            if token is None or self.at_operator(")", *CASE_ENDS):
                return result
            if isinstance(token, Word) and token.literal in terminators:
                return result
            and_or = self.and_or()
            background = self.at_operator("&")
            if background or self.at_operator(";", "\n"):
                self.index += 1
            elif (
                self.peek() is not None
                and not self.at_operator(")", *CASE_ENDS)
                and not self.at_terminator(terminators)
            ):
                raise BashSyntaxError("a command is followed by something Bash would not read")
            result.items.append((and_or, background))

    def at_terminator(self, terminators: frozenset[str]) -> bool:
        token = self.peek()
        return isinstance(token, Word) and token.literal in terminators

    def and_or(self) -> AndOr:
        result = AndOr(self.pipeline())
        while self.at_operator("&&", "||"):
            operator = self.operator()
            self.skip_newlines()
            result.rest.append((operator, self.pipeline()))
        return result

    def pipeline(self) -> Pipeline:
        negated = False
        while self.at_reserved("!", "time"):
            word = self.advance()
            negated ^= isinstance(word, Word) and word.literal == "!"
            if isinstance(word, Word) and word.literal == "time":
                for option in TIME_OPTIONS:
                    if self.at_reserved(option):
                        self.index += 1
        result = Pipeline([self.command()], negated)
        while self.at_operator("|", "|&"):
            self.index += 1
            start = self.index
            self.skip_newlines()
            # Bash reads `time` as its keyword only where a pipeline may start, so after a pipe, or the one newline
            # right after one, it is the time program; after more newlines it is the keyword, which Bash refuses there.
            piped = self.at_reserved("time") and self.index - start <= 1
            result.commands.append(self.simple() if piped else self.command())
        return result

    def command(self) -> Command:
        token = self.peek()
        if token is None:
            raise BashSyntaxError("a command is missing")
        if isinstance(token, Operator):
            if token.text in REDIRECTIONS:
                return self.simple()  # a redirection may stand anywhere in a simple command, its name's place too
            if token.text != "(":
                raise BashSyntaxError(f"`{token.text.strip() or 'newline'}` where a command should start")
            self.index += 1
            body = self.command_list()
            if not self.at_operator(")"):
                raise BashSyntaxError("a subshell is not closed")
            self.index += 1
            return Subshell(body, self.redirects())
        if isinstance(token, Word) and token.arithmetic:
            self.index += 1
            return Arithmetic(token, self.redirects())
        compound = COMPOUND_READERS.get(token.literal or "") if isinstance(token, Word) else None
        if compound is not None:
            self.index += 1
            return compound(self)
        if isinstance(token, Word) and token.literal == COPROCESS:
            self.index += 1
            return self.coprocess()
        if isinstance(token, Word) and token.literal in RESERVED - {"[["}:
            raise BashSyntaxError(f"`{token.literal}` where a command should start")
        return self.simple()

    def starts_compound(self, index: int) -> bool:
        """Whether the token at index starts a compound command a coprocess may run: a subshell, an arithmetic
        command, `[[`, or a compound command's reserved word."""
        token = self.tokens[index] if index < len(self.tokens) else None
        if isinstance(token, Operator):
            return token.text == "("
        return isinstance(token, Word) and (
            token.arithmetic or token.literal == "[[" or token.literal in COMPOUND_READERS.keys() - {"function"}
        )

    def coprocess(self) -> Command:
        """`coproc [NAME] command`, read as the subshell Bash runs its command in, in the background: a word before a
        compound command is the coprocess's NAME, as Bash takes it, and any other command is a simple one."""
        token = self.peek()
        if isinstance(token, Word) and token.literal not in RESERVED and self.starts_compound(self.index + 1):
            self.index += 1
        token = self.peek()
        if self.starts_compound(self.index):
            command = self.command()
        elif isinstance(token, Word) and token.literal in RESERVED - {"time"}:
            raise BashSyntaxError(f"`{token.literal}` where a command should start")
        elif token is None or (isinstance(token, Operator) and token.text not in REDIRECTIONS):
            raise BashSyntaxError(f"`{COPROCESS}` names no command")
        else:
            command = self.simple()
        return Subshell(CommandList([(AndOr(Pipeline([command])), False)]))

    def simple(self) -> Command:
        words: list[Word] = []
        redirects: list[Redirect] = []
        first = self.peek()
        line = first.line if isinstance(first, (Word, Operator)) else 0
        conditional = self.at_reserved("[[")
        # Bash reads NAME=( ) among the assignments before a command's name, and among a declaration builtin's
        # arguments, where it reads that name as one; neither after a redirection that follows a word, as it stops
        # reading the words before the name as assignments there, though it still assigns each that is one.
        prefix, declaration, assigning = True, False, True
        while True:
            token = self.peek()
            if token is None:
                break
            if isinstance(token, Operator):
                if conditional:
                    self.index += 1  # inside [[ ]], operators and line breaks are the expression's own, up to `]]`
                    continue
                if token.text in REDIRECTIONS:
                    assigning = assigning and not words
                    redirects.append(self.redirect())
                    continue
                if token.text == "(" and len(words) == 1 and not redirects and words[0].assignment is None:
                    # Bash reads a function's name wherever a command's first word stands, but never an assignment.
                    return self.function_definition(words[0].literal)
                break
            if isinstance(token, HereDocument):
                raise BashSyntaxError("a here-document has no operator")
            if prefix and token.assignment is None:
                prefix, declaration = False, assigning and token.literal in DECLARATION_BUILTINS
            if not ((prefix and assigning) or declaration):
                refuse_array(token)
            words.append(token)
            self.index += 1
            if conditional and token.literal == "]]":
                conditional = False
        if conditional:
            raise BashSyntaxError("a `[[` is not closed")
        return Simple(words, redirects, line)

    def redirect(self) -> Redirect:
        token = self.peek()
        descriptor = token.descriptor if isinstance(token, Operator) else ""
        operator = self.operator()
        target = self.peek()
        if not isinstance(target, Word):
            raise BashSyntaxError(f"`{operator}` names nothing")
        refuse_array(target)
        self.index += 1
        here = None
        if operator in HERE_DOCUMENTS:
            document = self.peek()
            if not isinstance(document, HereDocument):
                raise BashSyntaxError(f"`{operator}` has no body")
            self.index += 1
            here = document
        return Redirect(operator, target, here, descriptor)

    def redirects(self) -> list[Redirect]:
        found: list[Redirect] = []
        while self.at_operator(*REDIRECTIONS):
            found.append(self.redirect())
        return found

    def function_definition(self, name: str | None) -> FunctionDefinition:
        self.index += 1
        if not self.at_operator(")"):
            raise BashSyntaxError("a function definition is not closed")
        self.index += 1
        return FunctionDefinition(name, self.function_body())

    def function_body(self) -> Command:
        self.skip_newlines()
        token = self.peek()
        opens = isinstance(token, Operator) and token.text == "("
        compound = isinstance(token, Word) and (token.literal in FUNCTION_BODIES or token.arithmetic)
        if not opens and not compound:
            raise BashSyntaxError("a function's body is not a compound command")
        return self.command()

    def function_keyword(self) -> Command:
        token = self.peek()
        if not isinstance(token, Word):
            raise BashSyntaxError("function names nothing")
        self.index += 1
        if self.at_operator("("):
            return self.function_definition(token.literal)
        return FunctionDefinition(token.literal, self.function_body())

    def group(self) -> Command:
        body = self.command_list(frozenset({"}"}))
        self.expect_reserved("}")
        return Group(body, self.redirects())

    def if_clause(self) -> Command:
        clauses: list[tuple[CommandList, CommandList]] = []
        otherwise: CommandList | None = None
        while True:
            condition = self.command_list(frozenset({"then"}))
            self.expect_reserved("then")
            clauses.append((condition, self.command_list(frozenset({"elif", "else", "fi"}))))
            if self.at_reserved("elif"):
                self.index += 1
                continue
            if self.at_reserved("else"):
                self.index += 1
                otherwise = self.command_list(frozenset({"fi"}))
            self.expect_reserved("fi")
            return If(clauses, otherwise, self.redirects())

    def while_clause(self, kind: str) -> Command:
        condition = self.command_list(frozenset({"do"}))
        return Loop(kind, condition, None, None, self.do_group(), self.redirects())

    def for_clause(self, kind: str) -> Command:
        token = self.peek()
        if kind == "for" and isinstance(token, Word) and token.arithmetic:
            # for (( ...; ...; ... )): the header is expanded before the first pass, so its substitutions run and its
            # assigning expansions assign; the arithmetic itself assigns no word the guard reads.
            self.index += 1
            return Loop(kind, None, None, None, self.do_group(), self.redirects(), [token])
        name = self.advance()
        if not isinstance(name, Word) or not name.literal or not NAME.fullmatch(name.literal):
            raise BashSyntaxError(f"{kind} names no variable")
        words: list[Word] | None = None
        self.skip_newlines()
        if self.at_reserved("in"):
            self.index += 1
            words = []
            while isinstance(token := self.peek(), Word):
                refuse_array(token)
                words.append(token)
                self.index += 1
        return Loop(kind, None, name.literal, words, self.do_group(), self.redirects())

    def do_group(self) -> CommandList:
        self.skip_separators()
        if self.at_reserved("{"):
            raise NotRead("a loop whose body is a `{ }` group is not read")
        self.expect_reserved("do")
        body = self.command_list(frozenset({"done"}))
        self.expect_reserved("done")
        return body

    def case_clause(self) -> Command:
        word = self.advance()
        if not isinstance(word, Word):
            raise BashSyntaxError("case names no word")
        refuse_array(word)
        self.skip_newlines()
        self.expect_reserved("in")
        branches: list[CommandList] = []
        patterns: list[Word] = []
        while True:
            self.skip_newlines()
            if self.at_reserved("esac"):
                self.index += 1
                return Case(word, branches, self.redirects(), patterns)
            if self.at_operator("("):
                self.index += 1
            while not self.at_operator(")"):
                if self.peek() is None:
                    raise BashSyntaxError("a case pattern is not closed")
                token = self.advance()
                refuse_array(token)
                if isinstance(token, Word):
                    patterns.append(token)
            self.index += 1
            branches.append(self.command_list(frozenset({"esac"})))
            if self.at_operator(*CASE_ENDS):
                self.index += 1


def refuse_array(token: Token) -> None:
    """Refuses NAME=( ) where Bash reads no assignment, as it refuses the `(` there."""
    if isinstance(token, Word) and token.array:
        raise BashSyntaxError("an array assignment where Bash reads no assignment")


COMPOUND_READERS: dict[str, Callable[[Parser], Command]] = {
    "{": Parser.group,
    "if": Parser.if_clause,
    "while": lambda parser: parser.while_clause("while"),
    "until": lambda parser: parser.while_clause("until"),
    "for": lambda parser: parser.for_clause("for"),
    "select": lambda parser: parser.for_clause("select"),
    "case": Parser.case_clause,
    "function": Parser.function_keyword,
}


def source_lines(text: str) -> Sequence[int] | None:
    """The script line of each character of text when it is a Source, else None, for its own."""
    return text.lines if isinstance(text, Source) else None


def parse(text: str, bash_3_2: bool = False) -> CommandList:
    """The command tree of text, read by Bash's grammar; raises BashSyntaxError where it cannot be read. Its lines
    are counted from 1, or, for a Source, are the script's lines it keeps. With bash_3_2 set, a case pattern's `)`
    inside a `$( )` ends the substitution, as Bash 3.2 reads it, for a reader that must judge both readings."""
    lexer = Lexer(text, lines=source_lines(text), bash_3_2=bash_3_2)
    try:
        parser = Parser(lexer.run())
        result = parser.command_list()
        if parser.peek() is not None:
            raise BashSyntaxError("a `)` or `;;` closes nothing")
    except BashSyntaxError as error:
        error.holds_bare_case = error.holds_bare_case or lexer.holds_bare_case
        raise
    except RecursionError:
        # The reader follows nesting by recursion, so nesting deeper than Python's limit is text it cannot read.
        raise NotRead("it nests deeper than the reader follows") from None
    return result


# ---------------------------------------------------------------------------------------------------------------
# What a script runs


def simple_commands(text: str) -> list[Simple]:
    """Each simple command Bash may run for text: every one in its lists, pipelines, and compound commands, a
    function's body included, and every one of each command and process substitution, which Bash runs in a subshell,
    read from wherever it stands: a word, an assignment's value, a redirection's target, an unquoted here-document's
    body, a case word or pattern, or a loop's words. A case word, a pattern, a function's name, and a quoted
    here-document's body run nothing of their own. Raises BashSyntaxError where text, or a substitution in it, cannot
    be read: Bash runs the lines before a syntax error, so a reader cannot say which of its commands run; and NotRead
    where a command turns on an option the reader does not read (option_refusal)."""
    return [command for command in every_command(text) if isinstance(command, Simple)]


def every_command(text: str) -> list[Command]:
    """Each command Bash may run for text, compound or simple, in the order the text holds them, read wherever
    simple_commands reads a simple one: a compound command before the commands it runs, and a function's definition
    before its body. Raises BashSyntaxError where text, or a substitution in it, cannot be read, and NotRead where a
    command turns on an option the reader does not read (option_refusal)."""
    tree = parse(text)
    try:
        return list(_tree_commands(tree))
    except RecursionError:
        # The walk follows each substitution by recursion too, so nesting deeper than Python's limit is text it
        # cannot read.
        raise NotRead("it nests deeper than the reader follows") from None


def _tree_commands(node: CommandList | Command) -> Iterator[Command]:
    if isinstance(node, CommandList):
        for and_or, _ in node.items:
            for pipeline in [and_or.first, *(pipeline for _, pipeline in and_or.rest)]:
                for command in pipeline.commands:
                    yield from _tree_commands(command)
        return
    refusal = option_refusal(node) or next((word.refusal for word in expanded_words(node) if word.refusal), None)
    if refusal is not None:
        raise NotRead(refusal)
    yield node
    if isinstance(node, FunctionDefinition):
        yield from _tree_commands(node.body)
        return
    substitutions = [text for word in expanded_words(node) for text in word.substitutions]
    documents = here_documents(node)
    for here in documents:
        if here.unclosed:
            raise NotRead(here.refusal or "a here-document's body holds an expansion the reader does not close")
    bodies = [document.substitutions for document in documents]
    for text in substitutions:
        yield from _tree_commands(parse(text))
    for document in bodies:
        yield from _body_commands(document)
    for body in command_bodies(node):
        yield from _tree_commands(body)


def _body_commands(substitutions: tuple[str, ...]) -> Iterator[Command]:
    """The commands an unquoted here-document's substitutions run. Bash runs whatever a substitution in the body holds,
    so one the grammar refuses raises NotRead, whether Bash refuses it too or not: the reader cannot tell a refusal of
    its own from Bash's, and text it cannot read is unread, never text that runs nothing."""
    for text in substitutions:
        try:
            tree = parse(text)
        except NotRead:
            raise
        except BashSyntaxError as error:
            raise NotRead(f"a here-document body's substitution is not read ({error})") from error
        yield from _tree_commands(tree)


def settled_text(word: Word) -> str | None:
    """A word's text after quote removal when the text settles it, each of its parts literal text and no unquoted one
    holding a glob or a brace expansion (EXPANDS_TO_WORDS), which Bash makes other words of; else None."""
    if all(part.kind == "text" and (part.quoted or not EXPANDS_TO_WORDS.search(part.text)) for part in word.parts):
        return "".join(part.text for part in word.parts)
    return None


def option_refusal(command: Command) -> str | None:
    """Why the reader does not read the text after a command: a simple one that turns on an option OPTIONS_REFUSED
    names, by set or shopt, behind builtin or command too, or may, given a word the text does not settle where either
    reads an option; or any that assigns a variable OPTION_VARIABLES names, before a simple command's name, as a
    declaration builtin's NAME=value, or by ${NAME:=word} in a word it expands (expanded_words) or an unquoted
    here-document's body. None for any other command, set and shopt that print or test options and turn one off
    included, and for a function's definition, whose body is read as its own command. Bash runs a command only through
    the name it has when the command runs, so a set or shopt behind a name the text does not settle is not read, a
    limit the module states."""
    words = list(command.words) if isinstance(command, Simple) else []
    assigned: list[str] = []
    while words and words[0].assignment is not None:
        assigned.append(words.pop(0).raw)
    words = builtin_words(words)
    program = settled_text(words[0]) if words else None
    if program in ASSIGNING_BUILTINS:
        assigned += [word.raw for word in words[1:] if word.assignment is not None]
    expansions = [
        *(word.assigns for word in expanded_words(command)),
        *(doc.assigns for doc in here_documents(command)),
    ]
    names = [NAME.match(text) for text in assigned + [name for assigns in expansions for name in assigns]]
    for name in (match.group() for match in names if match is not None):
        if name in OPTION_VARIABLES:
            return f"it assigns {name}, which {OPTION_VARIABLES[name]}"
    if program == "set":
        return set_refusal(words[1:])
    if program == "shopt":
        return shopt_refusal(words[1:])
    return None


def builtin_words(words: list[Word]) -> list[Word]:
    """A command's words from the builtin `builtin` or `command` runs, past either, the `--` builtin takes, and
    command's options; none for a command given -v or -V, which only looks a name up."""
    while words and settled_text(words[0]) in ("builtin", "command"):
        runs_options = settled_text(words[0]) == "command"
        words = words[1:]
        if not runs_options and words and settled_text(words[0]) == "--":
            words = words[1:]
        while runs_options and words and (option := settled_text(words[0])) is not None and option[:1] == "-":
            words = words[1:]
            if option == "--":
                break
            if "v" in option or "V" in option:
                return []
    return words


def option_name(key: str) -> str:
    """The option a key of OPTIONS_REFUSED or NOT_READ names, without the command that sets it."""
    return key.rpartition(" ")[2]


def refused_option(key: str) -> str | None:
    """Why turning on the option key names is refused; None for one OPTIONS_REFUSED does not name."""
    reason = OPTIONS_REFUSED.get(key)
    return None if reason is None else f"it turns on {option_name(key)}: {reason}"


def unsettled_option(builtin: str) -> str:
    return f"{builtin} is given a word the text does not settle, which may turn on an option the reader does not read"


def set_refusal(arguments: list[Word]) -> str | None:
    """Why set's arguments are refused, read as set reads them: each word that opens with `-` or `+` up to `--`, `-`,
    or the first that does not, its letters turning an option on after `-` and off after `+`, and an `o` taking the
    next word as an option's name unless that word opens with `-` or `+`, where set prints its options and reads that
    word as its own. Refused: an option OPTIONS_REFUSED names turned on, by name or by its letter (SET_LETTERS), and a
    word the text does not settle where set reads an option or a name."""
    index = 0
    while index < len(arguments):
        text = settled_text(arguments[index])
        if text is None:
            return unsettled_option("set")
        if text in ("-", "--") or text[:1] not in ("-", "+"):
            return None
        on = text[0] == "-"
        for letter in text[1:]:
            if letter == "o" and index + 1 < len(arguments):
                name = settled_text(arguments[index + 1])
                if name is None:
                    return unsettled_option("set")
                if name[:1] in ("-", "+"):
                    continue
                index += 1
                refusal = refused_option(f"set -o {name}") if on else None
            else:
                refusal = refused_option(f"set -o {SET_LETTERS[letter]}") if on and letter in SET_LETTERS else None
            if refusal is not None:
                return refusal
        index += 1
    return None


def shopt_refusal(arguments: list[Word]) -> str | None:
    """Why shopt's arguments are refused, read as shopt reads them: its options first, each word that opens with `-` up
    to `--` or the first that does not, then the names of the options it sets given -s, of set's given -o too. Refused:
    an option OPTIONS_REFUSED names turned on, and any word the text does not settle. A shopt given both -s and -u, or
    an option it does not take, sets nothing, as Bash refuses it."""
    texts = [settled_text(word) for word in arguments]
    if None in texts:
        return unsettled_option("shopt")
    names = [text for text in texts if text is not None]
    letters = ""
    while names and names[0][:1] == "-" and names[0] != "-":
        option = names.pop(0)
        if option == "--":
            break
        letters += option[1:]
    if "s" not in letters or "u" in letters or set(letters) - set("opqs"):
        return None
    keys = (f"set -o {name}" if "o" in letters else f"shopt {name}" for name in names)
    return next((refusal for refusal in map(refused_option, keys) if refusal is not None), None)


def expanded_words(node: Command) -> list[Word]:
    """The words a command expands itself, in the order the text holds them, each redirection's target included: a
    simple command's, a case's word and its patterns, a for or select loop's words and an arithmetic for's header, and
    an arithmetic command's expression; none of a function's definition or of the commands a compound one runs."""
    if isinstance(node, Simple):
        words: list[Word] = list(node.words)
    elif isinstance(node, Case):
        words = [node.word, *node.patterns]
    elif isinstance(node, Loop):
        words = [*(node.words or []), *node.header]
    elif isinstance(node, Arithmetic):
        words = [node.expression]
    else:
        words = []
    if isinstance(node, FunctionDefinition):
        return words
    return words + [redirect.target for redirect in node.redirects if redirect.target is not None]


def here_documents(node: Command) -> list[HereDocument]:
    """The here-documents a command's own redirections read, in the order the text holds them."""
    if isinstance(node, FunctionDefinition):
        return []
    return [redirect.here for redirect in node.redirects if redirect.here is not None]


def command_bodies(node: Command) -> list[CommandList]:
    """The command lists a compound command runs, its conditions included, in the order the text holds them; none for
    a simple command, an arithmetic one, or a function's definition, whose body is one command."""
    if isinstance(node, (Subshell, Group)):
        return [node.body]
    if isinstance(node, If):
        return [part for clause in node.clauses for part in clause] + ([node.otherwise] if node.otherwise else [])
    if isinstance(node, Loop):
        return [node.condition, node.body] if node.condition is not None else [node.body]
    if isinstance(node, Case):
        return list(node.branches)
    return []


def word_text(word: Word) -> str:
    """A word's text after Bash's quote removal, each expansion in it left in the form that names it: `$NAME` for a
    parameter, `~` for a leading tilde, and a dynamic part's own text, such as `$(` for a command substitution or the
    whole of a `${ }` expansion, which no literal of a program's or a subcommand's name matches."""
    texts = {"parameter": "$", "home": "~"}
    return "".join(texts.get(part.kind, "") + part.text for part in word.parts)


def unsettled_part(part: Part) -> bool:
    """Whether a part's text names only its form, FORM_ONLY, never the text it stands for."""
    return part.kind == "dynamic" and part.text in FORM_ONLY


def command_words(command: Simple, unsettled: str | None = None) -> list[str]:
    """The words of a simple command from its program's name, each as word_text gives it: past the assignments Bash
    reads before the name, which run nothing; empty for a command of assignments and redirections alone. Given
    `unsettled`, each part whose text names only its form, FORM_ONLY, such as a substitution, is that text in its
    word instead, for a reader that must not read the form's own text as a script; a special parameter or a `${ }`
    expansion keeps its text."""
    words = list(command.words)
    while words and words[0].assignment is not None:
        words.pop(0)
    if unsettled is None:
        return [word_text(word) for word in words]
    return [
        word_text(
            Word(word.raw, tuple(Part("text", unsettled) if unsettled_part(part) else part for part in word.parts))
        )
        for word in words
    ]
