"""Configuration, the service registry, and every input validator.

Everything that reaches a remote command or a remote path is validated here
first. Route handlers never build shell strings or paths themselves.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


class ValidationError(ValueError):
    """Raised when user-supplied input fails an allowlist check."""


# ---------------------------------------------------------------------------
# Service registry
#
# To add a service: add an entry here and restart. Nothing else needs to change.
#   hub_filename     -> the `filename=` value on the installation hub. Only the
#                       filename differs between services; the rest of the URL
#                       is identical for all of them.
#                       LEAVE EMPTY if the hub expects the JAR name itself
#                       (tx-test-mgmt-1.6.0.jar) - that is the default.
#                       SET IT if the hub uses its own identifier for the
#                       artifact instead (e.g. "opsBinaries").
#   jar_prefix       -> Maven artifactId; the JAR is saved as
#                       <jar_prefix>-<version>.jar locally and on the server
#   systemd_service  -> unit file in /etc/systemd/system
#   default_port     -> used for the lsof port-conflict check
#   log_file         -> the application's own log, tailed live alongside
#                       journalctl. These services log to a file rather than
#                       to stdout, so journald only shows systemd events.
#                       Empty -> <REMOTE_WEBDAV_DIR>/<jar_prefix>.log
# ---------------------------------------------------------------------------
SERVICES: dict[str, dict[str, Any]] = {
    "tx-test-mgmt": {
        "display_name": "TX Test Management",
        "hub_filename": "",  # empty -> use the JAR filename
        # Verified with `ls /var/www/webdav` on the server. The log names do not
        # follow the JAR name, so each one is set explicitly.
        "log_file": "/var/www/webdav/txTestMgmt.log",
        "jar_prefix": "tx-test-mgmt",
        "systemd_service": "aiTXTestMgmt.service",
        "default_version": "1.6.0",
        "default_port": 8096,
    },
    "ai-dap-app": {
        "display_name": "AI DAP App",
        "hub_filename": "",  # empty -> use the JAR filename
        "log_file": "/var/www/webdav/aiDAPApp.log",
        "jar_prefix": "ai-dap-app",
        "systemd_service": "aiDAPApp.service",
        "default_version": "1.6.0",
        "default_port": 80,
    },
    "tx-integration-agent": {
        "display_name": "TX Integration Agent",
        "hub_filename": "",  # empty -> use the JAR filename
        "log_file": "/var/www/webdav/tx-integration-agent.log",
        "jar_prefix": "tx-integration-agent",
        "systemd_service": "aiTXIntegrationAgent.service",
        "default_version": "1.6.0",
        "default_port": 9091,
    },
}


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _env_bool(key: str, default: bool) -> bool:
    raw = _env(key)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _env_int(key: str, default: int) -> int:
    raw = _env(key)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


@dataclass
class Settings:
    app_host: str = "127.0.0.1"
    app_port: int = 5002

    dry_run: bool = True
    dry_run_connect: bool = True

    ssh_host: str = "vm-mms-cims02.na.corp.mckesson.com"
    ssh_address: str = "10.15.128.5"
    ssh_port: int = 22
    ssh_username: str = "day6sio"
    ssh_key_path: str = ""
    ssh_key_passphrase: str = ""
    ssh_password: str = ""
    ssh_host_key_policy: str = "strict"
    ssh_known_hosts: str = ""
    ssh_connect_timeout: int = 20
    ssh_command_timeout: int = 120

    use_sudo: bool = True
    sudo_password: str = ""

    installation_hub_url: str = ""
    installation_code: str = ""
    download_timeout: int = 300
    # APP_CHECKSUM is the SHA-256 of the JAR itself. If the pasted value does
    # not match the JAR that arrived, the service fails its startup integrity
    # check - so stop at DOWNLOAD, before anything on the server is touched.
    verify_jar_checksum: bool = True

    remote_binaries_dir: str = "/home/AidenAI/binaries"
    remote_systemd_dir: str = "/etc/systemd/system"
    remote_copydata_dir: str = "/home/day6sio/CopyData"
    remote_webdav_dir: str = "/var/www/webdav"

    backup_layout: str = "nested"
    backup_root: str = "/home/AidenAI/binaries/backups"
    # Aug15 - the existing manual convention for dated folders.
    backup_date_format: str = "%b%d"

    # ── AidenOps ────────────────────────────────────────────────────────────
    # A different application on the same server: one app with two artifacts
    # rather than a service with one JAR, and a database that migrates on every
    # start. Paths are verified against the live box, not assumed.
    aidenops_unit: str = "aidenops.service"
    # /home/AidenAI/ops1, not ops - `ops` is a stale duplicate that is being
    # deleted, and aidenops-api.service / aidenops-ui.service point at it.
    # Restarting either reports success and changes nothing.
    aidenops_ops_dir: str = "/home/AidenAI/ops1"
    aidenops_venv: str = "/home/AidenAI/ops1/venv"
    aidenops_staging_dir: str = "/home/AidenAI/ops1/staging"
    aidenops_backup_root: str = "/home/AidenAI/backups"
    aidenops_web_root: str = "/var/www/aidenops"
    # Where release archives are dropped on the VDI. A folder listing rather
    # than a browser upload: multipart would add python-multipart to this
    # tool's dependencies, on a machine whose PyPI access is exactly the
    # thing we cannot rely on.
    aidenops_incoming_dir: Path = field(default_factory=lambda: BASE_DIR / "incoming")
    aidenops_health_url: str = "http://localhost:8000/health"
    aidenops_ui_url: str = "http://localhost:8080/"
    # The port does not open until Alembic finishes. The unit file's own comment
    # puts a migration plus a dataset restore at up to ten minutes, so a refused
    # connection is the expected early answer, not a failure.
    aidenops_health_timeout: int = 600
    aidenops_health_interval: int = 5
    # One previous dist stays in place for an instant revert; older copies are
    # pruned. Every path the tool writes to gets a retention policy in the same
    # change that starts writing to it - three directories leaked before this
    # rule existed.
    aidenops_keep_previous_dist: int = 1
    aidenops_keep_archives: int = 3
    aidenops_keep_dumps: int = 3

    checksum_pattern: str = r"^[a-fA-F0-9]{64}$"

    log_tail_lines: int = 200
    health_check_delay: int = 8

    temp_dir: Path = field(default_factory=lambda: BASE_DIR / "temp" / "deployments")
    keep_temp_files: bool = False
    audit_log: Path = field(default_factory=lambda: BASE_DIR / "temp" / "deployments.log")
    # A one-line record of the most recent run, so the dashboard can still show
    # it after the app restarts.
    last_deployment_file: Path = field(
        default_factory=lambda: BASE_DIR / "temp" / "last-deployment.json"
    )

    @classmethod
    def from_env(cls) -> "Settings":
        temp_dir = Path(_env("TEMP_DIR", "temp/deployments"))
        audit_log = Path(_env("AUDIT_LOG", "temp/deployments.log"))
        last_file = Path(_env("LAST_DEPLOYMENT_FILE", "temp/last-deployment.json"))
        # Resolved against BASE_DIR like the other paths: the working
        # directory a service is started from is not something to depend on.
        incoming = Path(_env("AIDENOPS_INCOMING_DIR", "incoming"))
        return cls(
            app_host=_env("APP_HOST", "127.0.0.1"),
            app_port=_env_int("APP_PORT", 5002),
            dry_run=_env_bool("DRY_RUN", True),
            dry_run_connect=_env_bool("DRY_RUN_CONNECT", True),
            ssh_host=_env("SSH_HOST", "vm-mms-cims02.na.corp.mckesson.com"),
            ssh_address=_env("SSH_ADDRESS", "10.15.128.5"),
            ssh_port=_env_int("SSH_PORT", 22),
            ssh_username=_env("SSH_USERNAME", "day6sio"),
            ssh_key_path=_env("SSH_KEY_PATH"),
            ssh_key_passphrase=os.getenv("SSH_KEY_PASSPHRASE", ""),
            ssh_password=os.getenv("SSH_PASSWORD", ""),
            ssh_host_key_policy=_env("SSH_HOST_KEY_POLICY", "strict").lower(),
            ssh_known_hosts=_env("SSH_KNOWN_HOSTS"),
            ssh_connect_timeout=_env_int("SSH_CONNECT_TIMEOUT", 20),
            ssh_command_timeout=_env_int("SSH_COMMAND_TIMEOUT", 120),
            use_sudo=_env_bool("USE_SUDO", True),
            sudo_password=os.getenv("SUDO_PASSWORD", ""),
            installation_hub_url=_env("INSTALLATION_HUB_URL"),
            installation_code=_env("INSTALLATION_CODE"),
            download_timeout=_env_int("DOWNLOAD_TIMEOUT", 300),
            verify_jar_checksum=_env_bool("VERIFY_JAR_CHECKSUM", True),
            aidenops_unit=_env("AIDENOPS_UNIT", "aidenops.service"),
            aidenops_ops_dir=_env("AIDENOPS_OPS_DIR", "/home/AidenAI/ops1"),
            aidenops_venv=_env("AIDENOPS_VENV", "/home/AidenAI/ops1/venv"),
            aidenops_staging_dir=_env("AIDENOPS_STAGING_DIR", "/home/AidenAI/ops1/staging"),
            aidenops_backup_root=_env("AIDENOPS_BACKUP_ROOT", "/home/AidenAI/backups"),
            aidenops_web_root=_env("AIDENOPS_WEB_ROOT", "/var/www/aidenops"),
            aidenops_incoming_dir=incoming if incoming.is_absolute() else BASE_DIR / incoming,
            aidenops_health_url=_env("AIDENOPS_HEALTH_URL", "http://localhost:8000/health"),
            aidenops_ui_url=_env("AIDENOPS_UI_URL", "http://localhost:8080/"),
            aidenops_health_timeout=_env_int("AIDENOPS_HEALTH_TIMEOUT", 600),
            aidenops_health_interval=_env_int("AIDENOPS_HEALTH_INTERVAL", 5),
            aidenops_keep_previous_dist=_env_int("AIDENOPS_KEEP_PREVIOUS_DIST", 1),
            aidenops_keep_archives=_env_int("AIDENOPS_KEEP_ARCHIVES", 3),
            aidenops_keep_dumps=_env_int("AIDENOPS_KEEP_DUMPS", 3),
            remote_binaries_dir=_env("REMOTE_BINARIES_DIR", "/home/AidenAI/binaries"),
            remote_systemd_dir=_env("REMOTE_SYSTEMD_DIR", "/etc/systemd/system"),
            remote_copydata_dir=_env("REMOTE_COPYDATA_DIR", "/home/day6sio/CopyData"),
            remote_webdav_dir=_env("REMOTE_WEBDAV_DIR", "/var/www/webdav"),
            backup_layout=_env("BACKUP_LAYOUT", "nested").lower(),
            backup_root=_env("BACKUP_ROOT", "/home/AidenAI/binaries/backups"),
            backup_date_format=_env("BACKUP_DATE_FORMAT", "%b%d"),
            checksum_pattern=_env("CHECKSUM_PATTERN", r"^[a-fA-F0-9]{64}$"),
            log_tail_lines=_env_int("LOG_TAIL_LINES", 200),
            health_check_delay=_env_int("HEALTH_CHECK_DELAY", 8),
            temp_dir=temp_dir if temp_dir.is_absolute() else BASE_DIR / temp_dir,
            keep_temp_files=_env_bool("KEEP_TEMP_FILES", False),
            audit_log=audit_log if audit_log.is_absolute() else BASE_DIR / audit_log,
            last_deployment_file=last_file if last_file.is_absolute() else BASE_DIR / last_file,
        )

    @property
    def ssh_target(self) -> str:
        """Hostname to dial, falling back to the raw IP when DNS is unavailable."""
        return self.ssh_host or self.ssh_address


settings = Settings.from_env()


def reload_settings() -> Settings:
    """Re-read the environment. Used by tests."""
    global settings
    settings = Settings.from_env()
    return settings


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------

_VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+){0,3}(?:-[A-Za-z0-9.]+)?$")
_JAR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.jar$")
_UNIT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]*\.service$")
# A release archive: aidenops-d00222c-635405c.zip. `+` is permitted because
# artifact names inside carry PEP 440 local versions.
_ARCHIVE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*\.zip$")


def list_services() -> list[dict[str, Any]]:
    out = []
    for key, cfg in SERVICES.items():
        out.append(
            {
                "key": key,
                "display_name": cfg["display_name"],
                "hub_filename": cfg.get("hub_filename", ""),
                "jar_prefix": cfg["jar_prefix"],
                "systemd_service": cfg["systemd_service"],
                "default_version": cfg["default_version"],
                "default_port": cfg["default_port"],
                "default_jar": jar_filename(key),
            }
        )
    return out


def get_service(service_key: str) -> dict[str, Any]:
    """Allowlist lookup. An unknown key never reaches the server."""
    if not isinstance(service_key, str) or service_key not in SERVICES:
        raise ValidationError(f"Unknown service: {service_key!r}")
    return SERVICES[service_key]


def validate_version(version: str) -> str:
    version = (version or "").strip()
    if not _VERSION_RE.match(version):
        raise ValidationError(f"Invalid version: {version!r}")
    return version


def jar_filename(service_key: str, version: str | None = None) -> str:
    cfg = get_service(service_key)
    version = validate_version((version or "").strip() or cfg["default_version"])
    return validate_jar_filename(f"{cfg['jar_prefix']}-{version}.jar")


def validate_jar_filename(filename: str) -> str:
    """Reject anything that is not a bare .jar basename."""
    filename = (filename or "").strip()
    if not _JAR_RE.match(filename):
        raise ValidationError(f"Invalid JAR filename: {filename!r}")
    if ".." in filename or "/" in filename or "\\" in filename:
        raise ValidationError(f"Path traversal rejected in filename: {filename!r}")
    if filename != Path(filename).name:
        raise ValidationError(f"Filename must not contain a path: {filename!r}")
    return filename


def hub_filename(service_key: str, version: str | None = None) -> str:
    """The `filename=` value to request from the installation hub.

    Defaults to the JAR name; a service can override it with `hub_filename`
    when the hub uses its own identifier for the artifact instead.
    """
    cfg = get_service(service_key)
    override = (cfg.get("hub_filename") or "").strip()
    return validate_hub_filename(override) if override else jar_filename(service_key, version)


def validate_hub_filename(name: str) -> str:
    """Validate an installation-hub `filename=` value.

    Accepts both a JAR name and a bare identifier. Rejects slashes, traversal
    and anything that would not survive a URL query safely.
    """
    name = (name or "").strip()
    if not name:
        raise ValidationError("Installation-hub filename is empty")
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", name):
        raise ValidationError(f"Invalid installation-hub filename: {name!r}")
    return name


def validate_unit_name(unit: str) -> str:
    """A unit name must be shape-valid *and* belong to a registered service."""
    unit = (unit or "").strip()
    if not _UNIT_RE.match(unit):
        raise ValidationError(f"Invalid systemd unit name: {unit!r}")
    known = {cfg["systemd_service"] for cfg in SERVICES.values()}
    if unit not in known:
        raise ValidationError(f"Unit {unit!r} is not a managed service")
    return unit


def validate_checksum(checksum: str) -> str:
    """Validate the checksum pasted from the Aiden application.

    Quotes, whitespace and newlines are always rejected: the value is written
    into Environment="APP_CHECKSUM=..." and must not be able to break out of it.
    """
    if checksum is None:
        raise ValidationError("Checksum is required")
    raw = checksum.strip()
    if not raw:
        raise ValidationError("Checksum is empty - paste the value copied from the Aiden application")
    if any(ch in raw for ch in ('"', "'", "\n", "\r", "\t", "\\", "$", "`")):
        raise ValidationError("Checksum contains characters that are not allowed")
    if any(ch.isspace() for ch in raw):
        raise ValidationError("Checksum must not contain whitespace")
    try:
        pattern = re.compile(settings.checksum_pattern)
    except re.error as exc:
        raise ValidationError(f"CHECKSUM_PATTERN in .env is not a valid regex: {exc}") from exc
    if not pattern.match(raw):
        raise ValidationError(
            "Checksum format is not valid. Expected a 64-character SHA-256 hex "
            "digest as produced by the Aiden build."
        )
    return raw


def validate_port(port: Any) -> int:
    try:
        value = int(port)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"Invalid port: {port!r}") from exc
    if not 1 <= value <= 65535:
        raise ValidationError(f"Port out of range: {value}")
    return value


def validate_pid(pid: Any) -> int:
    try:
        value = int(pid)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"Invalid PID: {pid!r}") from exc
    if value <= 1:
        # PID 1 is init. Killing it takes the server down.
        raise ValidationError(f"Refusing to operate on PID {value}")
    return value


# ---------------------------------------------------------------------------
# Remote path helpers - the only place remote paths are constructed
# ---------------------------------------------------------------------------


def _safe_component(component: str) -> str:
    component = (component or "").strip()
    if not component or "/" in component or ".." in component:
        raise ValidationError(f"Unsafe path component: {component!r}")
    return component


def remote_path(directory: str, *components: str) -> str:
    base = directory.rstrip("/")
    parts = [_safe_component(c) for c in components]
    return "/".join([base, *parts]) if parts else base


def backup_date_folder(when: date | None = None) -> str:
    """The dated folder name shared by CopyData and the backup directory.

    Defaults to the manual convention: Aug15, not 2026-08-15.
    """
    when = when or datetime.now().date()
    return _safe_component(when.strftime(settings.backup_date_format))


def backup_dir(when: date | None = None) -> str:
    """Dated backup directory for this deployment.

    nested -> <BACKUP_ROOT>/Aug15      (recommended)
    flat   -> <BINARIES_DIR>/Aug15     (existing manual convention)
    """
    folder = backup_date_folder(when)
    if settings.backup_layout == "flat":
        return remote_path(settings.remote_binaries_dir, folder)
    return remote_path(settings.backup_root, folder)


def copydata_dir(when: date | None = None) -> str:
    return remote_path(settings.remote_copydata_dir, backup_date_folder(when))


def binaries_path(filename: str) -> str:
    return remote_path(settings.remote_binaries_dir, validate_jar_filename(filename))


def systemd_path(unit: str) -> str:
    return remote_path(settings.remote_systemd_dir, validate_unit_name(unit))


_LOG_PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]+$")


def service_log_file(service_key: str) -> str:
    """Absolute path of the application's own log file.

    These services write to a file rather than stdout, so journald shows only
    systemd events. Defaults to <REMOTE_WEBDAV_DIR>/<jar_prefix>.log, which is
    the convention the unit files use.
    """
    cfg = get_service(service_key)
    path = (cfg.get("log_file") or "").strip()
    if not path:
        path = f"{settings.remote_webdav_dir.rstrip('/')}/{cfg['jar_prefix']}.log"
    return validate_log_path(path)


def validate_log_path(path: str) -> str:
    path = (path or "").strip()
    if not _LOG_PATH_RE.match(path) or ".." in path:
        raise ValidationError(f"Invalid log file path: {path!r}")
    return path


def validate_release_archive(name: str) -> str:
    """The archive filename, checked before it reaches a remote command line."""
    candidate = (name or "").strip()
    if not candidate or ".." in candidate or not _ARCHIVE_RE.match(candidate):
        raise ValidationError(f"Refusing to use unsafe archive name: {name!r}")
    return candidate
