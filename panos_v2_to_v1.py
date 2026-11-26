#!/usr/bin/env python3
"""
panos_v2_to_v1.py - convert PAN-OS Terraform code written in the provider v2 style back to
provider v1.x (schemas checked against v1.11.1), e.g. so it runs on Terraform 1.5.7.

Works on MIXED code: resources already written in v1 style are left untouched; only the
v2-style ones are rewritten.

Usage:
    python panos_v2_to_v1.py <file-or-folder> [-o OUTPUT_FOLDER]

    python panos_v2_to_v1.py .                 -> converts every *.tf in this folder into ./v1_converted/
    python panos_v2_to_v1.py objects.tf -o out

Your original files are never modified. Review the report printed at the end; anything the
script could not convert automatically is listed there and marked  # TODO(v1):  in the output.

Converted (v2 -> v1):
    provider "panos"                 skip_verify_certificate -> verify_certificate (inverted), etc.
    terraform { required_providers } panos version -> "~> 1.11"
    panos_address                    -> panos_address_object        (ip_netmask/fqdn/... -> type + value)
    panos_address_group              -> panos_address_group         (static -> static_addresses, tag -> tags,
                                                                     dynamic.filter -> dynamic_match)
    panos_service                    -> panos_service_object        (protocol = { tcp = {...} } -> protocol/ports)
    panos_service_group              -> panos_service_group         (members -> services)
    panos_administrative_tag         -> panos_administrative_tag    (comments -> comment)
    panos_security_policy            -> panos_security_policy       (rules = [...] -> rule { } blocks)
    panos_security_policy_rules      -> panos_security_rule_group   (position -> position_keyword/reference)
    panos_addresses                  -> panos_address_objects       (addresses = { ... } map -> object { } blocks)
    location = { vsys / device_group / shared }  -> vsys / device_group  (Panorama objects become the
                                                                     v1 panos_panorama_* resource types)
    References to renamed resources everywhere in your code.

Only the *.tf files directly in the given folder are converted (sub-folders / modules are not).
This converts the CODE only: state written by the v2 provider cannot be read by v1, so anything
already applied with v2 has to be imported into a new state.
"""
import argparse
import os
import re
import sys

# --------------------------------------------------------------------------------------
# Minimal HCL reader: keeps every expression as raw text so variables, functions,
# for-expressions etc. survive untouched.
# --------------------------------------------------------------------------------------

IDENT = r"[A-Za-z_][A-Za-z0-9_-]*"
RE_ATTR = re.compile(r"^(\s*)(" + IDENT + r")\s*=(?!=)\s*(.*)$", re.S)
RE_BLOCK = re.compile(r"^(\s*)(" + IDENT + r")((?:\s+\"[^\"]*\"|\s+" + IDENT + r")*)\s*\{(.*)$", re.S)
RE_HEREDOC = re.compile(r"<<-?\s*([A-Za-z_][A-Za-z0-9_]*)\s*$")
RE_HEREDOC_ANYWHERE = re.compile(r"<<-?\s*[A-Za-z_][A-Za-z0-9_]*\s*$", re.M)


def bracket_delta(line):
    """Net change in ({[ depth for one line, ignoring strings and comments."""
    d = 0
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if c == '"':
            i += 1
            depth_interp = 0
            while i < n:
                if line[i] == "\\":
                    i += 2
                    continue
                if line[i:i + 2] == "${":
                    depth_interp += 1
                    i += 2
                    continue
                if line[i] == "}" and depth_interp:
                    depth_interp -= 1
                elif line[i] == '"' and not depth_interp:
                    break
                i += 1
        elif c == "#" or line[i:i + 2] == "//":
            break
        elif c in "({[":
            d += 1
        elif c in ")}]":
            d -= 1
        i += 1
    return d


class Attr:
    def __init__(self, key, expr):
        self.key, self.expr = key, expr.strip()


class Block:
    def __init__(self, btype, labels, items, raw):
        self.type, self.labels, self.items, self.raw = btype, labels, items, raw

    def attrs(self):
        return {i.key: i.expr for i in self.items if isinstance(i, Attr)}

    def blocks(self, name=None):
        return [i for i in self.items if isinstance(i, Block) and (name is None or i.type == name)]

    def comments(self):
        return [i for i in self.items if isinstance(i, str) and i.strip()]


def _collect(lines, i, depth):
    """Consume lines starting at i until bracket depth (starting at `depth`) returns to 0."""
    start = i
    heredoc = None
    first = True
    while i < len(lines):
        line = lines[i]
        if heredoc:
            if line.strip() == heredoc:
                heredoc = None
        else:
            if not first:
                depth += bracket_delta(line)
            m = RE_HEREDOC.search(line)
            if m:
                heredoc = m.group(1)
        first = False
        i += 1
        if depth <= 0 and not heredoc:
            break
    return lines[start:i], i


