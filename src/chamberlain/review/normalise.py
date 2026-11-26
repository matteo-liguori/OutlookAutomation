"""Shell commands reduced to a short, safe summary (the privacy core of the review track).

Contract: docs/contracts/06-memory.md (M-40). A summary is the program name and, for a short list
of programs, one sub-command word (`git status`, `python -m pytest`). It never holds an argument,
a path, a quote, a number or a URL, so it cannot carry mail text, file contents or secrets.
Anything that does not fit the strict shape is dropped or shown as `(other)`.
"""

import re
from dataclasses import dataclass

from chamberlain.review.tuning import SUMMARY_MAX

_PROGRAM = re.compile(r"^[a-z][a-z0-9_.+-]{0,29}$")
_WORD = re.compile(r"^[a-z][a-z0-9_-]{0,20}$")
_MODULE = re.compile(r"^[a-z_][a-z0-9_.]{0,40}$")
_CMDLET = re.compile(r"^[a-z]{2,12}-[a-z]{2,24}$")
_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PYTHON = re.compile(r"^(python|pythonw|py)[0-9.]*$")
_SHELL_FLAG = re.compile(r"^-[a-z]*c[a-z]*$")
_SUFFIXES = (".exe", ".cmd", ".bat", ".ps1", ".sh", ".com")
_SEPARATORS = re.compile(r"[\\/]")


def _words(text: str) -> frozenset[str]:
    return frozenset(text.split())


KNOWN = _words(
    "python python3 py pythonw pip pip3 uv pytest ruff pyright mypy tsc node npm npx pnpm "
    "yarn git gh cargo rustc go make cmake docker kubectl curl wget ssh scp rg grep sed "
    "awk find ls cat head tail sort uniq wc cp mv rm mkdir touch echo tar unzip zip "
    "sqlite3 jq dotnet java javac mvn gradle code thunderbird taskkill tasklist where "
    "which type dir cmd powershell pwsh bash sh zsh chamberlain diff stat du pwd cd sleep "
    "xargs tee tr cut less more open start explorer reg ping winget choco scoop brew "
    "poetry conda "
)
"""Programs worth naming even when they were started by a path."""
WITH_SUBCOMMAND = _words(
    "git gh npm npx pnpm yarn pip pip3 uv cargo docker kubectl dotnet brew winget choco "
    "scoop make go poetry conda "
)
SKIPPED_WITH_VALUE = {"git": frozenset({"-c", "-C"})}
NOISE = frozenset({"cd", "pushd", "popd", "set-location", "sl", "true", "exit", "export", "set"})
SHELLS = frozenset({"powershell", "pwsh", "bash", "sh", "zsh", "dash", "fish", "cmd"})


@dataclass(frozen=True)
class Summary:
    patterns: tuple[str, ...]
    """One short summary per command in the line (empty: nothing recordable)."""
    chamberlain: bool
    """The line ran a Chamberlain tool from the command line."""


def _tokens(text: str) -> list[tuple[str, int, int]]:
    """Words with quotes removed, and where each started and ended in `text`."""
    found: list[tuple[str, int, int]] = []
    word: list[str] = []
    quote = ""
    start = 0
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = ""
            else:
                word.append(char)
        elif char in "\"'":
            quote = char
            if not word:
                start = index
        elif char.isspace():
            if word:
                found.append(("".join(word), start, index))
                word = []
        else:
            if not word:
                start = index
            word.append(char)
    if word:
        found.append(("".join(word), start, len(text)))
    return found


def _parts(text: str) -> list[str]:
    """Split on `;`, `&&`, `||`, `|` and newlines outside quotes."""
    parts: list[str] = []
    current: list[str] = []
    quote = ""
    for char in text:
        if quote:
            if char == quote:
                quote = ""
            current.append(char)
        elif char in "\"'":
            quote = char
            current.append(char)
        elif char in ";|\n&":
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def _program(token: str) -> tuple[str, bool] | None:
    """`(name, started_by_path)`, or None when the word is not a program name."""
    by_path = bool(_SEPARATORS.search(token))
    name = _SEPARATORS.split(token)[-1].lower()
    for suffix in _SUFFIXES:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    if not _PROGRAM.match(name):
        return None
    return name, by_path


