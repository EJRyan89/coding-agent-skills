"""Bash command text read by Bash's own grammar, for the hub guard in tools/hub_guard.py.

`parse` splits a command into words and operators by Bash's quoting rules (single and double quotes, `$'...'`,
backslash escapes and line continuations, comments, and here-documents, whose bodies are data and never commands)
and builds the tree Bash runs: lists, `&&` and `||`, pipelines, subshells, groups, `if`, `while`, `until`, `for`,
`select`, `case`, `[[ ]]`, function definitions, and simple commands with their redirections and the assignments
before their name. Every assignment Bash reads there is one: NAME=value and NAME+=value, an element's,
NAME[SUBSCRIPT]=value and NAME[SUBSCRIPT]+=value, whose subscript is read whole, blanks, quotes, and nested brackets
and all, as Bash reads it where an assignment may stand, and an array's, NAME=( ), NAME+=( ), and the same to a
subscript, which Bash also reads among a declaration builtin's arguments. A word keeps its parts apart, so a reader
can tell literal text from a `$NAME` it must look up and from what only running the command settles: a command or
process substitution, arithmetic, a special parameter, a glob, or a brace expansion. The text of every command and
process substitution, and of each one in an unquoted here-document's body, is kept for a reader to read as the
subshell Bash runs it in.

Nothing here runs or judges anything. Text Bash itself would refuse, or that this grammar does not cover, raises
BashSyntaxError; the guard refuses such a command when it names git.

tests/tools/test_bash_syntax.py holds the reader to Bash's own syntax check, `bash -n`, on every fixture, as the hub
guard's suite does on every Bash command it reads and the deployer's rendering suite on every rendered fragment it
checks for quoting. Where they differ is a limit stated here and pinned there:
- Inside `[[ ]]` the expression's own grammar is not read, so text Bash refuses there, such as `[[ -n ]]`, is read;
  Bash runs none of a command it refuses.
"""

from __future__ import annotations

import re
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
# Words Bash reads as reserved at the start of a command.
RESERVED = frozenset(
    {"!", "{", "}", "if", "then", "elif", "else", "fi", "while", "until", "for", "select", "do", "done", "case"}
    | {"esac", "in", "function", "time", "[["}
)
NAME = re.compile(r"[A-Za-z_]\w*", re.ASCII)
# A subscript after a name where Bash reads no assignment, whose `[ ]` holds only plain characters of one word.
PLAIN_SUBSCRIPT = re.compile(r"\[[^]\s|&;()<>'\"\\$`]*\]")
# The reserved words after which the next word starts a command, where Bash reads an assignment.
COMMAND_OPENERS = frozenset({"!", "{", "if", "then", "elif", "else", "while", "until", "do", "time"})
# The builtins whose arguments Bash reads as assignments, so NAME=( ) among them is an array.
DECLARATION_BUILTINS = frozenset({"alias", "declare", "eval", "export", "let", "local", "readonly", "typeset"})
# What may follow a function's name and `()`: a compound command, as Bash requires of a function's body.
FUNCTION_BODIES = frozenset({"{", "if", "while", "until", "for", "select", "case", "[["})
SPECIAL_PARAMETERS = frozenset("@*#?$!-0123456789")
# What a double quote's backslash escapes; before any other character the backslash is kept.
DOUBLE_QUOTED_ESCAPES = frozenset('$`"\\\n')


class BashSyntaxError(ValueError):
    """Text Bash would refuse, or that this grammar does not read."""


@dataclass(frozen=True)
class Part:
    """A piece of a word: literal text, quoted or not, a `$NAME` to look up, a leading `~`, or what only running
    settles."""

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


@dataclass(frozen=True)
class Operator:
    text: str


Token = Word | Operator | HereDocument


@dataclass
class Redirect:
    operator: str
    target: Word | None
    here: HereDocument | None = None


@dataclass
class Simple:
    words: list[Word]
    redirects: list[Redirect] = field(default_factory=list)


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


@dataclass
class Case:
    word: Word
    branches: list[CommandList]
    redirects: list[Redirect] = field(default_factory=list)


@dataclass
class FunctionDefinition:
    name: str
    body: Command


Command = Simple | Subshell | Group | If | Loop | Case | FunctionDefinition


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


