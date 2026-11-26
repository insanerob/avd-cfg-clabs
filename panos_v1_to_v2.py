#!/usr/bin/env python3
"""
panos_v1_to_v2.py - convert Terraform code written for the PAN-OS provider v1.x
to the v2.x provider (schemas checked against v2.0.10).

Usage:
    python panos_v1_to_v2.py <file-or-folder> [-o OUTPUT_FOLDER] [--panorama]

    python panos_v1_to_v2.py .                 -> converts every *.tf in this folder into ./v2_converted/
    python panos_v1_to_v2.py services.tf -o out

Add --panorama when the code targets a Panorama: resources that set neither `vsys` nor
`device_group` are then placed in `shared` (as v1 did on Panorama) instead of `vsys1`.

Your original files are never modified. Review the output (and the report printed at the end)
before replacing your originals. Anything the script could not convert automatically is marked
in the output with a  # TODO(v2):  comment.

Converted:
    provider "panos"                       verify_certificate -> skip_verify_certificate (inverted), etc.
    terraform { required_providers }       panos version -> 2.0.10
    panos_address_object                   -> panos_address
    panos_address_group / panorama_        -> panos_address_group
    panos_service_object / panorama_       -> panos_service
    panos_service_group / panorama_        -> panos_service_group
    panos_administrative_tag / panorama_   -> panos_administrative_tag
    panos_security_policy / panorama_      -> panos_security_policy
    panos_security_rule_group / panorama_  -> panos_security_policy_rules
    References to the renamed resources in the rest of your code (e.g. panos_service_object.web.name)

Anything else (zones, interfaces, NAT, data sources, ...) is copied unchanged and listed in the report.
Only the *.tf files directly in the given folder are converted (sub-folders / modules are not).

This converts the CODE only. The v2 provider cannot read v1 state ("no automatic state upgrade"
- see the provider's migration guide), so existing resources have to be imported into a new state.
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
        return expr
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
        w = max((len(k) for k in value if not k.startswith("#")), default=0)
        inner = []
        for k, v in value.items():
            if k.startswith("#"):
                inner.append(f"{pad}  {v}")
            elif isinstance(v, (dict, list)):
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
    flat = {k: v for k, v in body.items() if not isinstance(v, (dict, list)) and not k.startswith("#")}
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
# Conversions
# --------------------------------------------------------------------------------------

META_ATTRS = {"count", "for_each", "depends_on", "provider"}
META_BLOCKS = {"lifecycle", "provisioner", "connection"}

RENAMES = {
    "panos_address_object": "panos_address",
    "panos_panorama_address_object": "panos_address",
    "panos_address_group": "panos_address_group",
    "panos_panorama_address_group": "panos_address_group",
    "panos_service_object": "panos_service",
    "panos_panorama_service_object": "panos_service",
    "panos_service_group": "panos_service_group",
    "panos_panorama_service_group": "panos_service_group",
    "panos_administrative_tag": "panos_administrative_tag",
    "panos_panorama_administrative_tag": "panos_administrative_tag",
    "panos_security_policy": "panos_security_policy",
    "panos_panorama_security_policy": "panos_security_policy",
    "panos_security_rule_group": "panos_security_policy_rules",
    "panos_panorama_security_rule_group": "panos_security_policy_rules",
}


class Ctx:
    def __init__(self):
        self.todo = []      # (file, resource, message)
        self.skipped = {}   # resource type -> count
        self.converted = {}
        self.file = ""
        self.panorama = False   # --panorama: no vsys/device_group means "shared"

    def warn(self, res, msg):
        self.todo.append((self.file, res, msg))


def object_location(a, rtype, res, ctx):
    """v1 vsys / device_group  ->  v2 location = {...}"""
    is_pano = "panorama" in rtype or ctx.panorama
    dg = a.get("device_group")
    vsys = a.get("vsys")
    if dg is not None or is_pano:
        if dg is None or literal(dg) == "shared":
            return {"shared": {}}
        return {"device_group": {"name": Raw(dg)}}
    if vsys is not None and literal(vsys) == "shared":
        ctx.warn(res, 'vsys = "shared" on a firewall - check location (v2 "shared" is Panorama-only)')
        return {"vsys": {"name": q("vsys1")}}
    return {"vsys": {"name": Raw(vsys) if vsys else q("vsys1")}}


def split_meta(block):
    meta = [i for i in block.items if (isinstance(i, Attr) and i.key in META_ATTRS)
            or (isinstance(i, Block) and i.type in META_BLOCKS)]
    return meta


def first_line(expr):
    lines = expr.strip().splitlines() or [""]
    return lines[0] + (" ..." if len(lines) > 1 else "")


def unknown_args(a, known, res, ctx, body):
    for k in a:
        if k not in known and k not in META_ATTRS:
            ctx.warn(res, f"argument '{k}' was not converted - check it by hand")
            body[f"# todo {k}"] = f"# TODO(v2): v1 argument not converted:  {k} = {first_line(a[k])}"


def conv_address(b, ctx):
    a, res = b.attrs(), f"{b.labels[0]}.{b.labels[1]}"
    body = {"location": object_location(a, b.labels[0], res, ctx), "name": Raw(a.get("name", '""'))}
    if "description" in a:
        body["description"] = Raw(a["description"])
    t = literal(a.get("type", '"ip-netmask"'))
    key = {"ip-netmask": "ip_netmask", "ip-range": "ip_range", "fqdn": "fqdn",
           "ip-wildcard": "ip_wildcard"}.get(t or "")
    if key is None:
        ctx.warn(res, f"address type is an expression ({a.get('type')}) - set ip_netmask/ip_range/fqdn/ip_wildcard by hand")
        body["# todo"] = f"# TODO(v2): type was {a.get('type')} - rename the line below to ip_netmask / ip_range / fqdn / ip_wildcard"
        key = "ip_netmask"
    body[key] = Raw(a.get("value", '""'))
    if "tags" in a:
        body["tags"] = Raw(a["tags"])
    unknown_args(a, {"name", "vsys", "device_group", "type", "value", "description", "tags"}, res, ctx, body)
    return emit_resource("panos_address", b.labels[1], body, b.comments(), split_meta(b))


def conv_address_group(b, ctx):
    a, res = b.attrs(), f"{b.labels[0]}.{b.labels[1]}"
    body = {"location": object_location(a, b.labels[0], res, ctx), "name": Raw(a.get("name", '""'))}
    if "description" in a:
        body["description"] = Raw(a["description"])
    if "static_addresses" in a:
        body["static"] = Raw(a["static_addresses"])
    if "dynamic_match" in a:
        body["dynamic"] = {"filter": Raw(a["dynamic_match"])}
    if "tags" in a:
        body["tag"] = Raw(a["tags"])  # NB: address_group uses `tag` in v2
    unknown_args(a, {"name", "vsys", "device_group", "description", "static_addresses", "dynamic_match", "tags"}, res, ctx, body)
    return emit_resource("panos_address_group", b.labels[1], body, b.comments(), split_meta(b))


def conv_service(b, ctx):
    a, res = b.attrs(), f"{b.labels[0]}.{b.labels[1]}"
    body = {"location": object_location(a, b.labels[0], res, ctx), "name": Raw(a.get("name", '""'))}
    if "description" in a:
        body["description"] = Raw(a["description"])
    proto = literal(a.get("protocol"))
    if proto not in ("tcp", "udp"):
        if proto == "sctp":
            ctx.warn(res, "sctp services are not supported by panos_service in v2 - converted as tcp, fix by hand")
        else:
            ctx.warn(res, f"protocol is {a.get('protocol')} - assumed tcp, fix by hand")
        body["# todo"] = f"# TODO(v2): v1 protocol was {a.get('protocol')} - assumed tcp"
        proto = "tcp"
    p = {}
    if "destination_port" in a:
        p["destination_port"] = Raw(a["destination_port"])
    if "source_port" in a:
        p["source_port"] = Raw(a["source_port"])
    ov = a.get("override_session_timeout")
    if ov is not None and literal(ov) != "false":
        o = {}
        if "override_timeout" in a:
            o["timeout"] = Raw(a["override_timeout"])
        if proto == "tcp":
            if "override_half_closed_timeout" in a:
                o["halfclose_timeout"] = Raw(a["override_half_closed_timeout"])
            if "override_time_wait_timeout" in a:
                o["timewait_timeout"] = Raw(a["override_time_wait_timeout"])
        if literal(ov) is None:
            ctx.warn(res, f"override_session_timeout is an expression ({ov}) - override block always added")
        p["override"] = o
    body["protocol"] = {proto: p}
    if "tags" in a:
        body["tags"] = Raw(a["tags"])
    unknown_args(a, {"name", "vsys", "device_group", "description", "protocol", "source_port", "destination_port",
                     "tags", "override_session_timeout", "override_timeout", "override_half_closed_timeout",
                     "override_time_wait_timeout"}, res, ctx, body)
    return emit_resource("panos_service", b.labels[1], body, b.comments(), split_meta(b))


def conv_service_group(b, ctx):
    a, res = b.attrs(), f"{b.labels[0]}.{b.labels[1]}"
    body = {"location": object_location(a, b.labels[0], res, ctx), "name": Raw(a.get("name", '""'))}
    if "services" in a:
        body["members"] = Raw(a["services"])
    if "tags" in a:
        body["tags"] = Raw(a["tags"])
    unknown_args(a, {"name", "vsys", "device_group", "services", "tags"}, res, ctx, body)
    return emit_resource("panos_service_group", b.labels[1], body, b.comments(), split_meta(b))


def conv_tag(b, ctx):
    a, res = b.attrs(), f"{b.labels[0]}.{b.labels[1]}"
    body = {"location": object_location(a, b.labels[0], res, ctx), "name": Raw(a.get("name", '""'))}
    if "color" in a and literal(a["color"]) != "":
        body["color"] = Raw(a["color"])
    if "comment" in a:
        body["comments"] = Raw(a["comment"])
    unknown_args(a, {"name", "vsys", "device_group", "color", "comment"}, res, ctx, body)
    return emit_resource("panos_administrative_tag", b.labels[1], body, b.comments(), split_meta(b))


RULE_SAME = ["name", "description", "group_tag", "source_zones", "source_addresses", "negate_source",
             "source_users", "destination_zones", "destination_addresses", "negate_destination",
             "applications", "services", "action", "log_setting", "log_start", "log_end", "disabled",
             "schedule", "icmp_unreachable", "disable_server_response_inspection"]
RULE_RENAMED = {"type": "rule_type", "tags": "tag", "categories": "category", "hip_profiles": "source_hip"}
PROFILES = ["virus", "spyware", "vulnerability", "url_filtering", "file_blocking", "wildfire_analysis",
            "data_filtering"]


def as_list(expr):
    """v1 profile settings were single strings, v2 takes lists."""
    return Raw(f"[{expr.strip()}]")


def conv_rule(r, res, ctx):
    a = r.attrs()
    rule = {}
    rule["name"] = Raw(a.get("name", '""'))
    if "type" in a:
        rule["rule_type"] = Raw(a["type"])
    for k in RULE_SAME[1:]:
        if k in a:
            rule[k] = Raw(a[k])
    for old, new in RULE_RENAMED.items():
        if old in a and old != "type":
            rule[new] = Raw(a[old])
    ps = {}
    if "group" in a and literal(a["group"]) != "":
        ps["group"] = as_list(a["group"])
    profs = {p: as_list(a[p]) for p in PROFILES if p in a and literal(a[p]) != ""}
    if profs:
        ps["profiles"] = profs
    if ps:
        rule["profile_setting"] = ps
    targets = r.blocks("target")
    if targets or "negate_target" in a:
        devs = []
        for t in targets:
            ta = t.attrs()
            d = {"name": Raw(ta.get("serial", '""'))}
            if "vsys_list" in ta:
                d["vsys"] = Raw(f"[for v in {ta['vsys_list'].strip()} : {{ name = v }}]")
            devs.append(d)
        tgt = {}
        if devs:
            tgt["devices"] = devs
        if "negate_target" in a:
            tgt["negate"] = Raw(a["negate_target"])
        rule["target"] = tgt
    if "audit_comment" in a:
        ctx.warn(res, f"rule {a.get('name')}: audit_comment dropped (v2 uses audit_comment_wo + audit_comment_version)")
        rule["# todo audit_comment"] = f"# TODO(v2): audit_comment dropped (was: {first_line(a['audit_comment'])})"
    known = set(RULE_SAME) | set(RULE_RENAMED) | set(PROFILES) | {"group", "negate_target", "audit_comment"}
    for k in a:
        if k not in known:
            ctx.warn(res, f"rule {a.get('name')}: argument '{k}' not converted")
            rule[f"# todo {k}"] = f"# TODO(v2): v1 argument not converted:  {k} = {first_line(a[k])}"
    return rule


def policy_location(a, rtype, res, ctx):
    is_pano = "panorama" in rtype or "device_group" in a or "rulebase" in a or ctx.panorama
    if is_pano:
        rb = Raw(a.get("rulebase", '"pre-rulebase"'))
        dg = a.get("device_group")
        if dg is None or literal(dg) == "shared":
            return {"shared": {"rulebase": rb}}
        return {"device_group": {"name": Raw(dg), "rulebase": rb}}
    return {"vsys": {"name": Raw(a.get("vsys", '"vsys1"'))}}


def conv_policy(b, ctx):
    a, res = b.attrs(), f"{b.labels[0]}.{b.labels[1]}"
    rules = [conv_rule(r, res, ctx) for r in b.blocks("rule")]
    dyn = b.blocks("dynamic")
    if dyn:
        ctx.warn(res, "contains `dynamic \"rule\"` blocks - not converted, rewrite as a for-expression in rules = [...]")
    body = {"location": policy_location(a, b.labels[0], res, ctx)}
    newtype = RENAMES[b.labels[0]]
    if newtype == "panos_security_policy_rules":
        kw = (literal(a.get("position_keyword", '""')) or "").strip().lower()
        directly = kw.startswith("directly")
        kw = kw.replace("directly", "").strip()
        where = {"": "last", "top": "first", "bottom": "last", "before": "before", "above": "before",
                 "after": "after", "below": "after"}.get(kw)
        if where is None:
            ctx.warn(res, f"position_keyword {a.get('position_keyword')} not understood - set position by hand")
            where = "last"
        pos = {"where": q(where)}
        if directly:
            pos["directly"] = Raw("true")
        if where in ("before", "after"):
            pos["pivot"] = Raw(a.get("position_reference", '""'))
        if kw == "":
            ctx.warn(res, "no position_keyword in v1 - v2 requires a position, set to last")
        body["position"] = pos
    body["rules"] = rules
    for n, d in enumerate(dyn):
        original = "\n".join("  # " + l.strip() for l in d.raw.rstrip("\n").split("\n"))
        body[f"# todo dynamic {n}"] = ("# TODO(v2): this dynamic block was NOT converted - add its rules to "
                                       "`rules = [...]` above, e.g. with concat() and a for-expression\n" + original)
    unknown_args(a, {"vsys", "device_group", "rulebase", "position_keyword", "position_reference"}, res, ctx, body)
    return emit_resource(newtype, b.labels[1], body, b.comments(), split_meta(b))


CONVERTERS = {
    "panos_address_object": conv_address, "panos_panorama_address_object": conv_address,
    "panos_address_group": conv_address_group, "panos_panorama_address_group": conv_address_group,
    "panos_service_object": conv_service, "panos_panorama_service_object": conv_service,
    "panos_service_group": conv_service_group, "panos_panorama_service_group": conv_service_group,
    "panos_administrative_tag": conv_tag, "panos_panorama_administrative_tag": conv_tag,
    "panos_security_policy": conv_policy, "panos_panorama_security_policy": conv_policy,
    "panos_security_rule_group": conv_policy, "panos_panorama_security_rule_group": conv_policy,
}


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
        if k == "verify_certificate":
            lit = literal(v)
            nv = {"true": "false", "false": "true"}.get(lit, f"!({v})")
            out.append(f"  skip_verify_certificate = {nv}")
        elif k == "json_config_file":
            out.append(f"  auth_file = {v}")
        elif k in ("timeout", "logging"):
            out.append(f"  # TODO(v2): '{k}' is not supported in v2 - removed (was: {v.splitlines()[0]})")
            ctx.warn('provider "panos"', f"'{k}' removed - not a v2 provider argument")
        else:
            out.append(f"  {k} = {indent_expr(v, '  ')}")
    out.append("}")
    return "\n".join(out) + "\n"


def conv_terraform_block(raw):
    # bump the panos version constraint inside required_providers
    def fix(m):
        obj = m.group(0)
        if re.search(r"\bversion\s*=", obj):
            return re.sub(r'(\bversion\s*=\s*)"[^"]*"', r'\1"2.0.10"', obj)
        return obj.rstrip("}").rstrip() + '\n      version = "2.0.10"\n    }'
    return re.sub(r"\bpanos\s*=\s*\{[^}]*\}", fix, raw)


def fix_references(text):
    # v1 rule-group style references: X.rule.0.name -> X.rules[0].name
    text = re.sub(r"\b(panos_(?:panorama_)?security_(?:rule_group|policy)\.[\w-]+)\.rule\.(\d+)\.",
                  r"\1.rules[\2].", text)
    for old, new in sorted(RENAMES.items(), key=lambda kv: -len(kv[0])):
        if old != new:
            # not after a dot: data.<type>.x refers to a data block, which is not converted
            text = re.sub(r"(?<![\w\".])" + old + r"\.", new + ".", text)
            text = re.sub(r"(depends_on\s*=\s*\[[^\]]*?)\b" + old + r"\b", r"\1" + new, text)
    return text


# attribute names that exist in v1 but not under that name in v2
V1_ONLY_ATTRIBUTES = {
    "panos_address": {"value": "ip_netmask / ip_range / fqdn / ip_wildcard", "type": None},
    "panos_address_group": {"static_addresses": "static", "dynamic_match": "dynamic.filter", "tags": "tag"},
    "panos_service": {"destination_port": "protocol.tcp|udp.destination_port",
                      "source_port": "protocol.tcp|udp.source_port"},
    "panos_service_group": {"services": "members"},
    "panos_administrative_tag": {"comment": "comments"},
}


def check_attribute_references(text, ctx):
    """Report references such as panos_address.web.value whose attribute was renamed in v2."""
    for rtype, attributes in V1_ONLY_ATTRIBUTES.items():
        pattern = (r"(?<![\w\".])(" + rtype + r"\.[\w-]+)(?:\[[^\]]*\])?\.("
                   + "|".join(attributes) + r")\b")
        for ref, attribute in sorted(set(re.findall(pattern, text))):
            new = attributes[attribute]
            ctx.warn(ref, f"something refers to .{attribute}, which "
                          + (f"is now {new}" if new else "no longer exists") + " in v2 - fix that reference")


def convert_text(text, ctx):
    out = []
    for item in parse_body(text):
        if isinstance(item, str):
            out.append(item)
            continue
        if isinstance(item, Attr):
            out.append(f"{item.key} = {item.expr}\n")
            continue
        b = item
        if b.type == "resource" and len(b.labels) == 2 and b.labels[0] in CONVERTERS:
            out.append(CONVERTERS[b.labels[0]](b, ctx))
            ctx.converted[b.labels[0]] = ctx.converted.get(b.labels[0], 0) + 1
        elif b.type == "provider" and b.labels and b.labels[0] == "panos":
            out.append(conv_provider(b, ctx))
        elif b.type == "terraform":
            out.append(conv_terraform_block(b.raw))
        else:
            if b.type in ("resource", "data") and b.labels and b.labels[0].startswith("panos_"):
                key = f"{b.type} {b.labels[0]}"
                ctx.skipped[key] = ctx.skipped.get(key, 0) + 1
            out.append(b.raw)
    result = fix_references("".join(out))
    check_attribute_references(result, ctx)
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="a .tf file or a folder of .tf files")
    ap.add_argument("-o", "--output", default="v2_converted", help="output folder (default: v2_converted)")
    ap.add_argument("--panorama", action="store_true",
                    help="the code targets a Panorama: no vsys/device_group means 'shared', not 'vsys1'")
    args = ap.parse_args()

    if os.path.isdir(args.path):
        files = sorted(os.path.join(args.path, f) for f in os.listdir(args.path) if f.endswith(".tf"))
    else:
        files = [args.path]
    if not files:
        sys.exit("No .tf files found.")
    os.makedirs(args.output, exist_ok=True)

    ctx = Ctx()
    ctx.panorama = args.panorama
    for f in files:
        ctx.file = os.path.basename(f)
        with open(f, encoding="utf-8-sig") as fh:
            text = fh.read().replace("\r\n", "\n")
        result = convert_text(text, ctx)
        target = os.path.join(args.output, os.path.basename(f))
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(result)
        print(f"  {f}  ->  {target}")

    print("\nConverted resources:")
    for k, v in sorted(ctx.converted.items()):
        print(f"  {v:4d}  {k}  ->  {RENAMES[k]}")
    if ctx.skipped:
        print("\nNOT converted (copied unchanged - check these against the v2 docs):")
        for k, v in sorted(ctx.skipped.items()):
            print(f"  {v:4d}  {k}")
    if ctx.todo:
        print(f"\nNeeds a manual look ({len(ctx.todo)}):")
        for f, r, m in ctx.todo:
            print(f"  [{f}] {r}: {m}")
    print("\nNext: copy the output over your files, delete .terraform.lock.hcl, then run "
          "`terraform init -upgrade` and `terraform validate`.")
    print("State: the v2 provider cannot read v1 state (no automatic state upgrade). Back up your state "
          "and import the existing objects into a new one before `terraform apply` - see the provider's "
          "migration guide.")


if __name__ == "__main__":
    main()