def parse_body(text):
    lines = text.splitlines(keepends=True)
    items, i = [], 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "//")):
            items.append(line)
            i += 1
            continue
        mb = RE_BLOCK.match(line)
        ma = RE_ATTR.match(line)
        if mb and not ma:
            d = bracket_delta(line)
            if d <= 0:  # one-line block:  name { a = 1 }
                inner = line[line.index("{") + 1: line.rindex("}")]
                chunk = [line]
                i += 1
                body = inner.replace(";", "\n") + "\n"
            else:
                chunk, i = _collect(lines, i, d)
                body = "".join(chunk[1:-1])
            labels = re.findall(r'"([^"]*)"|(' + IDENT + ")", mb.group(3))
            labels = [a or b for a, b in labels]
            items.append(Block(mb.group(2), labels, parse_body(body), "".join(chunk)))
            continue
        if ma:
            d = bracket_delta(line)
            heredoc = RE_HEREDOC.search(line)
            if d > 0 or heredoc:
                # a plain heredoc (d == 0) ends at its terminator line
                chunk, i = _collect(lines, i, max(d, 0))
            else:
                chunk = [line]
                i += 1
            first = chunk[0]
            m0 = RE_ATTR.match(first)
            expr = m0.group(3) + "".join(chunk[1:])
            # strip trailing comment on single-line exprs
            if len(chunk) == 1:
                expr = strip_trailing_comment(expr)
            items.append(Attr(m0.group(2), expr.rstrip()))
            continue
        items.append(line)  # unknown - keep verbatim
        i += 1
    return items


def strip_trailing_comment(expr):
    out, i, n, in_str = [], 0, len(expr), False
    while i < n:
        c = expr[i]
        if c == '"' and (i == 0 or expr[i - 1] != "\\"):
            in_str = not in_str
        if not in_str and (c == "#" or expr[i:i + 2] == "//"):
            break
        out.append(c)
        i += 1
    return "".join(out).rstrip()


# --------------------------------------------------------------------------------------
# HCL writer
# --------------------------------------------------------------------------------------

class Raw(str):
    """An expression to emit as-is."""


def q(s):
    return Raw('"' + s + '"')


def literal(expr):
    """Return the plain value of a quoted string / bool / number literal, else None."""
    if expr is None:
        return None
    e = expr.strip()
    m = re.fullmatch(r'"((?:[^"\\$]|\\.)*)"', e)
    if m:
        return m.group(1)
    if e in ("true", "false") or re.fullmatch(r"-?\d+(\.\d+)?", e):
        return e
    return None


def indent_expr(expr, pad):
    lines = expr.split("\n")
    if len(lines) == 1 or RE_HEREDOC_ANYWHERE.search(expr):
        return expr  # heredoc: the body is content and the terminator must stay where it is
    # re-indent continuation lines relative to their minimum indentation
    rest = [l for l in lines[1:] if l.strip()]
    base = min((len(l) - len(l.lstrip()) for l in rest), default=0)
    out = [lines[0]]
    for l in lines[1:]:
        last = l is lines[-1] and l.strip() in ("]", "}", ")", "])", "})")
        out.append((pad if last else pad + "  ") + l[base:].lstrip() if l.strip() else "")
    return "\n".join(out)


def render(value, pad):
    if isinstance(value, dict):
        if not value:
            return "{}"
        w = max(len(k) for k in value)
        inner = []
        for k, v in value.items():
            if isinstance(v, (dict, list)):
                inner.append(f"{pad}  {k} = {render(v, pad + '  ')}")
            else:
                inner.append(f"{pad}  {k.ljust(w)} = {indent_expr(str(v), pad + '  ')}")
        return "{\n" + "\n".join(inner) + f"\n{pad}}}"
    if isinstance(value, list):
        if not value:
            return "[]"
        parts = [f"{pad}  {render(v, pad + '  ')}," for v in value]
        return "[\n" + "\n".join(parts) + f"\n{pad}]"
    return indent_expr(str(value), pad)


def emit_resource(rtype, name, body, comments=(), passthrough=()):
    """body: ordered dict of key -> Raw / dict / list. passthrough: original meta-arg items."""
    out = [f'resource "{rtype}" "{name}" {{']
    for c in comments:
        out.append("  " + c.strip())
    for item in passthrough:
        if isinstance(item, Attr):
            out.append(f"  {item.key} = {indent_expr(item.expr, '  ')}")
        else:
            raw = item.raw.rstrip("\n").split("\n")
            base = min(len(l) - len(l.lstrip()) for l in raw if l.strip())
            out.extend("  " + l[base:] for l in raw)
    flat = {k: v for k, v in body.items() if not isinstance(v, (dict, list))}
    w = max((len(k) for k in flat), default=0)
    for k, v in body.items():
        if k.startswith("#"):
            out.append(f"  {v}")
        elif isinstance(v, (dict, list)):
            out.append(f"  {k} = {render(v, '  ')}")
        else:
            out.append(f"  {k.ljust(w)} = {indent_expr(str(v), '  ')}")
    out.append("}")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------------------
# Parse v2 object / list literals ( { a = 1, b = { c = "x" } }  /  [ {..}, {..} ] )
# Leaf values stay raw text.
# --------------------------------------------------------------------------------------

class ParseError(Exception):
    pass


def _skip_ws(s, i, newlines=True):
    while i < len(s):
        c = s[i]
        if c in " \t\r" or (newlines and c == "\n"):
            i += 1
        elif c == "#" or s.startswith("//", i):
            while i < len(s) and s[i] != "\n":
                i += 1
        else:
            break
    return i


def _scan_string(s, i):
    """s[i] == '"'; return index after the closing quote."""
    i += 1
    interp = 0
    while i < len(s):
        if s[i] == "\\":
            i += 2
            continue
        if s.startswith("${", i):
            interp += 1
            i += 2
            continue
        if s[i] == "}" and interp:
            interp -= 1
        elif s[i] == '"' and not interp:
            return i + 1
        i += 1
    raise ParseError("unterminated string")


