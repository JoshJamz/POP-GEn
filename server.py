#!/usr/bin/env python3
"""POP Assistant local web server.

Serves the browser UI and extracts payment fields from user-supplied PDF/text
files. Image OCR is enabled automatically when the optional `tesseract`
command is installed.
"""
from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
MAX_UPLOAD = int(os.getenv("MAX_UPLOAD_BYTES", str(15 * 1024 * 1024)))


def load_local_env() -> None:
    """Load simple KEY=VALUE entries from a local, git-ignored .env file."""
    env_file = ROOT / ".env"
    if not env_file.is_file():
        return
    for raw_line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip().lstrip("\ufeff")
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value:
            os.environ.setdefault(key, value)


load_local_env()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")

FIELD_ALIASES = {
    "currency": ["Currency sent", "Transfer currency", "Payment currency", "Currency"],
    "amount": ["Amount paid", "Amount sent", "Transfer amount", "Payment amount", "Amount"],
    "fee": ["Transfer fee", "Payment fee", "Charges", "Fee"],
    "method": ["Payment method", "Transfer method", "Payment type", "Method"],
    "transactionDate": ["Transaction date", "Payment date", "Transfer date", "Value date"],
    "transactionId": ["Transaction ID", "Transaction reference", "Payment reference", "Transfer ID", "Reference ID", "Reference"],
    "uetr": ["UETR", "Unique end-to-end transaction reference"],
    "narrative": ["Statement narrative", "Payment purpose", "Description", "Narrative", "Purpose"],
    "remitterName": ["Remitter name", "Originator name", "Payer name", "Account holder"],
    "remitterAddress": ["Remitter address", "Originator address", "Payer address"],
    "senderName": ["Ultimate sender", "Sender name"],
    "senderAddress": ["Sender address"],
    "recipientName": ["Recipient name", "Beneficiary name", "Payee name", "Recipient", "Beneficiary"],
    "bankCountry": ["Beneficiary bank country", "Bank country"],
    "bankName": ["Beneficiary bank name", "Beneficiary bank", "Recipient bank", "Bank name"],
    "account": ["Account Number/IBAN", "Account number or IBAN", "Account number", "IBAN"],
    "status": ["Payment status", "Transaction status", "Status"],
}
LABELS = sorted({label for aliases in FIELD_ALIASES.values() for label in aliases}, key=len, reverse=True)


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip(" \t\r\n:|")


def first_match(text: str, patterns: list[str]) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
        if match:
            return clean(match.group(1))
    return ""


def labelled_value(text: str, label: str) -> str:
    """Return a same-line label value, stopping before another known label."""
    labels = "|".join(re.escape(item) for item in LABELS if item.lower() != label.lower())
    pattern = rf"\b{re.escape(label)}\b[ \t]*:?[ \t]*(.*?)(?=[ \t]+(?:{labels})[ \t]*:?|\n|$)"
    return first_match(text, [pattern])


def labelled_any(text: str, field: str) -> str:
    for alias in FIELD_ALIASES[field]:
        value = labelled_value(text, alias)
        if value:
            return value
    return ""


