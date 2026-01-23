#!/usr/bin/env python
import ast, io, os, re, sys, tokenize

SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".venv", "venv", "dist", "build",
}
COMPOUND_NODES = (
    ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try,
    ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
)

def iter_py_files(root: str):
    "Iter py files."
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if not name.endswith(".py"): continue
            path = os.path.join(dirpath, name)
            if os.path.islink(path): continue
            yield path

def is_identifier_str(node) -> bool:
    "Identifier str."
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.isidentifier()

def is_docstring_stmt(stmt) -> bool:
    "Docstring stmt."
    return isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str)

def node_lines(source: str, lines: list[str], node) -> list[str]:
    "Node lines."
    seg = ast.get_source_segment(source, node)
    if seg: return [line.rstrip() for line in seg.splitlines()]
    lineno = getattr(node, "lineno", None)
    if lineno and 1 <= lineno <= len(lines): return [lines[lineno - 1].rstrip("\n")]
    return []

def suite_len(lines: list[str], header_lineno: int | None, stmt_lineno: int | None) -> int | None:
    "Suite length."
    if not header_lineno or not stmt_lineno: return None
    if header_lineno < 1 or stmt_lineno < 1: return None
    if header_lineno > len(lines) or stmt_lineno > len(lines): return None
    first = lines[header_lineno - 1]
    second = lines[stmt_lineno - 1]
    indent = len(first) - len(first.lstrip())
    return len(first.strip()) + len(second.strip()) + indent

def find_suite_header(lines: list[str], start: int, stop: int, keyword: str) -> int:
    "Find suite header."
    if start < 1 or stop < 1 or start > len(lines): return stop
    stop = max(1, min(stop, len(lines)))
    for idx in range(start - 1, stop - 2, -1):
        if lines[idx].lstrip().startswith(f"{keyword}:"): return idx + 1
    return stop

def add_violation(violations: list[tuple[str, int, str, list[str]]], path: str, lineno: int,
    msg: str, lines: list[str]):
    "Add violation."
    violations.append((path, lineno, msg, lines))

def check_single_line_docstring(source: str, lines: list[str], stmt, path: str,
    violations: list[tuple[str, int, str, list[str]]]):
    "Check single-line docstring."
    doc = stmt.value.value
    if "\n" in doc: return
    seg = ast.get_source_segment(source, stmt) or ""
    if re.match(r'^[ \t]*[rRuUbBfF]*\"\"\"', seg): add_violation(violations, path, stmt.lineno,
        "single-line docstring uses triple quotes", node_lines(source, lines, stmt))

def check_suite(parent_kind: str, node, suite, path: str, source: str, lines: list[str],
    violations: list[tuple[str, int, str, list[str]]]):
    "Check single-statement suites."
    if not suite: return
    if len(suite) != 1: return
    stmt = suite[0]
    if is_docstring_stmt(stmt): return
    if parent_kind == "else" and isinstance(node, ast.If) and isinstance(stmt, ast.If): return
    if isinstance(stmt, COMPOUND_NODES): return
    if getattr(stmt, "end_lineno", stmt.lineno) > stmt.lineno: return
    header_lineno = getattr(node, "lineno", stmt.lineno)
    if parent_kind in ("else", "finally"): header_lineno = find_suite_header(lines, stmt.lineno, header_lineno, parent_kind)
    if stmt.lineno <= header_lineno: return
    total_len = suite_len(lines, header_lineno, stmt.lineno)
    if total_len is not None and total_len > 130: return
    header_line = lines[header_lineno - 1].rstrip("\n")
    body_line = lines[stmt.lineno - 1].rstrip("\n")
    add_violation(violations, path, header_lineno, f"{parent_kind} single-statement body not one-liner",
        [header_line] if header_lineno == stmt.lineno else [header_line, body_line])

def check_file(path: str) -> list[tuple[str, int, str, list[str]]]:
    "Check file."
    with open(path, encoding="utf-8") as f: source = f.read()
    lines = source.splitlines()
    try: tree = ast.parse(source, filename=path)
    except SyntaxError as e: return [(path, e.lineno or 1, f"syntax error: {e.msg}", [])]
    violations: list[tuple[str, int, str, list[str]]] = []
    for lineno, line in enumerate(lines, start=1):
        if len(line) > 150: add_violation(violations, path, lineno, "line >150 chars", [line])
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.OP and tok.string == ";":
                lineno = tok.start[0]
                add_violation(violations, path, lineno, "semicolon statement separator", [lines[lineno - 1]])
    except tokenize.TokenError: pass
    if tree.body and is_docstring_stmt(tree.body[0]): check_single_line_docstring(
        source, lines, tree.body[0], path, violations)
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict) and len(node.keys) >= 3 and all(is_identifier_str(k) for k in node.keys): add_violation(
            violations, path, node.lineno, "dict literal with 3+ identifier keys",
            node_lines(source, lines, node))
        if isinstance(node, ast.ImportFrom):
            seg = ast.get_source_segment(source, node) or ""
            if "\n" in seg:
                import_lines = node_lines(source, lines, node)
                total_len = sum(len(line.strip()) for line in import_lines)
                if total_len <= 150: add_violation(violations, path, node.lineno,
                    "multi-line from-import", import_lines)
        has_doc = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body
        if has_doc and is_docstring_stmt(node.body[0]): check_single_line_docstring(
            source, lines, node.body[0], path, violations)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)): check_suite(
            "def", node, node.body, path, source, lines, violations)
        elif isinstance(node, ast.If):
            check_suite("if", node, node.body, path, source, lines, violations)
            check_suite("else", node, node.orelse, path, source, lines, violations)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            check_suite("for", node, node.body, path, source, lines, violations)
            check_suite("else", node, node.orelse, path, source, lines, violations)
        elif isinstance(node, ast.While):
            check_suite("while", node, node.body, path, source, lines, violations)
            check_suite("else", node, node.orelse, path, source, lines, violations)
        elif isinstance(node, (ast.With, ast.AsyncWith)): check_suite(
            "with", node, node.body, path, source, lines, violations)
        elif isinstance(node, ast.Try):
            check_suite("try", node, node.body, path, source, lines, violations)
            for handler in node.handlers: check_suite(
                "except", handler, handler.body, path, source, lines, violations)
            check_suite("else", node, node.orelse, path, source, lines, violations)
            check_suite("finally", node, node.finalbody, path, source, lines, violations)
    return violations

def main(argv: list[str]) -> int:
    "Main."
    root = argv[1] if len(argv) > 1 else "."
    all_violations: list[tuple[str, int, str, list[str]]] = []
    for path in iter_py_files(root): all_violations.extend(check_file(path))
    for path, lineno, msg, lines in sorted(all_violations,
        key=lambda item: (item[0], item[1], item[2])):
        print(f"# {path}:{lineno}: {msg}")
        for line in lines: print(line)
    print(f"found {len(all_violations)} potential violation(s)")
    return 1 if all_violations else 0

if __name__ == "__main__": raise SystemExit(main(sys.argv))