def _unwrap(tokens: list[tuple[str, int, int]], text: str, program: str) -> str | None:
    """The command inside `powershell -Command ...`, `bash -lc ...` or `cmd /c ...`."""
    for index, (token, _, end) in enumerate(tokens[1:], start=1):
        flag = token.lower()
        if program in {"powershell", "pwsh"} and flag in {"-c", "-command"}:
            return text[end:].strip().strip("\"'")
        if program == "cmd" and flag in {"/c", "/k"}:
            return text[end:].strip().strip("\"'")
        if program not in {"powershell", "pwsh", "cmd"} and _SHELL_FLAG.match(flag):
            return tokens[index + 1][0] if index + 1 < len(tokens) else None
    return None


def _python(words: list[str]) -> tuple[str, bool]:
    """`(summary, is_chamberlain)` for a Python command line."""
    for index, word in enumerate(words):
        if word == "-m" and index + 1 < len(words):
            module = words[index + 1].lower()
            if _MODULE.match(module):
                return f"python -m {module}", module.split(".")[0] == "chamberlain"
            return "python -m", False
        if word == "-c":
            return "python -c", False
        if index > 0 and not word.startswith("-"):
            return "python (script)", False
    return "python", False


def _subcommand(program: str, words: list[str]) -> str:
    skip = SKIPPED_WITH_VALUE.get(program, frozenset())
    index = 1
    while index < len(words):
        word = words[index]
        if word in skip:
            index += 2
        elif word.startswith("-"):
            index += 1
        else:
            sub = word.lower()
            return f"{program} {sub}" if _WORD.match(sub) else program
    return program


def _summarise_part(part: str, depth: int) -> tuple[list[str], bool]:
    tokens = _tokens(part)
    while tokens and (tokens[0][0] == "." or _ENV.match(tokens[0][0])):
        tokens = tokens[1:]
    if not tokens:
        return [], False
    named = _program(tokens[0][0])
    if named is None:
        return [], False
    program, by_path = named
    if program in SHELLS and depth < 2:
        inner = _unwrap(tokens, part, program)
        if inner:
            return _summarise(inner, depth + 1)
    words = [t[0] for t in tokens]
    if _PYTHON.match(program):
        summary, chamberlain = _python(words)
        return [summary], chamberlain
    if program == "chamberlain":
        return [], True
    if by_path and program not in KNOWN:
        return ["(script)"], False
    if program in WITH_SUBCOMMAND:
        return [_subcommand(program, words)], False
    if program in KNOWN or _CMDLET.match(program):
        return [program], False
    return ["(other)"], False


def _summarise(command: str, depth: int) -> tuple[list[str], bool]:
    found: list[str] = []
    chamberlain = False
    for part in _parts(command):
        patterns, is_chamberlain = _summarise_part(part, depth)
        found.extend(p[:SUMMARY_MAX] for p in patterns)
        chamberlain = chamberlain or is_chamberlain
    return found, chamberlain


def summarise(command: str) -> Summary:
    """The summaries of one command line. Noise such as `cd` is left out when anything else ran."""
    found, chamberlain = _summarise(command, 0)
    useful = [p for p in found if p not in NOISE]
    return Summary(tuple(useful or found), chamberlain)


def from_argv(argv: list[str]) -> str:
    """One command line from an argument list (Codex gives commands this way)."""
    if len(argv) >= 3 and (_SHELL_FLAG.match(argv[1].lower()) or argv[1].lower() == "-command"):
        return argv[2]
    return " ".join(f'"{a}"' if " " in a else a for a in argv)
