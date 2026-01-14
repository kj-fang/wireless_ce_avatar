import os 
import snowflake.connector

def snowflake_query(passwd, sql_query, schema, fetch_mode="all"):
    os.environ["HTTP_PROXY"] = "http://proxy-dmz.intel.com:911"
    os.environ["HTTPS_PROXY"] = "http://proxy-dmz.intel.com:912"
    os.environ["NO_PROXY"] = "xd14286-ecdw.privatelink.snowflakecomputing.com"

    conn = snowflake.connector.connect(
        user="SYS_ECDW_WCS_WIRELESSBUGS_DSA_PROD",
        password=passwd,
        role="ROLE_CDA_SALES_SUPPORT_PREMIER_ANALYSIS_READER",
        account = "XD14286-ECDWPROD",
        warehouse="WH_SMG_CONSUMPTION", 
        database="SALES_MARKETING",
        schema=schema
        )
    
    cs = conn.cursor()
    cs.execute(sql_query)

    if fetch_mode == "all":
        result = cs.fetchall()
    elif fetch_mode == "one":
        result = cs.fetchone()
    else:
        raise ValueError(f"Fetch_mode not supported: {fetch_mode} (current support: all, one)")
    
    cs.close()
    conn.close()
    return result