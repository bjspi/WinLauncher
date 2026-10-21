"""The search bar's calculator: arithmetic, percentages and a few conversions.

`calculate` parses the text itself and never evaluates Python code. It returns
None for anything that is not clearly a calculation, so ordinary searches such as
`report`, `2024-06` or `2024-2025` show no result.

- Numbers: `1.5`, `1,5` (decimal comma), `2e3`, `0x1F`, `0b101`, `0o17`.
- Operators: `+ - * / ^` (also `**`, `×`, `÷`) and parentheses; `^` is power.
- Percent: `200 + 10%` is 220 and `200 - 10%` is 180; elsewhere `50%` is 0.5.
- Constants: `pi`/`π`, `e`, `tau`. Names are case-insensitive.
- Functions: see `_NUMERIC` and `_TEXT`. Multi-argument functions separate
  arguments with `,`, or with `;` when the numbers use decimal commas.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

MAX_LENGTH = 200
MAX_DEPTH = 40
MAX_INTEGER_BITS = 3400  # About 1000 decimal digits.
MAX_FACTORIAL = 170  # The largest factorial a float can still approximate.
SIGNIFICANT_DIGITS = 12
# Digits joined by hyphens are dates, ranges or numbers to search for: 2024-06-15.
_HYPHENATED_DIGITS = re.compile(r"\d+(?:-\d+)+")
_TOKEN = re.compile(
    r"""
    (?P<space>\s+)
  | (?P<number>0[xX][0-9a-fA-F]+|0[bB][01]+|0[oO][0-7]+
      |(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)
  | (?P<name>[A-Za-z_π][A-Za-z_0-9]*)
  | (?P<string>"[^"]*"|'[^']*')
  | (?P<operator>\*\*|[-+*/^%(),;×÷])
    """,
    re.VERBOSE,
)
_CONSTANTS = {"pi": math.pi, "π": math.pi, "e": math.e, "tau": math.tau}


class CalculationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Calculation:
    expression: str
    result: str  # Formatted for display and the clipboard.


@dataclass(frozen=True, slots=True)
class _Token:
    kind: str
    text: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class _Value:
    number: int | float | None = None
    text: str | None = None
    percent: bool = False  # A bare `x%`, which `+` and `-` apply to their left side.

    def numeric(self) -> int | float:
        if self.number is None:
            raise CalculationError("Text cannot be used in arithmetic.")
        return self.number


def calculate(expression: str) -> Calculation | None:
    """The result of `expression`, or None if it is not a valid calculation."""
    text = expression.strip()
    if not text or len(text) > MAX_LENGTH or _HYPHENATED_DIGITS.fullmatch(text):
        return None
    try:
        parser = _Parser(text)
        value = parser.parse()
    except (CalculationError, ArithmeticError, ValueError):
        return None
    if not parser.calculated:
        return None  # A lone number, constant or negation is a search, not a sum.
    try:
        result = value.text if value.text is not None else _format(value.numeric())
    except (CalculationError, OverflowError):
        return None
    if parser.decimal_comma and value.text is None:
        result = result.replace(".", ",")
    return Calculation(text, result)


def _format(number: int | float) -> str:
    if isinstance(number, float):
        if not math.isfinite(number):
            raise CalculationError("The result is not finite.")
        if number.is_integer() and abs(number) < 10**15:
            return str(int(number))
        return format(number, f".{SIGNIFICANT_DIGITS}g")
    if abs(number) >= 10**30:
        # Decimal formats any integer exactly; float() overflows above 1e308.
        mantissa, exponent = format(Decimal(number), f".{SIGNIFICANT_DIGITS - 1}e").split("e")
        return f"{mantissa.rstrip('0').rstrip('.')}e{exponent}"
    return str(number)


def _tokens(text: str) -> list[_Token]:
    tokens = []
    position = 0
    while position < len(text):
        match = _TOKEN.match(text, position)
        if match is None:
            raise CalculationError(f"Unexpected {text[position]!r}.")
        if match.lastgroup != "space":
            tokens.append(_Token(match.lastgroup, match.group(), match.start(), match.end()))
        position = match.end()
        if match.lastgroup == "name" and match.group().lower() in _RAW_TEXT:
            position = _raw_argument(text, position, tokens)
    return tokens


def _raw_argument(text: str, position: int, tokens: list[_Token]) -> int:
    """`hex2ascii(4A 42)` and `ord(@)` take text, not an expression: `(`, raw, `)`."""
    opening = text.find("(", position)
    if opening < 0 or text[position:opening].strip():
        return position
    start = opening + 1
    search_from = start
    argument = text[start:].lstrip()
    if argument[:1] in ("'", '"'):
        quote_start = text.index(argument[0], start)
        quote_end = text.find(argument[0], quote_start + 1)
        if quote_end < 0:
            raise CalculationError("Unclosed quote.")
        search_from = quote_end + 1
    closing = text.find(")", search_from)
    if closing < 0:
        raise CalculationError("Expected ')'.")
    tokens.append(_Token("operator", "(", opening, start))
    tokens.append(_Token("raw", text[start:closing], start, closing))
    tokens.append(_Token("operator", ")", closing, closing + 1))
    return closing + 1


def _number(text: str) -> int | float:
    if text[:2].lower() in ("0x", "0b", "0o"):
        return int(text, 0)
    if any(character in text for character in ".eE"):
        return float(text)
    return int(text)


# Arithmetic ------------------------------------------------------------------


def _add(left: int | float, right: int | float) -> int | float:
    return left + right


def _divide(left: int | float, right: int | float) -> int | float:
    if right == 0:
        raise CalculationError("Division by zero.")
    if isinstance(left, int) and isinstance(right, int) and left % right == 0:
        return left // right
    return left / right


def _power(base: int | float, exponent: int | float) -> int | float:
    if isinstance(base, int) and isinstance(exponent, int) and exponent >= 0:
        if base not in (-1, 0, 1) and exponent * math.log2(abs(base)) > MAX_INTEGER_BITS:
            raise CalculationError("The result is too large.")
        return base**exponent
    result = float(base) ** float(exponent)
    if isinstance(result, complex):
        raise CalculationError("The result is not a real number.")
    return result


def _integer(number: int | float) -> int:
    if isinstance(number, float):
        if not number.is_integer():
            raise CalculationError("A whole number is needed.")
        return int(number)
    return number


def _factorial(number: int | float) -> int:
    value = _integer(number)
    if not 0 <= value <= MAX_FACTORIAL:
        raise CalculationError(f"fact() takes 0 to {MAX_FACTORIAL}.")
    return math.factorial(value)


def _log(number: int | float, base: int | float = 10) -> float:
    return math.log(number, base)


def _round(number: int | float, digits: int | float = 0) -> int | float:
    """Halves round away from zero, like a pocket calculator (Python rounds to even)."""
    places = _integer(digits)
    if not -MAX_DEPTH <= places <= SIGNIFICANT_DIGITS:
        raise CalculationError("round() keeps up to 12 decimal places.")
    rounded = Decimal(repr(number)).quantize(Decimal(1).scaleb(-places), ROUND_HALF_UP)
    return int(rounded) if places <= 0 else float(rounded)


def _modulo(left: int | float, right: int | float) -> int | float:
    if right == 0:
        raise CalculationError("Division by zero.")
    return left % right


# name: (function, minimum arguments, maximum arguments or None for any number)
_NUMERIC: dict[str, tuple[Callable[..., int | float], int, int | None]] = {
    "sqrt": (math.sqrt, 1, 1),
    "exp": (math.exp, 1, 1),
    "ln": (math.log, 1, 1),
    "log": (_log, 1, 2),  # Base 10 unless given, like a pocket calculator.
    "log2": (math.log2, 1, 1),
    "log10": (math.log10, 1, 1),
    "sin": (math.sin, 1, 1),
    "cos": (math.cos, 1, 1),
    "tan": (math.tan, 1, 1),
    "asin": (math.asin, 1, 1),
    "acos": (math.acos, 1, 1),
    "atan": (math.atan, 1, 1),
    "deg": (math.degrees, 1, 1),
    "rad": (math.radians, 1, 1),
    "abs": (abs, 1, 1),
    "floor": (math.floor, 1, 1),
    "ceil": (math.ceil, 1, 1),
    "round": (_round, 1, 2),
    "min": (min, 1, None),
    "max": (max, 1, None),
    "mod": (_modulo, 2, 2),
    "fact": (_factorial, 1, 1),
}


# Text conversions --------------------------------------------------------------


def _character(number: int | float) -> str:
    code = _integer(number)
    if not 0 <= code <= 0x10FFFF:
        raise CalculationError("No such character.")
    return _visible(chr(code))


def _visible(text: str) -> str:
    """Control characters as escapes, so the result row shows something."""
    return "".join(
        character if character.isprintable() else repr(character)[1:-1] for character in text
    )


def _hex_to_text(raw: str) -> str:
    digits = re.sub(r"[\s:,]|0[xX]", "", raw)
    if not digits or len(digits) % 2 or not re.fullmatch(r"[0-9a-fA-F]+", digits):
        raise CalculationError("hex2ascii() takes pairs of hex digits.")
    return _visible(bytes.fromhex(digits).decode("latin-1"))


def _text_to_hex(raw: str) -> str:
    return " ".join(f"{code:02X}" for code in _unquoted(raw).encode("utf-8"))


def _ordinal(raw: str) -> str:
    text = _unquoted(raw)
    if len(text) != 1:
        raise CalculationError("ord() takes one character.")
    return str(ord(text))


def _unquoted(raw: str) -> str:
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


_INTEGER_TEXT: dict[str, Callable[[int], str]] = {"bin": bin, "hex": hex, "oct": oct}
# Functions that read their argument as raw text instead of an expression.
_RAW_TEXT: dict[str, Callable[[str], str]] = {
    "hex2ascii": _hex_to_text,
    "ascii2hex": _text_to_hex,
    "ord": _ordinal,
}
_CHARACTER = ("chr", "ascii")


# Parser -------------------------------------------------------------------------


class _Parser:
    """Recursive descent over the token list; evaluates while parsing."""

    def __init__(self, text: str):
        self.text = text
        self.tokens = _tokens(text)
        self.position = 0
        self.depth = 0
        self.argument_commas = False  # Inside min(1, 2): commas separate arguments.
        self.calculated = False  # Saw an operator, a function, `%` or a 0x/0b/0o literal.
        self.decimal_comma = False

    def parse(self) -> _Value:
        value = self.expression()
        if self.position != len(self.tokens):
            raise CalculationError("Unexpected text.")
        return value

    def peek(self, offset: int = 0) -> _Token | None:
        index = self.position + offset
        return self.tokens[index] if index < len(self.tokens) else None

    def accept(self, *texts: str) -> _Token | None:
        token = self.peek()
        if token is not None and token.kind == "operator" and token.text in texts:
            self.position += 1
            return token
        return None

    def expect(self, text: str) -> None:
        if self.accept(text) is None:
            raise CalculationError(f"Expected {text!r}.")

    def expression(self) -> _Value:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise CalculationError("Too deeply nested.")
        value = self.term()
        while operator := self.accept("+", "-"):
            self.calculated = True
            right = self.term()
            left = value.numeric()
            amount = left * right.numeric() if right.percent else right.numeric()
            value = _Value(_add(left, amount if operator.text == "+" else -amount))
        self.depth -= 1
        return value

    def term(self) -> _Value:
        value = self.unary()
        while operator := self.accept("*", "/", "×", "÷"):
            self.calculated = True
            right = self.unary().numeric()
            left = value.numeric()
            value = _Value(left * right if operator.text in "*×" else _divide(left, right))
        return value

    def unary(self) -> _Value:
        if self.accept("-"):
            value = self.unary()
            return _Value(-value.numeric(), percent=value.percent)
        if self.accept("+"):
            return self.unary()
        return self.power()

    def power(self) -> _Value:
        base = self.postfix()
        if self.accept("^", "**"):
            self.calculated = True
            exponent = self.unary()  # Right-associative: 2^3^2 is 2^9.
            return _Value(_power(base.numeric(), exponent.numeric()))
        return base

    def postfix(self) -> _Value:
        value = self.primary()
        if self.accept("%"):
            self.calculated = True
            return _Value(value.numeric() / 100, percent=True)
        return value

    def primary(self) -> _Value:
        token = self.peek()
        if token is None:
            raise CalculationError("Unexpected end.")
        if self.accept("("):
            outer, self.argument_commas = self.argument_commas, False
            value = self.expression()
            self.argument_commas = outer
            self.expect(")")
            return _Value(value.number, value.text)
        self.position += 1
        if token.kind == "number":
            return _Value(self.number(token))
        if token.kind == "name":
            name = token.text.lower()
            if self.accept("("):
                self.calculated = True
                return self.call(name, token)
            if name in _CONSTANTS:
                return _Value(_CONSTANTS[name])
        raise CalculationError(f"Unexpected {token.text!r}.")

    def number(self, token: _Token) -> int | float:
        if token.text[:2].lower() in ("0x", "0b", "0o"):
            self.calculated = True
            return _number(token.text)
        comma, fraction = self.peek(), self.peek(1)
        if (
            not self.argument_commas
            and token.text.isdigit()
            and comma is not None
            and comma.text == ","
            and fraction is not None
            and fraction.kind == "number"
            and fraction.text.isdigit()
            and token.end == comma.start
            and comma.end == fraction.start
        ):
            self.position += 2
            self.decimal_comma = True
            return float(f"{token.text}.{fraction.text}")
        return _number(token.text)

    def call(self, name: str, token: _Token) -> _Value:
        if name in _RAW_TEXT:
            raw = self.peek()
            if raw is None or raw.kind != "raw":
                raise CalculationError(f"{name}() takes text.")
            self.position += 1
            self.expect(")")
            return _Value(text=_RAW_TEXT[name](raw.text))
        arguments = self.arguments(multiple=name in ("min", "max", "log", "round", "mod"))
        if name in _INTEGER_TEXT or name in _CHARACTER:
            if len(arguments) != 1:
                raise CalculationError(f"{name}() takes one number.")
            number = arguments[0].numeric()
            if name in _CHARACTER:
                return _Value(text=_character(number))
            return _Value(text=_INTEGER_TEXT[name](_integer(number)))
        if name not in _NUMERIC:
            raise CalculationError(f"Unknown function {token.text!r}.")
        function, least, most = _NUMERIC[name]
        if len(arguments) < least or (most is not None and len(arguments) > most):
            raise CalculationError(f"Wrong number of arguments for {name}().")
        return _Value(function(*(argument.numeric() for argument in arguments)))

    def arguments(self, *, multiple: bool) -> list[_Value]:
        """Comma-separated, unless a `;` separates them and commas are decimal commas."""
        semicolons = self.separated_by_semicolons()
        outer, self.argument_commas = self.argument_commas, multiple and not semicolons
        separators = (";",) if semicolons else (",", ";") if multiple else (";",)
        arguments = [self.expression()]
        while self.accept(*separators):
            arguments.append(self.expression())
        self.argument_commas = outer
        self.expect(")")
        return arguments

    def separated_by_semicolons(self) -> bool:
        depth = 0
        for token in self.tokens[self.position :]:
            if token.kind != "operator":
                continue
            if token.text == "(":
                depth += 1
            elif token.text == ")":
                if depth == 0:
                    return False
                depth -= 1
            elif token.text == ";" and depth == 0:
                return True
        return False
