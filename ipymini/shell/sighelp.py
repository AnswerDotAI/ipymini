"Signature help for the call at a cursor: the callable's parameters, and the index of the one being typed."
import inspect, io, tokenize

import jedi
from IPython.utils.tokenutil import token_at_cursor


def _param_idx(code, cursor_pos):
    "Index of the argument being typed at `cursor_pos`, in the innermost open call"
    toks = []
    g = tokenize.generate_tokens(io.StringIO(code[:cursor_pos]).readline)
    while True:
        try: toks.append(next(g))
        except (tokenize.TokenError, StopIteration): break
    depth, idx = 0, 0
    for t in reversed(toks):
        if t.type != tokenize.OP: continue
        if t.string in ")]}": depth += 1
        elif t.string in "([{":
            if depth == 0: return idx
            depth -= 1
        elif t.string == "," and depth == 0: idx += 1
    return 0


def _sig_dyn(ipy, code, cursor_pos):
    "Signature help from the live object named at the cursor"
    name = token_at_cursor(code, cursor_pos)
    info = ipy._object_find(name)
    if not (info.found and callable(info.obj)): return []
    try: ps = list(inspect.signature(info.obj).parameters.values())
    except (ValueError, TypeError): return []
    idx = _param_idx(code, cursor_pos)
    vp = next((i for i, p in enumerate(ps) if p.kind is p.VAR_POSITIONAL), None)
    if vp is not None and idx > vp: idx = vp  # *args takes every positional argument after it
    return [dict(label=name, typ=type(info.obj).__name__, mod=getattr(info.obj, "__module__", ""), idx=idx,
        doc=info.obj.__doc__ or "", params=[dict(name=p.name, desc=str(p)) for p in ps])]


def _sig_jedi(ipy, code, cursor_pos):
    "Signature help from jedi's static analysis, for calls that the namespace can't resolve, such as to a function defined in the same cell"
    line, col = code.count("\n", 0, cursor_pos) + 1, cursor_pos - code.rfind("\n", 0, cursor_pos) - 1
    sigs = jedi.Interpreter(code, [ipy.user_ns]).get_signatures(line, col) or jedi.Script(code).get_signatures(line, col)
    return [dict(label=s.description, typ=s.type, mod=s.module_name, doc=s.docstring(), idx=s.index,
        params=[dict(name=p.name, desc=p.description) for p in s.params]) for s in sigs]


def sig_help(ipy, code, cursor_pos):
    "Signature help for the call at `cursor_pos` in `code`"
    return _sig_dyn(ipy, code, cursor_pos) or _sig_jedi(ipy, code, cursor_pos)
