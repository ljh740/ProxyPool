FROM ubuntu/squid

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-redis \
    && rm -rf /var/lib/apt/lists/*

COPY helper /opt/helper
COPY scripts /opt/scripts
COPY squid/entrypoint.sh /entrypoint.sh

RUN chmod +x /entrypoint.sh /opt/helper/router.py /opt/helper/auth.py /opt/helper/proxy_server.py /opt/helper/upstream_pool.py /opt/scripts/gen_squid_conf.py

ENTRYPOINT ["/entrypoint.sh"]
