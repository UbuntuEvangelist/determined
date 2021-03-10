#!/usr/bin/env python3

import argparse
import json
import os
import re
import sys
from typing import List, Optional, Tuple

HERE = os.path.dirname(__file__)
ALL_PKGS = ["expconf"]
URLBASE = "http://determined.ai/schemas"


def camel_to_snake(name: str) -> str:
    """Convert CamelCase to snake_case, handling acronyms properly."""
    out = name[0].lower()
    for c0, c1, c2 in zip(name[:-2], name[1:-1], name[2:]):
        # Catch lower->upper transitions.
        if c0.islower() and c1.isupper():
            out += "_"
        # Catch acronym endings.
        if c0.isupper() and c1.isupper() and c2.islower():
            out += "_"
        out += c1.lower()
    out += name[-1].lower()
    return out


class Schema:
    def __init__(self, url: str, text: str) -> None:
        self.url = url
        self.text = text
        try:
            self.schema = json.loads(text)
        except Exception as e:
            raise ValueError(f"{url} is not a valid json file") from e
        self.golang_title = self.schema["title"] + self.version().upper()
        self.python_title = camel_to_snake(self.golang_title)

    def version(self) -> str:
        return os.path.basename(os.path.dirname(self.url))


def list_files(package: str) -> List[str]:
    """List all json schema files in a package (like `expconf`)."""
    out = []
    root = os.path.join(HERE, package)
    for dirpath, _, files in os.walk(root):
        out += [os.path.join(dirpath, f) for f in files if f.endswith(".json")]
    return sorted(out)


def read_schemas(files: List[str]) -> List[Schema]:
    """Read all the schemas in a list of files."""
    schemas = []
    for file in files:
        urlend = os.path.relpath(file, os.path.dirname(__file__))
        url = os.path.join(URLBASE, urlend)
        with open(file) as f:
            schema = Schema(url, f.read())
            schemas.append(schema)
    # Sort schemas so that the output is deterministic.
    schemas.sort(key=lambda s: s.url)
    return schemas


def gen_go_schemas_package(schemas: List[Schema]) -> List[str]:
    """
    Generate a file at the level of pkg/schemas/ that has all of the schemas embedded into it for
    all config types.

    This is necesary to have a single place that can create validators with all of the schema
    urls, so that schemas of one type are free to reference schemas of another type.
    """
    lines = []
    lines.append("// Code generated by gen.py. DO NOT EDIT.")
    lines.append("")
    lines.append("package schemas")
    lines.append("")
    lines.append("import (")
    lines.append('\t"encoding/json"')
    lines.append(")")
    lines.append("")

    # Global variables (lazily loaded but otherwise constants).
    lines.append("var (")
    # Schema texts.
    lines.extend(
        [f"\ttext{schema.golang_title} = []byte(`{schema.text}`)" for schema in schemas]
    )
    # Cached schema values, initially nil.
    for schema in schemas:
        lines.append(f"\tschema{schema.golang_title} interface{{}}")
        lines.append("")
    # Cached map of urls to schema values, initially nil.
    lines.append("\tcachedSchemaMap map[string]interface{}")
    lines.append("")
    lines.append("\tcachedSchemaBytesMap map[string][]byte")
    lines.append(")")
    lines.append("")

    # Schema getters.  These are exported so that they can be used in the individual packages.
    for schema in schemas:
        lines.extend(
            [
                f"func Parsed{schema.golang_title}() interface{{}} {{",
                f"\tif schema{schema.golang_title} != nil {{",
                f"\t\treturn schema{schema.golang_title}",
                "\t}",
                f"\terr := json.Unmarshal(text{schema.golang_title}, &schema{schema.golang_title})",
                "\tif err != nil {",
                f'\t\tpanic("invalid embedded json for {schema.golang_title}")',
                "\t}",
                f"\treturn schema{schema.golang_title}",
                "}",
            ]
        )
        lines.append("")

    # SchemaBytesMap, used internally by NewCompiler, which has to have a list of all schemas.
    lines.append("func schemaBytesMap() map[string][]byte {")
    lines.append("\tif cachedSchemaBytesMap != nil {")
    lines.append("\t\treturn cachedSchemaBytesMap")
    lines.append("\t}")
    lines.append("\tvar url string")
    lines.append("\tcachedSchemaBytesMap = map[string][]byte{}")
    for schema in schemas:
        lines.append(f'\turl = "{schema.url}"')
        lines.append(f"\tcachedSchemaBytesMap[url] = text{schema.golang_title}")
    lines.append("\treturn cachedSchemaBytesMap")
    lines.append("}")

    return lines


