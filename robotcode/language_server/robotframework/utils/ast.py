from __future__ import annotations

import ast
from typing import (
    Any,
    AsyncIterator,
    Generator,
    Iterator,
    List,
    Optional,
    Protocol,
    Tuple,
    runtime_checkable,
)

from ...common.lsp_types import Position, Range
from .async_ast import walk


def iter_nodes(node: ast.AST) -> Generator[ast.AST, None, None]:
    for _field, value in ast.iter_fields(node):
        if isinstance(value, list):
            for item in value:
                if isinstance(item, ast.AST):
                    yield item
                    yield from iter_nodes(item)

        elif isinstance(value, ast.AST):
            yield value

            yield from iter_nodes(value)


def range_from_node(node: ast.AST) -> Range:
    return Range(
        start=Position(line=node.lineno - 1, character=node.col_offset),
        end=Position(
            line=node.end_lineno - 1 if node.end_lineno is not None else -1,
            character=node.end_col_offset if node.end_col_offset is not None else -1,
        ),
    )


@runtime_checkable
class Token(Protocol):
    type: Optional[str]
    value: str
    lineno: int
    col_offset: int
    error: Optional[str]

    @property
    def end_col_offset(self) -> int:
        ...

    def tokenize_variables(self) -> Iterator[Token]:
        ...


@runtime_checkable
class HasTokens(Protocol):
    tokens: Tuple[Token, ...]


@runtime_checkable
class HasError(Protocol):
    error: Optional[str]


@runtime_checkable
class HasErrors(Protocol):
    errors: Optional[List[str]]


@runtime_checkable
class Statement(Protocol):
    def get_token(self, type: str) -> Token:
        ...

    def get_tokens(self, *types: str) -> Tuple[Token, ...]:
        ...

    def get_value(self, type: str, default: Any = None) -> Any:
        ...

    def get_values(self, *types: str) -> Tuple[Any, ...]:
        ...

    @property
    def lineno(self) -> int:
        ...

    @property
    def col_offset(self) -> int:
        ...

    @property
    def end_lineno(self) -> int:
        ...

    @property
    def end_col_offset(self) -> int:
        ...


def range_from_token(token: Token) -> Range:
    return Range(
        start=Position(line=token.lineno - 1, character=token.col_offset),
        end=Position(
            line=token.lineno - 1,
            character=token.end_col_offset,
        ),
    )


def token_in_range(token: Token, range: Range) -> bool:
    token_range = range_from_token(token)
    return token_range.start.is_in_range(range) or token_range.end.is_in_range(range)


def node_in_range(node: ast.AST, range: Range) -> bool:
    node_range = range_from_node(node)
    return node_range.start.is_in_range(range) or node_range.end.is_in_range(range)


def range_from_token_or_node(node: ast.AST, token: Optional[Token]) -> Range:
    if token is not None:
        return range_from_token(token)
    if node is not None:
        return range_from_node(node)
    return Range.zero()


def is_non_variable_token(token: Token) -> bool:
    from robot.errors import VariableError

    try:
        r = list(token.tokenize_variables())
        if len(r) == 1 and r[0] == token:
            return True
    except VariableError:
        pass
    return False


def whitespace_at_begin_of_token(token: Token) -> int:
    s = str(token.value)

    result = 0
    for c in s:
        if c == " ":
            result += 1
        elif c == "\t":
            result += 2
        else:
            break
    return result


def whitespace_from_begin_of_token(token: Token) -> str:
    s = str(token.value)

    result = ""
    for c in s:
        if c in [" ", "\t"]:
            result += c
        else:
            break

    return result


def get_tokens_at_position(node: HasTokens, position: Position) -> List[Token]:
    return [t for t in node.tokens if position.is_in_range(range := range_from_token(t)) or range.end == position]


def iter_nodes_at_position(node: ast.AST, position: Position) -> AsyncIterator[ast.AST]:
    return (n async for n in walk(node) if position.is_in_range(range := range_from_node(n)) or range.end == position)


async def get_nodes_at_position(node: ast.AST, position: Position) -> List[ast.AST]:
    return [n async for n in iter_nodes_at_position(node, position)]


async def get_node_at_position(node: ast.AST, position: Position) -> Optional[ast.AST]:
    result_nodes = await get_nodes_at_position(node, position)
    if not result_nodes:
        return None

    return result_nodes[-1]


def _tokenize_no_variables(token: Token) -> Generator[Token, None, None]:
    yield token


def tokenize_variables(
    token: Token, identifiers: str = "$@&%", ignore_errors: bool = False
) -> Generator[Token, Any, Any]:
    from robot.api.parsing import Token as RobotToken
    from robot.variables import VariableIterator

    if token.type not in {*RobotToken.ALLOW_VARIABLES, RobotToken.KEYWORD, RobotToken.ASSIGN}:
        return _tokenize_no_variables(token)
    variables = VariableIterator(token.value, identifiers=identifiers, ignore_errors=ignore_errors)
    if not variables:
        return _tokenize_no_variables(token)
    return _tokenize_variables(token, variables)


def _tokenize_variables(token: Token, variables: Any) -> Generator[Token, Any, Any]:
    from robot.api.parsing import Token as RobotToken

    lineno = token.lineno
    col_offset = token.col_offset
    remaining = ""
    for before, variable, remaining in variables:
        if before:
            yield RobotToken(token.type, before, lineno, col_offset)
            col_offset += len(before)
        yield RobotToken(RobotToken.VARIABLE, variable, lineno, col_offset)
        col_offset += len(variable)
    if remaining:
        yield RobotToken(token.type, remaining, lineno, col_offset)