def parse_payment_text(raw_text: str) -> dict[str, str]:
    # Preserve line boundaries while normalizing odd PDF spacing.
    text = raw_text.replace("\u00a0", " ").replace("\x00", "")
    lines = [clean(line) for line in text.splitlines() if clean(line)]
    joined = "\n".join(lines)

    fields = {
        "currency": labelled_any(joined, "currency"),
        "amount": labelled_any(joined, "amount"),
        "fee": labelled_any(joined, "fee"),
        "method": labelled_any(joined, "method"),
        "transactionDate": labelled_any(joined, "transactionDate"),
        "transactionId": labelled_any(joined, "transactionId"),
        "uetr": labelled_any(joined, "uetr"),
        "narrative": labelled_any(joined, "narrative"),
        "remitterName": labelled_any(joined, "remitterName"),
        "remitterAddress": labelled_any(joined, "remitterAddress"),
        "senderName": labelled_any(joined, "senderName"),
        "senderAddress": labelled_any(joined, "senderAddress"),
        "recipientName": labelled_any(joined, "recipientName"),
        "bankCountry": labelled_any(joined, "bankCountry"),
        "bankName": labelled_any(joined, "bankName"),
        "account": labelled_any(joined, "account"),
        "status": "",
        "summaryDate": "",
    }

    # Common summary layouts: labels on one line, values on the next.
    for index, line in enumerate(lines):
        if "amount sent" in line.lower() and "status" in line.lower() and "date" in line.lower():
            if index + 1 < len(lines):
                value_line = lines[index + 1]
                status_match = re.search(r"\b(Completed|Pending|Processing|Failed|Cancelled|Canceled)\b", value_line, re.I)
                date_match = re.search(r"\b\d{1,4}[/-]\d{1,2}[/-]\d{1,4}\b", value_line)
                if status_match:
                    fields["status"] = status_match.group(1).title()
                    amount_part = value_line[:status_match.start()].strip()
                    if not fields["amount"]:
                        fields["amount"] = clean(amount_part)
                if date_match:
                    fields["summaryDate"] = date_match.group(0)
            break

    if not fields["status"]:
        raw_status = labelled_any(joined, "status")
        fields["status"] = first_match(raw_status, [r"\b(Completed|Pending|Processing|Failed|Cancelled|Canceled)\b"])
    if not fields["status"]:
        fields["status"] = first_match(joined, [r"\b(Completed|Pending|Processing|Failed|Cancelled|Canceled)\b"])
    if not fields["summaryDate"]:
        fields["summaryDate"] = fields["transactionDate"]

    # UETR must be a UUID; discard a spillover label value.
    if fields["uetr"] and not re.fullmatch(r"[0-9a-fA-F-]{32,36}", fields["uetr"]):
        fields["uetr"] = ""

    # Do not treat a section heading or underscore divider as a value.
    for key, value in list(fields.items()):
        if value.startswith("_") or value.upper() in {"PAYMENT DETAILS", "REMITTER DETAILS", "ULTIMATE SENDER", "RECIPIENT DETAILS"}:
            fields[key] = ""

    return fields


GEMINI_FIELD_KEYS = [
    "currency", "amount", "fee", "method", "transactionDate", "transactionId",
    "uetr", "narrative", "remitterName", "remitterAddress", "senderName",
    "senderAddress", "recipientName", "bankCountry", "bankName", "account",
    "status", "summaryDate",
]


def gemini_extract_fields(filename: str, data: bytes, mime_type: str) -> dict[str, str]:
    """Extract fields directly from the source with Gemini structured output."""
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Gemini is not configured")
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise RuntimeError("Gemini support is not installed. Run: pip install -r requirements.txt") from exc

    properties = {
        key: {
            "type": "string",
            "description": "Exact value visible in the source document, or an empty string when absent.",
        }
        for key in GEMINI_FIELD_KEYS
    }
    schema = {
        "type": "object",
        "properties": properties,
        "required": GEMINI_FIELD_KEYS,
        "additionalProperties": False,
    }
    prompt = """
You are a strict payment-document data extraction system. Read the attached source and return the requested fields.
Rules:
- Extract only values explicitly visible in the source. Never guess, calculate, complete, or invent a value.
- Preserve names, identifiers, account numbers, punctuation, currency, amount formatting, and dates exactly as shown.
- Use an empty string for any field that is absent, obscured, or uncertain.
- remitterName/remitterAddress mean the account remitter, originator, or payer.
- senderName/senderAddress mean the ultimate sender. If no separate ultimate sender is shown, leave them empty.
- recipientName means beneficiary or payee.
- transactionId means the provider's transaction ID/reference. UETR is separate.
- summaryDate should be the displayed payment-summary date; if none is separately displayed, use the explicit transaction date.
- Do not treat instructions, examples, email addresses, page numbers, or generic processing notices as payment details.
- A status may be returned only when the source explicitly shows it.
Return JSON matching the schema only.
""".strip()

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=os.getenv("GEMINI_MODEL", GEMINI_MODEL),
        contents=[
            types.Part.from_bytes(data=data, mime_type=mime_type),
            prompt,
        ],
        config={
            "response_mime_type": "application/json",
            "response_json_schema": schema,
            "temperature": 0,
        },
    )
    if not response.text:
        raise RuntimeError("Gemini returned an empty result")
    try:
        payload = json.loads(response.text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Gemini returned an invalid structured result") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Gemini returned an unexpected result")
    return {key: clean(str(payload.get(key, ""))) for key in GEMINI_FIELD_KEYS}


