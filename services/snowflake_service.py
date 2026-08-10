import os
import time
import threading
import snowflake.connector

_snowflake_conn = None
_snowflake_passwd = None
_snowflake_lock = threading.Lock()


def _get_connection(passwd):
    """Return a cached Snowflake connection, reconnecting only when necessary."""
    global _snowflake_conn, _snowflake_passwd
    with _snowflake_lock:
        if (
            _snowflake_conn is None
            or _snowflake_passwd != passwd
            or _snowflake_conn.is_closed()
        ):
            # Disable OCSP cache server lookup — ocsp.snowflakecomputing.com is unreachable
            # on this corporate network and each failed attempt adds ~5s timeout delay.
            os.environ["SF_OCSP_RESPONSE_CACHE_SERVER_ENABLED"] = "false"

            t_conn = time.time()
            _snowflake_conn = snowflake.connector.connect(
                user="SYS_ECDW_WCS_WIRELESSBUGS_DSA_PROD",
                password=passwd,
                role="ROLE_CDA_SALES_SUPPORT_PREMIER_ANALYSIS_READER",
                account="XD14286-ECDWPROD",
                warehouse="WH_SMG_CONSUMPTION",
                database="SALES_MARKETING",
                # Proxy passed as connection params, NOT env vars: the Selenium
                # driver manager pops HTTP(S)_PROXY from os.environ at arbitrary
                # times (create_download_driver / open_browser), and requests
                # re-resolves env proxies on every request — env-based proxying
                # therefore races with Chrome startup and dies mid-query with
                # connect timeouts. Params live inside the connector's session
                # manager and are immune to os.environ mutation (verified: no
                # env writes, works with all proxy env vars removed).
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
