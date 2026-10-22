"""Generate the documentation pages that are derived from other files.

Run before `zensical build` (the docs workflow does; locally: `python tools/gen_docs.py`).
It writes, into docs/, files that are gitignored and never committed:

- index.md, from the repository README (links to docs/... rewritten);
- prototype.md, from prototype/README.md;
- schemas/index.md, listing every schema;
- schemas/<name>.md, one reference page per schema (newest version);
- schemas/<name>/<version>/schema.json and examples/*.json, so the raw
  documents are downloadable from the site.

A schema page shows the top-level property table, one subsection per nested
object, the conditional rules (allOf/if/then) in prose, the $defs once each,
and the examples as tabbed code blocks. Cross-schema $refs
(urn:benchx:schema:<name>:<version>#/...) link to the other schema's page.

Stale output is removed first, so deleting a schema deletes its page.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = ROOT / "schemas"
DOCS = ROOT / "docs"
REPO_URL = "https://github.com/benchx-dev/benchx"  # keep in step with repo_url in zensical.toml
URN = re.compile(r"^urn:benchx:schema:(?P<name>[a-z0-9-]+):(?P<version>[0-9.]+)(?:#(?P<frag>.*))?$")


# --------------------------------------------------------------------------- utilities

def esc(text: str) -> str:
    """Make a description safe inside a Markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ").strip()


def code(value) -> str:
    return "`" + json.dumps(value) + "`"


def anchor(path: list[str]) -> str:
    return "-".join(p.lower().replace("_", "-") for p in path) or "root"


