from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_PHP_OPEN_RE = re.compile(r"<\?(?:php|=)?", re.I)
_SUPERGLOBAL_RE = re.compile(r"\$_(GET|POST|REQUEST|COOKIE)\s*\[\s*['\"]([^'\"]+)['\"]\s*\]", re.I)
_FUNCTION_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_INCLUDE_RE = re.compile(r"\b(include|include_once|require|require_once)\b\s*(?:\(?\s*)?([^;\n]+)", re.I)
_LOOSE_COMPARE_RE = re.compile(r"(?<![=!<>])(?:==|!=)(?![=])")
_PREG_PATTERN_RE = re.compile(r"preg_match\s*\(\s*([\"'])(.*?)\1", re.I | re.S)
_ASSIGN_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?);", re.S)
_ECHO_RE = re.compile(r"\becho\s+(.+?);", re.I | re.S)

_DANGEROUS_FUNCTIONS = {
    "assert",
    "eval",
    "exec",
    "file",
    "file_get_contents",
    "fopen",
    "highlight_file",
    "include",
    "include_once",
    "passthru",
    "popen",
    "readfile",
    "require",
    "require_once",
    "shell_exec",
    "show_source",
    "system",
}


@dataclass(frozen=True)
class PHPAnalysis:
    source_count: int = 0
    parameters: list[dict[str, str]] = field(default_factory=list)
    dangerous_functions: list[str] = field(default_factory=list)
    comparisons: list[dict[str, str]] = field(default_factory=list)
    include_points: list[dict[str, str]] = field(default_factory=list)
    blacklist_patterns: list[str] = field(default_factory=list)
    strategies: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def has_findings(self) -> bool:
        return bool(
            self.parameters
            or self.dangerous_functions
            or self.comparisons
            or self.include_points
            or self.blacklist_patterns
            or self.strategies
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_count": self.source_count,
            "parameters": list(self.parameters),
            "dangerous_functions": list(self.dangerous_functions),
            "comparisons": list(self.comparisons),
            "include_points": list(self.include_points),
            "blacklist_patterns": list(self.blacklist_patterns),
            "strategies": list(self.strategies),
            "notes": list(self.notes),
        }

    def to_prompt_summary(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


def extract_php_sources(text: str) -> list[str]:
    """Recover raw PHP from plain source or highlight_file/show_source HTML."""
    candidates: list[str] = []
    for candidate in (text, _html_to_text(text)):
        if _PHP_OPEN_RE.search(candidate):
            cleaned = candidate.replace("\xa0", " ")
            if cleaned not in candidates:
                candidates.append(cleaned)
    return candidates


def summarize_php_observation(text: str) -> dict[str, Any] | None:
    analysis = analyze_php_text(text)
    if not analysis.has_findings:
        return None
    return analysis.to_dict()


def analyze_php_text(text: str) -> PHPAnalysis:
    sources = extract_php_sources(text)
    if not sources and _looks_like_php_fragment(text):
        sources = [text]
    if not sources:
        return PHPAnalysis()

    parameters: list[dict[str, str]] = []
    dangerous: set[str] = set()
    comparisons: list[dict[str, str]] = []
    includes: list[dict[str, str]] = []
    blacklist_patterns: list[str] = []
    notes: list[str] = []

    for source in sources:
        compact = _strip_comments(source)
        parameters.extend(_parameters(compact))
        dangerous.update(_dangerous_functions(compact))
        comparisons.extend(_comparisons(compact))
        includes.extend(_include_points(compact))
        blacklist_patterns.extend(_blacklist_patterns(compact))
        if "parse_str" in compact:
            notes.append("parse_str can create attacker-controlled variables or arrays from query-like input.")
        if "highlight_file" in compact or "show_source" in compact:
            notes.append("highlight_file/show_source source disclosure observed; analyze leaked PHP before more fuzzing.")

    strategies = _strategies(parameters, dangerous, comparisons, includes, blacklist_patterns, "\n".join(sources))
    return PHPAnalysis(
        source_count=len(sources),
        parameters=_dedupe_dicts(parameters),
        dangerous_functions=sorted(dangerous),
        comparisons=_dedupe_dicts(comparisons),
        include_points=_dedupe_dicts(includes),
        blacklist_patterns=_dedupe_strings(blacklist_patterns),
        strategies=strategies,
        notes=_dedupe_strings(notes),
    )


def simple_php_echo_output(path: str | Path) -> str | None:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    assignments = {name: _eval_php_string_expr(expr) for name, expr in _ASSIGN_RE.findall(text)}
    for expression in _ECHO_RE.findall(text):
        rendered = _eval_php_string_expr(expression, assignments)
        if rendered:
            return rendered
    return None


def lfi_replay_candidates(entrypoint: str | Path, root: str | Path | None = None) -> list[dict[str, str]]:
    entry = Path(entrypoint)
    root_path = Path(root) if root is not None else entry.parent
    analysis = analyze_php_text(entry.read_text(encoding="utf-8", errors="replace"))
    if not analysis.include_points:
        return []
    candidates: list[dict[str, str]] = []
    for target in sorted(root_path.glob("*.php")):
        if target.resolve() == entry.resolve():
            continue
        output = simple_php_echo_output(target)
        if not output:
            continue
        resource = target.stem
        candidates.append(
            {
                "target": target.name,
                "payload": f"php://filter/convert.base64-encode/resource={resource}",
                "direct_payload": resource,
                "simulated_output": output,
            }
        )
    return candidates


def _html_to_text(text: str) -> str:
    normalized = _BR_RE.sub("\n", text)
    normalized = normalized.replace("</div>", "\n").replace("</p>", "\n").replace("</span>", "")
    return html.unescape(_TAG_RE.sub("", normalized))


def _looks_like_php_fragment(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in ("$_get[", "$_post[", "parse_str(", "include", "require", "md5(", "in_array("))


def _strip_comments(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"//.*?$|#.*?$", "", source, flags=re.M)


def _parameters(source: str) -> list[dict[str, str]]:
    params = [{"superglobal": match.group(1).upper(), "name": match.group(2)} for match in _SUPERGLOBAL_RE.finditer(source)]
    for match in re.finditer(r"parse_str\s*\((.*?)\)", source, flags=re.I | re.S):
        for nested in _SUPERGLOBAL_RE.finditer(match.group(1)):
            params.append({"superglobal": nested.group(1).upper(), "name": nested.group(2), "via": "parse_str"})
    return params


def _dangerous_functions(source: str) -> list[str]:
    return [name for name in {match.group(1).lower() for match in _FUNCTION_RE.finditer(source)} if name in _DANGEROUS_FUNCTIONS]


def _comparisons(source: str) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for line in source.splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        if _LOOSE_COMPARE_RE.search(stripped):
            kind = "loose-comparison"
            if "md5(" in lowered or "sha1(" in lowered:
                kind = "hash-loose-comparison"
            findings.append({"kind": kind, "expr": _clip(stripped)})
        if "in_array" in lowered and not re.search(r"in_array\s*\([^)]*,\s*true\s*\)", stripped, re.I):
            findings.append({"kind": "in-array-loose", "expr": _clip(stripped)})
        if re.search(r"intval\s*\([^)]*,\s*0\s*\)", stripped, re.I):
            findings.append({"kind": "intval-base-zero", "expr": _clip(stripped)})
        if "strpos" in lowered:
            findings.append({"kind": "strpos-position-check", "expr": _clip(stripped)})
    return findings


def _include_points(source: str) -> list[dict[str, str]]:
    return [{"function": match.group(1).lower(), "expr": _clip(match.group(2).strip(" )"))} for match in _INCLUDE_RE.finditer(source)]


def _blacklist_patterns(source: str) -> list[str]:
    patterns = [_normalize_preg_pattern(match.group(2)) for match in _PREG_PATTERN_RE.finditer(source)]
    for line in source.splitlines():
        if any(marker in line.lower() for marker in ("blacklist", "deny", "forbidden", "str_replace", "strpos", "preg_match")):
            patterns.append(_clip(line.strip()))
    return patterns


def _strategies(parameters, dangerous, comparisons, includes, blacklist_patterns, source):
    names = {item["name"] for item in parameters if item.get("name")}
    params = sorted(names)
    comparison_kinds = {item["kind"] for item in comparisons}
    strategies: list[dict[str, Any]] = []

    def add(name: str, rationale: str, payload_hints: list[str]) -> None:
        strategies.append({"name": name, "rationale": rationale, "payload_hints": payload_hints, "parameters": params})

    if "parse_str" in source:
        add("parse_str-array-injection", "parse_str on user input can turn key[]=x into arrays or overwrite local variables.", [f"{p}[]=x" for p in params] or ["a[]=x&b[]=y"])
    if "hash-loose-comparison" in comparison_kinds:
        add("md5-array-or-magic-hash", "Loose comparison around md5/sha1 can be bypassed with array inputs or 0e-style hashes in PHP CTFs.", ["x[]=1", "240610708", "QNKCDZO", "0e12345"])
    if "loose-comparison" in comparison_kinds:
        add("php-loose-comparison", "Use PHP type juggling inputs against ==/!= gates before assuming the value must match exactly.", ["0", "", "0e12345", "true as JSON", "[] via param[]=x"])
    if "in-array-loose" in comparison_kinds:
        add("in_array-loose", "in_array without strict=true can match coerced numbers and strings.", ["0", "0e12345", "00", "1abc"])
    if "intval-base-zero" in comparison_kinds:
        add("intval-base-0", "intval($x, 0) accepts decimal, octal, and hex notation while later string use may keep the raw payload.", ["0x10", "010", "0/../../flag"])
    if "strpos-position-check" in comparison_kinds:
        add("strpos-offset-bypass", "strpos returns 0 at the beginning and false when absent; loose checks often mishandle both cases.", ["allowed_prefix", "php://filter", "../flag"])
    if includes:
        add("lfi-php-wrapper", "Dynamic include/require can leak PHP source or include local challenge files via wrappers/traversal.", ["php://filter/convert.base64-encode/resource=index", "../flag", "flag", "php://filter/resource=flag"])
    if includes and (comparison_kinds or "parse_str" in source):
        add("type-juggling-plus-lfi-chain", "Satisfy PHP type gates first, then steer the include parameter toward a wrapper or flag resource.", ["token[]=x&page=flag", "a[]=x&b[]=y&page=php://filter/convert.base64-encode/resource=flag"])
    if blacklist_patterns:
        add("blacklist-bypass-review", "Regex/string filters were observed; compare filtered checks with the later sink and try parser mismatch payloads locally.", ["....//", "php://filter", "flag/../flag"])
    if dangerous & {"highlight_file", "show_source"}:
        add("source-first", "Source disclosure was observed; prioritize reading and structuring PHP code before path fuzzing.", ["save highlighted HTML", "decode HTML entities", "analyze conditions"])
    return _dedupe_strategies(strategies)


def _normalize_preg_pattern(pattern: str) -> str:
    if len(pattern) >= 2 and pattern[0] in {"/", "#", "~"}:
        end = pattern.rfind(pattern[0])
        if end > 0:
            return pattern[1:end]
    return pattern


def _eval_php_string_expr(expression: str, variables: dict[str, str | None] | None = None) -> str | None:
    variables = variables or {}
    parts = [part.strip() for part in expression.split(".")]
    rendered = ""
    for part in parts:
        if not part:
            continue
        string_match = re.fullmatch(r"(['\"])(.*?)\1", part, flags=re.S)
        if string_match:
            rendered += bytes(string_match.group(2), "utf-8").decode("unicode_escape")
            continue
        var_match = re.fullmatch(r"\$([A-Za-z_][A-Za-z0-9_]*)", part)
        if var_match and variables.get(var_match.group(1)) is not None:
            rendered += str(variables[var_match.group(1)])
            continue
        return None
    return rendered or None


def _dedupe_dicts(items: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[tuple[str, str], ...]] = set()
    deduped: list[dict[str, str]] = []
    for item in items:
        key = tuple(sorted((str(k), str(v)) for k, v in item.items()))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _dedupe_strings(items: list[str]) -> list[str]:
    deduped: list[str] = []
    for item in items:
        if item and item not in deduped:
            deduped.append(item)
    return deduped


def _dedupe_strategies(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for item in items:
        name = str(item.get("name") or "")
        if not name or name in seen:
            continue
        seen.add(name)
        deduped.append(item)
    return deduped


def _clip(value: str, limit: int = 240) -> str:
    return value if len(value) <= limit else value[: limit - 3] + "..."
