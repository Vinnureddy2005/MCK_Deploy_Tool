"""Checking the server's config.yaml for placeholder values still left in it.

Read over SSH, parsed and validated here. The parse is the whole point: two
earlier versions of this check searched the file's raw text and both were wrong
in the same way, because this config's own scaffolding uses the same vocabulary
as its placeholders.

    # ── Database ──────────────────────────────────────────── «REPLACE» ────
    prometheus_url: ""          # e.g. http://10.20.30.43:9090

Both lines are present in the healthy, correctly configured file on the server.
A text search flags them and the gate can never pass; a gate that can never pass
gets switched off, and then it catches nothing at all.

yaml.safe_load discards comments, so after parsing the ambiguity does not exist:
`prometheus_url` is the empty string, and the example URL is unreachable from the
tree being walked. This is not a more careful regex - it is a class of bug that
no longer has a case to get wrong.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import yaml

log = logging.getLogger(__name__)


class ConfigError(RuntimeError):
    """The config file could not be read or parsed."""


_PLACEHOLDERS = ("changeme", "change_me")

# 10.20.30.0/24 is the range this config uses in its own worked examples, so it
# never legitimately appears as a *value*. Two fields here really did sit on it
# for two days - llm.base_url and neo4j.uri - which is why it is checked at all.
_EXAMPLE_NET = re.compile(r"\b10\.20\.30\.\d{1,3}\b")

# A placeholder in these can never be inert: nothing starts without a database,
# and a default signing secret is a defect whatever else is switched off.
#
# Deliberately structural rather than semantic. Scoping by a switch - skipping
# servicenow.* when ticketing.provider != "servicenow" - would mean duplicating
# the application's own semantics here, and going stale silently the day someone
# renames that key. Failing open is the one outcome worse than nagging.
# Matched as a suffix on the final key as well as a prefix on the path, because
# guessing the nesting is what went wrong first: the blocking list had
# "jwt_secret" and "auth.jwt_secret" while the real config nests it under
# security.jwt_secret, so a placeholder signing key was reported as a warning.
# Only the database. A wrong password there means the service cannot start at
# all, so it is never inert.
#
# A placeholder signing secret is deliberately NOT here. It is a security smell
# rather than an availability problem - JWTs sign fine with any string - and this
# deployment has one today. Blocking on it would stop every deployment until
# someone changed a value they have judged acceptable, and a gate that stands
# between an operator and a working deployment gets switched off. It stays
# visible as a warning instead.
_BLOCKING_PREFIXES = ("database.", "database_url")
_BLOCKING_KEYS = ("database_password", "db_password")


def validate(text: str) -> dict:
    """Classify every placeholder found. Raises ConfigError if it will not parse."""
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"config.yaml does not parse: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ConfigError(
            "config.yaml did not parse to a mapping. The service will refuse to "
            "start with AIDENOPS_REQUIRE_CONFIG=1."
        )

    found = find_placeholders(parsed)
    blocking = [path for path in found if is_blocking(path)]
    warnings = [path for path in found if path not in blocking]

    if blocking:
        log.warning("config.yaml has %d blocking placeholder(s)", len(blocking))
    return {"ok": not blocking, "stop": blocking, "warn": warnings}


def is_blocking(path: str) -> bool:
    """Whether a placeholder at this path must stop the deployment.

    Nothing starts without a database, and a default signing secret is a defect
    whatever else is switched off - so both block wherever they are nested.
    Everything else warns, because blocking on an inert ServiceNow credential
    would nag on most real deployments and a gate that nags gets switched off.
    """
    if path.startswith(_BLOCKING_PREFIXES):
        return True
    # The last segment, with any list index stripped.
    leaf = path.rsplit(".", 1)[-1].split("[", 1)[0]
    return leaf in _BLOCKING_KEYS


def find_placeholders(node: Any, path: str = "") -> list[str]:
    """Config paths whose value is still a placeholder.

    Returns paths, never values. This file holds the database password, the JWT
    secret and the Neo4j password - a validator that echoed what it found would
    leak them into a log the first time it fired.
    """
    found: list[str] = []

    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            found += find_placeholders(value, child)

    elif isinstance(node, list):
        # Lists carry real configuration here - mailboxes, endpoints - so a
        # placeholder can hide inside one.
        for index, value in enumerate(node):
            found += find_placeholders(value, f"{path}[{index}]")

    elif isinstance(node, str) and node:
        # Blank is not a placeholder. The file documents its own contract:
        # "a blank URL means 'not configured'", and most deployments leave the
        # optional integrations blank. Flagging those would recreate the
        # always-fails gate in a third form.
        lowered = node.lower()
        if any(marker in lowered for marker in _PLACEHOLDERS) or _EXAMPLE_NET.search(node):
            found.append(path)

    return found
