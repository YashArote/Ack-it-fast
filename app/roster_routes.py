from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import HTMLResponse

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
        <h1>Update shift roster</h1>
        <p>Upload the latest Excel roster. The uploaded file replaces the roster currently used for P1 assignment decisions.</p>
        <form action="/roster/upload" method="post" enctype="multipart/form-data">
            <label for="file">Roster Excel file</label>
            <input id="file" name="file" type="file" accept=".xlsx,.xls" required>
            <small>Accepted formats: .xlsx and .xls</small>
            <label for="api_key">Application API key</label>
            <input id="api_key" name="api_key" type="password" autocomplete="off" required>
            <button type="submit">Upload roster</button>
        </form>
    </main>
</body>
</html>"""


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