class Lexer:
    """Splits text into tokens; with nested set it reads a `$( )` body and stops at its closing parenthesis."""

    def __init__(self, text: str, start: int = 0, nested: bool = False) -> None:
        self.text = text
        self.index = start
        self.nested = nested
        self.tokens: list[Token] = []
        self.pending: list[HereDocument] = []
        # Whether the next word is where Bash reads an assignment: a command's first word, or one after its
        # assignments and redirections; and whether it is a redirection's target, which leaves that as it was.
        self.command_start = True
        self.redirecting = False
        self.naming_function = False

    def peek(self, offset: int = 0) -> str:
        position = self.index + offset
        return self.text[position] if position < len(self.text) else ""

    def run(self) -> list[Token]:
        depth = 0
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
            elif character in METACHARACTERS:
                operator = next(item for item in OPERATORS if self.text.startswith(item, self.index))
                if self.nested and operator == ")" and depth == 0:
                    return self.tokens
                depth += {"(": 1, ")": -1}.get(operator, 0)
                self.index += len(operator)
                self.tokens.append(Operator(operator))
                if operator in REDIRECTIONS:
                    self.redirecting = operator not in HERE_DOCUMENTS
                else:
                    self.command_start = operator not in CASE_ENDS
                if operator == "\n":
                    self.read_here_documents()
                elif operator in HERE_DOCUMENTS:
                    self.open_here_document(operator)
            else:
                word = self.word(subscripts=self.command_start and not self.redirecting)
                if not (self.peek() and self.peek() in "<>" and DESCRIPTOR.fullmatch(word.raw)):
                    self.follow_word(word)
                    self.tokens.append(word)
        if self.nested:
            raise BashSyntaxError("a command substitution is not closed")
        self.read_here_documents()
        return self.tokens

    def follow_word(self, word: Word) -> None:
        """Whether the word after this one starts a command: after an assignment, a reserved word that opens one,
        `time -p`, or a function's name, where the word itself started one."""
        if self.redirecting:
            self.redirecting = False
            return
        after_time = bool(self.tokens) and isinstance(self.tokens[-1], Word) and self.tokens[-1].literal == "time"
        opens = word.assignment is not None or word.literal in COMMAND_OPENERS or (after_time and word.literal == "-p")
        started, naming = self.command_start, self.naming_function
        self.command_start = naming or (started and opens)
        self.naming_function = started and not naming and word.literal == "function"

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
        Bash reads it."""
        for document in self.pending:
            lines: list[str] = []
            while self.index < len(self.text):
                end = self.text.find("\n", self.index)
                end = len(self.text) if end < 0 else end
                line = self.text[self.index : end]
                self.index = min(end + 1, len(self.text))
                compared = line.lstrip("\t") if document.strip_tabs else line
                if compared.rstrip("\r") == document.delimiter:
                    break
                lines.append(compared)
            document.body = "\n".join(lines)
            if not document.quoted:
                document.substitutions = tuple(expansion_substitutions(document.body))
        self.pending = []

    def word(self, subscripts: bool = False) -> Word:
        """One word; with subscripts set it stands where Bash reads an assignment, so a subscript after a name at its
        start is read whole."""
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
                parts.append(Part("dynamic"))
                array = True
            elif character in "<>" and self.peek(1) == "(":
                substitutions.append(self.balanced(self.index + 2))
                parts.append(Part("dynamic"))
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
                parts.append(Part("dynamic"))
            else:
                add_text(parts, character, quoted=False)
                self.index += 1
        raw = self.text[start : self.index]
        if head is None:
            return Word(raw, tuple(parts), tuple(substitutions), array)
        whole = [Part("text", self.text[start:value_start])]
        for part in parts:
            if part.kind == "text" and len(whole) == 1:
                add_text(whole, part.text, part.quoted)
            else:
                whole.append(part)
        return Word(raw, tuple(whole), tuple(substitutions), array, Assignment(*head, tuple(parts)))

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
        inside are its own, and quotes, expansions, and nested brackets are read as Bash reads them."""
        depth = 0
        self.index += 1
        while True:
            character = self.peek()
            if not character:
                raise BashSyntaxError("an array subscript is not closed")
            if character == "\\":
                self.index += 2
            elif character == "'":
                self.single_quoted(self.index + 1)
            elif character == '"':
                self.index += 1
                self.double_quoted([], substitutions)
            elif character == "$":
                self.dollar([], substitutions, quoted=False)
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
            parts.append(Part("dynamic"))  # ~user, ~+, and ~- name what only running settles
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
                parts.append(Part("dynamic", quoted=True))
            else:
                add_text(parts, character, quoted=True)
                self.index += 1

    def dollar(self, parts: list[Part], substitutions: list[str], quoted: bool) -> None:
        following = self.peek(1)
        if self.text.startswith("$((", self.index):
            substitutions.extend(self.arithmetic(self.index + 3))
            parts.append(Part("dynamic", quoted=quoted))
        elif following == "(":
            substitutions.append(self.balanced(self.index + 2))
            parts.append(Part("dynamic", quoted=quoted))
        elif following == "{":
            self.braced(parts, substitutions, quoted)
        elif following == "'" and not quoted:
            text = self.ansi_c_quoted(self.index + 2)
            parts.append(Part("dynamic", quoted=True) if "\\" in text else Part("text", text, quoted=True))
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
            parts.append(Part("dynamic", quoted=quoted))
            self.index += 2
        else:
            add_text(parts, "$", quoted=quoted)
            self.index += 1

    def balanced(self, start: int) -> str:
        """The body of a `$( )`, `<( )`, or `>( )` that opens before start, read as commands up to its close."""
        inner = Lexer(self.text, start, nested=True)
        inner.run()
        self.index = inner.index + 1
        return self.text[start : inner.index]

    def arithmetic(self, start: int) -> list[str]:
        found: list[str] = []
        depth = 0
        self.index = start
        while True:
            character = self.peek()
            if not character:
                raise BashSyntaxError("an arithmetic expansion is not closed")
            if character == "$" and self.peek(1) == "(" and self.peek(2) != "(":
                found.append(self.balanced(self.index + 2))
            elif character == "`":
                found.append(self.backquoted())
            elif character == "(":
                depth += 1
                self.index += 1
            elif character == ")" and depth == 0:
                if self.peek(1) != ")":
                    raise BashSyntaxError("an arithmetic expansion is not closed")
                self.index += 2
                return found
            else:
                depth -= character == ")"
                self.index += 1

    def braced(self, parts: list[Part], substitutions: list[str], quoted: bool) -> None:
        start = self.index + 2
        self.index = start
        depth = 0
        while True:
            character = self.peek()
            if not character:
                raise BashSyntaxError("a ${ } expansion is not closed")
            if character == "\\":
                self.index += 2
            elif character == "'" and not quoted:
                self.single_quoted(self.index + 1)
            elif character == '"':
                self.index += 1
                self.double_quoted([], substitutions)
            elif character == "$" and self.peek(1) and self.peek(1) in "({":
                if self.peek(1) == "{":
                    depth += 1
                    self.index += 2
                else:
                    self.dollar([], substitutions, quoted=True)
            elif character == "`":
                substitutions.append(self.backquoted())
            elif character == "}" and depth == 0:
                break
            else:
                depth -= character == "}"
                self.index += 1
        content = self.text[start : self.index]
        self.index += 1
        if NAME.fullmatch(content):
            parts.append(Part("parameter", content, quoted=quoted))
        else:
            parts.append(Part("dynamic", quoted=quoted))

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

    def backquoted(self) -> str:
        """The command a backquote pair runs, with the backslashes that escape a backquote, `$`, or `\\` removed."""
        index = self.index + 1
        body: list[str] = []
        while index < len(self.text):
            character = self.text[index]
            if character == "\\" and index + 1 < len(self.text) and self.text[index + 1] in "`$\\":
                body.append(self.text[index + 1])
                index += 2
            elif character == "`":
                self.index = index + 1
                return "".join(body)
            else:
                body.append(character)
                index += 1
        raise BashSyntaxError("a backquote is not closed")