def _scan_raw(s, i):
    """Raw expression: read until , or newline or closing bracket at depth 0."""
    start, depth = i, 0
    while i < len(s):
        c = s[i]
        if c == '"':
            i = _scan_string(s, i)
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and (c in ",\n" or c == "#" or s.startswith("//", i)):
            break
        i += 1
    return Raw(s[start:i].strip()), i


def _scan_balanced(s, i):
    """s[i] is an opening bracket; return index after its partner."""
    depth = 0
    while i < len(s):
        c = s[i]
        if c == '"':
            i = _scan_string(s, i)
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ParseError("unbalanced brackets")


def parse_value(s, i=0):
    i = _skip_ws(s, i)
    if i < len(s) and s[i] in "[{":
        j = _skip_ws(s, i + 1)
        if s.startswith("for ", j) or s.startswith("for\t", j):  # for-expression: keep raw
            end = _scan_balanced(s, i)
            return Raw(s[i:end]), end
        if s[i] == "{":
            return _parse_object(s, i)
        return _parse_list(s, i)
    return _scan_raw(s, i)


def _parse_object(s, i):
    obj = {}
    i += 1
    while True:
        i = _skip_ws(s, i)
        if i >= len(s):
            raise ParseError("unterminated object")
        if s[i] == "}":
            return obj, i + 1
        if s[i] == ",":
            i += 1
            continue
        if s[i] == '"':
            j = _scan_string(s, i)
            key = s[i + 1:j - 1]
            i = j
        else:
            m = re.compile(IDENT).match(s, i)
            if not m:
                raise ParseError(f"bad object key near: {s[i:i + 20]!r}")
            key, i = m.group(0), m.end()
        i = _skip_ws(s, i)
        if i >= len(s) or s[i] not in "=:":
            raise ParseError(f"expected = after {key}")
        val, i = parse_value(s, i + 1)
        obj[key] = val


def _parse_list(s, i):
    items = []
    i += 1
    while True:
        i = _skip_ws(s, i)
        if i >= len(s):
            raise ParseError("unterminated list")
        if s[i] == "]":
            return items, i + 1
        if s[i] == ",":
            i += 1
            continue
        val, i = parse_value(s, i)
        items.append(val)


def parse_expr(expr):
    """Parse an attribute expression; returns dict/list/Raw. Falls back to Raw on anything unusual."""
    try:
        val, end = parse_value(expr)
        if _skip_ws(expr, end) < len(expr):
            return Raw(expr.strip())
        return val
    except ParseError:
        return Raw(expr.strip())


def get(d, *path):
    for p in path:
        if not isinstance(d, dict) or p not in d:
            return None
        d = d[p]
    return d


def to_raw(v, pad="  "):
    """dict/list/Raw -> HCL text (used when a nested value is kept as-is)."""
    if isinstance(v, list) and all(not isinstance(x, (dict, list)) for x in v):
        return "[" + ", ".join(str(x) for x in v) + "]"
    if isinstance(v, (dict, list)):
        return render(v, pad)
    return str(v)


# --------------------------------------------------------------------------------------
# v1 writer (v1 uses nested *blocks* such as  rule { ... }  and  target { ... })
# --------------------------------------------------------------------------------------

def emit_v1(rtype, name, body, comments=(), passthrough=(), todos=()):
    """body: list of (key, value) where value is Raw/str, or ('block', [ (k, v), ... ])."""
    out = [f'resource "{rtype}" "{name}" {{']
    for c in comments:
        out.append("  " + c.strip())
    for t in todos:
        out.append(f"  # TODO(v1): {t}")
    for item in passthrough:
        if isinstance(item, Attr):
            out.append(f"  {item.key} = {indent_expr(item.expr, '  ')}")
        else:
            raw = item.raw.rstrip("\n").split("\n")
            base = min(len(l) - len(l.lstrip()) for l in raw if l.strip())
            out.extend("  " + l[base:] for l in raw)
    out.extend(_emit_items(body, "  "))
    out.append("}")
    return "\n".join(out) + "\n"


def _emit_items(items, pad):
    lines = []
    flat = [k for k, v in items if not (isinstance(v, tuple) and v[0] == "block") and not k.startswith("#")]
    w = max((len(k) for k in flat), default=0)
    for k, v in items:
        if k.startswith("#"):
            lines.extend(pad + l for l in str(v).split("\n"))
        elif isinstance(v, tuple) and v[0] == "block":
            lines.append("")
            lines.append(f"{pad}{k} {{")
            lines.extend(_emit_items(v[1], pad + "  "))
            lines.append(f"{pad}}}")
        else:
            lines.append(f"{pad}{k.ljust(w)} = {indent_expr(to_raw(v, pad), pad)}")
    return lines


# --------------------------------------------------------------------------------------
# Conversions  v2 -> v1
# --------------------------------------------------------------------------------------

META_ATTRS = {"count", "for_each", "depends_on", "provider"}
META_BLOCKS = {"lifecycle", "provisioner", "connection"}

