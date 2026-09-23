"""Manual smoke test for the Report Ingestion Endpoint (avatar_mcp/ingestion_server.py).
Not part of the app; run manually against a live server:

    python -m avatar_mcp._smoke_test_client <path-to-bt.zip> --api-key <key> \
        [--host 127.0.0.1] [--port 8443]

Standalone by design (only imports httpx2 + mcp, not avatar_mcp.ingestion_config)
so this one file can be copied to a different test machine on its own - host,
port and api-key are whatever the server operator hands out, never read from a
local config file (a client-local ingestion_config.json would just contain
that machine's own auto-generated placeholder values, which won't match the
real server's key/port).

Requires the server (python -m avatar_mcp.ingestion_server) already running,
and its port reachable from this machine (firewall etc.).
"""
import argparse
import asyncio
import base64
import os

import httpx2

from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client


async def main(zip_path: str, host: str, port: int, api_key: str) -> None:
    base_url = f"http://{host}:{port}"
    http_client = httpx2.AsyncClient(headers={"Authorization": f"Bearer {api_key}"})
    transport = streamable_http_client(f"{base_url}/mcp", http_client=http_client)

    async with Client(transport) as client:
        size_bytes = os.path.getsize(zip_path)
        filename = os.path.basename(zip_path)

        print(f"-> create_report_upload({filename}, {size_bytes})")
        create_result = await client.call_tool(
            "create_report_upload", {"filename": filename, "size_bytes": size_bytes}
        )
        print("    is_error:", create_result.is_error)
        print("    content:", create_result.content)
        print("    structured_content:", create_result.structured_content)
        upload_id = create_result.structured_content["upload_id"]
        upload_path = create_result.structured_content["upload_path"]

        print(f"-> PUT {upload_path} ({size_bytes} bytes)")
        with open(zip_path, "rb") as fh:
            put_resp = await http_client.put(f"{base_url}{upload_path}", content=fh.read())
        print("   status:", put_resp.status_code)
        put_resp.raise_for_status()

        print(f"-> start_report_analysis({upload_id})")
        start_result = await client.call_tool("start_report_analysis", {"upload_id": upload_id})
        print("   ", start_result.structured_content)
        job_id = start_result.structured_content["job_id"]

        while True:
            status_result = await client.call_tool("get_report_status", {"job_id": job_id})
            data = status_result.structured_content
            print("-> get_report_status:", data["status"])
            if data["status"] in ("done", "error"):
                print("=== FINAL ===")
                print(data)
                report_file = data.get("report_file")
                if report_file:
                    out_path = os.path.join(os.path.dirname(zip_path), report_file["filename"])
                    with open(out_path, "wb") as out_fh:
                        out_fh.write(base64.b64decode(report_file["content_base64"]))
                    print(f"-> saved report file to {out_path}")
                break
            await asyncio.sleep(3)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("zip_path", help="Path to a BT report .zip/.7z/.rar/.hci.txt/.etl")
    parser.add_argument("--host", default="127.0.0.1", help="Ingestion server host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8443, help="Ingestion server port (default: 8443)")
    parser.add_argument("--api-key", required=True, help="Bearer API key issued by the server operator")
    args = parser.parse_args()
    asyncio.run(main(args.zip_path, args.host, args.port, args.api_key))
