import io

import openpyxl
from fastapi import APIRouter, Depends, HTTPException, UploadFile

from app.auth.dependencies import get_current_user

router = APIRouter(prefix="/uploads", dependencies=[Depends(get_current_user)])
MAX_UPLOAD_BYTES = 2 * 1024 * 1024


@router.post("/timetable")
async def upload_timetable(file: UploadFile):
    # Planted: the whole upload is read into memory before parsing, with no size limit.
    content = file.file.read()
    book = openpyxl.load_workbook(io.BytesIO(content), read_only=True)
    return {"sheets": book.sheetnames}


@router.post("/fares")
async def upload_fares(file: UploadFile):
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="file too large")
    book = openpyxl.load_workbook(io.BytesIO(content), read_only=True)
    return {"sheets": book.sheetnames}
