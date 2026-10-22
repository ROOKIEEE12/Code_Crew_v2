"""Read TOML without a third-party dependency.

Python 3.11+ ships ``tomllib``; CodeCrew supports 3.10 and installs nothing,
so on 3.10 a small parser covers what CodeCrew reads from a project:
tables (``[a.b]``), and keys holding strings, numbers, booleans and arrays
of those (arrays may span lines). Anything it does not understand raises
``TomlError`` instead of being guessed at; callers treat that as "no
configuration", and say so.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

try:  # pragma: no cover - exercised on 3.11+
    import tomllib as _tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on 3.10
    _tomllib = None


class TomlError(ValueError):
    pass


def load(path: Path) -> dict:
    return loads(path.read_text(encoding="utf-8"))


def loads(text: str, *, force_fallback: bool = False) -> dict:
    if _tomllib is not None and not force_fallback:
        try:
            return _tomllib.loads(text)
        except _tomllib.TOMLDecodeError as exc:
            raise TomlError(str(exc)) from exc
    return _Parser(text).parse()


_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text.replace("\r\n", "\n")
        self.pos = 0
        self.line = 1

    # -- driver -------------------------------------------------------------------

    def parse(self) -> dict:
        root: dict = {}
        table = root
        while True:
            self._skip_blank()
            if self._eof():
                return root
            ch = self._peek()
            if ch == "[":
                if self.text.startswith("[[", self.pos):
                    self.pos += 2
                    keys = self._key_path("]]")
                    parent = self._descend(root, keys[:-1])
                    items = parent.setdefault(keys[-1], [])
                    if not isinstance(items, list):
                        raise self._error(f"{'.'.join(keys)} is not an array of tables")
                    table = {}
                    items.append(table)
                else:
                    self.pos += 1
                    keys = self._key_path("]")
                    table = self._descend(root, keys)
                self._end_of_line()
                continue
            keys = self._key_path("=")
            self._skip_spaces()
            value = self._value()
            target = self._descend(table, keys[:-1])
            if keys[-1] in target:
                raise self._error(f"duplicate key {keys[-1]!r}")
            target[keys[-1]] = value
            self._end_of_line()

    # -- pieces ---------------------------------------------------------------------

    def _key_path(self, terminator: str) -> list[str]:
        keys = []
        while True:
            self._skip_spaces()
            ch = self._peek()
            if ch in ('"', "'"):
                keys.append(self._string())
            else:
                match = _BARE_KEY.match(self.text, self.pos)
                if not match:
                    raise self._error("expected a key")
                keys.append(match.group())
                self.pos = match.end()
            self._skip_spaces()
            if self.text.startswith(terminator, self.pos):
                self.pos += len(terminator)
                return keys
            if self._peek() != ".":
                raise self._error(f"expected '.' or {terminator!r}")
            self.pos += 1

    def _value(self) -> Any:
        ch = self._peek()
        if ch in ('"', "'"):
            return self._string()
        if ch == "[":
            return self._array()
        if ch == "{":
            return self._inline_table()
        match = re.compile(r"true|false|[+-]?\d[\d_]*(\.\d[\d_]*)?([eE][+-]?\d+)?").match(
            self.text, self.pos)
        if not match:
            raise self._error("unsupported value")
        self.pos = match.end()
        word = match.group()
        if word == "true":
            return True
        if word == "false":
            return False
        number = word.replace("_", "")
        return float(number) if any(c in number for c in ".eE") else int(number)

    def _string(self) -> str:
        quote = self._peek()
        triple = self.text.startswith(quote * 3, self.pos)
        if triple:
            self.pos += 3
            if self._peek() == "\n":
                self.pos += 1
            end = self.text.find(quote * 3, self.pos)
            if end < 0:
                raise self._error("unterminated string")
            raw = self.text[self.pos:end]
            self.line += raw.count("\n")
            self.pos = end + 3
            return raw if quote == "'" else _unescape(raw)
        self.pos += 1
        out = []
        while True:
            if self._eof() or self._peek() == "\n":
                raise self._error("unterminated string")
            ch = self.text[self.pos]
            if ch == quote:
                self.pos += 1
                text = "".join(out)
                return text if quote == "'" else _unescape(text)
            if ch == "\\" and quote == '"':
                out.append(self.text[self.pos:self.pos + 2])
                self.pos += 2
                continue
            out.append(ch)
            self.pos += 1

    def _array(self) -> list:
        self.pos += 1
        items = []
        while True:
            self._skip_blank()
            if self._peek() == "]":
                self.pos += 1
                return items
            items.append(self._value())
            self._skip_blank()
            if self._peek() == ",":
                self.pos += 1
                continue
            self._skip_blank()
            if self._peek() != "]":
                raise self._error("expected ',' or ']' in array")

    def _inline_table(self) -> dict:
        self.pos += 1
        table: dict = {}
        self._skip_spaces()
        if self._peek() == "}":
            self.pos += 1
            return table
        while True:
            keys = self._key_path("=")
            self._skip_spaces()
            self._descend(table, keys[:-1])[keys[-1]] = self._value()
            self._skip_spaces()
            if self._peek() == "}":
                self.pos += 1
                return table
            if self._peek() != ",":
                raise self._error("expected ',' or '}' in inline table")
            self.pos += 1

    # -- helpers -----------------------------------------------------------------

    def _descend(self, table: dict, keys: list[str]) -> dict:
        for key in keys:
            node = table.setdefault(key, {})
            if isinstance(node, list):
                node = node[-1] if node and isinstance(node[-1], dict) else None
            if not isinstance(node, dict):
                raise self._error(f"{key!r} is not a table")
            table = node
        return table

    def _peek(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def _eof(self) -> bool:
        return self.pos >= len(self.text)

    def _skip_spaces(self) -> None:
        while self._peek() in (" ", "\t"):
            self.pos += 1

    def _skip_blank(self) -> None:
        """Spaces, newlines and comments."""
        while not self._eof():
            ch = self._peek()
            if ch in (" ", "\t"):
                self.pos += 1
            elif ch == "\n":
                self.pos += 1
                self.line += 1
            elif ch == "#":
                end = self.text.find("\n", self.pos)
                self.pos = len(self.text) if end < 0 else end
            else:
                return

    def _end_of_line(self) -> None:
        self._skip_spaces()
        if self._peek() == "#":
            end = self.text.find("\n", self.pos)
            self.pos = len(self.text) if end < 0 else end
        if self._eof():
            return
        if self._peek() != "\n":
            raise self._error("unexpected text after a value")
        self.pos += 1
        self.line += 1

    def _error(self, message: str) -> TomlError:
        return TomlError(f"line {self.line}: {message}")


_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "b": "\b", "f": "\f"}


def _unescape(text: str) -> str:
    out = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt in _ESCAPES:
                out.append(_ESCAPES[nxt])
                i += 2
                continue
            if nxt in "uU":
                width = 4 if nxt == "u" else 8
                out.append(chr(int(text[i + 2:i + 2 + width], 16)))
                i += 2 + width
                continue
        out.append(ch)
        i += 1
    return "".join(out)
