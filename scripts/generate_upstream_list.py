#!/usr/bin/env python3

import argparse
import os
import sys
from pathlib import Path
from urllib.parse import quote


def env_int(name, default):
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def build_uri_line(scheme, host, port, username, password):
    auth = ""
    if username or password:
        auth = f"{quote(username, safe='')}:{quote(password, safe='')}@"
    return f"{scheme}://{auth}{host}:{port}"


def build_colon_line(host, port, username, password):
    if username or password:
        return f"{host}:{port}:{username}:{password}"
    return f"{host}:{port}"


def build_csv_line(scheme, host, port, username, password):
    return ",".join([scheme, host, str(port), username, password])


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate line-based upstream proxy lists from one host + port range."
    )
    parser.add_argument("--scheme", default=os.getenv("UPSTREAM_SCHEME", "http"))
    parser.add_argument("--host", default=os.getenv("UPSTREAM_HOST", "proxy.example.com"))
    parser.add_argument("--username", default=os.getenv("UP_USER", ""))
    parser.add_argument("--password", default=os.getenv("UP_PASS", ""))
    parser.add_argument("--port-first", type=int, default=env_int("PORT_FIRST", 10001))
    parser.add_argument("--port-last", type=int, default=env_int("PORT_LAST", 10100))
    parser.add_argument(
        "--format",
        choices=("uri", "colon", "csv"),
        default="uri",
        help="Output line format.",
    )
    parser.add_argument(
        "--output",
        default="-",
        help="Output file path. Use - for stdout.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if not args.host:
        print("host is required", file=sys.stderr)
        return 1
    if args.port_last < args.port_first:
        print("port-last must be >= port-first", file=sys.stderr)
        return 1

    builders = {
        "uri": lambda port: build_uri_line(args.scheme, args.host, port, args.username, args.password),
        "colon": lambda port: build_colon_line(args.host, port, args.username, args.password),
        "csv": lambda port: build_csv_line(args.scheme, args.host, port, args.username, args.password),
    }

    lines = [builders[args.format](port) for port in range(args.port_first, args.port_last + 1)]
    content = "\n".join(lines) + "\n"

    if args.output == "-":
        sys.stdout.write(content)
        return 0

    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    print(f"generated {len(lines)} upstreams -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
