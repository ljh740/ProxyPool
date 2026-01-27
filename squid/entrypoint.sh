#!/usr/bin/env sh
set -eu

python3 /opt/scripts/gen_squid_conf.py > /etc/squid/squid.conf
squid -k parse -f /etc/squid/squid.conf
rm -f /run/squid.pid /var/run/squid.pid
exec squid -N -f /etc/squid/squid.conf