# v1 firewall type, v1 Panorama type
TARGETS = {
    "address": ("panos_address_object", "panos_address_object"),
    "address_group": ("panos_address_group", "panos_panorama_address_group"),
    "service": ("panos_service_object", "panos_panorama_service_object"),
    "service_group": ("panos_service_group", "panos_panorama_service_group"),
    "tag": ("panos_administrative_tag", "panos_panorama_administrative_tag"),
    "policy": ("panos_security_policy", "panos_security_policy"),
    "rule_group": ("panos_security_rule_group", "panos_security_rule_group"),
    "addresses": ("panos_address_objects", "panos_address_objects"),
}

KIND = {
    "panos_address": "address", "panos_address_object": "address",
    "panos_address_group": "address_group", "panos_panorama_address_group": "address_group",
    "panos_service": "service", "panos_service_object": "service", "panos_panorama_service_object": "service",
    "panos_service_group": "service_group", "panos_panorama_service_group": "service_group",
    "panos_administrative_tag": "tag", "panos_panorama_administrative_tag": "tag",
    "panos_security_policy": "policy",
    "panos_security_policy_rules": "rule_group", "panos_security_rule_group": "rule_group",
    "panos_addresses": "addresses",
}

# Attributes that only exist in v2 - if a block has none of these it is already v1 and is left alone.
V2_MARKERS = {
    "address": {"location", "ip_netmask", "ip_range", "fqdn", "ip_wildcard", "disable_override"},
    "address_group": {"location", "static", "dynamic", "tag", "disable_override"},
    "service": {"location", "disable_override"},           # + protocol given as an object
    "service_group": {"location", "members", "disable_override"},
    "tag": {"location", "comments", "disable_override"},
    "policy": {"location", "rules"},
    "rule_group": {"location", "rules", "position"},
    "addresses": {"location", "addresses"},
}


class Ctx:
    def __init__(self):
        self.todo, self.converted, self.left, self.skipped = [], {}, {}, {}
        self.refmap = {}  # (old_type, name) -> new_type
        self.file = ""

    def warn(self, res, msg):
        self.todo.append((self.file, res, msg))


def meta_items(b):
    return [i for i in b.items if (isinstance(i, Attr) and i.key in META_ATTRS)
            or (isinstance(i, Block) and i.type in META_BLOCKS)]


def location_v1(a, res, ctx, policy=False):
    """returns (is_panorama, [(k, v)...])"""
    loc_raw = a.get("location")
    if loc_raw is None:
        out = []
        if "vsys" in a:
            out.append(("vsys", Raw(a["vsys"])))
        if "device_group" in a:
            out.append(("device_group", Raw(a["device_group"])))
            if policy and "rulebase" in a:
                out.append(("rulebase", Raw(a["rulebase"])))
            return True, out
        return False, out
    loc = parse_expr(loc_raw)
    if not isinstance(loc, dict):
        ctx.warn(res, f"location is not a literal object ({loc_raw[:40]}) - set vsys/device_group by hand")
        return False, [("vsys", q("vsys1"))]
    if "vsys" in loc:
        name = get(loc, "vsys", "name")
        if get(loc, "vsys", "ngfw_device") is not None:
            ctx.warn(res, "location.vsys.ngfw_device has no v1 equivalent - dropped")
        return False, [("vsys", name if name is not None else q("vsys1"))]
    if "device_group" in loc:
        out = [("device_group", get(loc, "device_group", "name") or q("shared"))]
        if policy:
            out.append(("rulebase", get(loc, "device_group", "rulebase") or q("pre-rulebase")))
        return True, out
    if "shared" in loc:
        out = [("device_group", q("shared"))]
        if policy:
            out.append(("rulebase", get(loc, "shared", "rulebase") or q("pre-rulebase")))
        return True, out
    ctx.warn(res, f"unknown location {list(loc)} - defaulted to vsys1")
    return False, [("vsys", q("vsys1"))]


def leftover(a, used, res, ctx):
    for k in a:
        if k not in used and k not in META_ATTRS:
            ctx.warn(res, f"argument '{k}' has no v1 equivalent here - dropped, check by hand")


def first_of_list(v, res, ctx, what):
    """v2 list -> v1 single string."""
    if isinstance(v, list):
        if len(v) > 1:
            ctx.warn(res, f"{what}: v1 takes one value, kept the first of {len(v)}")
        return v[0] if v else q("")
    return Raw(f"{v}[0]")


def conv_address(b, ctx):
    a, res = b.attrs(), f"{b.labels[0]}.{b.labels[1]}"
    pano, loc = location_v1(a, res, ctx)
    body = list(loc) + [("name", Raw(a.get("name", '""')))]
    used = {"location", "vsys", "device_group", "name"}
    types = {"ip_netmask": "ip-netmask", "ip_range": "ip-range", "fqdn": "fqdn", "ip_wildcard": "ip-wildcard"}
    found = [k for k in types if k in a]
    if found:
        body += [("type", q(types[found[0]])), ("value", Raw(a[found[0]]))]
        used |= set(found)
    else:
        for k in ("type", "value"):
            if k in a:
                body.append((k, Raw(a[k])))
                used.add(k)
    for k in ("description", "tags"):
        if k in a:
            body.append((k, Raw(a[k])))
            used.add(k)
    used.add("disable_override")
    leftover(a, used, res, ctx)
    return TARGETS["address"][pano], body


