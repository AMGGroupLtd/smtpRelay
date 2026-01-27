#!/usr/bin/env python3
"""
env_validator.py

Validate a .env file against a schema described in an env.example file
using a custom, structured comment format like:

    #SETTING_NAME
    #    name='SETTING_NAME',
    #    type='string',            # one of: string, int, float, bool, url (default: string)
    #    default='value',          # or default=None (means OPTIONAL)
    #    required=true,            # default is false if not specified
    #    valid_values=[
    #        'LITERAL',
    #        '/^[A-Za-z0-9_]+$/i', # regex (slash form, optional flags: i m s x a L u)
    #        'regex:^[A-Za-z]+$',  # regex (prefix form; use inline flags like (?i) if needed)
    #    ]
    #    note='Some guidance text'

Key rules:
  - default=None        => the variable is OPTIONAL.
  - required=true|false => explicit requiredness (default false when omitted).
  - type=...            => enforce value type:
        * string: any string (still validated by valid_values if provided)
        * int:    base-10 integer (e.g., -10, 0, 42)
        * float:  Python float (e.g., -1.25, 3.14, 1e-6)
        * bool:   one of (true/false/1/0/yes/no/on/off) case-insensitive
        * url:    must have a scheme and netloc (e.g., https://example.com)
  - valid_values: mix of literals and regexes; pass if value matches ANY item:
        * Literal compare is case-insensitive by default (toggle with CLI)
        * Regex uses fullmatch() with provided flags or inline modifiers

Commands:
  Validation:
    python env_validator.py --example env.example --env .env
    python env_validator.py --example env.example --env .env --strict
    python env_validator.py --example env.example --env .env --format json
    python env_validator.py --example env.example --env .env --case-sensitive-values
    python env_validator.py --example env.example --env .env --verbose

  Scaffolding:
    python env_validator.py --example env.example --scaffold > .env
    python env_validator.py --example env.example --scaffold --out .env
    python env_validator.py --example env.example --scaffold --out .env --force
    # Quoting controls for scaffold:
    #   --preserve-default-quotes (default on)
    #   --no-preserve-default-quotes
    #   --quote-strings  (always quote type=string)

  Docs / Schema:
    python env_validator.py --example env.example --emit-markdown > CONFIG.md
    python env_validator.py --example env.example --emit-schema > schema.json

Exit codes:
    0 = success (no validation errors)
    1 = validation errors (missing required vars, invalid values, type errors)
    2 = parse error (malformed schema or files not found)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple, Any
from urllib.parse import urlparse


# ---------------------------
# Data models
# ---------------------------

@dataclass
class ConfigOption:
    """Represents a single configuration option from the schema."""
    name: str
    type_name: str = "string"  # string | int | float | bool | url
    default: Optional[str] = None
    required: bool = False  # explicit; default false when omitted
    valid_literals: Optional[List[str]] = None
    valid_regex: Optional[List[str]] = None  # stored for display (e.g., '/^...$/i')
    _compiled_regex: Optional[List[Tuple[re.Pattern, str]]] = None  # (compiled, display)
    note: Optional[str] = None
    # Track whether the default in schema was quoted (to preserve in scaffold)
    default_was_quoted: bool = False

    def to_dict(self) -> dict:
        # exclude compiled regex from emitted schema
        d = asdict(self)
        d.pop("_compiled_regex", None)
        return d


@dataclass
class ValidationIssue:
    level: str  # 'error' or 'warning'
    code: str   # 'missing', 'invalid_value', 'unknown', 'parse_error', 'type_mismatch'
    message: str
    setting: Optional[str] = None
    details: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "setting": self.setting,
            "details": self.details or {},
        }


@dataclass
class ValidationReport:
    issues: List[ValidationIssue]
    errors: int
    warnings: int

    def to_dict(self) -> dict:
        return {
            "errors": self.errors,
            "warnings": self.warnings,
            "issues": [i.to_dict() for i in self.issues],
        }


# ---------------------------
# Parsing utilities
# ---------------------------

SETTING_HEADER_RE = re.compile(r"^\s*#\s*([A-Z0-9_]+)\s*$")
NAME_LINE_RE = re.compile(r"^\s*#\s*name\s*=\s*'([^']+)'\s*,?\s*(?:#.*)?$")
DEFAULT_LINE_RE = re.compile(r"^\s*#\s*default\s*=\s*(.+?)(?:\s*#.*)?$")
TYPE_LINE_RE = re.compile(r"^\s*#\s*type\s*=\s*(.+?)(?:\s*#.*)?$")
VALID_VALUES_LINE_RE = re.compile(r"^\s*#\s*valid_values\s*=\s*\[(.*)\]\s*,?\s*$")
NOTE_LINE_RE = re.compile(r"^\s*#\s*note\s*=\s*(.+?)\s*$")
REQUIRED_LINE_RE = re.compile(r"^\s*#\s*required\s*=\s*(true|false)\s*,?\s*$", re.IGNORECASE)


def _strip_quotes(token: str) -> str:
    token = token.strip()
    if len(token) >= 2 and ((token[0] == token[-1]) and token[0] in ("'", '"')):
        return token[1:-1]
    return token


def _parse_default_value_with_quote_info(token: str) -> Tuple[Optional[str], bool]:
    """
    Returns (value, was_quoted).
    None indicates the literal None in schema meaning OPTIONAL (per your design).
    """
    raw = token.strip().rstrip(",")
    if raw.lower() == "none":
        return None, False
    was_quoted = (len(raw) >= 2 and raw[0] in ("'", '"') and raw[-1] == raw[0])
    val = _strip_quotes(raw)
    return val, was_quoted


def _parse_type_value(token: str) -> str:
    """
    Parse the type value. Accepts quoted or bare string.
    Allowed: string, int, float, bool, url
    """
    value = _strip_quotes(token.strip().rstrip(",")).lower()
    allowed = {"string", "int", "float", "bool", "url"}
    if value not in allowed:
        raise ValueError(f"Unsupported type '{value}'. Allowed: {sorted(allowed)}")
    return value


def _tokenize_list(inner: str) -> List[str]:
    """
    Tokenize the content of a [...] list into items separated by commas,
    respecting quotes and regex slash blocks (/.../flags).

    Note:
      If your regex contains commas, wrap the whole token in quotes.
    """
    items: List[str] = []
    current: List[str] = []
    in_quote = False
    quote_char = None
    in_regex = False
    regex_escaped = False

    s = inner.strip()
    i = 0
    while i < len(s):
        ch = s[i]

        if in_quote:
            current.append(ch)
            if ch == quote_char:
                in_quote = False
            i += 1
            continue

        if in_regex:
            current.append(ch)
            if ch == "\\":
                regex_escaped = not regex_escaped
            else:
                if ch == "/" and not regex_escaped:
                    in_regex = False
                regex_escaped = False
            i += 1
            continue

        if ch in ("'", '"'):
            in_quote = True
            quote_char = ch
            current.append(ch)
            i += 1
            continue

        if ch == "/":
            in_regex = True
            regex_escaped = False
            current.append(ch)
            i += 1
            continue

        if ch == ",":
            token = "".join(current).strip()
            if token:
                items.append(token)
            current = []
            i += 1
            continue

        current.append(ch)
        i += 1

    token = "".join(current).strip()
    if token:
        items.append(token)
    return items


def _map_regex_flags(flags_str: str) -> int:
    """Map /pattern/flags letters to re flags."""
    flag_map = {
        'i': re.IGNORECASE,
        'm': re.MULTILINE,
        's': re.DOTALL,
        'x': re.VERBOSE,
        'a': re.ASCII,
        'L': re.LOCALE,
        'u': 0,  # Python 3 default is Unicode; accept but ignore
    }
    flags = 0
    for ch in flags_str:
        flags |= flag_map.get(ch, 0)
    return flags


def _interpret_allowed_token(token: str) -> Tuple[str, Any, Optional[str]]:
    """
    Interpret a valid_values token.
    Returns a tuple: (kind, value, display)
      - ('literal', 'DEBUG', None)
      - ('regex', (compiled_pattern, flags_int), '/^foo$/i')
    Supports:
      * Slash form: /pattern/flags
      * Prefix form: regex:pattern   (flags can be embedded as (?i) in the pattern)
    """
    raw = token.strip().rstrip(",")
    unquoted = _strip_quotes(raw)

    # Slash form: /pattern/flags
    m = re.match(r"^/(.*?)/([A-Za-z]*)$", unquoted)
    if m:
        pattern = m.group(1)
        flags_str = m.group(2)
        flags = _map_regex_flags(flags_str)
        try:
            compiled = re.compile(pattern, flags)
        except re.error as e:
            raise ValueError(f"Invalid regex in valid_values: /{pattern}/{flags_str} ({e})")
        display = f"/{pattern}/{flags_str}"
        return ('regex', (compiled, display), display)

    # Prefix form: regex:pattern
    if unquoted.lower().startswith("regex:"):
        pattern = unquoted[len("regex:"):]
        try:
            compiled = re.compile(pattern)
        except re.error as e:
            raise ValueError(f"Invalid regex in valid_values: regex:{pattern} ({e})")
        display = f"regex:{pattern}"
        return ('regex', (compiled, display), display)

    # literal
    return ('literal', unquoted, None)


def _parse_valid_values(inner: str) -> Tuple[List[str], List[Tuple[re.Pattern, str]]]:
    """
    Parse a valid_values list into literals and compiled regex patterns.
    Returns:
        (literals, compiled_regex_with_display)
    """
    literals: List[str] = []
    regex_list: List[Tuple[re.Pattern, str]] = []
    tokens = _tokenize_list(inner)
    for tok in tokens:
        kind, val, display = _interpret_allowed_token(tok)
        if kind == 'literal':
            literals.append(val)
        elif kind == 'regex':
            compiled, disp = val
            regex_list.append((compiled, disp))
    return literals, regex_list


def parse_env_example_schema(path: str) -> Tuple[Dict[str, ConfigOption], List[ValidationIssue]]:
    """
    Parse an env.example file to extract schema from comment blocks.

    Returns:
        (schema_map, issues)
        schema_map: dict of name -> ConfigOption
        issues: parse warnings/errors (as ValidationIssue)
    """
    issues: List[ValidationIssue] = []
    schema: Dict[str, ConfigOption] = {}

    if not os.path.exists(path):
        issues.append(ValidationIssue(
            level="error",
            code="parse_error",
            message=f"env.example file not found: {path}",
        ))
        return {}, issues

    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:
        issues.append(ValidationIssue(
            level="error",
            code="parse_error",
            message=f"Failed to read env.example file: {e}",
        ))
        return {}, issues

    i = 0
    total = len(lines)
    while i < total:
        line = lines[i]
        m = SETTING_HEADER_RE.match(line)
        if not m:
            i += 1
            continue

        setting_name = m.group(1)
        i += 1
        meta: Dict[str, Any] = {
            "name": None,
            "type_name": "string",   # default
            "default": None,         # Optional[str]
            "default_was_quoted": False,
            "valid_literals": None,  # Optional[List[str]]
            "valid_regex": None,     # Optional[List[Tuple[re.Pattern, str]]]
            "note": None,
            "required": None,        # Optional[bool]
        }

        while i < total:
            meta_line = lines[i]
            # Stop at first non-comment or next header
            if not meta_line.lstrip().startswith("#"):
                break
            if SETTING_HEADER_RE.match(meta_line):
                break

            m_name = NAME_LINE_RE.match(meta_line)
            m_default = DEFAULT_LINE_RE.match(meta_line)
            m_type = TYPE_LINE_RE.match(meta_line)
            m_valid = VALID_VALUES_LINE_RE.match(meta_line)
            m_note = NOTE_LINE_RE.match(meta_line)
            m_required = REQUIRED_LINE_RE.match(meta_line)

            if m_name:
                meta["name"] = m_name.group(1).strip()
            elif m_default:
                try:
                    val, was_quoted = _parse_default_value_with_quote_info(m_default.group(1))
                    meta["default"] = val
                    meta["default_was_quoted"] = was_quoted
                except Exception as e:
                    issues.append(ValidationIssue(
                        level="error",
                        code="parse_error",
                        message=f"Invalid default value for {setting_name}: {e}",
                        setting=setting_name,
                    ))
            elif m_type:
                try:
                    meta["type_name"] = _parse_type_value(m_type.group(1))
                except Exception as e:
                    issues.append(ValidationIssue(
                        level="error",
                        code="parse_error",
                        message=f"Invalid type for {setting_name}: {e}",
                        setting=setting_name,
                    ))
            elif m_valid:
                try:
                    lits, regexes = _parse_valid_values(m_valid.group(1))
                    meta["valid_literals"] = lits or None
                    meta["valid_regex"] = regexes or None
                except Exception as e:
                    issues.append(ValidationIssue(
                        level="error",
                        code="parse_error",
                        message=f"Invalid valid_values for {setting_name}: {e}",
                        setting=setting_name,
                    ))
            elif m_note:
                note_raw = m_note.group(1).strip().rstrip(",")
                meta["note"] = _strip_quotes(note_raw)
            elif m_required:
                meta["required"] = (m_required.group(1).lower() == "true")

            i += 1

        opt_name = (meta["name"] or setting_name).strip()
        default = meta["default"]
        default_was_quoted = bool(meta.get("default_was_quoted", False))
        type_name = meta["type_name"]
        valid_literals = meta["valid_literals"]
        valid_regex_pairs = meta["valid_regex"]
        note = meta["note"]

        # Requiredness: explicit only; default is False when not specified
        required = bool(meta["required"]) if meta["required"] is not None else False

        display_regex = [disp for (_compiled, disp) in (valid_regex_pairs or [])]
        option = ConfigOption(
            name=opt_name,
            type_name=type_name,
            default=default,
            required=required,
            valid_literals=valid_literals,
            valid_regex=display_regex or None,
            _compiled_regex=valid_regex_pairs or None,
            note=note,
            default_was_quoted=default_was_quoted,
        )

        if opt_name in schema:
            issues.append(ValidationIssue(
                level="warning",
                code="parse_error",
                message=f"Duplicate schema entry for '{opt_name}' in env.example; later entry overwrites earlier.",
                setting=opt_name,
            ))
        schema[opt_name] = option

    if not schema:
        issues.append(ValidationIssue(
            level="warning",
            code="parse_error",
            message="No schema blocks found in env.example (expected '#SETTING' blocks with meta lines).",
        ))
    return schema, issues


# ---------------------------
# .env parsing
# ---------------------------

ENV_LINE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")

def _parse_env_value(raw: str) -> str:
    """
    Parse a .env value respecting quotes and inline comments.
    Rules:
      - If value starts with a quote (' or "), consume until matching quote (supports escapes).
      - Otherwise, strip trailing inline comments starting with '#' if preceded by space.
      - Trim whitespace and outer quotes.
    """
    s = raw.rstrip("\n").rstrip("\r").strip()
    if not s:
        return ""

    if s[0] in ("'", '"'):
        q = s[0]
        escaped = False
        val_chars: List[str] = []
        for ch in s[1:]:
            if ch == q and not escaped:
                return "".join(val_chars)
            if ch == "\\" and not escaped:
                escaped = True
            else:
                escaped = False
            val_chars.append(ch)
        # no closing quote; return remainder without the opening quote
        return s[1:]

    # Not quoted: cut off inline comment starting with # (if preceded by whitespace)
    hash_pos = s.find("#")
    if hash_pos != -1:
        before_hash = s[:hash_pos]
        if before_hash.endswith(" ") or before_hash.endswith("\t"):
            s = before_hash.strip()

    return s.strip().strip("'").strip('"')


def load_dotenv(path: str) -> Tuple[Dict[str, str], List[ValidationIssue]]:
    """
    Load a .env file into a dict, ignoring comments and blank lines.
    """
    issues: List[ValidationIssue] = []
    env: Dict[str, str] = {}

    if not os.path.exists(path):
        issues.append(ValidationIssue(
            level="error",
            code="parse_error",
            message=f".env file not found: {path}",
        ))
        return {}, issues

    try:
        with open(path, "r", encoding="utf-8") as f:
            for ln, raw in enumerate(f, start=1):
                s = raw.strip()
                if not s or s.startswith("#"):
                    continue
                m = ENV_LINE_RE.match(raw)
                if not m:
                    issues.append(ValidationIssue(
                        level="warning",
                        code="parse_error",
                        message=f"Unrecognized line in .env at {path}:{ln}: {raw.strip()}",
                    ))
                    continue
                key = m.group(1)
                val_raw = m.group(2).strip()
                val = _parse_env_value(val_raw)
                env[key] = val
    except Exception as e:
        issues.append(ValidationIssue(
            level="error",
            code="parse_error",
            message=f"Failed to read .env file: {e}",
        ))
        return {}, issues

    return env, issues


# ---------------------------
# Type enforcement
# ---------------------------

_BOOL_TRUE = {"true", "1", "yes", "y", "on"}
_BOOL_FALSE = {"false", "0", "no", "n", "off"}

def _coerce_bool(value: str) -> Tuple[bool, str]:
    v = value.strip().lower()
    if v in _BOOL_TRUE:
        return True, "true"
    if v in _BOOL_FALSE:
        return False, "false"
    raise ValueError("expected a boolean (true/false/1/0/yes/no/on/off)")

def _coerce_int(value: str) -> Tuple[int, str]:
    if not re.fullmatch(r"[+-]?\d+", value.strip()):
        raise ValueError("expected an integer (e.g., -10, 0, 42)")
    i = int(value.strip(), 10)
    return i, str(i)

def _coerce_float(value: str) -> Tuple[float, str]:
    try:
        f = float(value.strip())
    except ValueError:
        raise ValueError("expected a float (e.g., -1.25, 3.14, 1e-6)")
    return f, str(f)

def _coerce_url(value: str) -> Tuple[str, str]:
    u = urlparse(value.strip())
    if not u.scheme or not u.netloc:
        raise ValueError("expected a URL with scheme and host (e.g., https://example.com)")
    return value.strip(), value.strip()

def _coerce_by_type(value: str, type_name: str) -> Tuple[Any, str]:
    """
    Returns (python_value, canonical_str) or raises ValueError on mismatch.
    """
    if type_name == "string":
        return value, value
    if type_name == "bool":
        return _coerce_bool(value)
    if type_name == "int":
        return _coerce_int(value)
    if type_name == "float":
        return _coerce_float(value)
    if type_name == "url":
        return _coerce_url(value)
    # Fallback to string
    return value, value


def _normalize_literal_for_type(literal: str, type_name: str) -> Any:
    """
    Attempt to normalize a literal against the target type for comparison.
    If it cannot be coerced for that type, fall back to the raw string.
    """
    try:
        py_val, canon = _coerce_by_type(literal, type_name)
        if type_name in ("bool", "int", "float"):
            return py_val
        return canon
    except Exception:
        return literal


# ---------------------------
# Validation
# ---------------------------

def validate_env(
        schema: Dict[str, ConfigOption],
        env: Dict[str, str],
        *,
        strict_unknown: bool = False,
        case_insensitive_values: bool = True,
) -> ValidationReport:
    """
    Validate env against schema.
    - Missing required variables => error
    - Type mismatch => error
    - If valid_literals or valid_regex are present:
        * Literal allowed if value equals ANY literal (coerced to type when possible).
          - For string/url types: string compare (case-insensitive optional).
          - For bool/int/float: compare normalized Python values.
        * Regex allowed if value FULLMATCHes any regex.
    - Unknown variables => error if strict_unknown=True
    """
    issues: List[ValidationIssue] = []

    # Unknown keys (not in schema)
    if strict_unknown:
        for key in sorted(env.keys()):
            if key not in schema:
                issues.append(ValidationIssue(
                    level="error",
                    code="unknown",
                    message=f"Unknown variable '{key}' present in .env but not in schema.",
                    setting=key,
                ))

    # For each schema option, check presence, type, allowed values
    for name, option in schema.items():
        present = name in env
        if not present:
            if option.required:
                issues.append(ValidationIssue(
                    level="error",
                    code="missing",
                    message=f"Missing required variable '{name}'.",
                    setting=name,
                    details={"default": option.default, "note": option.note},
                ))
            else:
                issues.append(ValidationIssue(
                    level="warning",
                    code="missing",
                    message=f"Variable '{name}' not set; default will be used.",
                    setting=name,
                    details={"default": option.default, "note": option.note},
                ))
            continue

        raw_value = env[name]

        # Type check
        try:
            py_val, canon_str = _coerce_by_type(raw_value, option.type_name)
        except ValueError as e:
            issues.append(ValidationIssue(
                level="error",
                code="type_mismatch",
                message=f"Type mismatch for '{name}': {e}",
                setting=name,
                details={"type": option.type_name, "value": raw_value},
            ))
            continue

        # Allowed values (if any)
        literals = option.valid_literals or []
        regex_pairs = option._compiled_regex or []
        if literals or regex_pairs:
            allowed_ok = False

            # literal compare
            if literals and not allowed_ok:
                if option.type_name in ("bool", "int", "float"):
                    normalized_allowed = []
                    for lit in literals:
                        normalized_allowed.append(_normalize_literal_for_type(lit, option.type_name))
                    if py_val in normalized_allowed:
                        allowed_ok = True
                else:
                    if case_insensitive_values:
                        lit_l = [lit.lower() for lit in literals]
                        if canon_str.lower() in lit_l:
                            allowed_ok = True
                    else:
                        if canon_str in literals:
                            allowed_ok = True

            # regex compare (always against original raw string)
            if not allowed_ok and regex_pairs:
                for compiled, _display in regex_pairs:
                    if compiled.fullmatch(raw_value) is not None:
                        allowed_ok = True
                        break

            if not allowed_ok:
                allowed_display: List[str] = []
                if literals:
                    allowed_display.extend(literals)
                if option.valid_regex:
                    allowed_display.extend(option.valid_regex)
                issues.append(ValidationIssue(
                    level="error",
                    code="invalid_value",
                    message=f"Invalid value for '{name}': '{raw_value}'. Allowed: {allowed_display}",
                    setting=name,
                    details={"allowed": allowed_display, "value": raw_value},
                ))

    errors = sum(1 for i in issues if i.level == "error")
    warnings = sum(1 for i in issues if i.level == "warning")
    return ValidationReport(issues=issues, errors=errors, warnings=warnings)


# ---------------------------
# Rendering / Output helpers
# ---------------------------

def _filter_issues(issues: List[ValidationIssue], verbose: bool) -> List[ValidationIssue]:
    if verbose:
        return issues
    return [i for i in issues if i.level == "error"]

def print_human_report(report: ValidationReport, *, verbose: bool) -> None:
    filtered = _filter_issues(report.issues, verbose)
    if not filtered:
        print("✅ No issues found.")
        return

    for issue in filtered:
        prefix = "ERROR" if issue.level == "error" else "WARN"
        setting = f" [{issue.setting}]" if issue.setting else ""
        print(f"{prefix}: {issue.code}{setting}: {issue.message}")
        if issue.details:
            details = ", ".join(f"{k}={v}" for k, v in issue.details.items() if v is not None)
            if details:
                print(f"       details: {details}")

    print()
    print(f"Summary: {report.errors} error(s), {report.warnings} warning(s).")

def _combine_allowed_for_display(opt: ConfigOption) -> str:
    parts: List[str] = []
    if opt.valid_literals:
        parts.extend(opt.valid_literals)
    if opt.valid_regex:
        parts.extend(opt.valid_regex)
    return ", ".join(parts)

def emit_markdown_table(schema: Dict[str, ConfigOption]) -> str:
    """
    Generate a Markdown table documenting the schema.
    """
    headers = ["Variable", "Type", "Required", "Default", "Allowed Values", "Notes"]
    lines = ["| " + " | ".join(headers) + " |",
             "| " + " | ".join(["---"] * len(headers)) + " |"]
    for name in sorted(schema.keys()):
        opt = schema[name]
        req = "Yes" if opt.required else "No"
        default = "" if opt.default is None else str(opt.default)
        allowed = _combine_allowed_for_display(opt)
        note = (opt.note or "").replace("|", "\\|")
        line = f"| {name} | {opt.type_name} | {req} | {default} | {allowed} | {note} |"
        lines.append(line)
    return "\n".join(lines)


# ---------------------------
# Scaffolding
# ---------------------------

def _needs_quotes(value: str) -> bool:
    """
    Decide whether to quote a value when writing .env lines.
    Quote if empty or contains whitespace, #, or special shell chars.
    """
    if value == "":
        return True
    if re.search(r"[ \t#:=]", value):
        return True
    return False

def _quote(value: str) -> str:
    if _needs_quotes(value):
        # Escape embedded double quotes
        val = value.replace('"', '\\"')
        return f"\"{val}\""
    return value

def _quote_for_scaffold(
        value: str,
        *,
        type_name: str,
        default_was_quoted: bool,
        preserve_default_quotes: bool,
        quote_strings: bool,
) -> str:
    """
    Decide final quoting for scaffold:
      - if preserve_default_quotes and schema default was quoted -> keep quoted
      - else if quote_strings and type=string -> quote
      - else use heuristic
    """
    if preserve_default_quotes and default_was_quoted:
        val = value.replace('"', '\\"')
        return f"\"{val}\""
    if quote_strings and type_name == "string":
        val = value.replace('"', '\\"')
        return f"\"{val}\""
    return _quote(value)

def generate_scaffold(
        schema: Dict[str, ConfigOption],
        *,
        preserve_default_quotes: bool = True,
        quote_strings: bool = False,
) -> str:
    """
    Create a .env scaffold from the schema.
    Rules:
      - Required variables: include with empty value if no default; add a CLEAR comment.
      - Optional with default: include with default value.
      - Optional without default: include as a commented placeholder.
      - Always include helpful comments showing type, required, allowed, and note.
    """
    lines: List[str] = []
    lines.append("# Generated by env_validator.py --scaffold")
    lines.append("# Fill in required values and review optional ones.\n")

    for name in sorted(schema.keys()):
        opt = schema[name]
        header = f"# {name}\n#   type={opt.type_name}, required={'true' if opt.required else 'false'}"
        if opt.default is not None:
            header += f", default={opt.default}"
        allowed = _combine_allowed_for_display(opt)
        if allowed:
            header += f"\n#   allowed: {allowed}"
        if opt.note:
            header += f"\n#   note: {opt.note}"
        lines.append(header)

        # Decide line value + comment hints
        if opt.required:
            if opt.default is not None:
                value = opt.default
                quoted = _quote_for_scaffold(
                    value,
                    type_name=opt.type_name,
                    default_was_quoted=opt.default_was_quoted,
                    preserve_default_quotes=preserve_default_quotes,
                    quote_strings=quote_strings,
                )
                lines.append(f"{name}={quoted}\n")
            else:
                lines.append(f"# REQUIRED: provide a value\n{name}=\n")
        else:
            if opt.default is not None:
                value = opt.default
                quoted = _quote_for_scaffold(
                    value,
                    type_name=opt.type_name,
                    default_was_quoted=opt.default_was_quoted,
                    preserve_default_quotes=preserve_default_quotes,
                    quote_strings=quote_strings,
                )
                lines.append(f"{name}={quoted}\n")
            else:
                lines.append(f"# (optional)\n# {name}=\n")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------
# CLI
# ---------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a .env file against an env.example schema with structured comments.")
    parser.add_argument("--example", required=True, help="Path to env.example file")

    # Validation / Output
    parser.add_argument("--env", help="Path to .env file (omit when using --emit-markdown/--emit-schema/--scaffold)")
    parser.add_argument("--strict", action="store_true", help="Fail on unknown variables not present in schema")
    parser.add_argument("--format", choices=["text", "json"], default="text", help="Output format for validation report")
    parser.add_argument("--case-insensitive-values", action="store_true", default=True, help="Compare allowed literals case-insensitively (default: on)")
    parser.add_argument("--case-sensitive-values", action="store_true", help="Override: compare allowed literals case-sensitively")
    parser.add_argument("--verbose", action="store_true", help="Include warnings in output (default: only errors)")

    # Docs / Schema
    parser.add_argument("--emit-markdown", action="store_true", help="Emit a Markdown table documenting the schema (no validation)")
    parser.add_argument("--emit-schema", action="store_true", help="Emit the parsed schema as JSON (no validation)")

    # Scaffold
    parser.add_argument("--scaffold", action="store_true", help="Emit a .env scaffold to stdout (or use --out)")
    parser.add_argument("--out", help="Output path for --scaffold; if omitted, prints to stdout")
    parser.add_argument("--force", action="store_true", help="Overwrite existing file with --out")

    # Scaffold quoting controls
    parser.add_argument("--preserve-default-quotes", dest="preserve_default_quotes",
                        action="store_true", default=True,
                        help="Preserve quoting from schema defaults when scaffolding (default: on)")
    parser.add_argument("--no-preserve-default-quotes", dest="preserve_default_quotes",
                        action="store_false",
                        help="Disable preserving schema default quotes when scaffolding")
    parser.add_argument("--quote-strings", action="store_true",
                        help="Always quote values for type=string when scaffolding")

    args = parser.parse_args(argv)

    schema, schema_issues = parse_env_example_schema(args.example)

    # If emitting doc/schema only
    if args.emit_markdown or args.emit_schema:
        if args.emit_markdown:
            # Print parse issues respecting verbosity (errors always shown; warnings only if verbose)
            for iss in schema_issues:
                if iss.level == "error" or args.verbose:
                    prefix = "ERROR" if iss.level == "error" else "WARN"
                    print(f"{prefix}: {iss.message}", file=sys.stderr)
            print(emit_markdown_table(schema))
        else:
            # JSON schema emit; include issues filtered per verbosity
            issues = schema_issues if args.verbose else _filter_issues(schema_issues, verbose=False)
            payload = {
                "schema": {name: opt.to_dict() for name, opt in schema.items()},
                "parse_issues": [i.to_dict() for i in issues],
            }
            print(json.dumps(payload, indent=2))
        return 0

    # Scaffolding path
    if args.scaffold:
        # Respect parse errors
        parse_errors = [i for i in schema_issues if i.level == "error"]
        if parse_errors:
            for i in parse_errors:
                print(f"ERROR: {i.message}", file=sys.stderr)
            return 2
        # Print warnings if verbose
        if args.verbose:
            for i in schema_issues:
                if i.level == "warning":
                    print(f"WARN: {i.message}", file=sys.stderr)

        content = generate_scaffold(
            schema,
            preserve_default_quotes=args.preserve_default_quotes,
            quote_strings=args.quote_strings,
        )
        if args.out:
            if os.path.exists(args.out) and not args.force:
                print(f"ERROR: Output file exists: {args.out}. Use --force to overwrite.", file=sys.stderr)
                return 2
            try:
                with open(args.out, "w", encoding="utf-8") as f:
                    f.write(content)
            except Exception as e:
                print(f"ERROR: Failed to write scaffold to {args.out}: {e}", file=sys.stderr)
                return 2
        else:
            print(content, end="")
        return 0

    # Validation path requires .env
    if not args.env:
        print("ERROR: --env is required when performing validation.", file=sys.stderr)
        return 2

    env, env_issues = load_dotenv(args.env)

    # Aggregate parse issues (non-fatal unless errors)
    parse_issues = schema_issues + env_issues
    parse_errors = [i for i in parse_issues if i.level == "error"]

    if parse_errors:
        # Print parse errors (warnings only if verbose)
        to_print_objs = parse_errors + ([i for i in parse_issues if i.level == "warning"] if args.verbose else [])
        if args.format == "json":
            errors_ct = sum(1 for i in to_print_objs if i.level == "error")
            warn_ct = sum(1 for i in to_print_objs if i.level == "warning")
            report = {
                "errors": errors_ct,
                "warnings": warn_ct,
                "issues": [i.to_dict() for i in to_print_objs],
            }
            print(json.dumps(report, indent=2))
        else:
            for i in to_print_objs:
                prefix = "ERROR" if i.level == "error" else "WARN"
                print(f"{prefix}: {i.message}", file=sys.stderr)
        return 2

    # Validate
    case_insensitive = args.case_insensitive_values and not args.case_sensitive_values
    report = validate_env(
        schema,
        env,
        strict_unknown=args.strict,
        case_insensitive_values=case_insensitive,
    )

    # Include parse warnings into the combined list for printing (but filter by verbosity)
    full_issues = parse_issues + report.issues
    full_report = ValidationReport(
        issues=full_issues,
        errors=sum(1 for i in full_issues if i.level == "error"),
        warnings=sum(1 for i in full_issues if i.level == "warning"),
    )

    if args.format == "json":
        filtered = _filter_issues(full_report.issues, args.verbose)
        payload = {
            "errors": sum(1 for i in filtered if i.level == "error"),
            "warnings": sum(1 for i in filtered if i.level == "warning"),
            "issues": [i.to_dict() for i in filtered],
        }
        print(json.dumps(payload, indent=2))
    else:
        print_human_report(full_report, verbose=args.verbose)

    return 0 if report.errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