def next_struct_name(file: str, start: int) -> str:
    """
    Find the name of the next struct definition in a go file starting at a given line.

    This is how we decide which struct to operate on for the //go:generate comments above structs.
    """
    with open(file) as f:
        for lineno, line in enumerate(f.readlines()):
            if lineno <= start:
                continue
            match = re.match("type ([\\S]+) struct", line)
            if match is not None:
                return match[1]
    raise AssertionError(f"did not find struct in {file} after line {line}")


# FieldSpec = (field, type, tag)
FieldSpec = Tuple[str, str, str]
# UnionSpec = (field, type)
UnionSpec = Tuple[str, str]


def find_struct(file: str, struct_name: str) -> Tuple[List[FieldSpec], List[UnionSpec]]:
    """
    Open a file and find a struct definition for a given name.

    This function uses regex to read the golang source code... hacky, but it works.
    """
    field_spec = []  # type: List[FieldSpec]
    union_spec = []  # type: List[UnionSpec]
    with open(file) as f:
        state = "pre"
        for lineno, line in enumerate(f.readlines()):
            if state == "pre":
                if line.startswith(f"type {struct_name} struct"):
                    state = "fields"
            elif state == "fields":
                if line.strip() == "}":
                    # No more fields
                    return field_spec, union_spec
                if line.strip() == "":
                    # No field on this line
                    continue
                if line.startswith("\t//"):
                    # comment line
                    continue

                # Union fields.
                match = re.match("\t([\\S]+)\\s+([\\S]+)\\s+`union.*", line)
                if match is not None:
                    field, type = match[1], match[2]
                    union_spec.append((field, type))
                    continue

                # Normal fields: capture the field name, the type, and the json tag.
                match = re.match('\t([\\S]+)\\s+([\\S]+)\\s+`json:"([^,"]+)', line)
                if match is not None:
                    field, type, tag = match[1], match[2], match[3]
                    # store the field name and the type
                    field_spec.append((field, type, tag))
                    continue

                raise AssertionError(
                    f"unsure how to handle line {lineno}: '{line.rstrip()}'"
                )

    # We should have exited when we saw the "}" line.
    raise AssertionError(
        f"failed to find struct definition for {struct_name} in {file}"
    )


def find_schema(package: str, struct: str) -> Schema:
    """Locate a json-schema file from a struct name."""
    if re.match(".*V[0-9]+", struct) is None:
        raise AssertionError(
            f"{struct} is not a valid schema type name; it should end in Vx where x is a digit"
        )
    version = struct[-2:].lower()
    dir = os.path.join(HERE, package, version)
    for file in os.listdir(dir):
        if not file.endswith(".json"):
            continue
        path = os.path.join(dir, file)
        urlend = os.path.relpath(path, HERE)
        url = os.path.join(URLBASE, urlend)
        with open(path) as f:
            schema = Schema(url, f.read())
            if schema.golang_title != struct:
                continue
            return schema
    raise AssertionError("failed to find schema")


def get_defaulted_type(schema: Schema, tag: str, type: str) -> Tuple[str, str, bool]:
    """
    Given the type string for a field of a given tag, determine the type of the after-defaulting
    value.  This is used by the auto-generated getters, so that parts of the code which consume
    experiment configs can use compile-time checks to know which pointer-typed fields values might
    be nil and which ones have defaults and will never be nil.
    """
    prop = schema.schema["properties"].get(tag, {})
    if prop is True:
        prop = {}
    default = prop.get("default")

    required = schema.schema.get("required", []) or schema.schema.get(
        "eventuallyRequired", []
    )

    if default is not None:
        if not type.startswith("*"):
            raise AssertionError(
                f"{tag} type ({type}) must be a pointer since it can be defaulted"
            )
        if type.startswith("**"):
            raise AssertionError(f"{tag} type ({type}) must not be a double pointer")
        type = type[1:]

    return type, default, required


def go_getters(struct: str, schema: Schema, spec: List[FieldSpec]) -> List[str]:
    lines = []  # type: List[str]

    if len(spec) < 1:
        return lines

    x = struct[0].lower()

    for field, type, tag in spec:
        defaulted_type, default, required = get_defaulted_type(schema, tag, type)

        if default is None:
            lines.append(f"func ({x} {struct}) Get{field}() {type} {{")
            lines.append(f"\treturn {x}.{field}")
            lines.append("}")
            lines.append("")
        else:
            lines.append(f"func ({x} {struct}) Get{field}() {defaulted_type} {{")
            lines.append(f"\tif {x}.{field} == nil {{")
            lines.append(
                f'\t\tpanic("You must call WithDefaults on {struct} before .Get{field}")'
            )
            lines.append("\t}")
            lines.append(f"\treturn *{x}.{field}")
            lines.append("}")
            lines.append("")

    return lines