def conv_address_group(b, ctx):
    a, res = b.attrs(), f"{b.labels[0]}.{b.labels[1]}"
    pano, loc = location_v1(a, res, ctx)
    body = list(loc) + [("name", Raw(a.get("name", '""')))]
    used = {"location", "vsys", "device_group", "name", "disable_override"}
    if "description" in a:
        body.append(("description", Raw(a["description"])))
    for old, new in (("static", "static_addresses"), ("static_addresses", "static_addresses")):
        if old in a:
            body.append((new, Raw(a[old])))
            used.add(old)
    if "dynamic" in a:
        d = parse_expr(a["dynamic"])
        f = get(d, "filter")
        body.append(("dynamic_match", f if f is not None else Raw(f"{a['dynamic']}.filter")))
        used.add("dynamic")
    if "dynamic_match" in a:
        body.append(("dynamic_match", Raw(a["dynamic_match"])))
        used.add("dynamic_match")
    for old in ("tag", "tags"):
        if old in a:
            body.append(("tags", Raw(a[old])))
            used.add(old)
    used.add("description")
    leftover(a, used, res, ctx)
    return TARGETS["address_group"][pano], body


def conv_service(b, ctx):
    a, res = b.attrs(), f"{b.labels[0]}.{b.labels[1]}"
    pano, loc = location_v1(a, res, ctx)
    body = list(loc) + [("name", Raw(a.get("name", '""')))]
    used = {"location", "vsys", "device_group", "name", "disable_override", "protocol"}
    if "description" in a:
        body.append(("description", Raw(a["description"])))
        used.add("description")
    p = parse_expr(a.get("protocol", '"tcp"'))
    if isinstance(p, dict):
        proto = next((k for k in ("tcp", "udp") if k in p), None)
        if proto is None:
            ctx.warn(res, "protocol object has no tcp/udp - set protocol by hand")
            proto = "tcp"
        inner = p.get(proto) or {}
        if not isinstance(inner, dict):
            ctx.warn(res, f"protocol.{proto} is an expression ({inner}) - set source_port/destination_port by hand")
            inner = {}
        body.append(("protocol", q(proto)))
        if "source_port" in inner:
            body.append(("source_port", inner["source_port"]))
        body.append(("destination_port", inner.get("destination_port", q(""))))
        ov = inner.get("override")
        if isinstance(ov, dict):
            body.append(("override_session_timeout", Raw("true")))
            for k2, k1 in (("timeout", "override_timeout"), ("halfclose_timeout", "override_half_closed_timeout"),
                           ("timewait_timeout", "override_time_wait_timeout")):
                if k2 in ov:
                    body.append((k1, ov[k2]))
    else:  # already v1 style
        body.append(("protocol", p))
        for k in ("source_port", "destination_port", "override_session_timeout", "override_timeout",
                  "override_half_closed_timeout", "override_time_wait_timeout"):
            if k in a:
                body.append((k, Raw(a[k])))
                used.add(k)
    if "tags" in a:
        body.append(("tags", Raw(a["tags"])))
        used.add("tags")
    leftover(a, used, res, ctx)
    return TARGETS["service"][pano], body


def conv_service_group(b, ctx):
    a, res = b.attrs(), f"{b.labels[0]}.{b.labels[1]}"
    pano, loc = location_v1(a, res, ctx)
    body = list(loc) + [("name", Raw(a.get("name", '""')))]
    used = {"location", "vsys", "device_group", "name", "disable_override"}
    for old in ("members", "services"):
        if old in a:
            body.append(("services", Raw(a[old])))
            used.add(old)
    if "tags" in a:
        body.append(("tags", Raw(a["tags"])))
        used.add("tags")
    leftover(a, used, res, ctx)
    return TARGETS["service_group"][pano], body


def conv_tag(b, ctx):
    a, res = b.attrs(), f"{b.labels[0]}.{b.labels[1]}"
    pano, loc = location_v1(a, res, ctx)
    body = list(loc) + [("name", Raw(a.get("name", '""')))]
    used = {"location", "vsys", "device_group", "name", "disable_override"}
    if "color" in a:
        body.append(("color", Raw(a["color"])))
        used.add("color")
    for old in ("comments", "comment"):
        if old in a:
            body.append(("comment", Raw(a[old])))
            used.add(old)
    leftover(a, used, res, ctx)
    return TARGETS["tag"][pano], body


ADDR_TYPES = {"ip_netmask": "ip-netmask", "ip_range": "ip-range", "fqdn": "fqdn", "ip_wildcard": "ip-wildcard"}


