#!/usr/bin/env python3

import argparse
import sys
from pathlib import Path
from urllib.parse import quote


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


def build_output_line(separator, static_hops, cycled_hops, index, generated_hop):
    hops = []
    if cycled_hops:
        hops.append(cycled_hops[index % len(cycled_hops)])
    hops.extend(static_hops)
    hops.append(generated_hop)
    return separator.join(hops)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate line-based upstream proxy lists from one host + port range."
    )
    parser.add_argument("--scheme", default="http")
    parser.add_argument("--host", default="proxy.example.com")
    parser.add_argument("--username", default="")
    parser.add_argument("--password", default="")
    parser.add_argument("--port-first", type=int, default=10001)
    parser.add_argument("--port-last", type=int, default=10100)
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
    parser.add_argument(
        "--prepend-hop",
        action="append",
        default=[],
        help="Prepend a fixed hop before each generated upstream hop. Can be repeated.",
    )
    parser.add_argument(
        "--cycle-first-hop",
        action="append",
        default=[],
        help="Cycle these first-hop values across generated lines. Can be repeated.",
    )
    parser.add_argument(
        "--separator",
        default=" | ",
        help="Separator used when composing chained hops.",
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
        "uri": lambda port: build_uri_line(
            args.scheme, args.host, port, args.username, args.password
        ),
        "colon": lambda port: build_colon_line(
            args.host, port, args.username, args.password
        ),
        "csv": lambda port: build_csv_line(
            args.scheme, args.host, port, args.username, args.password
        ),
    }

    static_hops = [value.strip() for value in args.prepend_hop if value.strip()]
    cycled_hops = [value.strip() for value in args.cycle_first_hop if value.strip()]

    lines = []
    for index, port in enumerate(range(args.port_first, args.port_last + 1)):
        generated_hop = builders[args.format](port)
        lines.append(
            build_output_line(
                args.separator,
                static_hops,
                cycled_hops,
                index,
                generated_hop,
            )
        )
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
