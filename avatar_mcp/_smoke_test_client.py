"""Manual smoke test for the Report Ingestion Endpoint (avatar_mcp/ingestion_server.py).
Not part of the app; run manually against a live server:

    python -m avatar_mcp._smoke_test_client <path-to-bt.zip> [server-host]

server-host defaults to 127.0.0.1 (same machine). Pass the server machine's
IP/hostname to test from a different machine, e.g.:

    python -m avatar_mcp._smoke_test_client report.zip 10.1.2.3

Requires the server (python -m avatar_mcp.ingestion_server) already running,
and its port reachable from this machine (firewall etc.).
"""
import asyncio
import os
import sys

import httpx2

from avatar_mcp import ingestion_config as cfg
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

API_KEY = next(iter(cfg.API_KEYS.values()))


async def main(zip_path: str, server_host: str = '127.0.0.1') -> None:
    base_url = f"http://{server_host}:{cfg.PORT}"
    http_client = httpx2.AsyncClient(headers={"Authorization": f"Bearer {API_KEY}"})
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
                    import base64
                    out_path = os.path.join(os.path.dirname(zip_path), report_file["filename"])
                    with open(out_path, "wb") as out_fh:
                        out_fh.write(base64.b64decode(report_file["content_base64"]))
                    print(f"-> saved report file to {out_path}")
                break
            await asyncio.sleep(3)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m avatar_mcp._smoke_test_client <path-to-bt.zip> [server-host]")
        sys.exit(1)
    host = sys.argv[2] if len(sys.argv) > 2 else '127.0.0.1'
    asyncio.run(main(sys.argv[1], host))
