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

"""Reading the values a stack program pins, without running the program.

A deploy workflow checks a stack's pins before Pulumi runs it:

    python -m ord_infrastructure.pins __main__.py ARTIFACTS_REVISION SOURCES_REVISION

prints one ``NAME=value`` line per name, the format ``$GITHUB_OUTPUT`` reads.
"""

import argparse
import ast
import pathlib
from collections.abc import Sequence


def _bound(node: ast.AST) -> list[str]:
    """Returns the names one syntax node binds, in whichever statement binds them."""
    match node:
        case ast.Name(ctx=ast.Store()):
            return [node.id]
        case ast.alias():
            return [node.asname or node.name.split(".")[0]]
        case (
            ast.FunctionDef()
            | ast.AsyncFunctionDef()
            | ast.ClassDef()
            | ast.ExceptHandler()
            | ast.MatchAs()
            | ast.MatchStar()
        ) if node.name is not None:
            return [node.name]
        case _:
            return []


def read_pins(program: pathlib.Path, names: Sequence[str]) -> dict[str, str]:
    """Returns the string literal each name is assigned at a program's top level.

    Args:
        program: A Python file, such as a stack's ``__main__.py``.
        names: The names to read.

    Returns:
        Each name's value, keyed by the name.

    Raises:
        ValueError: If the program binds a name anywhere but in one top-level
            assignment of a string literal, since its value is then what the
            program computes rather than what it reads.
    """
    tree = ast.parse(program.read_text(), filename=str(program))
    literals = {
        node.targets[0].id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    bindings = [name for node in ast.walk(tree) for name in _bound(node)]
    pins = {}
    for name in names:
        if name not in literals or bindings.count(name) != 1:
            raise ValueError(
                f"{program} binds {name} other than in one top-level assignment of a "
                "string literal"
            )
        pins[name] = literals[name]
    return pins


def main(argv: Sequence[str] | None = None) -> None:
    """Prints each pin a program assigns as a ``NAME=value`` line.

    Args:
        argv: The program's path, then the names to read; None reads sys.argv.
    """
    parser = argparse.ArgumentParser(description="Print the pins a program assigns.")
    parser.add_argument("program", type=pathlib.Path)
    parser.add_argument("names", nargs="+")
    args = parser.parse_args(argv)
    for name, value in read_pins(args.program, args.names).items():
        print(f"{name}={value}")


if __name__ == "__main__":
    main()
