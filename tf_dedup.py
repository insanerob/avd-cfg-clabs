#!/usr/bin/env python3
"""
Remove duplicate top-level blocks from a Terraform file.

A block is a duplicate if its header (e.g. `resource "panos_service" "tcp_8080"`)
has already appeared earlier in the file. The first occurrence is kept.

Usage:
    python tf_dedup.py services.tf            -> writes services.dedup.tf.txt + prints a report
    python tf_dedup.py services.tf --inplace  -> overwrites services.tf (keeps services.tf.bak)

The report flags duplicates whose body DIFFERS from the kept copy, so you can
check those by hand before committing.

Never deduplicated: `locals` (several are legal) and `provider` (aliased
providers legitimately repeat the same header). Anything inside a /* ... */
comment is treated as a comment, not as a block.
"""
import argparse
import os
import re
import sys
import shutil

HEADER = re.compile(r'^\s*(resource|data|variable|output|module|locals|provider)\b[^{]*\{')
QUOTED_STRING = re.compile(r'"(?:\\.|[^"\\])*"')
COMMENT_START = re.compile(r'/\*|#|//')
HEREDOC_START = re.compile(r'<<-?\s*([A-Za-z_][A-Za-z0-9_]*)\s*$')
BLANK_LINES_AT_START = re.compile(r'^(?:[ \t]*\r?\n)+')
NEVER_DEDUPLICATED = ("locals", "provider")


def code_only(line, in_block_comment):
    """Return (line with strings emptied and comments removed, still inside /* */ ?)."""
    if in_block_comment:
        end = line.find("*/")
        if end < 0:
            return "", True
        line = line[end + 2:]
    line = QUOTED_STRING.sub('""', line)
    code = ""
    while True:
        m = COMMENT_START.search(line)
        if not m:
            return code + line, False
        code += line[:m.start()]
        if m.group() != "/*":           # '#' or '//' comment runs to end of line
            return code, False
        end = line.find("*/", m.end())
        if end < 0:                     # block comment continues on later lines
            return code, True
        line = line[end + 2:]


def split_blocks(text):
    """Yield (header_key or None, chunk_text). None = text between blocks (comments, blank lines)."""
    lines = text.splitlines(keepends=True)
    i, n = 0, len(lines)
    loose = []
    in_block_comment = False
    while i < n:
        # a header only counts if it is real code, not inside a /* ... */ comment
        code, comment_continues = code_only(lines[i], in_block_comment)
        if in_block_comment or not HEADER.match(code):
            in_block_comment = comment_continues
            loose.append(lines[i])
            i += 1
            continue
        if loose:
            yield None, "".join(loose)
            loose = []
        start = i
        depth = 0
        in_heredoc = None
        while i < n:
            line = lines[i]
            if in_heredoc:
                if line.strip() == in_heredoc:
                    in_heredoc = None
            else:
                # count braces outside of quoted strings and comments
                code, in_block_comment = code_only(line, in_block_comment)
                depth += code.count("{") - code.count("}")
                hd = None if in_block_comment else HEREDOC_START.search(line)
                if hd:
                    in_heredoc = hd.group(1)
            i += 1
            if depth <= 0 and not in_heredoc and not in_block_comment:
                break
        block = "".join(lines[start:i])
        key = " ".join(lines[start].split("{")[0].split())
        yield key, block
    if loose:
        yield None, "".join(loose)


def normalise(block):
    return re.sub(r"\s+", " ", block).strip()


def main():
    parser = argparse.ArgumentParser(
        description="Remove duplicate top-level blocks from a Terraform file.")
    parser.add_argument("path", help="the .tf file to deduplicate")
    parser.add_argument("--inplace", action="store_true",
                        help="overwrite the file, keeping the original as <file>.bak")
    args = parser.parse_args()
    path, inplace = args.path, args.inplace

    # newline="" keeps the file's own line endings (LF or CRLF) untouched
    with open(path, encoding="utf-8", newline="") as f:
        text = f.read()

    seen = {}
    out = []
    removed_same, removed_diff = [], []
    just_removed = False
    for key, chunk in split_blocks(text):
        if key is None or key.startswith(NEVER_DEDUPLICATED):
            if key is None and just_removed:
                # drop the blank lines that separated the removed block from what
                # follows; nothing else in the file is reformatted
                chunk = BLANK_LINES_AT_START.sub("", chunk)
            just_removed = False
            out.append(chunk)
            continue
        if key in seen:
            if normalise(chunk) == normalise(seen[key]):
                removed_same.append(key)
            else:
                removed_diff.append(key)
            just_removed = True
            continue
        just_removed = False
        seen[key] = chunk
        out.append(chunk)

    result = "".join(out)
    if just_removed:
        # the last block in the file was removed: don't leave blank lines at the end
        result = re.sub(r"(\r?\n)(?:[ \t]*\r?\n)+$", r"\1", result)

    if not removed_same and not removed_diff:
        print(f"No duplicate blocks in {path}; nothing written")
        return

    if inplace:
        backup = path + ".bak"
        if os.path.exists(backup):
            sys.exit(f"ERROR: {backup} already exists - not overwriting it, because it may be "
                     f"the only copy of the original. Move or delete it and run again.")
        shutil.copyfile(path, backup)
        target = path
    else:
        target = re.sub(r"\.tf$", "", path) + ".dedup.tf.txt"
    with open(target, "w", encoding="utf-8", newline="") as f:
        f.write(result)

    print(f"Kept {len(seen)} unique blocks; wrote {target}")
    print(f"Removed {len(removed_same)} identical duplicates")
    if removed_diff:
        print(f"\nWARNING: {len(removed_diff)} duplicates had DIFFERENT contents "
              f"(first copy kept) - check these by hand:")
        for k in removed_diff:
            print("  ", k)


if __name__ == "__main__":
    main()
