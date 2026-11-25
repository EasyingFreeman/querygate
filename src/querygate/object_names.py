import re

CATALOG_IDENTIFIER = r"[^\x00-\x1f\x7f]{1,128}"
PLAIN_PART = r'[^.\[\]"\x00-\x1f\x7f]{1,128}'
QUOTED_PART = r'"(?:[^"\x00-\x1f\x7f]|""){1,128}"'
BRACKETED_PART = r"\[(?:[^\]\x00-\x1f\x7f]|\]\]){1,128}\]"
IDENTIFIER_PART = rf"(?:{PLAIN_PART}|{QUOTED_PART}|{BRACKETED_PART})"
QUALIFIED_NAME = re.compile(rf"({IDENTIFIER_PART})\.({IDENTIFIER_PART})")


def qualified_object_name(schema: str, name: str) -> str:
    def encode(part: str) -> str:
        if part == part.strip() and re.fullmatch(PLAIN_PART, part):
            return part
        return '"' + part.replace('"', '""') + '"'

    return f"{encode(schema)}.{encode(name)}"


def normalize_object_name(value: str) -> str:
    match = QUALIFIED_NAME.fullmatch(value)
    if not match:
        raise ValueError("Use a two-part Schema.Object name")

    def decode(part: str) -> str:
        if part.startswith('"'):
            return part[1:-1].replace('""', '"')
        if part.startswith("["):
            return part[1:-1].replace("]]", "]")
        return part

    schema, name = map(decode, match.groups())
    return qualified_object_name(schema, name)