def conv_addresses(b, ctx):
    """v2 panos_addresses (map) -> v1 panos_address_objects (repeated object { } blocks)."""
    a, res = b.attrs(), f"{b.labels[0]}.{b.labels[1]}"
    pano, loc = location_v1(a, res, ctx)
    body = list(loc)
    raw = a.get("addresses")
    m = parse_expr(raw) if raw is not None else {}
    if isinstance(m, dict):
        for key, entry in m.items():
            if not isinstance(entry, dict):
                ctx.warn(res, f"address '{key}' is not a literal object ({entry}) - could not be converted")
                entry = {}
            obj = [("name", q(key))]
            found = [k for k in ADDR_TYPES if k in entry]
            if found:
                obj += [("type", q(ADDR_TYPES[found[0]])), ("value", entry[found[0]])]
                if len(found) > 1:
                    ctx.warn(res, f"address '{key}' sets more than one of {found} - kept {found[0]}")
            else:
                ctx.warn(res, f"address '{key}' has no ip_netmask/ip_range/fqdn/ip_wildcard - set type/value by hand")
                obj += [("type", q("ip-netmask")), ("value", q(""))]
            for k in ("description", "tags"):
                if k in entry:
                    obj.append((k, entry[k]))
            for k in entry:
                if k not in ADDR_TYPES and k not in ("description", "tags", "disable_override"):
                    ctx.warn(res, f"address '{key}': '{k}' has no v1 equivalent - dropped")
            body.append(("object", ("block", obj)))
    else:
        # addresses comes from a variable / local / for-expression: use a dynamic block
        lk = lambda k: f'lookup(object.value, "{k}", null)'
        body.append(('dynamic "object"', ("block", [
            ("for_each", m),
            ("content", ("block", [
                ("name", Raw("object.key")),
                ("type", Raw(f'{lk("fqdn")} != null ? "fqdn" : {lk("ip_range")} != null ? "ip-range" : '
                             f'{lk("ip_wildcard")} != null ? "ip-wildcard" : "ip-netmask"')),
                ("value", Raw(f'coalesce({lk("ip_netmask")}, {lk("ip_range")}, {lk("fqdn")}, {lk("ip_wildcard")})')),
                ("description", Raw(lk("description"))),
                ("tags", Raw(lk("tags"))),
            ])),
        ])))
        ctx.warn(res, "addresses is not a literal map - converted to a dynamic \"object\" block, check it")
    leftover(a, {"location", "vsys", "device_group", "addresses"}, res, ctx)
    return TARGETS["addresses"][pano], body


RULE_SAME = ["description", "group_tag", "source_zones", "source_addresses", "negate_source",
             "source_users", "destination_zones", "destination_addresses", "negate_destination",
             "applications", "services", "action", "log_setting", "log_start", "log_end", "disabled",
             "schedule", "icmp_unreachable", "disable_server_response_inspection"]
RULE_RENAMED = {"rule_type": "type", "tag": "tags", "category": "categories", "source_hip": "hip_profiles",
                # already-v1 names pass straight through:
                "type": "type", "tags": "tags", "categories": "categories", "hip_profiles": "hip_profiles"}
RULE_REQUIRED_V1 = ["source_zones", "source_addresses", "source_users", "destination_zones",
                    "destination_addresses", "applications", "services", "categories"]
PROFILES = ["virus", "spyware", "vulnerability", "url_filtering", "file_blocking", "wildfire_analysis",
            "data_filtering"]


def conv_rule(r, res, ctx):
    """r: dict (from v2 rules list) -> list of (k, v) for a v1 rule block."""
    rname = str(r.get("name", '""'))
    items = [("name", r.get("name", q("")))]
    have = set()
    for k in RULE_SAME:
        if k in r:
            items.append((k, r[k]))
            have.add(k)
    for old, new in RULE_RENAMED.items():
        if old in r:
            items.append((new, r[old]))
            have.add(new)
    for k in RULE_REQUIRED_V1:
        if k not in have:
            items.append((k, Raw('["any"]')))
            ctx.warn(res, f"rule {rname}: '{k}' is required in v1 - added [\"any\"]")
    ps = r.get("profile_setting")
    if isinstance(ps, dict):
        if "group" in ps:
            items.append(("group", first_of_list(ps["group"], res, ctx, f"rule {rname} group")))
        profs = ps.get("profiles") or {}
        if isinstance(profs, dict):
            for p in PROFILES:
                if p in profs:
                    items.append((p, first_of_list(profs[p], res, ctx, f"rule {rname} {p}")))
    elif ps is not None:
        ctx.warn(res, f"rule {rname}: profile_setting is an expression - convert by hand")
    for p in PROFILES + ["group"]:  # v1-style profile attrs already present
        if p in r:
            items.append((p, r[p]))
    tgt = r.get("target")
    if isinstance(tgt, dict):
        devs = tgt.get("devices")
        if isinstance(devs, list):
            for d in devs:
                if not isinstance(d, dict):
                    continue
                t = [("serial", d.get("name", q("")))]
                vs = d.get("vsys")
                if isinstance(vs, list):
                    names = [get(x, "name") for x in vs if isinstance(x, dict)]
                    t.append(("vsys_list", Raw("[" + ", ".join(str(n) for n in names) + "]")))
                elif vs is not None:
                    t.append(("vsys_list", Raw(f"[for v in {vs} : v.name]")))
                items.append(("target", ("block", t)))
        if "negate" in tgt:
            items.append(("negate_target", tgt["negate"]))
        if "tags" in tgt:
            ctx.warn(res, f"rule {rname}: target.tags has no v1 equivalent - dropped")
    known = set(RULE_SAME) | set(RULE_RENAMED) | set(PROFILES) | {"name", "profile_setting", "target", "group"}
    for k in r:
        if k not in known:
            ctx.warn(res, f"rule {rname}: '{k}' has no v1 equivalent - dropped")
    return items