def add_text(parts: list[Part], text: str, quoted: bool) -> None:
    """Appends literal text, joining it to the part before when that is literal text quoted the same way."""
    if parts and parts[-1].kind == "text" and parts[-1].quoted == quoted:
        parts[-1] = Part("text", parts[-1].text + text, quoted)
    else:
        parts.append(Part("text", text, quoted))


def expansion_substitutions(text: str) -> list[str]:
    """The command substitutions in text that expands as a double-quoted string does, such as an unquoted
    here-document's body; a double quote there is an ordinary character."""
    lexer = Lexer(text.replace('"', " "))
    found: list[str] = []
    while lexer.index < len(lexer.text):
        character = lexer.peek()
        if character == "\\":
            lexer.index += 2
        elif character == "$":
            lexer.dollar([], found, quoted=True)
        elif character == "`":
            found.append(lexer.backquoted())
        else:
            lexer.index += 1
    return found


# ---------------------------------------------------------------------------------------------------------------
# The command tree


class Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = tokens
        self.index = 0

    def peek(self) -> Token | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def advance(self) -> Token:
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
            token = self.peek()
            if isinstance(token, Word) and token.literal == "-p":
                self.index += 1
        result = Pipeline([self.command()], negated)
        while self.at_operator("|", "|&"):
            self.index += 1
            self.skip_newlines()
            result.commands.append(self.command())
        return result

    def command(self) -> Command:
        token = self.peek()
        if token is None:
            raise BashSyntaxError("a command is missing")
        if isinstance(token, Operator):
            if token.text != "(":
                raise BashSyntaxError(f"`{token.text.strip() or 'newline'}` where a command should start")
            self.index += 1
            body = self.command_list()
            if not self.at_operator(")"):
                raise BashSyntaxError("a subshell is not closed")
            self.index += 1
            return Subshell(body, self.redirects())
        compound = COMPOUND_READERS.get(token.literal or "") if isinstance(token, Word) else None
        if compound is not None:
            self.index += 1
            return compound(self)
        if isinstance(token, Word) and token.literal in RESERVED - {"[["}:
            raise BashSyntaxError(f"`{token.literal}` where a command should start")
        return self.simple()

    def simple(self) -> Command:
        words: list[Word] = []
        redirects: list[Redirect] = []
        conditional = self.at_reserved("[[")
        # Bash reads NAME=( ) among the assignments before a command's name, and among a declaration builtin's
        # arguments.
        prefix, declaration = True, False
        while True:
            token = self.peek()
            if token is None:
                break
            if isinstance(token, Operator):
                if conditional and token.text != "\n":
                    self.index += 1  # inside [[ ]], operators are the expression's own
                    continue
                if token.text in REDIRECTIONS:
                    redirects.append(self.redirect())
                    continue
                if token.text == "(" and len(words) == 1 and not redirects and words[0].literal:
                    return self.function_definition(words[0].literal)
                break
            if isinstance(token, HereDocument):
                raise BashSyntaxError("a here-document has no operator")
            if prefix and token.assignment is None:
                prefix, declaration = False, token.literal in DECLARATION_BUILTINS
            if not (prefix or declaration):
                refuse_array(token)
            words.append(token)
            self.index += 1
            if conditional and token.literal == "]]":
                conditional = False
        return Simple(words, redirects)

    def redirect(self) -> Redirect:
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
        return Redirect(operator, target, here)

    def redirects(self) -> list[Redirect]:
        found: list[Redirect] = []
        while self.at_operator(*REDIRECTIONS):
            found.append(self.redirect())
        return found

    def function_definition(self, name: str) -> FunctionDefinition:
        self.index += 1
        if not self.at_operator(")"):
            raise BashSyntaxError("a function definition is not closed")
        self.index += 1
        return FunctionDefinition(name, self.function_body())

    def function_body(self) -> Command:
        self.skip_newlines()
        token = self.peek()
        opens = isinstance(token, Operator) and token.text == "("
        if not opens and not (isinstance(token, Word) and token.literal in FUNCTION_BODIES):
            raise BashSyntaxError("a function's body is not a compound command")
        return self.command()

    def function_keyword(self) -> Command:
        token = self.advance()
        if not isinstance(token, Word) or not token.literal:
            raise BashSyntaxError("function names nothing")
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
        if self.at_operator("("):
            # for (( ...; ...; ... )): the arithmetic header assigns no word the guard reads.
            depth = 0
            while True:
                token = self.advance() if self.peek() is not None else None
                if token is None:
                    raise BashSyntaxError("an arithmetic for is not closed")
                if isinstance(token, Operator) and token.text in "()":
                    depth += 1 if token.text == "(" else -1
                    if depth == 0:
                        break
            return Loop(kind, None, None, None, self.do_group(), self.redirects())
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
        while True:
            self.skip_newlines()
            if self.at_reserved("esac"):
                self.index += 1
                return Case(word, branches, self.redirects())
            if self.at_operator("("):
                self.index += 1
            while not self.at_operator(")"):
                if self.peek() is None:
                    raise BashSyntaxError("a case pattern is not closed")
                refuse_array(self.advance())
            self.index += 1
            branches.append(self.command_list(frozenset({"esac"})))
            if self.at_operator(*CASE_ENDS):
                self.index += 1


def refuse_array(token: Token) -> None:
    """Refuses NAME=( ) where Bash reads no assignment, as it refuses the `(` there."""
    if isinstance(token, Word) and token.array:
        raise BashSyntaxError("an array assignment where Bash reads no assignment")


COMPOUND_READERS = {
    "{": Parser.group,
    "if": Parser.if_clause,
    "while": lambda parser: parser.while_clause("while"),
    "until": lambda parser: parser.while_clause("until"),
    "for": lambda parser: parser.for_clause("for"),
    "select": lambda parser: parser.for_clause("select"),
    "case": Parser.case_clause,
    "function": Parser.function_keyword,
}


def parse(text: str) -> CommandList:
    """The command tree of text, read by Bash's grammar; raises BashSyntaxError where it cannot be read."""
    parser = Parser(Lexer(text).run())
    result = parser.command_list()
    if parser.peek() is not None:
        raise BashSyntaxError("a `)` or `;;` closes nothing")
    return result
