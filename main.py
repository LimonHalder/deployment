import asyncio
import io
import logging
import os

from docx import Document
from docx.shared import Pt
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from google import genai
from google.genai import types
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener
from pydantic import BaseModel

register_heif_opener()  # iPhone HEIC photos
log = logging.getLogger("ocr")
logging.basicConfig(level=logging.INFO)

import os
from dotenv import load_dotenv

# Load variables from the .env file
load_dotenv()

# Access the model variable with a fallback default
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
MAX_FILES = 10
MAX_TOTAL_BYTES = 18 * 1024 * 1024  # Gemini inline request limit is ~20 MB

SYSTEM = (
    "You are a precise OCR engine for Bangla (বাংলা) documents, both handwritten "
    "and printed, with occasional English words and numbers."
)
PROMPT = (
    "Transcribe all text in this document exactly as written. Rules:\n"
    "- Keep the original language and script. Do NOT translate.\n"
    "- Preserve line breaks, paragraphs, lists and table rows (use ' | ' between columns).\n"
    "- Keep Bangla digits (০-৯) as Bangla and English digits as English.\n"
    "- Do not correct spelling or grammar. If a word is unreadable write [অস্পষ্ট].\n"
    "- Output only the transcribed text, with no commentary or markdown fences."
)

app = FastAPI(title="Bangla Scanner")
_client = None


def get_client():
    global _client
    if _client is None:
        key = os.getenv("GEMINI_API_KEY")
        if not key:
            raise HTTPException(500, "GEMINI_API_KEY is not set on the server")
        _client = genai.Client(api_key=key)
    return _client


def prep_image(data: bytes) -> bytes:
    """Fix rotation, shrink big phone photos, return JPEG bytes."""
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    img.thumbnail((2400, 2400))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/api/ocr")
async def ocr(files: list[UploadFile] = File(...)):
    if not files or len(files) > MAX_FILES:
        raise HTTPException(400, f"Upload 1 to {MAX_FILES} files")

    parts, total = [], 0
    for f in files:
        data = await f.read()
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise HTTPException(413, "Files too large (max ~18 MB total)")
        if f.content_type == "application/pdf":
            parts.append(types.Part.from_bytes(data=data, mime_type="application/pdf"))
        else:
            try:
                jpg = await asyncio.to_thread(prep_image, data)
            except Exception:
                raise HTTPException(400, f"{f.filename}: not a valid image")
            parts.append(types.Part.from_bytes(data=jpg, mime_type="image/jpeg"))

    try:
        resp = await get_client().aio.models.generate_content(
            model=MODEL,
            contents=[*parts, PROMPT],
            config=types.GenerateContentConfig(system_instruction=SYSTEM, temperature=0),
        )
    except HTTPException:
        raise
    except Exception as e:
        log.exception("Gemini error")
        raise HTTPException(502, f"Gemini request failed: {e}")

    return {"text": (resp.text or "").strip()}


class DocxIn(BaseModel):
    text: str


@app.post("/api/docx")
def to_docx(body: DocxIn):
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Nirmala UI"  # Windows Bangla font; Word falls back elsewhere
    style.font.size = Pt(12)
    for line in body.text.split("\n"):
        doc.add_paragraph(line)
    buf = io.BytesIO()
    doc.save(buf)
    return Response(
        buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": 'attachment; filename="scan.docx"'},
    )


app.mount("/", StaticFiles(directory="static", html=True), name="static")
