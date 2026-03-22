FROM ubuntu/squid

COPY helper/requirements.txt /tmp/helper-requirements.txt

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip \
    && python3 -m pip install --break-system-packages --no-cache-dir -r /tmp/helper-requirements.txt \
    && rm -rf /var/lib/apt/lists/* /tmp/helper-requirements.txt

COPY helper /opt/helper
COPY scripts /opt/scripts
COPY squid/entrypoint.sh /entrypoint.sh

RUN chmod +x /entrypoint.sh /opt/helper/router.py /opt/helper/auth.py /opt/helper/proxy_server.py /opt/helper/upstream_pool.py

ENTRYPOINT ["/entrypoint.sh"]