def version_key(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


# --------------------------------------------------------------------------- $ref links

def ref_link(ref: str, page: "SchemaPage") -> str:
    """A Markdown link for a $ref, local (#/$defs/x) or cross-schema (urn:...)."""
    if ref.startswith("#"):
        frag, prefix = ref[1:], ""
    else:
        m = URN.match(ref)
        if not m:
            return f"`{ref}`"
        frag = m.group("frag") or ""
        if m.group("name") == page.name:
            prefix = ""
        else:
            prefix = f"{m.group('name')}.md"
    parts = [p for p in frag.split("/") if p]
    # #/$defs/x/properties/y  ->  label x.y, anchor of x
    # #/properties/a/properties/b -> label a.b, anchor a-b
    label, path = [], []
    i = 0
    while i < len(parts):
        if parts[i] in ("$defs", "properties") and i + 1 < len(parts):
            label.append(parts[i + 1])
            if parts[i] == "$defs" or not path or parts[i] == "properties":
                path.append(parts[i + 1])
            i += 2
        else:
            label.append(parts[i])
            i += 1
    if parts and parts[0] == "$defs":
        target = anchor(["def", parts[1]])
    else:
        target = anchor(path)
    text = ".".join(label) if label else (m.group("name") if not ref.startswith("#") else "root")
    if prefix and not frag:
        return f"[{text}]({prefix})"
    return f"[{text}]({prefix}#{target})"


# --------------------------------------------------------------------------- type summaries

def type_str(s, page: "SchemaPage") -> str:
    """One-line type summary for a subschema, with links for $refs."""
    if s is True:
        return "any"
    if s is False:
        return "nothing (forbidden)"
    if "$ref" in s:
        out = ref_link(s["$ref"], page)
        extra = [k for k in s if k not in ("$ref", "description", "$comment")]
        if extra:
            out += " " + type_str({k: s[k] for k in extra}, page)
        return out
    if s.get("properties"):
        return "object"
    if "const" in s:
        return f"constant {code(s['const'])}"
    if "enum" in s:
        return "one of " + ", ".join(code(v) for v in s["enum"])
    for key, word in (("oneOf", "exactly one of"), ("anyOf", "any of"), ("allOf", "all of")):
        if key in s:
            inner = "; ".join(type_str(x, page) for x in s[key])
            rest = {k: v for k, v in s.items() if k not in (key, "description", "$comment")}
            tail = (" " + type_str(rest, page)) if rest else ""
            return f"{word}: {inner}{tail}"
    if "not" in s and len([k for k in s if k not in ("description", "$comment")]) == 1:
        return "not " + type_str(s["not"], page)

    t = s.get("type")
    if isinstance(t, list):
        base = " or ".join(t)
    elif t == "array" or "contains" in s or "items" in s:
        items = s.get("items")
        base = "array of " + (type_str(items, page) if items is not None else "any")
        if "contains" in s and items is None:
            base = "array"
    elif t == "object":
        ap = s.get("additionalProperties")
        if not s.get("properties") and isinstance(ap, dict):
            base = "map of " + type_str(ap, page)
        else:
            base = "object"
    elif t:
        base = t
    elif "required" in s or "properties" in s:
        base = "object"
    elif any(k in s for k in ("pattern", "minLength", "maxLength", "format")):
        base = "string"
    elif any(k in s for k in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum")):
        base = "number"
    else:
        base = "any"

    cons = []
    if "format" in s:
        cons.append(f"format {s['format']}")
    if "pattern" in s:
        cons.append(f"pattern `{s['pattern']}`")
    for k, label in (("minimum", ">="), ("exclusiveMinimum", ">"), ("maximum", "<="), ("exclusiveMaximum", "<")):
        if k in s:
            cons.append(f"{label} {s[k]}")
    for k, label in (("minLength", "min length"), ("maxLength", "max length"),
                     ("minItems", "min items"), ("maxItems", "max items"),
                     ("minProperties", "min properties")):
        if k in s:
            cons.append(f"{label} {s[k]}")
    if s.get("uniqueItems"):
        cons.append("unique items")
    if "contains" in s:
        cons.append("contains an item that is " + type_str(s["contains"], page))
    if "required" in s and "properties" not in s and t != "object":
        cons.append("requires " + ", ".join(f"`{r}`" for r in s["required"]))
    if "not" in s:
        cons.append("not " + type_str(s["not"], page))
    if "propertyNames" in s:
        cons.append("keys " + type_str(s["propertyNames"], page))
    return base + (f" ({'; '.join(cons)})" if cons else "")


# --------------------------------------------------------------------------- conditionals

PREDICATE_KEYS = {"properties", "required", "not", "anyOf", "allOf", "contains"}


def condition(s, page: "SchemaPage", path: list[str] | None = None) -> str:
    """Prose for an `if` schema (or any small predicate). Falls back to JSON."""
    path = path or []
    if s is True:
        return "always"
    if s is False:
        return "never"
    parts = []
    for k, v in s.get("properties", {}).items():
        dotted = ".".join(path + [k])
        if v is False:
            parts.append(f"`{dotted}` is absent")
        elif isinstance(v, dict) and v and not (v.keys() - PREDICATE_KEYS):
            parts.append(condition(v, page, path + [k]))
        elif isinstance(v, dict) and "const" in v:
            parts.append(f"`{dotted}` is {code(v['const'])}")
        elif isinstance(v, dict) and "enum" in v:
            parts.append(f"`{dotted}` is one of " + ", ".join(code(x) for x in v["enum"]))
        elif isinstance(v, dict) and v.get("type") == "null":
            parts.append(f"`{dotted}` is null")
        elif isinstance(v, dict) and "not" in v and len(v) == 1:
            parts.append(f"`{dotted}` is not {type_str(v['not'], page)}")
        else:
            parts.append(f"`{dotted}` is {type_str(v, page)}")
    for r in s.get("required", []):
        dotted = ".".join(path + [r])
        if not any(re.search("`" + re.escape(dotted) + "[.`]", p) for p in parts):
            parts.append(f"`{dotted}` is present")
    if "contains" in s:
        what = ".".join(path) or "the array"
        parts.append(f"`{what}` contains an item where " + condition(s["contains"], page))
    if "not" in s:
        inner = s["not"]
        if set(inner) == {"required"}:
            parts.append(", ".join(f"`{'.'.join(path + [r])}` is absent" for r in inner["required"]))
        else:
            parts.append("not (" + condition(inner, page, path) + ")")
    for key, word in (("anyOf", " or "), ("allOf", " and ")):
        if key in s:
            joined = word.join(condition(x, page, path) for x in s[key])
            parts.append(joined if not parts and not (s.keys() - {key}) else "(" + joined + ")")
    if "$ref" in s:
        parts.append("satisfies " + ref_link(s["$ref"], page))
    if "const" in s and not path:
        parts.append("the value is " + code(s["const"]))
    if "type" in s and not path and "properties" not in s:
        parts.append("the value is " + type_str(s, page))
    if not parts:
        return "`" + json.dumps(s) + "`"
    return " and ".join(parts)


def _required_only(s) -> list[str] | None:
    """The names if `s` is {"required": [...]} or anyOf/allOf of such; else None."""
    if not isinstance(s, dict):
        return None
    if set(s) == {"required"}:
        return list(s["required"])
    for key in ("anyOf", "allOf"):
        if set(s) == {key}:
            names = []
            for x in s[key]:
                r = _required_only(x)
                if r is None:
                    return None
                names.extend(r)
            return names
    return None


def consequence(s, page: "SchemaPage", path: list[str] | None = None) -> str:
    """Prose for a `then`/`else` schema. Falls back to JSON."""
    path = path or []
    if s is True:
        return "no further constraint"
    if s is False:
        return "the document is invalid"
    req, forbid, other = [], [], []
    dot = lambda k: "`" + ".".join(path + [k]) + "`"
    req += [dot(r) for r in s.get("required", [])]
    for k, v in s.get("properties", {}).items():
        if v is False:
            forbid.append(dot(k))
        elif isinstance(v, dict) and set(v) == {"contains"}:
            other.append(f"{dot(k)} contains an item that is {type_str(v['contains'], page)}")
        elif isinstance(v, dict) and set(v) <= {"required", "not", "properties", "allOf", "anyOf", "$comment", "description"}:
            other.append(consequence(v, page, path + [k]))
        else:
            other.append(f"{dot(k)} must be {type_str(v, page)}")
    if "not" in s:
        names = _required_only(s["not"])
        if names is not None:
            forbid += [dot(n) for n in names]
        else:
            other.append("forbids " + condition(s["not"], page, path))
    for key, word in (("anyOf", "at least one of: "), ("allOf", "all of: "), ("oneOf", "exactly one of: ")):
        if key in s:
            other.append(word + "; ".join(consequence(x, page, path) for x in s[key]))
    if "$ref" in s:
        other.append("must satisfy " + ref_link(s["$ref"], page))
    if "minProperties" in s:
        other.append(f"at least {s['minProperties']} propert{'y' if s['minProperties'] == 1 else 'ies'}")
    parts = []
    if req:
        parts.append("requires " + ", ".join(req))
    if forbid:
        parts.append("forbids " + ", ".join(forbid))
    parts += other
    if not parts:
        return "`" + json.dumps(s) + "`"
    return "; ".join(parts)


def rules(s: dict, page: "SchemaPage") -> list[str]:
    """Bullet list of object-level rules: if/then, allOf, anyOf, oneOf, not, contains."""
    out = []
    if "if" in s:
        line = f"**If** {condition(s['if'], page)}, **then** {consequence(s.get('then', True), page)}"
        if "else" in s:
            line += f"; **else** {consequence(s['else'], page)}"
        out.append(line + ".")
    for item in s.get("allOf", []):
        if isinstance(item, dict) and "if" in item:
            out.extend(rules(item, page))
        elif isinstance(item, dict) and "$ref" in item and len(item) == 1:
            out.append("Must also satisfy " + ref_link(item["$ref"], page) + ".")
        else:
            out.append("Must satisfy: " + consequence(item, page) + ".")
    if "anyOf" in s:
        out.append("At least one of: " + "; ".join(consequence(x, page) for x in s["anyOf"]) + ".")
    if "oneOf" in s and s.get("type") == "object":
        out.append("Exactly one of: " + "; ".join(consequence(x, page) for x in s["oneOf"]) + ".")
    if "not" in s:
        if s["not"] == {"type": "null"}:
            out.append("Must not be null.")
        else:
            out.append("Must not: " + condition(s["not"], page) + ".")
    if "contains" in s:
        out.append("At least one item where " + condition(s["contains"], page) + ".")
    if "dependentRequired" in s:
        for k, v in s["dependentRequired"].items():
            out.append(f"If `{k}` is present, requires " + ", ".join(f"`{x}`" for x in v) + ".")
    if "minProperties" in s and s.get("properties"):
        n = s["minProperties"]
        out.append(f"At least {'one property' if n == 1 else f'{n} properties'} must be present.")
    return out


# --------------------------------------------------------------------------- objects

def has_table(s) -> bool:
    return isinstance(s, dict) and bool(s.get("properties"))


def wants_section(s) -> bool:
    """Properties that deserve their own heading (and therefore a stable anchor)."""
    return has_table(s) or (isinstance(s, dict) and any(k in s for k in ("oneOf", "anyOf", "$comment")))


def render_object(s: dict, page: "SchemaPage", path: list[str], level: int, out: list[str]) -> None:
    """Property table for one object, then a subsection per nested object."""
    required = set(s.get("required", []))
    props = s.get("properties", {})
    nested = []
    if props:
        out.append("| Property | Required | Type | Description |")
        out.append("|---|---|---|---|")
        for name, sub in props.items():
            desc = esc(sub.get("description", "")) if isinstance(sub, dict) else ""
            if wants_section(sub):
                nested.append((name, sub))
                desc += f" See [`{'.'.join(path + [name])}`](#{anchor(path + [name])})."
            out.append(f"| `{name}` | {'yes' if name in required else ''} | {type_str(sub, page)} | {desc.strip()} |")
        out.append("")
    ap = s.get("additionalProperties")
    if ap is False and props:
        out.append("No other properties are allowed.")
        out.append("")
    elif isinstance(ap, dict):
        out.append(f"Other properties: {type_str(ap, page)}" + (f". {esc(ap.get('description', ''))}" if ap.get("description") else "") + ".")
        out.append("")
        if wants_section(ap):
            nested.append(("*", ap))
    r = rules(s, page)
    if r:
        out.append("Rules:")
        out.append("")
        out.extend(f"- {line}" for line in r)
        out.append("")
    for name, sub in nested:
        sub_path = path + [name]
        hashes = "#" * min(level + 1, 6)
        out.append(f"{hashes} `{'.'.join(sub_path)}` {{ #{anchor(sub_path)} }}")
        out.append("")
        if sub.get("description"):
            out.append(sub["description"].strip())
            out.append("")
        if sub.get("$comment"):
            out.append('!!! note "Implementer note"')
            out.append("    " + sub["$comment"].strip().replace("\n", "\n    "))
            out.append("")
        if has_table(sub):
            render_object(sub, page, sub_path, min(level + 1, 6), out)
        else:
            out.append(f"Type: {type_str(sub, page)}")
            out.append("")
            r = rules(sub, page)
            if r:
                out.extend(f"- {line}" for line in r)
                out.append("")


# --------------------------------------------------------------------------- pages

class SchemaPage:
    def __init__(self, name: str, version: str, schema: dict, examples: list[Path], older: list[str]):
        self.name, self.version, self.schema, self.examples, self.older = name, version, schema, examples, older

    def render(self, repo_url: str | None) -> str:
        s, out = self.schema, []
        out.append(f"# {s.get('title', self.name)}")
        out.append("")
        src = f"{self.name}/{self.version}/schema.json"
        links = [f"[schema.json]({src})"]
        if repo_url:
            links.append(f"[source]({repo_url}/blob/main/schemas/{src})")
        out.append(f"`{s.get('$id', '')}` · version {self.version} · " + " · ".join(links))
        if self.older:
            out.append("")
            out.append("Older versions: " + ", ".join(f"[{v}]({self.name}/{v}/schema.json)" for v in self.older))
        out.append("")
        if s.get("description"):
            out.append(s["description"].strip())
            out.append("")
        if s.get("$comment"):
            out.append('!!! note "Implementer note"')
            out.append("    " + s["$comment"].strip().replace("\n", "\n    "))
            out.append("")
        out.append("## Properties")
        out.append("")
        if s.get("type") == "object" or "properties" in s:
            render_object(s, self, [], 2, out)
        else:
            out.append(f"Type: {type_str(s, self)}")
            out.append("")
        defs = s.get("$defs", {})
        if defs:
            out.append("## Definitions")
            out.append("")
            out.append("Reusable subschemas referenced from the tables above and from other schemas.")
            out.append("")
            for name, d in defs.items():
                out.append(f"### `{name}` {{ #{anchor(['def', name])} }}")
                out.append("")
                if isinstance(d, dict) and d.get("description"):
                    out.append(d["description"].strip())
                    out.append("")
                if isinstance(d, dict) and d.get("$comment"):
                    out.append('!!! note "Implementer note"')
                    out.append("    " + d["$comment"].strip().replace("\n", "\n    "))
                    out.append("")
                if has_table(d):
                    render_object(d, self, [name], 3, out)
                else:
                    out.append(f"Type: {type_str(d, self)}")
                    out.append("")
                    r = rules(d, self) if isinstance(d, dict) else []
                    if r:
                        out.extend(f"- {line}" for line in r)
                        out.append("")
        if self.examples:
            out.append("## Examples")
            out.append("")
            for ex in self.examples:
                out.append(f'=== "{ex.stem}"')
                out.append("")
                out.append("    ```json")
                out.append("    " + ex.read_text().strip().replace("\n", "\n    "))
                out.append("    ```")
                out.append("")
        return "\n".join(out)


def discover() -> list[SchemaPage]:
    pages = []
    for name_dir in sorted(p for p in SCHEMAS.iterdir() if p.is_dir()):
        versions = sorted((p.name for p in name_dir.iterdir() if (p / "schema.json").exists()), key=version_key)
        if not versions:
            continue
        latest = versions[-1]
        schema = json.loads((name_dir / latest / "schema.json").read_text())
        examples = sorted((name_dir / latest / "examples").glob("*.json")) if (name_dir / latest / "examples").is_dir() else []
        pages.append(SchemaPage(name_dir.name, latest, schema, examples, versions[:-1]))
    return pages


def schema_index(pages: list[SchemaPage]) -> str:
    out = ["# Schemas", "",
           "The JSON schemas that define benchx's documents. Every component produces or "
           "consumes one of these; the design documents explain the reasoning behind each.", "",
           "| Schema | Version | Defines |", "|---|---|---|"]
    for p in pages:
        desc = esc(p.schema.get("description", ""))
        desc = desc.split(". ")[0].rstrip(".") + "." if desc else ""
        out.append(f"| [{p.schema.get('title', p.name)}]({p.name}.md) | {p.version} | {desc} |")
    out.append("")
    out.append("Each page links the raw `schema.json` and its examples. Schemas are validated by the prototype's `bx validate`.")
    return "\n".join(out)


def rewrite_readme(text: str, strip_prefix: str) -> str:
    """Rewrite relative links in a README so they resolve from the generated page."""
    text = re.sub(r"\]\(" + re.escape(strip_prefix), "](", text)
    return text


# --------------------------------------------------------------------------- main

GENERATED = ["index.md", "prototype.md", "schemas"]  # paths under docs/, all gitignored


def main() -> None:
    import shutil

    for rel in GENERATED:
        path = DOCS / rel
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()

    def write(rel: str, content: str | bytes) -> None:
        path = DOCS / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content if content.endswith("\n") else content + "\n")

    # Landing page from the repository README.
    write("index.md", rewrite_readme((ROOT / "README.md").read_text(), "docs/"))

    # Prototype README.
    proto = ROOT / "prototype" / "README.md"
    if proto.exists():
        write("prototype.md", proto.read_text().replace("](../docs/", "]("))

    # Schema reference pages and raw documents.
    pages = discover()
    write("schemas/index.md", schema_index(pages))
    for p in pages:
        write(f"schemas/{p.name}.md", p.render(REPO_URL))
        for v in [p.version, *p.older]:
            vdir = SCHEMAS / p.name / v
            write(f"schemas/{p.name}/{v}/schema.json", (vdir / "schema.json").read_bytes())
            for ex in sorted((vdir / "examples").glob("*.json")) if (vdir / "examples").is_dir() else []:
                write(f"schemas/{p.name}/{v}/examples/{ex.name}", ex.read_bytes())
    print(f"generated {len(pages)} schema pages under {DOCS.relative_to(ROOT)}/")


if __name__ == "__main__":
    main()
