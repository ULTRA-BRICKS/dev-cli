"""Data-driven safety rules for ``dev kill-orphan`` / ``dev clean-logs``.

Accessed via: ``dev_cli.cli``'s ``_cmd_kill_orphan`` / ``_cmd_clean_logs``, which call
``resolve_kill_target`` / ``resolve_clean_log_paths`` and translate a ``RuleError`` into a
``DevError`` for consistent CLI exit codes. This module has no dependency on ``cli.py`` (only
the reverse), so it stays independently testable and stdlib-only except for the lazy ``yaml``
import in ``load_rules``.

Design note (dev-cli#10): callers pass structured fields (``--harness``, ``--worker``,
``--instance``), never a free-text pattern. A family in ``rules.yaml`` supplies the argv
anchor, the id template, and the signal method. This module enforces that contract IN CODE,
not just by convention: ``_validate_kill_family`` runs on every family lookup and rejects a
family definition that would silently degrade into an unanchored substring match (e.g. a
missing ``anchor_flag``, or an ``{instance}`` placeholder ordered before ``{worker}`` in
``id_template``, or a worker-only prefix match with no delimiter guaranteeing where the
worker's digits end).

The SELECTION decision — which enumerated processes actually match a resolved ``KillTarget``,
and whether that set exceeds the configured ``max_matches`` — also lives here
(``KillTarget.select_matches``), not in ``cli.py``. Moving a would-be-unsafe kill decision to
an ungated file is exactly as dangerous as loosening a regex in rules.yaml itself, so the code
that decides which pids die must live in the file the quorum gate actually watches.
``rules.yaml`` and this file are BOTH safety boundaries and BOTH require a passed review-cli
quorum to change (see ``scripts/git-hooks/check_rules_quorum.py``).
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

# Resolves relative to this file's location: <repo>/dev_cli/cleanup_rules.py -> <repo>/
# rules.yaml. This is correct for the supported install path — install.sh symlinks bin/dev
# back into the checked-out repo (see bin/dev's own docstring), so dev_cli/ and rules.yaml
# stay co-located at runtime. It would NOT resolve correctly for a `pip install dev-cli`
# wheel install (rules.yaml is a top-level file, not packaged as package data) — that install
# path is not currently supported (pyproject.toml: "the editable/source checkout is the
# supported install path (mirrors the sibling symlink-install CLIs)"); if wheel installs are
# ever added, this needs importlib.resources instead.
DEFAULT_RULES_PATH = Path(__file__).resolve().parent.parent / "rules.yaml"
_MAX_LOG_PATHS_PER_CALL = 20
_KNOWN_METHODS = frozenset({"sigkill", "sigterm", "sigterm_then_sigkill"})
_TEMPLATE_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")
# A worker/instance VALUE (the actual digits/hex/letters interpolated in) is alphanumeric —
# never '-', '.', or '_', which are the delimiter characters real templates use between
# placeholders (e.g. "hvsc-{worker}-{instance}"). A prefix-mode id_literal must be followed by
# a character OUTSIDE this alnum class, or a shorter worker's digits could be read as the
# start of a longer worker's digits with nothing to stop them (e.g. worker "1" matching a
# process actually running worker "10") — but a literal delimiter like '-' after {worker}
# already fully disambiguates two different numeric/hex strings, so '-'/'.'/'_' must NOT be
# treated as part of the value here (an earlier version of this check wrongly did, and broke
# the shipped "hvsc-{worker}-{instance}" template, which uses exactly this delimiter).
_VALUE_CHAR_RE = re.compile(r"[0-9a-zA-Z]")


class RuleError(Exception):
    """A user-actionable rules.yaml / target-validation failure."""


def load_rules(path: Optional[Path] = None) -> dict:
    if path is None:
        path = DEFAULT_RULES_PATH
    if not path.is_file():
        raise RuleError(
            f"cleanup rules file not found at {path}; dev kill-orphan/clean-logs refuse to "
            "run without an explicit rules.yaml"
        )
    try:
        import yaml
    except ModuleNotFoundError as exc:
        raise RuleError(
            "PyYAML is required to read rules.yaml; install the dev package with its "
            "runtime dependencies"
        ) from exc
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RuleError(f"rules.yaml is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise RuleError("rules.yaml must contain a top-level mapping")
    return data


# ── kill_families ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class KillTarget:
    family: str
    matcher: "re.Pattern[str]"
    enumerate_contains: str
    method: str
    grace_seconds: float
    max_matches: int
    worker: Optional[str]
    instance: Optional[str]

    def matches(self, command: str) -> bool:
        return self.enumerate_contains in command and bool(self.matcher.search(command))

    def select_matches(self, processes: Sequence[Tuple[int, str]]) -> List[Tuple[int, str]]:
        """Return the subset of (pid, command) pairs this target matches.

        Raises RuleError if the match count exceeds max_matches — refusing BEFORE any caller
        can act on the list. This lives here, not in cli.py, so the max_matches safety check
        is itself covered by the rules.yaml/cleanup_rules.py review-quorum gate.
        """
        matched = [(pid, command) for pid, command in processes if self.matches(command)]
        if len(matched) > self.max_matches:
            raise RuleError(
                f"{len(matched)} processes matched family {self.family!r} worker={self.worker} "
                f"instance={self.instance or '<any>'} — exceeds the configured "
                f"max_matches={self.max_matches} in rules.yaml; refusing (this usually means "
                "the match is unexpectedly broad, investigate before retrying)"
            )
        return matched

    def describe(self) -> str:
        return f"family={self.family} worker={self.worker} instance={self.instance or '<any>'}"


def resolve_kill_target(
    rules: dict, family_name: str, worker: Optional[str], instance: Optional[str]
) -> KillTarget:
    family = _find_family(rules, family_name)
    resolved_worker = _validate_field(family, "worker", worker, "--worker")
    resolved_instance = _validate_field(family, "instance", instance, "--instance")
    matcher = _build_id_matcher(family, resolved_worker, resolved_instance)
    return KillTarget(
        family=family_name,
        matcher=matcher,
        enumerate_contains=family["enumerate_argv_contains"],
        method=family.get("method", "sigterm"),
        grace_seconds=float(family.get("grace_seconds", 0)),
        max_matches=int(family.get("max_matches", 1)),
        worker=resolved_worker,
        instance=resolved_instance,
    )


def _find_family(rules: dict, family_name: str) -> dict:
    families = rules.get("kill_families")
    if not isinstance(families, list) or not families:
        raise RuleError("rules.yaml has no kill_families configured")
    seen_names: set = set()
    found: Optional[dict] = None
    for family in families:
        if not isinstance(family, dict):
            continue
        this_name = family.get("name")
        if this_name in seen_names:
            raise RuleError(
                f"rules.yaml has more than one kill_families entry named {this_name!r} — "
                "editing one silently has no effect if the interpreter keeps using the other"
            )
        seen_names.add(this_name)
        if this_name == family_name:
            found = family
    if found is None:
        known = ", ".join(sorted(n for n in seen_names if n))
        raise RuleError(
            f"unknown --harness {family_name!r}; configured families: {known or '<none>'}"
        )
    _validate_kill_family(found)
    return found


def _validate_kill_family(family: dict) -> None:
    """Reject a family definition that could silently degrade into an unsafe match.

    Runs on every lookup (cheap: one family, not the whole file) so a malformed rules.yaml
    fails with a clear RuleError at the point of use instead of a raw traceback deeper in
    resolve_kill_target / _build_id_matcher.
    """
    name = family.get("name")
    if not isinstance(name, str) or not name:
        raise RuleError("every kill_families entry needs a non-empty 'name'")
    if (
        not isinstance(family.get("enumerate_argv_contains"), str)
        or not family["enumerate_argv_contains"]
    ):
        raise RuleError(f"family {name!r} needs a non-empty 'enumerate_argv_contains'")
    if not isinstance(family.get("anchor_flag"), str) or not family["anchor_flag"]:
        raise RuleError(
            f"family {name!r} needs a non-empty 'anchor_flag' — an unanchored family lets "
            "the id literal match anywhere in a process's argv, which is exactly the "
            "unscoped kill this command exists to prevent"
        )
    _validate_kill_family_template(family, name)
    _validate_kill_family_method(family, name)
    for field in ("worker", "instance"):
        if field in family:
            _validate_field_spec(family[field], name, field)


def _validate_kill_family_template(family: dict, name: str) -> None:
    template = family.get("id_template")
    if not isinstance(template, str) or not template:
        raise RuleError(f"family {name!r} needs a non-empty 'id_template'")
    placeholders = set(_TEMPLATE_PLACEHOLDER_RE.findall(template))
    unknown = placeholders - {"worker", "instance"}
    if unknown:
        raise RuleError(
            f"family {name!r} id_template has unsupported placeholder(s): {sorted(unknown)}"
        )
    if "worker" not in placeholders:
        raise RuleError(f"family {name!r} id_template must include a {{worker}} placeholder")
    worker_spec = family.get("worker")
    if not isinstance(worker_spec, dict) or not worker_spec.get("required"):
        raise RuleError(
            f"family {name!r} id_template uses {{worker}} so 'worker: {{required: true}}' "
            "is mandatory (otherwise an omitted --worker interpolates the literal 'None')"
        )
    has_instance_field = "instance" in family
    if ("instance" in placeholders) != has_instance_field:
        raise RuleError(
            f"family {name!r}: id_template's use of {{instance}} and the presence of an "
            "'instance' field must match (both or neither) — otherwise a passed --instance "
            "is silently ignored, or a declared field is unreachable"
        )
    # \w+-only placeholder detection (above) misses str.format syntax that isn't a bare
    # {name} — an empty {}, a positional {0}, or a format-spec like {:.2f} would slip past a
    # regex that only recognizes \{(\w+)\}. A trial format (with throwaway values) exercises
    # the SAME parser resolve_kill_target uses, so any such form raises RuleError here instead
    # of a raw IndexError/KeyError/ValueError deep in _build_id_matcher at kill time. Runs
    # LAST, after the placeholder-name checks above, so a template with both an unknown named
    # placeholder AND format-spec noise gets the more specific "unsupported placeholder"
    # message rather than a generic format error.
    try:
        template.format(worker="0", instance="00000000")
        template.split("{instance}")[0].format(worker="0")
    except (IndexError, KeyError, ValueError) as exc:
        raise RuleError(f"family {name!r} id_template is not a valid template: {exc}") from exc
    if "instance" in placeholders:
        _validate_worker_before_instance(template, name)
    _validate_prefix_has_boundary_after_worker(family, template, name)


def _validate_worker_before_instance(template: str, name: str) -> None:
    """{worker} must appear before {instance} in the template.

    Prefix mode (an omitted --instance) truncates the template at the FIRST {instance}
    occurrence and formats only what remains — if {worker} came AFTER {instance} in the
    template, the prefix segment would never contain the worker at all, and the resulting
    matcher would ignore --worker entirely (an unscoped, anchor-only match).
    """
    if template.index("{worker}") > template.index("{instance}"):
        raise RuleError(
            f"family {name!r} id_template must place {{worker}} before {{instance}} — "
            "the reverse order makes a worker-only (--instance omitted) match ignore the "
            "worker entirely"
        )


def _validate_prefix_has_boundary_after_worker(family: dict, template: str, name: str) -> None:
    """The prefix-mode (worker-only) id_literal must end at a real delimiter.

    Without this, a shorter worker's prefix can match a longer worker's id (worker "1"
    matching a process actually running worker "10") because nothing stops the interpolated
    worker digits from being read as the START of a longer number. Requires: the character in
    the template immediately following the {worker} placeholder (before the next placeholder
    or end of the prefix segment) exists and is OUTSIDE the value-character class
    (_VALUE_CHAR_RE, [0-9a-zA-Z]) — i.e. a genuine separator like '-', '.', '_', or '/'. This
    check is deliberately looser than the FULL-instance boundary used in _build_id_matcher
    ((?=[^0-9a-zA-Z_.-]|$)), which also treats '.', '_', '-' as non-boundary: full-instance
    mode matches a complete, already-delimited id, so it can afford the stricter class; prefix
    mode only needs ANY genuine separator to stop prefix bleed between two worker VALUES,
    which are always plain alphanumeric (see the module-level _VALUE_CHAR_RE comment for why
    '.'/'_'/'-' must NOT be in a value's own character class).
    """
    prefix = template.split("{instance}")[0] if "{instance}" in template else template
    after_worker = prefix.split("{worker}", 1)[1]
    if not after_worker:
        raise RuleError(
            f"family {name!r} id_template must have a delimiter character immediately after "
            "{worker} (e.g. 'x-{worker}-...', not 'x-{worker}' with nothing following) — "
            "otherwise a worker-only match can't tell a short worker id from the prefix of a "
            "longer one"
        )
    first_char = after_worker[0]
    if _VALUE_CHAR_RE.match(first_char):
        raise RuleError(
            f"family {name!r} id_template's character immediately after {{worker}} ({first_char!r}) "
            "is not a real delimiter — it must be outside [0-9a-zA-Z] so a worker-only match "
            "can't be confused with a longer worker id's prefix"
        )


def _validate_kill_family_method(family: dict, name: str) -> None:
    method = family.get("method", "sigterm")
    if method not in _KNOWN_METHODS:
        raise RuleError(f"family {name!r} has unknown method {method!r}")
    grace_raw = family.get("grace_seconds", 0)
    if isinstance(grace_raw, bool) or not isinstance(grace_raw, (int, float)):
        raise RuleError(f"family {name!r} grace_seconds must be a number, got {grace_raw!r}")
    grace = float(grace_raw)
    if not math.isfinite(grace):
        raise RuleError(f"family {name!r} grace_seconds must be finite, got {grace_raw!r}")
    if grace < 0:
        raise RuleError(f"family {name!r} grace_seconds must not be negative")
    if method == "sigterm_then_sigkill" and grace <= 0:
        raise RuleError(
            f"family {name!r} method sigterm_then_sigkill needs grace_seconds > 0 (0 "
            "silently degrades to a bare SIGTERM with no escalation)"
        )
    max_matches_raw = family.get("max_matches", 1)
    if isinstance(max_matches_raw, bool) or not isinstance(max_matches_raw, int):
        raise RuleError(f"family {name!r} max_matches must be an integer, got {max_matches_raw!r}")
    if max_matches_raw < 1:
        raise RuleError(f"family {name!r} max_matches must be >= 1")


def _validate_field_spec(spec: object, name: str, field: str) -> None:
    if not isinstance(spec, dict):
        raise RuleError(f"family {name!r} field {field!r} must be a mapping")
    pattern = spec.get("pattern")
    if pattern is None:
        return
    if not isinstance(pattern, str):
        raise RuleError(f"family {name!r} field {field!r} pattern must be a string")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise RuleError(
            f"family {name!r} field {field!r} pattern is not a valid regex: {exc}"
        ) from exc


def _validate_field(family: dict, field: str, value: Optional[str], flag: str) -> Optional[str]:
    if field not in family:
        if value is not None:
            raise RuleError(
                f"family {family['name']!r} does not accept {flag} (no {field!r} rule declared)"
            )
        return None
    spec = family[field]  # already shape-validated by _validate_field_spec
    if value is None:
        if spec.get("required"):
            raise RuleError(f"--harness {family['name']!r} requires {flag}")
        return None
    pattern = spec.get("pattern")
    if pattern and not re.fullmatch(pattern, value):
        raise RuleError(
            f"{flag} {value!r} does not match the required pattern {pattern!r} "
            f"for family {family['name']!r}"
        )
    return value


def _build_id_matcher(
    family: dict, worker: Optional[str], instance: Optional[str]
) -> "re.Pattern[str]":
    template = family["id_template"]
    if instance is not None:
        id_literal = template.format(worker=worker, instance=instance)
        boundary = r"(?=[^0-9a-zA-Z_.-]|$)"
    else:
        prefix_template = template.split("{instance}")[0]
        id_literal = prefix_template.format(worker=worker)
        boundary = ""
    anchor = family["anchor_flag"]  # validated non-empty by _validate_kill_family
    # (?:^|(?<=\s)) requires the anchor to start a fresh argv token (line start or preceded by
    # whitespace) — without it, the anchor string can appear as a SUBSTRING inside a different
    # argument's value (e.g. --note=--user-data-dir=/tmp/hvsc-2-x), matching a process that has
    # no such flag at all.
    pattern = (
        r"(?:^|(?<=\s))" + re.escape(anchor) + r"(?:[^\s]*/)?" + re.escape(id_literal) + boundary
    )
    return re.compile(pattern)


# ── clean_log_rules ──────────────────────────────────────────────────────────────────────────

# Fixed, code-level allow-list of safe temp roots. Deliberately NOT env-derived (e.g. no
# ${TMPDIR} expansion): the same process invoking `dev clean-logs` controls its own
# environment, so an env-derived root is not actually a boundary — it collapses to whatever
# TMPDIR happens to be set to at call time (including "", which resolves to the cwd, or "/",
# which resolves to the filesystem root). rules.yaml's `allowed_roots` entries are matched
# against this fixed set; a raw_root that isn't one of these is rejected outright rather than
# silently expanded. macOS's actual per-user temp dir (what Python's tempfile module and many
# TMPDIR-aware tools actually use) is /var/folders/..., not /tmp — both are covered.
_SAFE_TEMP_ROOTS = (
    Path("/tmp"),
    Path("/private/tmp"),
    Path("/var/folders"),
    Path("/private/var/folders"),
)


def resolve_clean_log_paths(rules: dict, raw_paths: Sequence[str]) -> List[Path]:
    log_rules = rules.get("clean_log_rules")
    if not isinstance(log_rules, list) or not log_rules:
        raise RuleError("rules.yaml has no clean_log_rules configured")
    for rule in log_rules:
        _validate_clean_log_rule(rule)
    if len(raw_paths) > _MAX_LOG_PATHS_PER_CALL:
        raise RuleError(
            f"{len(raw_paths)} paths in one call exceeds the {_MAX_LOG_PATHS_PER_CALL} sanity "
            "cap; split into multiple dev clean-logs calls"
        )
    return [_resolve_one_log_path(log_rules, raw) for raw in raw_paths]


def _validate_clean_log_rule(rule: object) -> None:
    if not isinstance(rule, dict):
        raise RuleError("each clean_log_rules entry must be a mapping")
    label = rule.get("name", "?")
    roots = rule.get("allowed_roots")
    if not isinstance(roots, list) or not roots or not all(isinstance(r, str) and r for r in roots):
        raise RuleError(
            f"clean_log_rules entry {label!r} needs a non-empty list of string allowed_roots"
        )
    suffixes = rule.get("allowed_suffixes")
    if (
        not isinstance(suffixes, list)
        or not suffixes
        or not all(isinstance(s, str) and s for s in suffixes)
    ):
        raise RuleError(
            f"clean_log_rules entry {label!r} needs a non-empty list of string allowed_suffixes"
        )


def _resolve_one_log_path(log_rules: list, raw: str) -> Path:
    if any(ch in raw for ch in "*?[]"):
        raise RuleError(f"{raw!r}: glob patterns are not supported; pass explicit file paths")
    candidate = Path(os.path.expanduser(raw))
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    resolved = candidate.resolve(strict=False)
    if resolved.exists() and not resolved.is_file():
        raise RuleError(f"{raw!r} (resolved: {resolved}) is not a regular file")
    for rule in log_rules:
        if _log_path_matches_rule(resolved, rule):
            return resolved
    raise RuleError(
        f"{raw!r} (resolved: {resolved}) does not match any clean_log_rules allowed "
        "root+suffix in rules.yaml"
    )


def _log_path_matches_rule(resolved: Path, rule: dict) -> bool:
    if resolved.suffix not in rule["allowed_suffixes"]:
        return False
    for raw_root in rule["allowed_roots"]:
        root = _resolve_safe_root(raw_root)
        if root is None or resolved == root:
            continue  # unrecognized root, or the bare root itself is never a valid file target
        if resolved.is_relative_to(root):
            return True
    return False


def _resolve_safe_root(raw_root: str) -> Optional[Path]:
    """Resolve raw_root only if it IS (or resolves onto) one of _SAFE_TEMP_ROOTS.

    Returns None for anything else, including a syntactically-plausible but disallowed path —
    the caller treats None as "this allowed_roots entry matches nothing", not an error, so one
    bad entry in rules.yaml doesn't block every other valid rule.
    """
    candidate = Path(raw_root).resolve(strict=False)
    for safe_root in _SAFE_TEMP_ROOTS:
        if candidate == safe_root or candidate.is_relative_to(safe_root):
            return candidate
    return None
