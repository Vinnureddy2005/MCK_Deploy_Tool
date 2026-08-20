"""Comparing a release's pinned dependencies against what is installed.

This is not a safeguard around pip - it is the dependency mechanism. The wheel
declares zero dependencies (verified: `Requires-Dist` count is 0, and
pyproject.toml says so deliberately - "pinned in requirements.txt, intentionally
NOT duplicated here"). So nothing ever installs a dependency on its own, and
nothing ever upgrades one.

Which means both kinds of change are equally invisible and equally fatal:

    a new package        never arrives        -> ImportError at start
    a moved pin          never upgrades       -> wrong version, subtle failures

Names are normalised per PEP 503 before comparing, so `aidenops_service` and
`aidenops-service` are one package rather than two.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

_NORMALISE = re.compile(r"[-_.]+")
# `pip freeze` emits name==version; requirements.txt here is pinned the same way.
_PINNED = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;#]+)")


def normalise(name: str) -> str:
    """PEP 503 canonical form."""
    return _NORMALISE.sub("-", name.strip()).lower()


def parse(text: str) -> dict[str, str]:
    """Package -> pinned version, from a requirements file or `pip freeze`.

    Anything that is not an exact pin is skipped: editable installs, VCS URLs,
    index options and bare names carry no version to compare, and guessing at
    one would invent a change that is not there.
    """
    pins: dict[str, str] = {}
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = _PINNED.match(line)
        if match:
            pins[normalise(match.group(1))] = match.group(2).strip()
    return pins


def diff(installed_text: str, required_text: str) -> list[dict]:
    """What would have to change for the release's pins to be satisfied.

    Ordered added, then repinned, then removed - the order of how much attention
    each deserves. A removal is informational: nothing uninstalls it, so it stays
    on disk harmlessly.
    """
    installed = parse(installed_text)
    required = parse(required_text)

    changes: list[dict] = []
    for name in sorted(set(required) - set(installed)):
        changes.append({"package": name, "change": "added", "to": required[name]})
    for name in sorted(set(installed) & set(required)):
        if installed[name] != required[name]:
            changes.append(
                {
                    "package": name,
                    "change": "repinned",
                    "from": installed[name],
                    "to": required[name],
                }
            )
    for name in sorted(set(installed) - set(required)):
        changes.append({"package": name, "change": "removed", "from": installed[name]})
    return changes


def summarise(changes: list[dict]) -> dict:
    """Whether this release needs pip to reach an index at all.

    Most releases move no pins. Recognising that is what keeps a locked-down
    server deployable: with no changes there is nothing to install, so PyPI
    access is not required for the deployment to succeed.
    """
    needed = [c for c in changes if c["change"] in ("added", "repinned")]
    return {
        "changes": changes,
        # Only added and repinned require an install; a removal needs nothing.
        "needs_install": bool(needed),
        "needs_index": bool(needed),
        "needs_confirmation": bool(needed),
        # Exactly the specifiers to probe when pip is too old for --dry-run.
        # Probing one sample package would prove PyPI is up, not that this
        # release resolves.
        "probe": [f"{c['package']}=={c['to']}" for c in needed],
    }