def get_union_common_members(
    file: str, package: str, union_types: List[str]
) -> List[Tuple[str, str]]:
    """
    Look at all of the union members types for a union type and automatically determine which
    members are common to all members.
    """
    # Find all members and types of all union member types
    per_struct_members = []
    for struct in union_types:
        schema = find_schema(package, struct)
        spec, union = find_struct(file, struct)
        if len(union) > 0:
            raise AssertionError(
                f"detected nested union; {struct} is a union member and also a union itself"
            )
        members = {}
        for field, type, tag in spec:
            type, _, _ = get_defaulted_type(schema, tag, type)
            members[field] = type
        per_struct_members.append(members)

    # Find common members by name.
    common_fields = set(per_struct_members[0].keys())
    for members in per_struct_members[1:]:
        common_fields = common_fields.intersection(set(members.keys()))

    # Validate types all match.
    for field in common_fields:
        field_types = {members[field] for members in per_struct_members}
        if len(field_types) != 1:
            raise AssertionError(
                f".{field} has multiple types ({field_types}) among union members {union_types}"
            )

    # Sort this so the generation is deterministic.
    return sorted(
        {field: per_struct_members[0][field] for field in common_fields}.items()
    )


def go_unions(
    struct: str, package: str, file: str, schema: Schema, union_spec: List[UnionSpec]
) -> List[str]:
    lines = []  # type: List[str]
    if len(union_spec) < 1:
        return lines
    x = struct[0].lower()

    # Define a GetUnionMember() that returns an interface.
    lines.append(f"func ({x} {struct}) GetUnionMember() interface{{}} {{")
    for field, _ in union_spec:
        lines.append(f"\tif {x}.{field} != nil {{")
        lines.append("\t\treturn nil")
        lines.append("\t}")
    lines.append('\tpanic("no union member defined")')
    lines.append("}")
    lines.append("")

    union_types = [type.lstrip("*") for _, type in union_spec]

    # Define getters for each of the common members of the union.
    common_members = get_union_common_members(file, package, union_types)
    for common_field, type in common_members:
        lines.append(f"func ({x} {struct}) Get{common_field}() {type} {{")
        for field, _ in union_spec:
            lines.append(f"\tif {x}.{field} != nil {{")
            lines.append(f"\t\treturn {x}.{field}.Get{common_field}()")
            lines.append("\t}")
        lines.append('\tpanic("no union member defined")')
        lines.append("}")
        lines.append("")

    return lines


def go_helpers(struct: str) -> List[str]:
    """
    Define WithDefaults() and Merge(), which are typed wrappers around schemas.WithDefaults() and
    schemas.Merge().
    """
    lines = []

    x = struct[0].lower()

    lines.append(f"func ({x} {struct}) WithDefaults() {struct} {{")
    lines.append(f"\treturn schemas.WithDefaults({x}).({struct})")
    lines.append("}")
    lines.append("")

    lines.append(f"func ({x} {struct}) Merge(other {struct}) {struct} {{")
    lines.append(f"\treturn schemas.Merge({x}, other).({struct})")
    lines.append("}")

    return lines


def go_schema_interface(struct: str, url: str) -> List[str]:
    """
    Generate the schemas.Schema interface for a particular schema.

    This is used for getting json-schema-based validators from Schema objects, as well as being
    used by the reflect code in defaults.go.
    """
    lines = []

    x = struct[0].lower()

    lines.append("")
    lines.append(f"func ({x} {struct}) ParsedSchema() interface{{}} {{")
    lines.append(f"\treturn schemas.Parsed{struct}()")
    lines.append("}")
    lines.append("")
    lines.append(f"func ({x} {struct}) SanityValidator() *jsonschema.Schema {{")
    lines.append(f'\treturn schemas.GetSanityValidator("{url}")')
    lines.append("}")
    lines.append("")
    lines.append(f"func ({x} {struct}) CompletenessValidator() *jsonschema.Schema {{")
    lines.append(f'\treturn schemas.GetCompletenessValidator("{url}")')
    lines.append("}")

    return lines


