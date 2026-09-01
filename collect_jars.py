#!/usr/bin/env python
"""Pull every backup JAR off the app server into a mirrored local tree, then
optionally delete the server copies.

Not part of the deployment tool. It borrows the tool's .env for credentials and
its SSH layer for sudo handling, and touches none of its code.

    python collect_jars.py                      # find + copy + verify (deletes nothing)
    python collect_jars.py --delete --yes       # copy, verify, then delete what verified

The local tree mirrors the server path for path:

    /home/AidenAI/binaries/Aug14/tx-test-mgmt-1.6.0.jar
    ->  <dest>/home/AidenAI/binaries/Aug14/tx-test-mgmt-1.6.0.jar

That matters more than it looks. Twenty of these files are called
tx-integration-agent-1.6.0.jar; flattened into one folder, all but one would be
overwritten - and then the originals deleted. The mirror keeps the folder each
came from, which is the only thing that says which release it was.

The three JARs under /tmp/aidap-jmeter/lib are out of scope: JMeter's own
libraries, neither copied nor deleted.

Nothing is deleted unless its local copy exists and its SHA-256 matches the
server's. Verify, then delete, never the other way round.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import posixpath
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import settings                      # noqa: E402
from app.services.ssh_service import SSHService      # noqa: E402

# Where to look. Everything the tool itself writes lives under the first two.
ROOTS = [
    "/home/AidenAI/binaries",
    "/home/day6sio/CopyData",
    "/home/ssctt1i/Jars",
]

# Never collected and never deleted.
#
# The live JARs sit directly in /home/AidenAI/binaries - that is where the
# deployment tool copies the new one and where systemd runs it from. Only the
# dated subfolders below it are backups. A depth check is what separates them,
# so it is enforced here rather than trusted to a hand-written path list.
LIVE_DIR = settings.remote_binaries_dir.rstrip("/")

# Out of scope entirely: the three JARs under /tmp/aidap-jmeter/lib are
# third-party libraries belonging to a JMeter install - jackson, tika - not
# deployment artifacts. /tmp is not in ROOTS, and this refuses them even if a
# root is passed on the command line.
EXCLUDED_PREFIXES = ["/tmp/"]


@dataclass
class Jar:
    remote: str
    size: int = 0
    remote_hash: str = ""
    local: Path | None = None
    local_hash: str = ""
    status: str = "found"
    note: str = ""

    @property
    def verified(self) -> bool:
        return bool(self.remote_hash) and self.remote_hash == self.local_hash


@dataclass
class Run:
    jars: list[Jar] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)


def local_path(dest: Path, remote: str) -> Path:
    """/home/AidenAI/binaries/Aug14/x.jar -> <dest>/home/AidenAI/binaries/Aug14/x.jar

    The leading slash is all that is dropped, so `home` and `tmp` are the top
    two folders and the rest reads exactly as it does on the server.
    """
    return dest.joinpath(*remote.lstrip("/").split("/"))


def is_live(remote: str) -> bool:
    """A JAR sitting directly in the binaries directory is the running one."""
    return posixpath.dirname(remote.rstrip("/")) == LIVE_DIR


def excluded(remote: str) -> str | None:
    """Not collected and not deleted."""
    if is_live(remote):
        return "live JAR - this is what systemd runs"
    for prefix in EXCLUDED_PREFIXES:
        if remote.startswith(prefix):
            return f"under {prefix} - JMeter libraries, not deployment artifacts"
    return None


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


async def discover(ssh: SSHService, roots: list[str]) -> Run:
    """Ask the server what is there rather than trusting a pasted list."""
    run = Run()
    seen: set[str] = set()

    for root in roots:
        exists = await ssh.run(["test", "-d", root], sudo=True, check=False)
        if exists.exit_code != 0:
            run.skipped.append((root, "no such directory"))
            continue

        found = await ssh.run(
            ["find", root, "-type", "f", "-name", "*.jar"], sudo=True, check=False
        )
        for line in (found.stdout or "").splitlines():
            remote = line.strip()
            # `find` walks each path once, but the roots may overlap and the
            # same file must not be copied - or counted - twice.
            if not remote.endswith(".jar") or remote in seen:
                continue
            seen.add(remote)

            why = excluded(remote)
            if why:
                run.skipped.append((remote, why))
                continue
            run.jars.append(Jar(remote=remote))

    return run


async def measure(ssh: SSHService, jars: list[Jar]) -> None:
    """Size and hash everything on the server, in batches."""
    for start in range(0, len(jars), 40):
        batch = jars[start:start + 40]
        paths = [jar.remote for jar in batch]

        sizes = await ssh.run(["stat", "-c", "%s %n", *paths], sudo=True, check=False)
        by_path = {}
        for line in (sizes.stdout or "").splitlines():
            size, _, name = line.strip().partition(" ")
            if size.isdigit():
                by_path[name] = int(size)

        hashes = await ssh.run(["sha256sum", *paths], sudo=True, check=False)
        digests = {}
        for line in (hashes.stdout or "").splitlines():
            digest, _, name = line.strip().partition("  ")
            if len(digest) == 64:
                digests[name] = digest

        for jar in batch:
            jar.size = by_path.get(jar.remote, 0)
            jar.remote_hash = digests.get(jar.remote, "")
            if not jar.remote_hash:
                jar.status = "unreadable"
                jar.note = "could not hash it on the server"


_staging: str | None = None


async def staging_dir(ssh: SSHService) -> str:
    """A directory this account owns, for the two-hop copy.

    Made *without* sudo, deliberately. `sudo mkdir` produces a root-owned
    directory, and sudo on this server runs with a restrictive umask - it comes
    out 0750 root:root, which this account cannot traverse. The staged file is
    then unreadable and the fallback fails with the same permission error it was
    written to solve. The same 0750-parent problem that makes nginx return 500
    here.

    It is in this account's own home, so no privilege is needed to create it.

    Ownership is then forced, because `mkdir -p` succeeds silently on an
    existing directory and leaves its ownership untouched. An earlier version
    created this with sudo; the root-owned directory it left behind broke every
    later run with the same permission error, and dropping the sudo from mkdir
    did nothing to fix it. So the state is asserted rather than assumed.
    """
    global _staging
    if _staging is None:
        home = await ssh.run(["sh", "-c", 'printf %s "$HOME"'], check=False)
        base = (home.stdout or "").strip() or f"/home/{settings.ssh_username}"
        _staging = f"{base}/.jarpull"
        await ssh.run(["mkdir", "-p", _staging], check=False)
        await ssh.run(["chown", f"{settings.ssh_username}:", _staging], sudo=True, check=False)
        await ssh.run(["chmod", "700", _staging], sudo=True, check=False)

        check = await ssh.run(["ls", "-ld", _staging], sudo=True, check=False)
        print(f"Staging root-owned files through {(check.stdout or '').strip()}")
    return _staging


async def fetch(ssh: SSHService, jar: Jar, dest: Path) -> None:
    """SFTP it down, falling back to a staged copy when the file is root-only.

    SFTP is a protocol, not a shell - it cannot sudo. So a file this account
    cannot read is copied by sudo into a directory it owns, pulled from there,
    and the staged copy removed. The same two-hop the tool uses for uploads, in
    reverse.
    """
    target = local_path(dest, jar.remote)
    target.parent.mkdir(parents=True, exist_ok=True)

    # Already here from an earlier run? Hash it and skip the transfer.
    #
    # This does not weaken the check that guards the delete. jar.remote_hash was
    # read from the server minutes ago in this same run, and the local file is
    # hashed here and now - so a match still means "these two are identical
    # right now". Re-downloading 6 GB to learn the same thing only widens the
    # window in which something can go wrong.
    if target.exists() and target.stat().st_size == jar.size:
        jar.local = target
        jar.local_hash = await asyncio.to_thread(sha256_of, target)
        if jar.verified:
            jar.status = "copied"
            jar.note = "already present, re-verified against the server"
            return

    sftp = await ssh.sftp()

    try:
        await asyncio.to_thread(sftp.get, jar.remote, str(target))
    except OSError as direct:
        # Expected for anything this account does not own. Which step of the
        # fallback then fails matters, so each one says so - a bare errno gives
        # no way to tell a failed sudo copy from an unreadable staging
        # directory, and they need opposite fixes.
        base = await staging_dir(ssh)
        staged = f"{base}/{jar.remote.strip('/').replace('/', '_')}"
        step = "?"
        try:
            step = "sudo cp to the staging directory"
            await ssh.run(["cp", jar.remote, staged], sudo=True)
            # cp as root leaves it root-owned, and under a restrictive sudo
            # umask unreadable to anyone else. Fixed before the pull, not after.
            step = "chown of the staged copy"
            await ssh.run(["chown", f"{settings.ssh_username}:", staged], sudo=True)
            step = "chmod of the staged copy"
            await ssh.run(["chmod", "640", staged], sudo=True)
            step = f"reading the staged copy at {staged}"
            await asyncio.to_thread(sftp.get, staged, str(target))
        except Exception as exc:                          # noqa: BLE001
            listing = await ssh.run(["ls", "-ld", base, staged], sudo=True, check=False)
            raise RuntimeError(
                f"direct read failed ({direct}); fallback failed at {step}: {exc}\n"
                f"      {(listing.stdout or listing.stderr or '').strip()}"
            ) from exc
        finally:
            await ssh.run(["rm", "-f", staged], sudo=True, check=False)
        jar.note = "staged via sudo (not readable directly)"

    jar.local = target
    jar.local_hash = await asyncio.to_thread(sha256_of, target)
    jar.status = "copied" if jar.verified else "MISMATCH"


async def remove(ssh: SSHService, jar: Jar) -> None:
    """Delete a server copy - only ever called for a verified local copy."""
    if not jar.verified:
        jar.status = "kept (not verified)"
        return
    # Checked again here rather than relying on the caller having done it: this
    # is the one irreversible step in the script.
    if excluded(jar.remote):
        jar.status = "kept (excluded)"
        return

    result = await ssh.run(["rm", "-f", jar.remote], sudo=True, check=False)
    jar.status = "deleted" if result.exit_code == 0 else "delete failed"
    if result.exit_code != 0:
        jar.note = (result.stderr or "").strip()[:120]


def human(size: int) -> str:
    return f"{size / 1024 / 1024:.1f} MB"


def write_manifest(run: Run, dest: Path) -> Path:
    """Without this the mirrored tree cannot be checked against the server."""
    path = dest / "manifest.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["remote_path", "local_path", "bytes", "sha256", "status", "note"])
        for jar in run.jars:
            writer.writerow([jar.remote, jar.local or "", jar.size,
                             jar.remote_hash, jar.status, jar.note])
        for remote, why in run.skipped:
            writer.writerow([remote, "", "", "", "skipped", why])
    return path


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect backup JARs from the app server into a mirrored local tree.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dest", type=Path, default=Path("jar-archive"),
                        help="local root of the mirrored tree (default: ./jar-archive)")
    parser.add_argument("--roots", nargs="*", default=ROOTS,
                        help="server directories to search")
    parser.add_argument("--delete", action="store_true",
                        help="delete the server copies that verified")
    parser.add_argument("--yes", action="store_true",
                        help="required with --delete; deleting is not reversible")
    args = parser.parse_args()

    if args.delete and not args.yes:
        print("--delete also needs --yes. Nothing was done.")
        return 2

    # DRY_RUN makes SSHService simulate every command it does not classify as
    # read-only. For the deployment tool that is the point; here it is poison.
    # `find`, `stat` and `sha256sum` would really run, so discovery and hashing
    # look perfect, while the `cp`/`chown`/`chmod` that stage a root-owned file
    # return success and do nothing - and the copy fails with a permission error
    # that has no obvious connection to the cause.
    #
    # Refused rather than warned about: a half-working collection is exactly the
    # state from which someone runs --delete.
    if settings.dry_run:
        print("DRY_RUN is on in .env, so every privileged copy would be simulated\n"
              "and no file would actually be staged. Set DRY_RUN=false and re-run.")
        return 2

    dest = args.dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)

    ssh = SSHService(settings)
    print(f"Connecting to {settings.ssh_host} as {settings.ssh_username}")
    await ssh.connect()

    try:
        run = await discover(ssh, args.roots)
        print(f"Found {len(run.jars)} JAR(s) to collect")
        for remote, why in run.skipped:
            print(f"  skipping {remote}\n      {why}")
        if not run.jars:
            return 0

        await measure(ssh, run.jars)
        total = sum(jar.size for jar in run.jars)
        print(f"Total {human(total)} into {dest}\n")

        for index, jar in enumerate(run.jars, 1):
            if jar.status == "unreadable":
                print(f"[{index}/{len(run.jars)}] !! {jar.remote} - {jar.note}")
                continue
            print(f"[{index}/{len(run.jars)}] {jar.remote}  ({human(jar.size)})")
            try:
                await fetch(ssh, jar, dest)
            except Exception as exc:                      # noqa: BLE001
                jar.status = "copy failed"
                jar.note = str(exc)[:400]   # long enough to keep the ls -ld line
            if jar.status == "MISMATCH":
                print("      MISMATCH - the copy differs from the server, keeping both")

        copied = [jar for jar in run.jars if jar.verified]
        print(f"\nCopied and verified: {len(copied)}/{len(run.jars)}")

        failed = [jar for jar in run.jars if not jar.verified]
        for jar in failed:
            print(f"  not verified: {jar.remote} - {jar.status} {jar.note}")

        if args.delete:
            if failed:
                # Deleting the verified ones is still correct, but say plainly
                # that the sweep will not be complete.
                print(f"\n{len(failed)} file(s) will be left on the server.")
            print(f"\nDeleting {len(copied)} verified server copies")
            for jar in copied:
                await remove(ssh, jar)
            deleted = [jar for jar in run.jars if jar.status == "deleted"]
            print(f"Deleted {len(deleted)}, freed about "
                  f"{human(sum(jar.size for jar in deleted))}")
        else:
            print("\nNothing deleted. Re-run with --delete --yes once you have "
                  "checked the copies.")

        print(f"Manifest: {write_manifest(run, dest)}")
        return 0
    finally:
        # The staged copies are removed one by one as they are pulled, so what
        # is left here is an empty directory. It stays: removing it wiped the
        # one piece of state worth inspecting after a failed run, which made a
        # permission problem look like a command that never executed.
        if _staging:
            print(f"Staging directory left in place for inspection: {_staging}")
        await ssh.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