def conv_policy(b, ctx, kind):
    a, res = b.attrs(), f"{b.labels[0]}.{b.labels[1]}"
    pano, loc = location_v1(a, res, ctx, policy=True)
    body = list(loc)
    if kind == "rule_group":
        pos = parse_expr(a.get("position", "{}"))
        if "position_keyword" in a:
            body.append(("position_keyword", Raw(a["position_keyword"])))
            if "position_reference" in a:
                body.append(("position_reference", Raw(a["position_reference"])))
        elif isinstance(pos, dict) and pos:
            where = literal(str(pos.get("where", '"last"')))
            directly = literal(str(pos.get("directly", "false"))) == "true"
            kw = {"first": "top", "last": "bottom", "before": "before", "after": "after"}.get(where or "")
            if kw is None:
                ctx.warn(res, f"position.where {pos.get('where')} not understood - set position_keyword by hand")
                kw = "bottom"
            if directly and kw in ("before", "after"):
                kw = "directly " + kw
            body.append(("position_keyword", q(kw)))
            if kw.endswith(("before", "after")):
                body.append(("position_reference", pos.get("pivot", q(""))))
    rules_raw = a.get("rules")
    if rules_raw is not None:
        rules = parse_expr(rules_raw)
        if isinstance(rules, list) and all(isinstance(r, dict) for r in rules):
            for r in rules:
                body.append(("rule", ("block", conv_rule(r, res, ctx))))
        else:
            ctx.warn(res, "rules is not a literal list (e.g. a for-expression) - rewrite as rule { } blocks or a "
                          "dynamic \"rule\" block by hand")
            original = "\n".join("# " + l for l in ("rules = " + rules_raw).split("\n"))
            body.append(("# rules", Raw(
                "# TODO(v1): the rules below were NOT converted. Do not apply this resource until they are\n"
                "# rewritten as rule { } blocks - v1 removes every rule that is not defined here.\n" + original)))
    for rb in b.blocks("rule"):  # already-v1 rule blocks: keep verbatim
        body.append(("__raw__", rb))
    used = {"location", "rules", "position", "vsys", "device_group", "rulebase", "position_keyword",
            "position_reference"}
    leftover(a, used, res, ctx)
    return TARGETS[kind][pano], body


def conv_resource(b, ctx):
    kind = KIND[b.labels[0]]
    a = b.attrs()
    markers = V2_MARKERS[kind]
    is_v2 = bool(markers & set(a)) or b.labels[0] in ("panos_address", "panos_service", "panos_security_policy_rules",
                                                       "panos_addresses")
    if kind == "service" and "protocol" in a and a["protocol"].lstrip().startswith("{"):
        is_v2 = True
    if not is_v2:
        ctx.left[b.labels[0]] = ctx.left.get(b.labels[0], 0) + 1
        return b.raw
    todo_count_before = len(ctx.todo)
    fn = {"address": conv_address, "address_group": conv_address_group, "service": conv_service,
          "service_group": conv_service_group, "tag": conv_tag, "addresses": conv_addresses}.get(kind)
    if fn:
        newtype, body = fn(b, ctx)
    else:
        newtype, body = conv_policy(b, ctx, kind)
    if newtype != b.labels[0]:
        ctx.refmap[(b.labels[0], b.labels[1])] = newtype
    key = f"{b.labels[0]} -> {newtype}"
    ctx.converted[key] = ctx.converted.get(key, 0) + 1
    # split out verbatim v1 rule blocks
    raws = [v for k, v in body if k == "__raw__"]
    body = [(k, v) for k, v in body if k != "__raw__"]
    todos = [message for _, _, message in ctx.todo[todo_count_before:]]
    text = emit_v1(newtype, b.labels[1], body, b.comments(), meta_items(b), todos)
    if raws:
        extra = "".join("\n" + "\n".join("  " + l for l in r.raw.rstrip().split("\n")) + "\n" for r in raws)
        text = text[: text.rstrip().rfind("}")] + extra.lstrip("\n") + "}\n"
    return text


def conv_provider(b, ctx):
    out = ['provider "panos" {']
    for item in b.items:
        if isinstance(item, str):
            if item.strip():
                out.append("  " + item.strip())
            continue
        if isinstance(item, Block):
            out.append("  " + item.raw.strip())
            continue
        k, v = item.key, item.expr
        if k == "skip_verify_certificate":
            lit = literal(v)
            nv = {"true": "false", "false": "true"}.get(lit, f"!({v})")
            out.append(f"  verify_certificate = {nv}")
        elif k == "auth_file":
            out.append(f"  json_config_file = {v}")
        elif k in ("api_key_in_request", "multi_config_batch_size", "sdk_log_categories", "sdk_log_level",
                   "config_file", "panos_version"):
            out.append(f"  # TODO(v1): '{k}' is v2-only - removed (was: {v.splitlines()[0]})")
            ctx.warn('provider "panos"', f"'{k}' removed - not a v1 provider argument")
        else:
            out.append(f"  {k} = {indent_expr(v, '  ')}")
    out.append("}")
    return "\n".join(out) + "\n"


def conv_terraform_block(raw):
    def fix(m):
        obj = m.group(0)
        if re.search(r"\bversion\s*=", obj):
            return re.sub(r'(\bversion\s*=\s*)"[^"]*"', r'\1"~> 1.11"', obj)
        return obj.rstrip("}").rstrip() + '\n      version = "~> 1.11"\n    }'
    return re.sub(r"\bpanos\s*=\s*\{[^}]*\}", fix, raw)