def gen_go_struct(
    package: str, file: str, line: int, imports: List[str]
) -> Tuple[str, List[str]]:
    """Used by the //go:generate decorations on structs."""
    struct = next_struct_name(file, line)
    field_spec, union_spec = find_struct(file, struct)

    if len(field_spec) and len(union_spec):
        raise AssertionError(f"{struct} has both union tags and normal fields")

    schema = find_schema(package, struct)

    lines = []
    lines.append("// Code generated by gen.py. DO NOT EDIT.")
    lines.append("")

    lines.append(f"package {package}")
    lines.append("")

    lines.append("import (")
    lines.append('\t"github.com/santhosh-tekuri/jsonschema/v2"')

    for imp in imports:
        lines.append("\t" + imp)
    lines.append("")
    lines.append('\t"github.com/determined-ai/determined/master/pkg/schemas"')
    lines.append(")")
    lines.append("")

    lines += go_getters(struct, schema, field_spec)
    lines += go_unions(struct, package, file, schema, union_spec)
    lines += go_helpers(struct)
    lines += go_schema_interface(struct, schema.url)

    filename = "zgen_" + camel_to_snake(struct) + ".go"

    return filename, lines


def gen_python(schemas: List[Schema]) -> List[str]:
    lines = []
    lines.append("# This is a generated file.  Editing it will make you sad.")
    lines.append("")
    lines.append("import json")
    lines.append("")
    lines.append("schemas = {")
    for schema in schemas:
        lines.append(f'    "{schema.url}": json.loads(')
        lines.append(f'        r"""\n{schema.text}\n"""')
        lines.append("    ),")
    lines.append("}")

    return lines


def maybe_write_output(lines: List[str], output: Optional[str]) -> None:
    """Write lines to output, unless output would be unchanged."""

    text = "\n".join(lines) + "\n"

    if output is None:
        # Write to stdout.
        sys.stdout.write(text)
        return

    if os.path.exists(output):
        with open(output, "r") as f:
            if f.read() == text:
                return

    with open(output, "w") as f:
        f.write(text)


def python_main(package: str, output: Optional[str]) -> None:
    assert package is not None, "--package must be provided"
    files = list_files(package)
    schemas = read_schemas(files)

    lines = gen_python(schemas)

    maybe_write_output(lines, output)


def go_struct_main(package: str, file: str, line: int, imports: Optional[str]) -> None:
    assert package is not None, "GOPACKAGE not set"
    assert file is not None, "GOFILE not set"
    assert line is not None, "GOLINE not set"

    def fmt_import(imp: str) -> str:
        """Turn e.g. `k8sV1:k8s.io/api/core/v1` into `k8sV1 "k8s.io/api/core/v1"`."""
        if ":" in imp:
            return imp.replace(":", ' "') + '"'
        else:
            return '"' + imp + '"'

    imports_list = []
    if imports is not None:
        imports_list = [fmt_import(i) for i in imports.split(",") if i]

    output, lines = gen_go_struct(package, file, line, imports_list)

    maybe_write_output(lines, output)


def go_root_main(output: Optional[str]) -> None:
    files = []
    for package in ALL_PKGS:
        files += list_files(package)
    schemas = read_schemas(files)

    lines = gen_go_schemas_package(schemas)

    maybe_write_output(lines, output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="generate code with embedded schemas")
    subparsers = parser.add_subparsers(dest="generator")

    # Python generator.
    python_parser = subparsers.add_parser("python")
    python_parser.add_argument("--package", required=True)
    python_parser.add_argument("--output")

    # Go struct generator, expect environment variables set by go generate.
    go_struct_parser = subparsers.add_parser("go-struct")
    go_struct_parser.add_argument("--package", default=os.environ.get("GOPACKAGE"))
    go_struct_parser.add_argument("--file", default=os.environ.get("GOFILE"))
    go_struct_parser.add_argument("--line", default=os.environ.get("GOLINE"), type=int)
    go_struct_parser.add_argument("--imports")

    # Go root generator.
    go_root_parser = subparsers.add_parser("go-root")
    go_root_parser.add_argument("--output")

    args = vars(parser.parse_args())

    try:
        assert "generator" in args, "missing generator argument on command line"
        generator = args.pop("generator")
        if generator == "python":
            python_main(**args)
        elif generator == "go-struct":
            go_struct_main(**args)
        elif generator == "go-root":
            go_root_main(**args)
        else:
            raise ValueError(f"unrecognized generator: {generator}")
    except AssertionError as e:
        print(e, file=sys.stderr)
        sys.exit(1)
