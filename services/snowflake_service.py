import os
import time
import threading
import snowflake.connector

_snowflake_conn = None
_snowflake_passwd = None
_snowflake_conn_mode = None  # "proxy" or "privatelink" — tracks how cached conn was built
_snowflake_lock = threading.Lock()

_PRIVATELINK_HOST = "xd14286-ecdw.privatelink.snowflakecomputing.com"
_PROXY_HTTP = "http://proxy-dmz.intel.com:911"
_PROXY_HTTPS = "http://proxy-dmz.intel.com:912"


def _apply_proxy_env(use_privatelink):
    """(Re-)apply proxy/OCSP env vars for the Snowflake connector.

    Must be called before EVERY connect and EVERY cached-conn reuse, because
    other code (Selenium driver manager) pops HTTP_PROXY/HTTPS_PROXY/NO_PROXY
    from os.environ, and snowflake-connector-python re-reads env on each HTTP
    request via requests.trust_env.
    """
    if use_privatelink:
        for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            os.environ.pop(var, None)
        os.environ["NO_PROXY"] = _PRIVATELINK_HOST
    else:
        os.environ["HTTP_PROXY"] = _PROXY_HTTP
        os.environ["HTTPS_PROXY"] = _PROXY_HTTPS
        os.environ["NO_PROXY"] = _PRIVATELINK_HOST
    # Disable OCSP cache server lookup — ocsp.snowflakecomputing.com is unreachable
    # on this corporate network and each failed attempt adds ~5s timeout delay.
    os.environ["SF_OCSP_RESPONSE_CACHE_SERVER_ENABLED"] = "false"


def _connect(passwd, use_privatelink):
    """Open a fresh Snowflake connection via proxy or privatelink."""
    _apply_proxy_env(use_privatelink)
    kwargs = dict(
        user="SYS_ECDW_WCS_WIRELESSBUGS_DSA_PROD",
        password=passwd,
        role="ROLE_CDA_SALES_SUPPORT_PREMIER_ANALYSIS_READER",
        account="XD14286-ECDWPROD",
        warehouse="WH_SMG_CONSUMPTION",
        database="SALES_MARKETING",
        insecure_mode=True,  # skip OCSP checks — ocsp.digicert.com unreachable on this network
        login_timeout=30,
        network_timeout=30,
    )
    if use_privatelink:
        kwargs["host"] = _PRIVATELINK_HOST
    return snowflake.connector.connect(**kwargs)


def _get_connection(passwd):
    """Return a cached Snowflake connection, reconnecting only when necessary.

    Tries via the corporate proxy first; on any failure falls back once to a
    direct connection through the Snowflake privatelink host.
    On cached-conn reuse, re-applies proxy env matching the cached conn's mode
    so queries after Selenium-triggered env clearing still route correctly.
    """
    global _snowflake_conn, _snowflake_passwd, _snowflake_conn_mode
    with _snowflake_lock:
        if (
            _snowflake_conn is None
            or _snowflake_passwd != passwd
            or _snowflake_conn.is_closed()
        ):
            t_conn = time.time()
            try:
                _snowflake_conn = _connect(passwd, use_privatelink=False)
                _snowflake_conn_mode = "proxy"
                print(f"  [Snowflake] New connection established via proxy: {time.time() - t_conn:.2f}s")
            except Exception as e:
                print(f"  [Snowflake] Proxy connect failed ({type(e).__name__}: {e}); retrying via privatelink")
                t_retry = time.time()
                _snowflake_conn = _connect(passwd, use_privatelink=True)
                _snowflake_conn_mode = "privatelink"
                print(f"  [Snowflake] New connection established via privatelink: {time.time() - t_retry:.2f}s")
            _snowflake_passwd = passwd
        else:
            _apply_proxy_env(use_privatelink=(_snowflake_conn_mode == "privatelink"))
            print(f"  [Snowflake] Reusing existing connection ({_snowflake_conn_mode}, 0.00s)")
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
