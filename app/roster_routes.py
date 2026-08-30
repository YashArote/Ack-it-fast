from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.config import ROSTER_PATH

router = APIRouter(prefix="/roster", tags=["roster"])


@router.post("/upload")
async def upload_roster(file: UploadFile = File(...), force_replace: bool = Form(False)) -> Dict[str, Any]:
    if not file.filename:
        raise HTTPException(status_code=400, detail="file is required")
    if not file.filename.endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="only Excel files are supported")

    file_path = ROSTER_PATH
    with open(file_path, "wb") as destination:
        content = await file.read()
        destination.write(content)

    return {
        "status": "ok",
        "file": file.filename,
        "path": file_path,
        "replaced": force_replace,
    }
