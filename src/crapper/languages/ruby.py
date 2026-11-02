"""Ruby methods, singleton methods, and cyclomatic complexity.

Decision points: `if`, `elsif`, `unless`, `while`, `until`, `for`, each
`rescue`, each `when` and `in` clause, a pattern guard, the ternary, the
modifier forms (`x if y`, `x rescue y`, ...), each `&&` / `||` / `and` /
`or`, safe navigation `&.`, and conditional assignment `||=` / `&&=`.

`def` and `def self.` are entries. A method defined inside another method,
and every block or lambda, stays inside the enclosing method. The namespace
is the lexical `module` / `class` nesting joined with `::`, where
`Point = Struct.new do ... end` (or `Class.new`, `Data.define`) counts as
class `Point`. A singleton
method (`def self.x`, or `def x` inside `class << self`) is named `self.x`.
A method outside any class or module is namespaced by its file path, without
the `lib/` or `src/` prefix and the `.rb` suffix.
"""

from crapper.languages.treesitter import (
    complexity,
    descendants,
    end_line,
    node_text,
    parse,
    start_line,
)
from crapper.model import Function

_METHODS = {"method", "singleton_method"}
_SCOPES = {"class", "module"}

_DECISIONS = {
    "if",
    "elsif",
    "unless",
    "while",
    "until",
    "for",
    "rescue",
    "when",
    "in_clause",
    "if_guard",
    "unless_guard",
    "conditional",
    "if_modifier",
    "unless_modifier",
    "while_modifier",
    "until_modifier",
    "rescue_modifier",
}

_LOGIC = {"&&", "||", "and", "or"}
_CONDITIONAL_ASSIGNMENT = {"||=", "&&="}


def _operator(node) -> str | None:
    operator = node.child_by_field_name("operator")
    if operator is None:
        return None
    return operator.type


def _is_decision(node) -> bool:
    # Keyword tokens share their construct's type (`elsif` inside `elsif`),
    # so only named nodes count.
    if not node.is_named:
        return False
    if node.type in _DECISIONS:
        return True
    if node.type == "binary":
        return _operator(node) in _LOGIC
    if node.type == "operator_assignment":
        return _operator(node) in _CONDITIONAL_ASSIGNMENT
    if node.type == "call":
        return _operator(node) == "&."
    return False


def _skip(node) -> bool:
    return node.type in _SCOPES or node.type == "singleton_class"


def _strip_prefix(relative: str, prefix: str) -> str:
    if relative.startswith(prefix + "/"):
        return relative[len(prefix) + 1 :]
    if f"/{prefix}/" in relative:
        return relative.split(f"/{prefix}/", 1)[1]
    return relative


def _file_namespace(path: str, project_root: str | None) -> str:
    relative = path.replace("\\", "/")
    root = (project_root or "").replace("\\", "/").rstrip("/")
    if root and root != "." and relative.startswith(root + "/"):
        relative = relative[len(root) + 1 :]
    while relative.startswith("./"):
        relative = relative[2:]
    for prefix in ("lib", "src"):
        stripped = _strip_prefix(relative, prefix)
        if stripped != relative:
            relative = stripped
            break
    if relative.endswith(".rb"):
        relative = relative[: -len(".rb")]
    return relative


def _scope_name(data: bytes, node) -> str | None:
    name = node.child_by_field_name("name")
    if name is None:
        return None
    return node_text(data, name).lstrip(":")


def _assigned_constant(data: bytes, block) -> str | None:
    """`Point = Struct.new(:x) do ... end` names a class, as `Class.new`,
    `Module.new`, and `Data.define` do. The block's methods belong to it."""

    call = block.parent
    assignment = call.parent if call is not None else None
    if assignment is None or assignment.type != "assignment":
        return None
    target = assignment.child_by_field_name("left")
    if target.type not in {"constant", "scope_resolution"}:
        return None
    return node_text(data, target).removeprefix("self::").lstrip(":")


def _scope_of(data: bytes, node) -> str | None:
    if node.type in _SCOPES:
        return _scope_name(data, node)
    if node.type in {"block", "do_block"}:
        return _assigned_constant(data, node)
    return None


def _is_nested_method(node) -> bool:
    current = node.parent
    while current is not None:
        if current.type in _METHODS:
            return True
        current = current.parent
    return False


def _enclosing(data: bytes, node) -> tuple[list[str], bool]:
    """Lexical module/class names, outermost first, and whether the method
    sits inside `class << self`."""

    names: list[str] = []
    singleton = False
    current = node.parent
    while current is not None:
        name = _scope_of(data, current)
        if name:
            names.append(name)
        elif current.type == "singleton_class" and not names:
            singleton = True
        current = current.parent
    names.reverse()
    return names, singleton


def _method_name(data: bytes, node, singleton: bool) -> str | None:
    name = node.child_by_field_name("name")
    if name is None:
        return None
    text = node_text(data, name)
    if node.type == "singleton_method":
        owner = node.child_by_field_name("object")
        owner_text = node_text(data, owner) if owner is not None else "self"
        return f"{owner_text}.{text}"
    if singleton:
        return f"self.{text}"
    return text


def functions_in_source(
    source: str, path: str, project_root: str | None = None
) -> list[Function]:
    data, tree = parse(source, "ruby")
    found: list[Function] = []
    for node in descendants(tree.root_node):
        if node.type not in _METHODS or _is_nested_method(node):
            continue
        scopes, singleton = _enclosing(data, node)
        name = _method_name(data, node, singleton)
        if name is None:
            continue
        namespace = "::".join(scopes) if scopes else _file_namespace(path, project_root)
        found.append(
            Function(
                name=name,
                namespace=namespace,
                complexity=complexity(node, _is_decision, _skip),
                start_line=start_line(node),
                end_line=end_line(node),
                path=path,
                language="ruby",
            )
        )
    return found
