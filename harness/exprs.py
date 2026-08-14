"""A tiny, deterministic condition language for pack rules.

Rule conditions and report milestones are plain text expressions like

    idle_s >= 120 and runs_total == 0

They are parsed once at pack load and evaluated against a flat dict of
engine-supplied facts. Only literals, names, arithmetic, comparisons
and boolean operators are allowed — no calls, no attributes, no
subscripts — so a pack can never reach outside the facts it is given,
and evaluation is a pure function of those facts.
"""

from __future__ import annotations

import ast
from typing import Any, Iterable, Mapping

_ALLOWED_NODES = (
    ast.Expression,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.UnaryOp,
    ast.Not,
    ast.USub,
    ast.BinOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Compare,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.List,
    ast.Tuple,
)

_WORD_LITERALS = {"true": True, "false": False, "none": None}


class ExprError(ValueError):
    pass


class Expr:
    """A compiled pack expression. Call it with a facts mapping."""

    def __init__(self, text: str, allowed_names: Iterable[str]) -> None:
        self.text = str(text)
        allowed = set(allowed_names)
        try:
            tree = ast.parse(self.text, mode="eval")
        except SyntaxError as exc:
            raise ExprError("bad expression %r: %s" % (self.text, exc)) from None
        for node in ast.walk(tree):
            if not isinstance(node, _ALLOWED_NODES):
                raise ExprError(
                    "%r: %s is not allowed here" % (self.text, type(node).__name__)
                )
            if isinstance(node, ast.Name):
                low = node.id.lower()
                if low not in _WORD_LITERALS and node.id not in allowed:
                    raise ExprError("%r: unknown fact %r" % (self.text, node.id))
        self._tree = tree.body

    def __call__(self, facts: Mapping[str, Any]) -> Any:
        return self._eval(self._tree, facts)

    def as_bool(self, facts: Mapping[str, Any]) -> bool:
        return bool(self._eval(self._tree, facts))

    def _eval(self, node: ast.AST, facts: Mapping[str, Any]) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            low = node.id.lower()
            if low in _WORD_LITERALS:
                return _WORD_LITERALS[low]
            return facts[node.id]
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                result: Any = True
                for value in node.values:
                    result = self._eval(value, facts)
                    if not result:
                        return result
                return result
            for value in node.values:
                result = self._eval(value, facts)
                if result:
                    return result
            return result
        if isinstance(node, ast.UnaryOp):
            operand = self._eval(node.operand, facts)
            if isinstance(node.op, ast.Not):
                return not operand
            return -operand
        if isinstance(node, ast.BinOp):
            left = self._eval(node.left, facts)
            right = self._eval(node.right, facts)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right
            if isinstance(node.op, ast.FloorDiv):
                return left // right
            return left % right
        if isinstance(node, ast.Compare):
            left = self._eval(node.left, facts)
            for op, comparator in zip(node.ops, node.comparators):
                right = self._eval(comparator, facts)
                if not self._compare(op, left, right):
                    return False
                left = right
            return True
        if isinstance(node, (ast.List, ast.Tuple)):
            return [self._eval(item, facts) for item in node.elts]
        raise ExprError("unreachable node %s" % type(node).__name__)

    @staticmethod
    def _compare(op: ast.cmpop, left: Any, right: Any) -> bool:
        if isinstance(op, ast.Eq):
            return left == right
        if isinstance(op, ast.NotEq):
            return left != right
        if isinstance(op, ast.Lt):
            return left < right
        if isinstance(op, ast.LtE):
            return left <= right
        if isinstance(op, ast.Gt):
            return left > right
        if isinstance(op, ast.GtE):
            return left >= right
        if isinstance(op, ast.In):
            return left in right
        return left not in right
