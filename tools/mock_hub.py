"""A stand-in installation hub, for when the Aiden demo server is unavailable.

It speaks exactly the same contract as the real hub:

    GET <base>/api/installation-hubs/path?filename=<FILE_NAME>&code=<CODE>
    -> the JAR bytes, or 404

so the deployment tool's real download path is exercised end to end. Switching
back to the demo server is then a single line in .env - no code changes.

Usage
-----
    1. Put the JARs you want to serve in `tools/hub_files/`:

           tools/hub_files/tx-test-mgmt-1.6.0.jar
           tools/hub_files/ai-dap-app-1.6.0.jar
           tools/hub_files/tx-integration-agent-1.6.0.jar

    2. Start it (stdlib only, no dependencies, separate terminal):

           python tools/mock_hub.py

    3. Point the deployment tool at it in .env:

           INSTALLATION_HUB_URL=http://127.0.0.1:8081/api/installation-hubs/path
           INSTALLATION_CODE=local-test

    4. On Monday, change that one INSTALLATION_HUB_URL line back to the demo
       server and stop this process. Nothing else changes.

Options
-------
    --port 8081             port to listen on
    --dir tools/hub_files   directory served
    --code local-test       required `code` value; use --code "" to accept any
"""

from __future__ import annotations

import argparse
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ENDPOINT = "/api/installation-hubs/path"
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class Handler(BaseHTTPRequestHandler):
    directory: Path
    expected_code: str

    def _fail(self, status: int, message: str) -> None:
        body = message.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        parsed = urlparse(self.path)
        if parsed.path.rstrip("/") != ENDPOINT:
            self._fail(404, f"Unknown endpoint. Use {ENDPOINT}?filename=...&code=...")
            return

        query = parse_qs(parsed.query)
        filename = (query.get("filename") or [""])[0]
        code = (query.get("code") or [""])[0]

        if self.expected_code and code != self.expected_code:
            self._fail(403, "Invalid installation code")
            return
        if not SAFE_NAME.match(filename or ""):
            self._fail(400, f"Invalid filename: {filename!r}")
            return

        target = (self.directory / filename).resolve()
        if self.directory.resolve() not in target.parents:
            self._fail(400, "Path traversal rejected")
            return
        if not target.is_file():
            available = sorted(p.name for p in self.directory.glob("*.jar"))
            self._fail(
                404,
                f"{filename} is not available.\nServing from: {self.directory}\n"
                f"Available: {', '.join(available) or '(none)'}",
            )
            return

        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/java-archive")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args) -> None:
        print(f"[mock-hub] {fmt % args}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Stand-in installation hub")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--dir", default=str(Path(__file__).parent / "hub_files"))
    parser.add_argument("--code", default="local-test", help='required code; "" accepts any')
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address. Keep 127.0.0.1 unless you are exposing it deliberately "
        "(cloudflared works with the default, since it connects locally).",
    )
    args = parser.parse_args()

    directory = Path(args.dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)

    Handler.directory = directory
    Handler.expected_code = args.code

    jars = sorted(p.name for p in directory.glob("*.jar"))
    print(f"Serving  : {directory}")
    print(f"JARs     : {', '.join(jars) if jars else '(none yet - copy them into the folder above)'}")
    print(f"Endpoint : http://127.0.0.1:{args.port}{ENDPOINT}?filename=<name>&code={args.code or '<any>'}")
    print("\nSet in .env:")
    print(f"  INSTALLATION_HUB_URL=http://127.0.0.1:{args.port}{ENDPOINT}")
    print(f"  INSTALLATION_CODE={args.code or '(anything)'}")
    print("\nCtrl+C to stop.")

    if args.host != "127.0.0.1":
        print(f"\nWARNING: binding to {args.host} - this server is reachable beyond this machine.")
        if not args.code:
            print("WARNING: --code is empty, so anyone who reaches it can download the JARs.")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