def rapidocr_image(data: bytes, ext: str = ".png") -> tuple[str, float]:
    """Read an image with the bundled local AI OCR engine.

    Results are regrouped by visual row so a label detected in one box and its
    value detected in another become a single parseable line.
    """
    try:
        from rapidocr import RapidOCR
    except ImportError as exc:
        raise RuntimeError("Local AI OCR is not installed") from exc

    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / ("source" + ext)
        source.write_bytes(data)
        output = RapidOCR()(str(source))

    boxes = getattr(output, "boxes", None)
    texts = getattr(output, "txts", None)
    output_scores = getattr(output, "scores", None)
    if boxes is None or texts is None or output_scores is None:
        return "", 0.0
    result = zip(boxes, texts, output_scores)

    items = []
    scores = []
    for box, text, score in result:
        if not clean(str(text)):
            continue
        xs = [float(point[0]) for point in box]
        ys = [float(point[1]) for point in box]
        item = {
            "x": min(xs), "y": sum(ys) / len(ys),
            "height": max(ys) - min(ys), "text": clean(str(text)),
        }
        items.append(item)
        scores.append(float(score))

    groups: list[dict] = []
    for item in sorted(items, key=lambda value: (value["y"], value["x"])):
        match = None
        for group in reversed(groups[-4:]):
            tolerance = max(8.0, item["height"] * 0.60, group["height"] * 0.60)
            if abs(item["y"] - group["y"]) <= tolerance:
                match = group
                break
        if match is None:
            groups.append({"y": item["y"], "height": item["height"], "items": [item]})
        else:
            match["items"].append(item)
            match["y"] = sum(part["y"] for part in match["items"]) / len(match["items"])
            match["height"] = max(part["height"] for part in match["items"])

    lines = []
    for group in sorted(groups, key=lambda value: value["y"]):
        row = " ".join(part["text"] for part in sorted(group["items"], key=lambda value: value["x"]))
        lines.append(clean(row))
    confidence = sum(scores) / len(scores) if scores else 0.0
    return "\n".join(lines), confidence


def find_tesseract() -> str | None:
    configured = os.getenv("TESSERACT_CMD", "").strip().strip('"')
    candidates = [
        configured,
        shutil.which("tesseract") or "",
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        str(Path.home() / "AppData/Local/Programs/Tesseract-OCR/tesseract.exe"),
    ]
    return next((item for item in candidates if item and Path(item).is_file()), None)


def tesseract_image(data: bytes, ext: str = ".png") -> str:
    tesseract = find_tesseract()
    if not tesseract:
        raise RuntimeError("Tesseract OCR is not installed")
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / ("source" + ext)
        source.write_bytes(data)
        result = subprocess.run(
            [tesseract, str(source), "stdout", "--psm", "6"],
            capture_output=True, text=True, timeout=60, check=False,
        )
        if result.returncode:
            raise RuntimeError("Tesseract could not read this image")
        return result.stdout


def ocr_image(data: bytes, ext: str = ".png") -> tuple[str, str, float]:
    """Use bundled local AI OCR first, then system Tesseract as a fallback."""
    try:
        text, confidence = rapidocr_image(data, ext)
        if clean(text):
            return text, "Local AI OCR", confidence
    except Exception as exc:
        print("Local AI OCR unavailable:", repr(exc))
    try:
        text = tesseract_image(data, ext)
        if clean(text):
            return text, "Tesseract OCR", 0.0
    except Exception as exc:
        print("Tesseract unavailable:", repr(exc))
    return "", "No OCR engine", 0.0


