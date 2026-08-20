"""Placeholder detection in the server's config.yaml.

The regression tests here matter more than the feature tests. Two earlier
versions of this check searched raw text and both flagged the file's own
scaffolding, making the gate unpassable - and an unpassable gate gets switched
off, at which point it catches nothing.
"""

from __future__ import annotations

import pytest

from app.services.config_validator import ConfigError, find_placeholders, validate

# Lifted from the live, healthy config on vm-mms-cims02. Every line here is
# present on a correctly configured server, and none of it may fire.
HEALTHY = """
# ── Database ─────────────────────────────────────────────────── «REPLACE» ────
database:
  url: "postgresql+asyncpg://aidap:realpassword@localhost/aidenops"
  password: "realpassword"

auth:
  jwt_secret: "a-real-generated-secret-value"

# ── Telemetry (all optional; blank means not configured) ──────────────────────
telemetry:
  prometheus_url: ""          # e.g. http://10.20.30.43:9090
  grafana_url: ""             # e.g. http://10.20.30.43:3000
  loki_url: ""                # e.g. http://10.20.30.43:3100

edi:
  axway_base_url: ""          # e.g. http://10.20.30.44:8080
  sterling_base_url: ""       # e.g. http://10.20.30.45:9080
  erp_base_url: ""            # e.g. http://10.20.30.46:8000

ticketing:
  provider: "none"

servicenow:
  password: "ChangeMe_SnowPass"
  webhook_secret: "ChangeMe_SnowHook"

email:
  mailboxes:
    - host: "imap.example.com"
      password: "ChangeMe_ImapPass"

qdrant:
  backend: "database"
"""


# --- the regression that matters ------------------------------------------


def test_the_healthy_config_produces_no_blocking_findings():
    """If this ever fails, the gate is unpassable and will be switched off."""
    result = validate(HEALTHY)
    assert result["stop"] == []
    assert result["ok"] is True


def test_the_replace_banner_is_not_a_finding():
    """«REPLACE» is a permanent section marker, not a placeholder. It survived
    into the live config, and a text search flagged it forever."""
    assert not any("REPLACE" in path for path in find_placeholders({"a": "b"}))
    # And in the real file it is a comment, so it is not in the tree at all.
    assert validate(HEALTHY)["stop"] == []
    assert validate(HEALTHY)["warn"] == [
        "servicenow.password",
        "servicenow.webhook_secret",
        "email.mailboxes[0].password",
    ]


def test_example_urls_in_comments_are_not_findings():
    """Six lines on this server carry `# e.g. http://10.20.30.4x` next to a
    correctly blank value. yaml.safe_load drops comments, so they are
    unreachable - that is what makes this categorically fixed."""
    result = validate(HEALTHY)
    assert not any("prometheus" in p or "grafana" in p or "loki" in p
                   for p in result["stop"] + result["warn"])
    assert not any("axway" in p or "sterling" in p or "erp" in p
                   for p in result["stop"] + result["warn"])


def test_a_blank_value_is_never_a_placeholder():
    """The file's own contract: "a blank URL means 'not configured'"."""
    assert find_placeholders({"telemetry": {"prometheus_url": ""}}) == []


# --- what does get caught -------------------------------------------------


def test_a_dummy_ip_as_an_actual_value_is_caught():
    """llm.base_url and neo4j.uri really did sit on this range for two days."""
    found = find_placeholders(
        {"llm": {"base_url": "http://10.20.30.40:8000"}, "neo4j": {"uri": "bolt://10.20.30.41:7687"}}
    )
    assert sorted(found) == ["llm.base_url", "neo4j.uri"]


@pytest.mark.parametrize(
    "value", ["ChangeMe", "changeme", "CHANGE_ME", "ChangeMe_SnowPass", "xx-changeme-xx"]
)
def test_placeholder_spellings_are_caught(value):
    assert find_placeholders({"database": {"password": value}}) == ["database.password"]


def test_placeholders_inside_lists_are_found():
    """Mailboxes and endpoints are lists, so a placeholder can hide in one."""
    found = find_placeholders(
        {"email": {"mailboxes": [{"password": "ok"}, {"password": "ChangeMe_ImapPass"}]}}
    )
    assert found == ["email.mailboxes[1].password"]


# --- severity -------------------------------------------------------------


def test_a_database_placeholder_blocks():
    """Never inert: nothing starts without a database."""
    result = validate('database:\n  password: "ChangeMe"\n')
    assert result["stop"] == ["database.password"]
    assert result["ok"] is False


def test_a_jwt_secret_placeholder_blocks():
    result = validate('auth:\n  jwt_secret: "ChangeMe"\n')
    assert result["stop"] == ["auth.jwt_secret"]


def test_an_optional_integration_placeholder_only_warns():
    """Most client deployments never wire up ServiceNow. Blocking on an inert
    credential would nag on the majority of real deployments, and a gate that
    nags gets disabled - taking the database check down with it."""
    result = validate(HEALTHY)
    assert "servicenow.password" in result["warn"]
    assert result["stop"] == []
    assert result["ok"] is True


def test_warnings_are_still_reported():
    """Not blocking is not the same as hidden."""
    assert len(validate(HEALTHY)["warn"]) == 3


# --- parse failures -------------------------------------------------------


def test_unparseable_yaml_is_an_error():
    with pytest.raises(ConfigError, match="does not parse"):
        validate("database:\n  url: \"unclosed\n  bad: [")


def test_a_non_mapping_is_an_error():
    """AIDENOPS_REQUIRE_CONFIG=1 means the service refuses to start on this."""
    with pytest.raises(ConfigError, match="mapping"):
        validate("- just\n- a\n- list\n")


def test_values_are_never_returned():
    """This file holds the database password, the JWT secret and the Neo4j
    password. A validator that echoed findings would leak them into a log."""
    result = validate('database:\n  password: "ChangeMe_SuperSecret"\n')
    assert result["stop"] == ["database.password"]
    assert "SuperSecret" not in repr(result)
