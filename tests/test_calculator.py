"""The search bar calculator: results, percent rules and what stays a search."""

from __future__ import annotations

import time
import unittest

from ci_timing import PROMPT_LIMIT_S

from pathscout.calculator import calculate


def result(expression: str) -> str | None:
    calculation = calculate(expression)
    return calculation.result if calculation is not None else None


class CalculatorTest(unittest.TestCase):
    def check(self, cases: dict[str, str | None]) -> None:
        for expression, expected in cases.items():
            with self.subTest(expression=expression):
                self.assertEqual(result(expression), expected)

    def test_arithmetic_and_precedence(self):
        self.check(
            {
                "1+2*3": "7",
                "(1+2)*3": "9",
                "10/4": "2.5",
                "10/5": "2",
                "2^10": "1024",
                "2**0.5": "1.41421356237",
                "2^3^2": "512",
                "-2^2": "-4",
                "2^-1": "0.5",
                "2 × 3 ÷ 4": "1.5",
                "0.1+0.2": "0.3",
                "1/3": "0.333333333333",
                "1e3+1": "1001",
            }
        )

    def test_percent_applies_to_the_left_side_of_plus_and_minus(self):
        self.check(
            {
                "200 + 10%": "220",
                "200 - 10%": "180",
                "50%": "0.5",
                "50% * 30": "15",
                "200 + 2*5%": "200.1",
                "(200 + 10%) * 2": "440",
            }
        )

    def test_decimal_commas_and_argument_separators(self):
        self.check(
            {
                "1,5 * 3": "4,5",
                "sqrt(2,25)": "1,5",
                "max(1, 5, 3)": "5",
                "max(1,5; 2)": "2",
                "min(4;2)": "2",
                "sqrt(2, 5)": None,
            }
        )

    def test_functions_and_constants(self):
        self.check(
            {
                "pi*2": "6.28318530718",
                "PI*2": "6.28318530718",
                "π/π": "1",
                "sin(pi/2)": "1",
                "cos(0)+tan(0)": "1",
                "floor(2.7)+ceil(2.1)": "5",
                "round(2.5)": "3",
                "round(-2.5)": "-3",
                "round(2.345, 2)": "2.35",
                "log(1000)": "3",
                "log(8, 2)": "3",
                "ln(e)": "1",
                "mod(10, 3)": "1",
                "fact(5)": "120",
                "abs(-3)": "3",
            }
        )

    def test_conversions(self):
        self.check(
            {
                "bin(10)": "0b1010",
                "hex(255)": "0xff",
                "oct(8)": "0o10",
                "0xff": "255",
                "0b101 + 1": "6",
                "chr(65)": "A",
                "ascii(10)": "\\n",
                "ord(A)": "65",
                'ord(")")': "41",
                "hex2ascii(30323030)": "0200",
                "hex2ascii(4A 42)": "JB",
                "ascii2hex(0200)": "30 32 30 30",
                "hex(2.5)": None,
                "hex(255)+1": None,
                "hex2ascii(123)": None,
            }
        )

    def test_searches_are_not_calculations(self):
        for text in (
            "report",
            "pi",
            "e",
            "42",
            "-5",
            "(5)",
            "1,5",
            "2024-06",
            "2024-06-15",
            "2024-2025",
            "report-2024",
            "*.json",
            "ext:pdf",
            "12.05.2024",
            "1.2.3",
            "~/Documents",
        ):
            with self.subTest(text=text):
                self.assertIsNone(calculate(text))

    def test_invalid_and_oversized_input_returns_none_quickly(self):
        started = time.perf_counter()
        self.check(
            {
                "1/0": None,
                "9^9^9": None,
                "fact(500)": None,
                "(-8)^(1/3)": None,
                "sqrt(-1)": None,
                "unknown(2)": None,
                "2 +": None,
                "(" * 60 + "1" + ")" * 60 + "+1": None,
                "1+" * 150 + "1": None,
                "exp(1000)": None,
            }
        )
        self.assertEqual(result("10^400"), "1e+400")
        self.assertLess(time.perf_counter() - started, PROMPT_LIMIT_S)


if __name__ == "__main__":
    unittest.main()