def extract_text(filename: str, data: bytes) -> tuple[str, list[str], str, float]:
    ext = Path(filename).suffix.lower()
    warnings: list[str] = []
    if ext == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RuntimeError("PDF support is not installed. Run: pip install -r requirements.txt") from exc
        reader = PdfReader(io.BytesIO(data))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        if clean(text):
            return text, warnings, "PDF text extraction", 1.0

        # Scanned PDFs have no text layer. Render up to three pages and OCR them.
        try:
            import pymupdf as fitz
            document = fitz.open(stream=data, filetype="pdf")
            parts, confidences, engines = [], [], []
            for page in list(document)[:3]:
                pixmap = page.get_pixmap(matrix=fitz.Matrix(2.2, 2.2), alpha=False)
                page_text, engine, confidence = ocr_image(pixmap.tobytes("png"), ".png")
                if page_text:
                    parts.append(page_text)
                    confidences.append(confidence)
                    engines.append(engine)
            if parts:
                warnings.append("This scanned PDF was read with local AI OCR. Carefully verify every extracted field.")
                return "\n".join(parts), warnings, engines[0], (sum(confidences) / len(confidences) if confidences else 0.0)
        except ImportError:
            warnings.append("Scanned-PDF OCR is not available in this deployment. Upload a text-based PDF, enable Gemini AI extraction, or enter the fields manually.")
        except Exception as exc:
            print("Scanned PDF OCR failed:", repr(exc))
        warnings.append("No readable text was found in this PDF. Try a clearer text-based file, enable Gemini AI extraction, or enter the fields manually.")
        return "", warnings, "No OCR engine", 0.0

    if ext in {".txt", ".csv"}:
        return data.decode("utf-8", errors="replace"), warnings, "Text extraction", 1.0

    if ext in {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"}:
        text, engine, confidence = ocr_image(data, ext)
        if text:
            confidence_note = f" (average confidence {confidence:.0%})" if confidence else ""
            warnings.append(f"Image text was read with {engine}{confidence_note}. Carefully verify every extracted field.")
            return text, warnings, engine, confidence
        warnings.append("No OCR engine is available in this deployment. Upload a text-based PDF, enable Gemini AI extraction, or enter the fields manually.")
        return "", warnings, engine, confidence

    raise ValueError("Unsupported file type. Upload PDF, PNG, JPG, WEBP, TXT, or CSV.")


class Handler(BaseHTTPRequestHandler):
    server_version = "POPAssistant/1.0"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path == "/api/health":
            self.send_json({
                "ok": True,
                "service": "POP Assistant",
                "geminiConfigured": bool(os.getenv("GEMINI_API_KEY", "").strip()),
                "geminiModel": os.getenv("GEMINI_MODEL", GEMINI_MODEL),
            })
            return
        if path == "/":
            path = "/index.html"
        candidate = (STATIC / path.lstrip("/")).resolve()
        try:
            candidate.relative_to(STATIC.resolve())
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        data = candidate.read_bytes()
        ctype = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/extract":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_UPLOAD:
                raise ValueError(f"Upload must be between 1 byte and {MAX_UPLOAD // (1024 * 1024)} MB.")
            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in content_type:
                raise ValueError("Expected a multipart file upload.")
            body = self.rfile.read(length)
            message = BytesParser(policy=default).parsebytes(
                b"Content-Type: " + content_type.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
            )
            parts = list(message.iter_parts())
            part = next((item for item in parts if item.get_param("name", header="content-disposition") == "file"), None)
            if part is None:
                raise ValueError("No file was uploaded.")
            mode_part = next((item for item in parts if item.get_param("name", header="content-disposition") == "useGemini"), None)
            use_gemini = bool(mode_part and (mode_part.get_payload(decode=True) or b"").decode("utf-8", errors="ignore").lower() == "true")
            filename = Path(part.get_filename() or "upload").name
            data = part.get_payload(decode=True) or b""
            if len(data) > MAX_UPLOAD:
                raise ValueError(f"File is larger than {MAX_UPLOAD // (1024 * 1024)} MB.")

            warnings: list[str] = []
            fields: dict[str, str] | None = None
            extraction_engine = ""
            ocr_confidence = 0.0
            if use_gemini:
                if not os.getenv("GEMINI_API_KEY", "").strip():
                    warnings.append("Gemini was requested but is not configured; local extraction was used instead.")
                else:
                    try:
                        mime_type = part.get_content_type() or mimetypes.guess_type(filename)[0] or "application/octet-stream"
                        fields = gemini_extract_fields(filename, data, mime_type)
                        extraction_engine = f"Gemini AI ({os.getenv('GEMINI_MODEL', GEMINI_MODEL)})"
                        warnings.append("This source was sent to the configured Gemini API for extraction. Verify every returned value.")
                    except Exception as exc:
                        print("Gemini extraction failed:", type(exc).__name__, str(exc)[:300])
                        warnings.append("Gemini extraction failed, so the private local extractor was used instead.")

            if fields is None:
                text, local_warnings, extraction_engine, ocr_confidence = extract_text(filename, data)
                warnings.extend(local_warnings)
                fields = parse_payment_text(text)

            populated = sum(bool(value) for value in fields.values())
            review_fields = [
                "currency", "amount", "status", "transactionDate", "transactionId",
                "remitterName", "senderName", "recipientName", "bankName", "account",
            ]
            missing_fields = [key for key in review_fields if not fields.get(key)]
            if populated == 0:
                warnings.append("No payment fields were detected. Try a higher-resolution source or use the guided form.")
            elif missing_fields:
                warnings.append(f"AI extraction found {populated} fields. Review the highlighted gaps before exporting.")
            self.send_json({
                "ok": True,
                "filename": filename,
                "sha256": hashlib.sha256(data).hexdigest(),
                "fields": fields,
                "fieldCount": populated,
                "missingFields": missing_fields,
                "extractionEngine": extraction_engine,
                "ocrConfidence": round(ocr_confidence, 4),
                "warnings": warnings,
            })
        except (ValueError, RuntimeError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
        except Exception as exc:
            print("Extraction error:", repr(exc))
            self.send_json({"ok": False, "error": "The file could not be processed."}, status=500)


def main() -> None:
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"POP Assistant running on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
