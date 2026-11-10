"""The one fold of .NET's ordinal comparison ignoring case, which MSBuild, NuGet, and the solution readers compare
names with: a package id, an assembly name, an item or property name, a framework name, or a project or solution
file's name.

.NET compares two characters ignoring case by their simple uppercase, one character for one, so a letter beyond ASCII
matches its own other case, as `é` matches `É`, but never an ASCII letter, and nothing folds onto several characters.
`casefold()` does neither: it folds the sharp s onto `ss`, the long s onto `s`, and the Kelvin sign onto `k`, so two
names .NET keeps apart read as one. A dotnet skill compares such names through `ordinal_fold`, never `casefold()`, and
gives each of its patterns ignoring case `re.ASCII`.
"""

from __future__ import annotations


def simple_uppercase(character: str) -> str | None:
    """A character as .NET's ordinal comparison ignoring case compares it, its simple uppercase, which never maps a
    character beyond ASCII onto an ASCII one. Python gives only the full uppercase: where that is several characters,
    as for the sharp s or a ligature, the simple one is the character itself, except where its titlecase is one
    character, as for a Greek letter with a subscript iota, whose simple uppercase this cannot tell, so None."""
    upper = character.upper()
    if len(upper) != 1:
        return None if len(character.title()) == 1 else character
    return character if upper.isascii() and not character.isascii() else upper


def ordinal_fold(text: str) -> str:
    """A text in the one form two spellings .NET's ordinal comparison ignoring case finds equal share, so a name it
    compares that way is looked up and compared by it. An ASCII letter is lowered and any other character takes its
    simple uppercase, which is never ASCII, so ASCII text keeps the order `casefold()` gave it; a character whose
    simple uppercase cannot be told is kept as written, so it matches only itself."""
    if text.isascii():
        return text.lower()
    return "".join(
        character.lower() if character.isascii() else simple_uppercase(character) or character for character in text
    )


def has_suffix(name: str, suffixes: str | tuple[str, ...]) -> bool:
    """Whether a file's name ends with one of `suffixes`, each lowercase ASCII, compared as .NET compares them,
    ordinally ignoring case: `App.CSPROJ` ends with `.csproj`, while a long s or the Kelvin sign in a suffix's place
    does not."""
    return ordinal_fold(name).endswith(suffixes)
