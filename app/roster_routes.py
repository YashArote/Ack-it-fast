from __future__ import annotations

import os
from typing import Any, Dict

from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from app.config import APP_API_KEY, ROSTER_PATH

router = APIRouter(prefix="/roster", tags=["roster"])


@router.get("/upload", response_class=HTMLResponse)
async def roster_upload_page() -> str:
        return """<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Roster Upload</title>
    <style>
        :root { font-family: Arial, sans-serif; color: #172033; background: #eef3f8; }
        body { margin: 0; min-height: 100vh; display: grid; place-items: center; }
        main { width: min(92vw, 520px); background: white; padding: 32px; border: 1px solid #d6dee8; border-radius: 8px; box-shadow: 0 12px 32px #17203318; }
        h1 { margin-top: 0; font-size: 24px; }
        p { color: #536176; line-height: 1.5; }
        label { display: block; margin: 18px 0 7px; font-weight: 600; }
        input[type=file], input[type=password] { box-sizing: border-box; width: 100%; padding: 10px; border: 1px solid #b8c4d3; border-radius: 5px; }
        button { margin-top: 24px; width: 100%; padding: 11px; border: 0; border-radius: 5px; color: white; background: #1769aa; font-size: 15px; cursor: pointer; }
        button:hover { background: #125487; }
        small { color: #68768a; }
    </style>
</head>
<body>
    <main>
        <h1>Legacy Excel roster</h1>
        <p>This upload page is deprecated. P1 assignment decisions now use the shift roster API; uploading an Excel file will not change assignment eligibility.</p>
        <form action="/roster/upload" method="post" enctype="multipart/form-data">
            <label for="file">Roster Excel file</label>
            <input id="file" name="file" type="file" accept=".xlsx,.xls" required>
            <small>Accepted formats: .xlsx and .xls</small>
            <label for="api_key">Application API key</label>
            <input id="api_key" name="api_key" type="password" autocomplete="off" required>
            <button type="submit">Upload roster</button>
            <button type="button" id="download">Download current roster</button>
        </form>
        <p id="status" role="status"></p>
        <script>
          document.getElementById('download').addEventListener('click', async function () {
            const key = document.getElementById('api_key').value;
            const status = document.getElementById('status');
            if (!key) { status.textContent = 'Enter the application API key first.'; return; }
            status.textContent = 'Preparing download...';
            const response = await fetch('/roster/download', { headers: { 'X-API-Key': key } });
            if (!response.ok) { status.textContent = 'Download failed. Check the API key and try again.'; return; }
            const blob = await response.blob();
            const link = document.createElement('a');
            link.href = URL.createObjectURL(blob);
            link.download = 'roster.xlsx';
            link.click();
            URL.revokeObjectURL(link.href);
            status.textContent = 'Download started.';
          });
        </script>
    </main>
</body>
</html>"""


@router.get("/download")
async def download_roster(x_api_key: str = Header(None)) -> FileResponse:
    if x_api_key != APP_API_KEY:
        raise HTTPException(status_code=401, detail="invalid api key")
    if not ROSTER_PATH:
        raise HTTPException(status_code=404, detail="roster path is not configured")
    if not os.path.isfile(ROSTER_PATH):
        raise HTTPException(status_code=404, detail="current roster was not found")
    return FileResponse(
        ROSTER_PATH,
        filename="roster.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@router.post("/upload")
async def upload_roster(
    file: UploadFile = File(...),
    force_replace: bool = Form(False),
    x_api_key: str = Header(None),
    api_key: str = Form(None),
) -> Dict[str, Any]:
    if (x_api_key or api_key) != APP_API_KEY:
        raise HTTPException(status_code=401, detail="invalid api key")
    if not file.filename:
        raise HTTPException(status_code=400, detail="file is required")
    if not file.filename.endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="only Excel files are supported")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="file is empty")

    file_path = ROSTER_PATH
    with open(file_path, "wb") as destination:
        destination.write(content)

    return {
        "status": "ok",
        "file": file.filename,
        "path": file_path,
        "replaced": force_replace,
    }
