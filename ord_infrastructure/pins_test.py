# Copyright 2026 Open Reaction Database Project Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for reading a stack program's pins from its source."""

import pathlib
import textwrap

import pytest

from ord_infrastructure import pins


def _program(tmp_path: pathlib.Path, source: str) -> pathlib.Path:
    """Writes a program holding `source` and returns its path."""
    path = tmp_path / "__main__.py"
    path.write_text(textwrap.dedent(source))
    return path


def test_reads_each_pin_the_program_assigns(tmp_path):
    program = _program(
        tmp_path,
        """
        import pulumi

        A = "a-commit"
        B = "b-commit"
        OTHER = A + B
        """,
    )
    assert pins.read_pins(program, ["B", "A"]) == {"B": "b-commit", "A": "a-commit"}


@pytest.mark.parametrize(
    "source",
    [
        # Never assigned.
        "B = 'b'\n",
        # Computed, so only running the program knows it.
        "A = 'a' + 'b'\n",
        "A = f'{0}'\n",
        "A = 1\n",
        # Rebound after the literal, at the top level or beneath it.
        "A = 'a'\nA = 'b'\n",
        "A = 'a'\nif True:\n    A = 'b'\n",
        "A = 'a'\nfor A in ['b']:\n    pass\n",
        # Bound together with another name.
        "A = B = 'a'\n",
    ],
)
def test_refuses_a_pin_the_program_does_not_assign_one_literal(tmp_path, source):
    program = _program(tmp_path, source)
    with pytest.raises(ValueError, match=r"binds A other than in one top-level"):
        pins.read_pins(program, ["A"])


def test_main_prints_one_output_line_per_pin(tmp_path, capsys):
    program = _program(tmp_path, "A = 'a-commit'\nB = 'b-commit'\n")
    pins.main([str(program), "A", "B"])
    assert capsys.readouterr().out == "A=a-commit\nB=b-commit\n"
