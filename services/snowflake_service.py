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
        # Bound retries so a network blip can't hang callers forever
        # (observed: handsfree fetch_case stuck 20+ min in the
        # connector's unbounded query-request retry loop).
        login_timeout=30,    # fresh connection attempt: fail after 30s
        network_timeout=180, # established-connection requests: fail after ~3 min
        # Heartbeat the session while the process is alive: the Handsfree
        # CLI keeps this connection cached between rounds hours apart, and
        # Snowflake ends an idle session after 4 h (error 390114 on the
        # next query). snowflake_query still reconnects once if it expires.
        client_session_keep_alive=True,
    )
    if use_privatelink:
        kwargs["host"] = _PRIVATELINK_HOST
    else:
        # Pass proxy as connection params so Snowflake doesn't depend on
        # HTTP(S)_PROXY/NO_PROXY staying stable in os.environ (DriverManager
        # clears those vars during Chrome startup).
        kwargs["proxy_host"] = "proxy-dmz.intel.com"
        kwargs["proxy_port"] = 912
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


def _drop_connection(conn):
    """Forget the cached connection (if it is still the one given) so the
    next _get_connection opens a fresh session."""
    global _snowflake_conn, _snowflake_conn_mode
    with _snowflake_lock:
        if _snowflake_conn is not None and _snowflake_conn is conn:
            _snowflake_conn = None
            _snowflake_conn_mode = None
    try:
        conn.close()
    except Exception:
        pass


# Snowflake answers a query on a session it has ended with one of these;
# the Python connection object is NOT closed by that, so is_closed() in
# _get_connection cannot tell, and only a new login helps.
_SESSION_EXPIRED_ERRNOS = {390111, 390112, 390114, 390115, 390195}
_SESSION_EXPIRED_WORDS = ("token has expired", "token is invalid",
                          "session no longer exists", "session does not exist",
                          "authenticate again")


def _session_expired(exc) -> bool:
    if getattr(exc, "errno", None) in _SESSION_EXPIRED_ERRNOS:
        return True
    msg = str(getattr(exc, "msg", None) or exc).lower()
    return any(w in msg for w in _SESSION_EXPIRED_WORDS)


def _run_query(conn, sql_query, fetch_mode):
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


def snowflake_query(passwd, sql_query, schema, fetch_mode="all"):
    """Run a query on the cached connection. When Snowflake reports the
    session as expired (a long-lived process reusing a connection hours
    later), the cached connection is dropped and the query is run ONCE more
    on a fresh login; any other error propagates unchanged."""
    conn = _get_connection(passwd)
    try:
        return _run_query(conn, sql_query, fetch_mode)
    except Exception as e:
        if not _session_expired(e):
            raise
        print(f"  [Snowflake] session expired ({type(e).__name__}: {e}); "
              "reconnecting and retrying the query once")
        _drop_connection(conn)
        conn = _get_connection(passwd)
        return _run_query(conn, sql_query, fetch_mode)
