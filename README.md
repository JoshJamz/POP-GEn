# POP Assistant — Vercel / GitHub deployment package

This package lets you publish **POP Assistant** (the proof-of-payment form
filler) on **your own GitHub repository** and **your own Vercel account**, so
the public link is permanent and under your control — no tunnels, no
temporary URLs.

## What this package contains

```
├── api/
│   ├── health.py        # GET  /api/health  (service + Gemini status)
│   └── extract.py       # POST /api/extract (file upload + field extraction)
├── public/
│   └── index.html       # the full app UI, served at the site root
├── server.py            # shared extraction logic (imported by api/*.py)
├── requirements.txt     # lightweight Python dependencies
├── vercel.json          # Vercel function settings (memory, 60 s duration)
├── .env.example         # template for optional Gemini settings
├── .gitignore           # keeps secrets and caches out of GitHub
└── README.md            # this file
```

## What works on Vercel — and what does not

| Feature | On Vercel | Notes |
| --- | --- | --- |
| Upload a text-based transaction PDF | ✅ | Extracted locally on the server, no OCR needed |
| Manual review, correction, guided form | ✅ | Fully client-side |
| Live template preview + print / PDF export | ✅ | Fully client-side |
| Optional Gemini AI extraction | ✅ | Set `GEMINI_API_KEY` as a Vercel environment variable |
| Image (PNG/JPG) OCR | ❌ | The local OCR stack (rapidocr + onnxruntime + PyMuPDF) exceeds Vercel's 250 MB function bundle limit, so it is excluded. The app shows a clear message and you can still use Gemini AI extraction or type the fields manually. |
| Scanned-PDF OCR | ❌ | Same reason. Use a text-based PDF, or enable Gemini. |

The full-featured **local version** (with bundled offline OCR that also reads
images and scanned PDFs) is the separate `POP-Assistant-Source-v3-Gemini.zip`
package — run it on your own computer with `start-windows.bat`.

## Deploy steps (GitHub → Vercel)

### 1. Put the files on GitHub

1. Download and unzip this package on your computer.
2. Go to [github.com/new](https://github.com/new) and create a new repository
   (e.g. `pop-assistant`). Choose **Public** or **Private** — your choice.
3. Upload the files: on the repo page, click **Add file → Upload files** and
   drag in everything from the unzipped folder (or use Git if you prefer:
   `git init`, add all files, commit, push).

> ⚠️ **Never upload a real API key.** The `.gitignore` excludes `.env`, and
> `.env.example` contains no secrets. If a key was ever committed, revoke it
> in Google AI Studio and create a new one.

### 2. Publish on Vercel

1. Go to [vercel.com](https://vercel.com) and sign in (or create a free
   account with GitHub).
2. Click **Add New… → Project**.
3. Under **Import Git Repository**, select your `pop-assistant` repo and click
   **Import**. (If you don't see it, click "Adjust GitHub App Permissions".)
4. On the configuration screen, leave the defaults — Vercel detects the
   Python functions in `api/` automatically. Click **Deploy**.
5. Wait for the build to finish (about 1–2 minutes), then click **Visit**.

Your app is now live at `https://<your-project>.vercel.app` — a permanent
public link that redeploys automatically every time you push to GitHub.

### 3. (Optional) Enable Gemini AI extraction

1. Get an API key from [Google AI Studio](https://aistudio.google.com/apikey).
   Use a **newly generated** key — never reuse a key that appeared in a
   screenshot or chat.
2. In Vercel: **Project → Settings → Environment Variables**.
3. Add `GEMINI_API_KEY` with your key (Production + Preview), and optionally
   `GEMINI_MODEL` = `gemini-3.1-flash-lite` (the default).
4. Redeploy (or wait for the next push) — the UI's Gemini toggle will light up.

Without a key, the app simply uses its built-in local extraction and tells
you so — nothing breaks.

## Limits and tips

- **Upload size:** Vercel caps serverless request bodies at **4.5 MB**, so
  the app accepts files up to **4 MB**. Larger PDFs get a clear error message.
- **Duration:** each extraction runs up to **60 seconds** (configured in
  `vercel.json`), which is plenty even for Gemini extraction.
- **Cost:** the free Hobby plan covers personal use; heavy commercial use
  requires a paid plan.
- **Privacy:** every visitor's upload is processed by your Vercel function.
  Files are never stored — only the extracted fields exist in the browser.
  Consider enabling **Project → Settings → Deployment Protection** (password)
  if you don't want the app fully public.
- **Custom domain:** Vercel lets you attach your own domain for free
  (Project → Settings → Domains).

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `ModuleNotFoundError: No module named 'server'` | Rare packaging issue. Add `"includeFiles": "{server.py,requirements.txt}"` to the `api/*.py` entry in `vercel.json` and redeploy. |
| `413` error on upload | The file is over the 4.5 MB platform limit. Use a smaller PDF. |
| `504 FUNCTION_INVOCATION_TIMEOUT` | The extraction took over 60 s (very rare; usually a slow Gemini call). Retry, or raise `maxDuration` in `vercel.json` (paid plans allow more). |
| Gemini toggle shows "off" | `GEMINI_API_KEY` is not set in Vercel, or the deployment predates the variable. Redeploy after adding it. |
| Image upload says OCR is unavailable | Expected on Vercel — see the table above. Use a text PDF, Gemini, or manual entry. |

## Security reminders

- **Revoke the old Gemini key** that appeared in the earlier screenshot — it
  is compromised. Generate a fresh one in Google AI Studio.
- Never paste API keys into chat, screenshots, or GitHub. Use Vercel
  environment variables only.
- The app never writes uploaded files to disk; extraction happens in memory.

## Local development

You can test this exact package locally before pushing:

```bash
pip install -r requirements.txt
python -c "from wsgiref.simple_server import make_server; import api.health"  # import check
```

Or simply run the full local app from the `POP-Assistant-Source-v3-Gemini`
package (`start-windows.bat` on Windows), which includes offline image OCR.