def fix_references(text, ctx):
    # v2 list indexing on rule groups/policies: X.rules[0].name -> X.rule.0.name
    for (old, name), new in ctx.refmap.items():
        # not after a dot (data.<type>.<name> is a data block, which is not converted) and the name
        # must end there (so "g" does not also match a different resource called "g-fw")
        text = re.sub(r"(?<![\w\".])" + re.escape(old) + r"\." + re.escape(name) + r"(?![\w-])",
                      new + "." + name, text)
    text = re.sub(r"\b(panos_security_(?:rule_group|policy)\.[\w-]+)\.rules\[(\d+)\]\.", r"\1.rule.\2.", text)
    return text


# attribute names that only exist in v2, per v1 resource type
V2_ONLY_ATTRIBUTES = {
    "panos_address_object": {"ip_netmask": "value", "ip_range": "value", "fqdn": "value", "ip_wildcard": "value"},
    "panos_address_group": {"static": "static_addresses", "dynamic": "dynamic_match", "tag": "tags"},
    "panos_panorama_address_group": {"static": "static_addresses", "dynamic": "dynamic_match", "tag": "tags"},
    "panos_service_group": {"members": "services"},
    "panos_panorama_service_group": {"members": "services"},
    "panos_administrative_tag": {"comments": "comment"},
    "panos_panorama_administrative_tag": {"comments": "comment"},
}


def check_attribute_references(text, ctx):
    """Report references such as panos_address_object.web.ip_netmask whose attribute is named differently in v1."""
    for rtype, attributes in V2_ONLY_ATTRIBUTES.items():
        pattern = (r"(?<![\w\".])(" + rtype + r"\.[\w-]+)(?:\[[^\]]*\])?\.("
                   + "|".join(attributes) + r")\b")
        for ref, attribute in sorted(set(re.findall(pattern, text))):
            ctx.warn(ref, f"something refers to .{attribute}, which is .{attributes[attribute]} in v1 "
                          f"- fix that reference")


def flag_leftovers(fname, text, ctx):
    """Report references that have no v1 equivalent."""
    for n, line in enumerate(text.split("\n"), 1):
        if re.search(r"panos_address_objects\.[\w-]+\.addresses\b", line):
            ctx.todo.append((fname + " (converted)", f"line {n}", "refers to the v2 addresses map - in v1 use the object name as a "
                             "string (e.g. \"web-server\") plus depends_on = [panos_address_objects.<name>]"))
        if "provider::panos::" in line:
            ctx.todo.append((fname + " (converted)", f"line {n}", "uses a v2 provider function (provider::panos::...) - not "
                             "available in v1 (or Terraform 1.5), rewrite by hand"))


def convert_text(text, ctx):
    out = []
    for item in parse_body(text):
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, Attr):
            out.append(f"{item.key} = {item.expr}\n")
        elif item.type == "resource" and len(item.labels) == 2 and item.labels[0] in KIND:
            out.append(conv_resource(item, ctx))
        elif item.type == "provider" and item.labels and item.labels[0] == "panos":
            out.append(conv_provider(item, ctx))
        elif item.type == "terraform":
            out.append(conv_terraform_block(item.raw))
        else:
            if item.type in ("resource", "data") and item.labels and item.labels[0].startswith("panos_") \
                    and "location" in item.attrs():
                key = f"{item.type} {item.labels[0]}"
                ctx.skipped[key] = ctx.skipped.get(key, 0) + 1
            out.append(item.raw)
    return "".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="a .tf file or a folder of .tf files")
    ap.add_argument("-o", "--output", default="v1_converted", help="output folder (default: v1_converted)")
    args = ap.parse_args()

    if os.path.isdir(args.path):
        files = sorted(os.path.join(args.path, f) for f in os.listdir(args.path) if f.endswith(".tf"))
    else:
        files = [args.path]
    if not files:
        sys.exit("No .tf files found.")
    os.makedirs(args.output, exist_ok=True)

    ctx = Ctx()
    results = {}
    for f in files:  # pass 1: convert blocks (collects renamed resources)
        ctx.file = os.path.basename(f)
        with open(f, encoding="utf-8-sig") as fh:
            text = fh.read().replace("\r\n", "\n")
        results[f] = convert_text(text, ctx)
    for f, text in results.items():  # pass 2: fix references across ALL files
        target = os.path.join(args.output, os.path.basename(f))
        ctx.file = os.path.basename(f)
        text = fix_references(text, ctx)
        check_attribute_references(text, ctx)
        flag_leftovers(os.path.basename(f), text, ctx)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"  {f}  ->  {target}")

    if ctx.converted:
        print("\nConverted from v2 style:")
        for k, v in sorted(ctx.converted.items()):
            print(f"  {v:4d}  {k}")
    if ctx.left:
        print("\nAlready v1 - left unchanged:")
        for k, v in sorted(ctx.left.items()):
            print(f"  {v:4d}  {k}")
    if ctx.skipped:
        print("\nv2-style but NOT converted (no automatic mapping - check by hand):")
        for k, v in sorted(ctx.skipped.items()):
            print(f"  {v:4d}  {k}")
    if ctx.todo:
        print(f"\nNeeds a manual look ({len(ctx.todo)}):")
        for f, r, m in ctx.todo:
            print(f"  [{f}] {r}: {m}")
    print("\nNext: copy the output over your files, delete .terraform.lock.hcl if it mentions panos 2.x, "
          "then push and re-run the pipeline.")
    print("State: anything already applied with the v2 provider is in a state the v1 provider cannot read - "
          "back it up and import those objects into a new state before `terraform apply`.")


if __name__ == "__main__":
    main()
