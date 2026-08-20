"""Live logs for AidenOps.

Three sources, and the nginx error log is the one that matters most: both real
UI failures on this server were diagnosed there and were invisible in the
AidenOps journal, so reading the journal alone is how a broken deployment looks
healthy.
"""

from __future__ import annotations

import asyncio

import pytest

from app.config import validate_unit_name, ValidationError
from app.services.aidenops_logs import AidenOpsLogStreamer
from app.services.log_service import Broadcaster, LogStreamer


class FakeChannel:
    def __init__(self):
        self.closed = False

    def recv_ready(self):
        return False

    def exit_status_ready(self):
        return True

    def recv(self, _size):
        return b""

    def close(self):
        self.closed = True


class FakeSSH:
    """Records what would be followed, and can pretend to be disconnected."""

    def __init__(self, connected=True):
        self.connected = connected
        self.channels: list[list[str]] = []
        self.commands: list[list[str]] = []

    def open_log_channel(self, argv, sudo=True):
        self.channels.append([str(a) for a in argv])
        return FakeChannel()

    async def run(self, argv, *, sudo=False, run_as=None, stdin_data=None,
                  timeout=None, check=True):
        from app.services.ssh_service import CommandResult

        argv = [str(a) for a in argv]
        self.commands.append(argv)
        return CommandResult(" ".join(argv), 0, f"output of {argv[0]}", "")


@pytest.fixture
def streamer(dry_settings):
    ssh = FakeSSH()
    return AidenOpsLogStreamer(ssh, Broadcaster(), dry_settings), ssh


# --- why this class exists at all -----------------------------------------


def test_the_java_validator_rejects_the_aidenops_unit():
    """The reason this is a subclass rather than a wider validator: aidenops is
    not a registered Java service, and widening that check would change the
    behaviour of a deployment path in daily use."""
    assert validate_unit_name("aiTXTestMgmt.service")
    with pytest.raises(ValidationError):
        validate_unit_name("aidenops.service")


def test_it_is_interchangeable_with_the_base_streamer():
    """Same interface, so a caller can hold either."""
    assert issubclass(AidenOpsLogStreamer, LogStreamer)
    for name in ("start", "stop", "recent", "active", "unit"):
        assert hasattr(AidenOpsLogStreamer, name), name


# --- what gets followed ---------------------------------------------------


@pytest.mark.asyncio
async def test_all_three_sources_are_followed(streamer):
    streamer, ssh = streamer
    await streamer.start()

    joined = [" ".join(argv) for argv in ssh.channels]
    assert any("journalctl -u aidenops.service" in c for c in joined)
    assert any("/var/log/nginx/error.log" in c for c in joined)
    assert any("/var/log/nginx/access.log" in c for c in joined)
    assert len(ssh.channels) == 3


@pytest.mark.asyncio
async def test_files_are_followed_with_capital_f(streamer):
    """logrotate moves the inode; -f would stop following at that moment."""
    streamer, ssh = streamer
    await streamer.start()

    for argv in ssh.channels:
        if argv[0] == "tail":
            assert "-F" in argv and "-f" not in argv


@pytest.mark.asyncio
async def test_the_journal_is_followed(streamer):
    streamer, ssh = streamer
    await streamer.start()

    journal = next(a for a in ssh.channels if a[0] == "journalctl")
    assert "-f" in journal
    assert "--no-pager" in journal


@pytest.mark.asyncio
async def test_no_application_log_file_is_followed(streamer):
    """AidenOps logs to stdout only - there is no file log the way the Java
    services have one, so nothing should try to tail one."""
    streamer, ssh = streamer
    await streamer.start()

    for argv in ssh.channels:
        assert "webdav" not in " ".join(argv)


@pytest.mark.asyncio
async def test_arguments_are_ignored_because_the_sources_are_fixed(streamer):
    """The signature matches the base class for interchangeability, but what to
    stream is not a per-call choice here."""
    streamer, ssh = streamer
    await streamer.start("something.service", "/tmp/whatever.log")

    assert not any("something.service" in " ".join(a) for a in ssh.channels)
    assert not any("/tmp/whatever.log" in " ".join(a) for a in ssh.channels)


