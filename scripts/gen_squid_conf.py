#!/usr/bin/env python3

import os
import sys


def env_int(name, default):
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def main():
    upstream_host = os.getenv("UPSTREAM_HOST", "proxy.example.com")
    up_user = os.getenv("UP_USER", "")
    up_pass = os.getenv("UP_PASS", "")
    port_first = env_int("PORT_FIRST", 10001)
    port_last = env_int("PORT_LAST", 10100)

    if port_last < port_first:
        print("PORT_LAST must be >= PORT_FIRST", file=sys.stderr)
        sys.exit(1)

    print("visible_hostname proxy")
    print("http_port 3128")
    print("")
    print("auth_param basic program /opt/helper/auth.py")
    print("auth_param basic children 5")
    print("auth_param basic realm Proxy")
    print("auth_param basic credentialsttl 1 hour")
    print("acl auth proxy_auth REQUIRED")
    print("")
    print("external_acl_type port_router ttl=0 negative_ttl=0 %LOGIN /opt/helper/router.py")
    print("acl route_ok external port_router")
    print("")

    for port in range(port_first, port_last + 1):
        peer_name = f"peer_{port}"
        line = f"cache_peer {upstream_host} parent {port} 0 no-query name={peer_name}"
        if up_user or up_pass:
            line += f" login={up_user}:{up_pass}"
        print(line)
        print(f"acl is_{port} tag peer_{port}")
        print(f"cache_peer_access {peer_name} allow is_{port}")
        print(f"cache_peer_access {peer_name} deny all")
        print("")

    print("never_direct allow all")
    print("http_access allow auth route_ok")
    print("http_access deny all")


if __name__ == "__main__":
    main()
