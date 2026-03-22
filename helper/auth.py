#!/usr/bin/env python3

import hmac
import os
import sys


def check_password(provided, expected):
    if not expected:
        return False
    return hmac.compare_digest(provided or "", expected)


def main():
    expected = os.getenv("AUTH_PASSWORD")

    if not expected:
        # Refuse all auth if password is not configured.
        while True:
            line = sys.stdin.readline()
            if not line:
                break
            print("ERR", flush=True)
        return

    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            print("ERR", flush=True)
            continue
        user, password = parts[0], parts[1]
        if not user:
            print("ERR", flush=True)
            continue
        if check_password(password, expected):
            print("OK", flush=True)
        else:
            print("ERR", flush=True)


if __name__ == "__main__":
    main()