# --- safety ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unsafe_unit_name_is_refused(dry_settings):
    import dataclasses

    settings = dataclasses.replace(dry_settings, aidenops_unit="evil; rm -rf /")
    broken = AidenOpsLogStreamer(FakeSSH(), Broadcaster(), settings)

    with pytest.raises(ValueError, match="unsafe unit name"):
        await broken.start()


@pytest.mark.asyncio
async def test_an_unsafe_log_path_is_refused(dry_settings):
    import dataclasses

    settings = dataclasses.replace(
        dry_settings, aidenops_nginx_error_log="/var/log/nginx/error.log; id"
    )
    broken = AidenOpsLogStreamer(FakeSSH(), Broadcaster(), settings)

    with pytest.raises(ValueError, match="unsafe log path"):
        await broken.start()


@pytest.mark.asyncio
async def test_a_blank_log_path_is_skipped_not_refused(dry_settings):
    """Turning one source off should not be an error."""
    import dataclasses

    settings = dataclasses.replace(dry_settings, aidenops_nginx_access_log="")
    streamer = AidenOpsLogStreamer(FakeSSH(), Broadcaster(), settings)
    await streamer.start()

    assert len(streamer.ssh.channels) == 2


@pytest.mark.asyncio
async def test_being_disconnected_says_so_rather_than_failing(dry_settings):
    ssh = FakeSSH(connected=False)
    streamer = AidenOpsLogStreamer(ssh, Broadcaster(), dry_settings)
    await streamer.start()

    assert ssh.channels == []
    assert streamer.active is False


@pytest.mark.asyncio
async def test_starting_twice_replaces_the_first_streams(streamer):
    """Otherwise orphaned channels keep following the same files."""
    streamer, ssh = streamer
    await streamer.start()
    await streamer.start()

    # Six opened in total, but only the second set is retained.
    assert len(ssh.channels) == 6
    await streamer.stop()
    assert streamer.active is False


# --- after a failure ------------------------------------------------------


@pytest.mark.asyncio
async def test_recent_reads_the_journal_and_the_error_log(streamer):
    """Reading only the journal is how a broken UI looks healthy."""
    streamer, ssh = streamer
    text = await streamer.recent(lines=50)

    ran = [" ".join(c) for c in ssh.commands]
    assert any("journalctl -u aidenops.service -n 50" in c for c in ran)
    assert any("tail -n 50 /var/log/nginx/error.log" in c for c in ran)
    assert "journalctl" in text and "error.log" in text


@pytest.mark.asyncio
async def test_the_requested_line_count_is_bounded(streamer):
    streamer, ssh = streamer
    await streamer.recent(lines=1)
    assert any("-n 1" in " ".join(c) for c in ssh.commands)


# --- credentials never reach the browser ---------------------------------


@pytest.mark.asyncio
async def test_only_safe_lines_reach_the_browser(dry_settings):
    """Inherited from the base streamer, and worth asserting here too: this
    filter is the only thing between a remote echo and the log pane.

    Run on the live loop so the scheduled publications actually complete -
    otherwise the assertion passes whether or not anything was published.
    """
    import dataclasses

    settings = dataclasses.replace(dry_settings, sudo_password="s3cret-pass")
    streamer = AidenOpsLogStreamer(FakeSSH(), Broadcaster(), settings)

    published: list[dict] = []

    async def capture(event):
        published.append(event)

    streamer.broadcaster.publish = capture
    loop = asyncio.get_running_loop()

    streamer._emit(loop, "journal", "starting up")
    streamer._emit(loop, "journal", "password is s3cret-pass here")
    streamer._emit(loop, "journal", "[sudo] password for day6sio:")
    streamer._emit(loop, "journal", "   ")
    await asyncio.sleep(0.05)

    assert [event["message"] for event in published] == ["starting up"]
