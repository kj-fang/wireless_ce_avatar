import os
import time
import threading
import snowflake.connector

_snowflake_conn = None
_snowflake_passwd = None
_snowflake_lock = threading.Lock()


def _apply_proxy_env():
    """Re-assert proxy/OCSP env vars before every Snowflake request.

    The Selenium driver manager pops HTTP_PROXY/HTTPS_PROXY/NO_PROXY from os.environ,
    and snowflake.connector resolves the proxy lazily per request (not at connect time),
    so a reused connection would otherwise go direct and hit a connect timeout.
    """
    os.environ["HTTP_PROXY"] = "http://proxy-dmz.intel.com:911"
    os.environ["HTTPS_PROXY"] = "http://proxy-dmz.intel.com:912"
    os.environ["NO_PROXY"] = "xd14286-ecdw.privatelink.snowflakecomputing.com"
    # Disable OCSP cache server lookup — ocsp.snowflakecomputing.com is unreachable
    # on this corporate network and each failed attempt adds ~5s timeout delay.
    os.environ["SF_OCSP_RESPONSE_CACHE_SERVER_ENABLED"] = "false"


def _get_connection(passwd):
    """Return a cached Snowflake connection, reconnecting only when necessary."""
    global _snowflake_conn, _snowflake_passwd
    with _snowflake_lock:
        _apply_proxy_env()
        if (
            _snowflake_conn is None
            or _snowflake_passwd != passwd
            or _snowflake_conn.is_closed()
        ):
            t_conn = time.time()
            _snowflake_conn = snowflake.connector.connect(
                user="SYS_ECDW_WCS_WIRELESSBUGS_DSA_PROD",
                password=passwd,
                role="ROLE_CDA_SALES_SUPPORT_PREMIER_ANALYSIS_READER",
                account="XD14286-ECDWPROD",
                warehouse="WH_SMG_CONSUMPTION",
                database="SALES_MARKETING",
                # Proxy is passed as connection params (proxy_host/proxy_port) so
                # Snowflake does not depend on HTTP(S)_PROXY/NO_PROXY staying stable
                # in os.environ (DriverManager clears those vars during Chrome startup).
                # Note: _apply_proxy_env() still mutates env vars today; if we keep that
                # for non-proxy Snowflake knobs (e.g. OCSP settings), avoid claiming
                # "no env writes" here to prevent confusion.
                proxy_host="proxy-dmz.intel.com",
                proxy_port=912,
                insecure_mode=True,  # skip OCSP checks — ocsp.digicert.com unreachable on this network
                # Bound retries so a network blip can't hang callers forever
                # (observed: handsfree fetch_case stuck 20+ min in the
                # connector's unbounded query-request retry loop).
                login_timeout=30,    # fresh connection attempt: fail after 30s
                network_timeout=180, # established-connection requests: fail after ~3 min
            )
            _snowflake_passwd = passwd
            print(f"  [Snowflake] New connection established: {time.time() - t_conn:.2f}s")
        else:
            print("  [Snowflake] Reusing existing connection (0.00s)")
        return _snowflake_conn


def snowflake_query(passwd, sql_query, schema, fetch_mode="all"):
    conn = _get_connection(passwd)
    cs = conn.cursor()
    try:
        t_exec = time.time()
        cs.execute(sql_query)

        if fetch_mode == "all":
            result = cs.fetchall()
        elif fetch_mode == "one":
            result = cs.fetchone()
        else:
            raise ValueError(f"Fetch_mode not supported: {fetch_mode} (current support: all, one)")
        print(f"  [Snowflake] Execute+fetch ({fetch_mode}): {time.time() - t_exec:.2f}s")
        return result
    finally:
        cs.close()
