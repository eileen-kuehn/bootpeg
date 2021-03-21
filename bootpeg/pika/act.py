"""
Pika bottom-up Peg parser extension to transform parsed source
"""
from typing import TypeVar, Any, Dict, Tuple
import re

from .peg import Clause, D, Match, MemoTable, MemoKey, Literal, nested_str, Reference
from ..utility import mono, cache_hash


#: Parser result: The output type for parsing, such as (str, ...)
R = TypeVar("R", contravariant=True)


class Debug(Clause[D]):
    def __init__(self, sub_clause, name: str = None):
        self.sub_clauses = (sub_clause,)
        self.name = name or str(sub_clause)

    @property
    def maybe_zero(self):
        return self.sub_clauses[0].maybe_zero

    def match(self, source: D, at: int, memo: MemoTable):
        try:
            parent_match = memo[MemoKey(at, self.sub_clauses[0])]
        except KeyError:
            return None
        else:
            print("match", self.name, "at", at, ":", parent_match.length)
            print("'", mono(source[at : at + parent_match.length]), "'", sep="")
            return parent_match

    def __eq__(self, other):
        return isinstance(other, Literal) and self.sub_clauses == other.sub_clauses

    def __hash__(self):
        return hash(self.sub_clauses)

    def __repr__(self):
        return f"{self.__class__.__name__}({self.sub_clauses[0]!r})"

    def __str__(self):
        return f":{self.name}:"


class Capture(Clause[D]):
    """Capture the result of matching a clause by name for an :py:class:`~.Action`"""

    __slots__ = ("name", "sub_clauses", "_hash")

    def __init__(self, name, sub_clause: Clause[D]):
        self.name = name
        self.sub_clauses = (sub_clause,)
        self._hash = None

    @property
    def maybe_zero(self):
        return self.sub_clauses[0].maybe_zero

    def match(self, source: D, at: int, memo: MemoTable):
        parent_match = memo[MemoKey(at, self.sub_clauses[0])]
        return Match(parent_match.length, (parent_match,), at, self)

    def __eq__(self, other):
        return (
            isinstance(other, Capture)
            and self.name == other.name
            and self.sub_clauses == other.sub_clauses
        )

    @cache_hash
    def __hash__(self):
        return hash((self.name, self.sub_clauses))

    def __repr__(self):
        return f"{self.__class__.__name__}({self.sub_clauses[0]!r})"

    def __str__(self):
        return f"{self.name}={nested_str(self.sub_clauses[0])}"


def captures(head: Clause):
    if isinstance(head, Capture):
        yield head
    elif isinstance(head, Reference):
        return
    else:
        for clause in head.sub_clauses:
            yield from captures(clause)


class ActionCaptureError(TypeError):
    def __init__(self, missing, extra, rule):
        self.missing = missing
        self.extra = extra
        self.rule = rule
        msg = f"extra {', '.join(map(repr, extra))}" if extra else ""
        msg += " and " if extra and missing else ""
        msg += f"missing {', '.join(map(repr, missing))}" if missing else ""
        super().__init__(f"{msg} captures: {self.rule}")


class Rule(Clause[D]):
    __slots__ = ("sub_clauses", "action", "_hash")

    def __init__(self, sub_clause: Clause[D], action: "Action"):
        self.sub_clauses = (sub_clause,)
        self.action = action
        self._hash = None
        self._verify_captures()

    def _verify_captures(self):
        captured_names = {capture.name for capture in captures(self.sub_clauses[0])}
        if captured_names.symmetric_difference(self.action.parameters):
            additional = captured_names.difference(self.action.parameters)
            missing = set(self.action.parameters) - captured_names
            raise ActionCaptureError(missing=missing, extra=additional, rule=self)

    @property
    def maybe_zero(self):
        return self.sub_clauses[0].maybe_zero

    def match(self, source: D, at: int, memo: MemoTable):
        parent_match = memo[MemoKey(at, self.sub_clauses[0])]
        return Match(parent_match.length, (parent_match,), at, self)

    def __eq__(self, other):
        return (
            isinstance(other, Rule)
            and self.action == other.action
            and self.sub_clauses == other.sub_clauses
        )

    @cache_hash
    def __hash__(self):
        return hash((self.action, self.sub_clauses))

    def __repr__(self):
        return f"{self.__class__.__name__}({self.sub_clauses[0]!r}, {self.action!r})"

    def __str__(self):
        return f"| {self.sub_clauses[0]} {self.action}"


class Discard:
    def __str__(self):
        return "∅"

    def __repr__(self):
        return f"{self.__class__.__name__}()"


class Action:
    __slots__ = ("literal", "_py_names", "_py_source", "_py_code")
    # TODO: Define these via a PEG parser
    unpack = re.compile(r"\.\*")
    named = re.compile(r"(^|[ (])\.([a-zA-Z]+)")
    mangle = "__pika_act_"

    def __init__(self, literal: str):
        self.literal = literal.strip()
        self._py_names = tuple(match.group(2) for match in self.named.finditer(literal))
        self._py_source = self._encode(self.literal)
        self._py_code = compile(self._py_source, self._py_source, "eval")

    @property
    def parameters(self) -> Tuple[str, ...]:
        """The parameter names used by the action"""
        return self._py_names

    def __call__(self, __namespace, *args, **kwargs):
        return eval(self._py_code, __namespace)(*args, **kwargs)

    def _encode(self, literal):
        names = [f"{self.mangle}{name}" for name in self._py_names]
        body = self.named.sub(
            rf"\1 {self.mangle}\2", self.unpack.sub(rf" {self.mangle}all", literal)
        )
        return f'lambda {self.mangle}all, {", ".join(names)}: {body}'

    def __eq__(self, other):
        return isinstance(other, Action) and self.literal == other.literal

    def __hash__(self):
        return hash(self.literal)

    def __str__(self):
        return f"{{{self.literal}}}"

    def __repr__(self):
        return f"{self.__class__.__name__}({self.literal!r})"


class TransformFailure(Exception):
    def __init__(self, clause, matches, captures, exc: Exception):
        super().__init__(f"failed to transform {clause}: {exc}")
        self.clause = clause
        self.matches = matches
        self.captures = captures
        self.exc = exc


def transform(head: Match, memo: MemoTable, namespace: Dict[str, Any]):
    return postorder_transform(head, memo.source, namespace)


# TODO: Use trampoline/coroutines for infinite depth
def postorder_transform(
    match: Match, source: D, namespace: Dict[str, Any]
) -> Tuple[Any, Dict[str, Any]]:
    matches, captures = (), {}
    for sub_match in match.sub_matches:
        sub_matches, sub_captures = postorder_transform(sub_match, source, namespace)
        matches += sub_matches
        captures.update(sub_captures)
    position, clause = match.position, match.clause
    if isinstance(clause, Capture):
        assert len(matches) <= 1, "Captured rule must provide no more than one value"
        captures[Action.mangle + clause.name] = (
            matches[0] if matches else source[position : position + match.length]
        )
        return (), captures
    elif isinstance(clause, Rule):
        matches = matches if matches else source[position : position + match.length]
        try:
            result = clause.action(namespace, matches, **captures)
        except ActionCaptureError:
            raise
        except Exception as exc:
            raise TransformFailure(clause, matches, captures, exc)
        return (result,) if not isinstance(result, Discard) else (), {}
    return matches, captures
