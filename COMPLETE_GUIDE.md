# Local PDF Suite — Complete Beginner Guide

**Version:** 1.5.0  
**GitHub (public):** https://github.com/abrokwah07/PDFsuite  
**What this is:** A private, on-your-computer PDF toolbox. Files stay on the machine. Nothing is uploaded to Google, Adobe cloud, or any third party.

This document is written so **anyone** can pick it up — even if they have never coded — and understand:

1. What the project does  
2. Every major feature / button  
3. How to install and run it  
4. How to deploy it for others  
5. Where things live on disk  
6. What to do when something breaks  

---

## 1. The big idea (one paragraph)

**Local PDF Suite** is a website that runs **on your computer** (or a company server you control). You open it in a browser (Chrome, Safari, Dia, etc.), upload PDFs (or Word/Excel/PowerPoint), and use tools: convert, merge, OCR, password-protect, compress, edit text, and more. Because processing happens locally, it is suitable for **bank letters, salary schedules, HR docs, and other confidential files**.

Think of it as:  
**“A private mini–Adobe Acrobat / iLovePDF that never leaves your building.”**

---

## 2. Who this is for

| Person | Why they care |
|--------|----------------|
| **You (owner)** | You built / maintain it; this is the full map |
| **Colleague** | Can run it from GitHub or a backup zip with this guide |
| **IT / admin** | Can deploy with Docker and harden with API key |
| **Business user** | Only needs “how to use the buttons” (Section 5) |

---

## 3. Important links & locations on this Mac

| Item | Location |
|------|----------|
| **Live project folder** | `/Users/abrokwah/Downloads/pdf suite` |
| **GitHub** | https://github.com/abrokwah07/PDFsuite |
| **Source-only backup** | `~/Documents/PDFSuite-Backups/PDFsuite-backup-*.zip` |
| **Full backup (with Python packages)** | `~/Documents/PDFSuite-Backups/PDFsuite-full-backup-*.zip` |
| **This guide (in project)** | `COMPLETE_GUIDE.md` |
| **This guide (Documents copy)** | `~/Documents/PDFSuite-Backups/Local_PDF_Suite_Complete_Guide.md` |
| **Audit log (created when app runs)** | `pdf suite/data/audit.jsonl` |

**App URL when running locally:**  
http://127.0.0.1:8000

---

## 4. What you need on the computer (requirements)

### Required

- A computer (Mac or Linux preferred; Windows works with small path differences)
- **Python 3.11 or newer** (3.12+ recommended)
- About **1 GB free disk** for the virtual environment

### Optional but strongly recommended (for full power)

| Tool | What it unlocks | How you know it’s there |
|------|-----------------|-------------------------|
| **Tesseract** | OCR (make scans searchable / better convert) | Sidebar badge “OCR (Tesseract)” green |
| **Ghostscript** (`gs`) | Strong PDF compression | “Ghostscript” badge green |
| **LibreOffice** (`soffice`) | Word/Excel/PowerPoint → PDF and cross-office convert | “LibreOffice” badge green |

On this Mac they are typically installed via Homebrew. If a badge is orange/red, that feature is limited until the tool is installed.

---

## 5. How to USE the app (no coding)

### 5.1 Start the app (easiest)

1. Open **Terminal** (Mac: Spotlight → type `Terminal`).
2. Paste this and press Enter:

```bash
cd "/Users/abrokwah/Downloads/pdf suite"
./run.sh
```

3. Open a browser and go to: **http://127.0.0.1:8000**
4. You should see the **PDFSuite** sidebar and tools.

To stop: press `Ctrl + C` in the Terminal window.

### 5.2 The screen layout

| Area | Purpose |
|------|---------|
| **Left sidebar** | List of tools + search + status lights |
| **Middle panel** | Options for the selected tool + Upload + Run button |
| **Right panel** | PDF page preview (when relevant) |
| **Footer badges** | Ghostscript / OCR / LibreOffice ready or missing |
| **Progress popup** | Shows % while a long job runs; Cancel for job-based tasks |

### 5.3 Keyboard shortcuts

| Shortcut | Action |
|----------|--------|
| `/` | Focus tool search in the sidebar |
| **⌘K** (Mac) or **Ctrl+K** (Windows) | Open **command palette** — jump to any tool or recent job |

### 5.4 Every tool explained (plain English)

#### Convert group

| Tool | What it does | Typical use |
|------|--------------|-------------|
| **Smart Convert** | Detects file type; you pick output (Word, Excel, PDF, PPTX…) | Everyday conversion |
| **Workflow presets** | One-click recipes for bank/HR | Salary PDF → Excel; scan → Word |
| **Batch / folder** | Many PDFs at once → zip of results, or one merged PDF | End-of-month folder of letters |
| **Job history** | List of recent jobs; re-download while still on server | “I closed the download, get it again” |
| **Audit log** | Local diary of what ran (filename + action) | Accountability / troubleshooting |
| **Shrink Office files** | Compress images inside .docx / .pptx | Huge PowerPoints |

#### PDF tools

| Tool | What it does |
|------|----------------|
| **Organize pages** | Rotate / delete pages visually |
| **Merge** | Combine several PDFs into one |
| **Extract pages** | Pull out page ranges (e.g. 1–3, 7) |
| **Compress PDF** | Make PDF smaller (needs Ghostscript for best results) |

#### Edit

| Tool | What it does |
|------|----------------|
| **Content editor / stamp** | Replace, redact, or add text on pages (page-relative, not full Acrobat reflow) |

#### Security

| Tool | What it does |
|------|----------------|
| **Unlock** | Remove PDF password (if you know it) |
| **Protect** | Add open/owner password, restrict print/copy/modify |
| **Watermark** | Stamp text across pages |
| **Scrub** | Strip metadata |
| **Searchable PDF (OCR)** | Run OCR so scanned pages become selectable text |

### 5.5 Workflow presets (bank / HR)

1. Open **Workflow presets**.  
2. Choose one or more PDFs.  
3. Click a preset card:

| Preset | Result |
|--------|--------|
| **Salary schedule → Excel** | Employee table → spreadsheet |
| **Scan → OCR → Word** | Scanned letter → editable Word (+ tables when found) |
| **Scan → Searchable PDF** | Same PDF, but text layer added |
| **Bank pack → Word + Excel** | One PDF → both Word and Excel inside a **zip** |
| **Merge many PDFs** | Several PDFs → one PDF |

### 5.6 Batch / folder jobs

1. Open **Batch / folder**.  
2. Either multi-select PDFs **or** pick a whole folder.  
3. Choose action: Excel / Word / OCR / Word+Excel pack / Merge.  
4. Optional page range (empty = all pages).  
5. **Run batch** → progress bar → download (usually a **.zip**).

### 5.7 Table preview (catch OCR mistakes)

1. Open **Smart Convert**, pick a PDF.  
2. Click **Preview tables**.  
3. Check rows/columns.  
4. If good → convert to Excel. If bad → run **OCR** first, then preview again.

### 5.8 Dark mode & print

- Use the moon/sun button (top of sidebar) for dark mode.  
- Table preview has a **Print** control; print styles hide the chrome and print the table cleanly.

### 5.9 Example: salary letter like “Salaries Carpenter.pdf”

1. Start the app.  
2. **Workflow presets** → upload the PDF → **Salary schedule → Excel**.  
   *Or* Smart Convert → Excel, with **Preview tables** first.  
3. Download the `.xlsx`.  
4. Optionally **Scan → OCR → Word** for the cover letter as editable text.  
5. Check **Job history** if you need to re-download within ~2 hours.

---

## 6. How conversion works under the hood (still in plain language)

When you convert a **scanned** PDF to Excel or Word, the app:

1. **Checks text quality** — empty / weak / good  
2. **Auto-OCR** if pages look like images with little text (capped, default 40 pages for auto)  
3. **Finds tables** using several methods (grid lines, text columns, salary-line parser)  
4. **Scores** tables and keeps the best (drops logo/stamp garbage)  
5. If no table: Excel still gets a **text lines** sheet so you are not empty-handed  
6. Word keeps paragraphs and inserts **real tables** when a schedule is detected  

That is why v1.4+ works much better on bank salary schedules than a dumb “dump all text into cells” converter.

---

## 7. Install from scratch (any computer)

### Option A — From GitHub (recommended)

```bash
git clone https://github.com/abrokwah07/PDFsuite.git
cd PDFsuite
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # optional fine-tuning
./run.sh                           # or: python main.py
```

Open http://127.0.0.1:8000

### Option B — From the full zip backup on this Mac

```bash
cd ~/Documents/PDFSuite-Backups
unzip PDFsuite-full-backup-YYYYMMDD-HHMMSS.zip -d ~/Desktop/
cd ~/Desktop/pdf\ suite
source .venv/bin/activate
./run.sh
```

(If the venv breaks on another machine, recreate it: delete `.venv`, then follow Option A steps from `python3 -m venv`.)

### Option C — Docker (good for servers)

```bash
cd PDFsuite   # or "pdf suite"
docker compose up --build -d
```

- App: http://localhost:8000  
- Health: http://localhost:8000/health  

Docker already packages many system tools; still depends on the image definition in `Dockerfile`.

### Install optional engines on Mac (Homebrew)

```bash
brew install tesseract ghostscript
# LibreOffice: install from https://www.libreoffice.org/ or brew install --cask libreoffice
```

---

## 8. Deploy for other people (office / LAN)

### Safe minimal deployment

1. Run on a machine you control (not a random public VPS without hardening).  
2. Copy project or pull from GitHub.  
3. Create `.env` from `.env.example`.  
4. Set at least:

```bash
APP_ENV=production
HOST=0.0.0.0
PORT=8000
API_KEY=pick-a-long-random-secret
ALLOW_ORIGINS=http://your-internal-url:8000
```

5. Start with Docker Compose **or** a process manager (systemd, launchd) running `uvicorn` / `./run.sh`.  
6. Prefer a reverse proxy (nginx/Caddy) with **HTTPS** in front.  
7. Tell users: open the URL; if `API_KEY` is set, the UI/API may need that key on API calls (header `X-API-Key`).

### What “production” changes

- Interactive API docs (`/docs`) are **off** when `APP_ENV=production`.  
- Use `APP_ENV=development` only while debugging on a trusted machine.

### Privacy reminder for deployers

All processing is local to that host. Do **not** put the server on the open internet without authentication, TLS, and network controls. Salary and bank PDFs are sensitive.

---

## 9. Configuration cheat sheet (`.env`)

Copy `.env.example` → `.env`. Important knobs:

| Variable | Default | Meaning |
|----------|---------|---------|
| `APP_ENV` | production | `development` turns on `/docs` |
| `HOST` | 127.0.0.1 | Use `0.0.0.0` to listen on the network |
| `PORT` | 8000 | Web port |
| `MAX_UPLOAD_BYTES` | 100 MB | Max size per file |
| `MAX_BATCH_FILES` | 40 | Batch job file cap |
| `MAX_OCR_PAGES` | 200 | Hard OCR ceiling |
| `MAX_AUTO_OCR_PAGES` | 40 | Auto-OCR during convert |
| `JOB_HISTORY_TTL_SECONDS` | 7200 | ~2 hours re-download window |
| `JOB_HISTORY_MAX` | 100 | Max jobs kept in memory |
| `MAX_CONCURRENT_JOBS` | 2 | Heavy jobs in parallel |
| `API_KEY` | empty | If set, required for `/api/*` |
| `ENABLE_OCR` / `ENABLE_GHOSTSCRIPT` / `ENABLE_LIBREOFFICE` | true | Feature switches |

---

## 10. Project map (folders & important files)

```
pdf suite/   (or PDFsuite/ from GitHub)
├── main.py                 # All web routes / API / job workers
├── index.html              # The entire browser UI
├── requirements.txt        # Python libraries
├── run.sh                  # One-command start script
├── Dockerfile              # Container build
├── docker-compose.yml      # Easy Docker run
├── .env.example            # Sample settings
├── COMPLETE_GUIDE.md       # THIS document
├── README.md               # Short technical readme
├── tests/                  # Automated tests
├── data/                   # Created at runtime (audit log) — not committed
└── app/
    ├── config.py           # Reads environment settings
    ├── conversion.py       # Word/Excel/OCR/table intelligence
    ├── presets.py          # Bank/HR one-click recipes
    ├── audit.py            # Local audit log writer
    ├── jobs.py             # Background job store + history
    ├── security.py         # Upload validation, zip safety
    ├── middleware.py       # Request IDs, rate limit, API key
    ├── pdf_editor.py       # Visual content edits
    ├── pptx_convert.py     # PowerPoint / office convert helpers
    ├── office_compress.py  # Shrink docx/pptx
    ├── tempfiles.py        # Temp files + download cleanup rules
    ├── job_limit.py        # Concurrency + rate limiting
    └── subprocess_util.py  # Timed Ghostscript / LibreOffice calls
```

---

## 11. API overview (for developers / integrators)

Base URL: `http://127.0.0.1:8000`

### Health

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | Web UI |
| GET | `/health` | Is the process alive? |
| GET | `/ready` | Engines + feature flags + limits |

### Jobs & history

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/jobs` | List recent jobs |
| GET | `/api/jobs/{id}` | Job status / progress |
| POST | `/api/jobs/{id}/cancel` | Cancel running job |
| GET | `/api/jobs/{id}/download` | Download result (kept until TTL) |
| POST | `/api/jobs/batch` | Batch many PDFs |
| POST | `/api/jobs/preset` | Run a named preset |
| POST | `/api/jobs/convert/to-word` | Background PDF → Word |
| POST | `/api/jobs/convert/to-excel` | Background PDF → Excel |
| POST | `/api/jobs/convert/to-pptx` | Background PDF → PowerPoint |
| POST | `/api/jobs/convert/office` | Background Office cross-convert |

### Convert & preview

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/convert/detect` | What can this filename convert to? |
| POST | `/api/convert/preview-tables` | JSON table preview (no download) |
| POST | `/api/convert/to-word` | Sync PDF → Word |
| POST | `/api/convert/to-excel` | Sync PDF → Excel |
| POST | `/api/convert/to-pdf` | Office → PDF |

### PDF operations

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/merge` | Merge PDFs |
| POST | `/api/split` | Extract pages |
| POST | `/api/compress` | Compress PDF |
| POST | `/api/ocr` | Force OCR |
| POST | `/api/protect` | Encrypt / restrict |
| POST | `/api/unlock` | Decrypt with password |
| POST | `/api/watermark` | Watermark |
| POST | `/api/scrub` | Strip metadata |
| POST | `/api/preview` | Page preview images |
| POST | `/api/modify-pages` | Rotate/delete pages |
| POST | `/api/edit/inspect` | Inspect text for editor |
| POST | `/api/edit/apply` | Apply text edits |
| POST | `/api/compress-office` | Shrink Office files |

### Meta

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/presets` | List workflow presets |
| GET | `/api/audit` | Read local audit events |

If `API_KEY` is set, send:

```http
X-API-Key: your-secret
```

or

```http
Authorization: Bearer your-secret
```

In **development** mode, interactive docs: http://127.0.0.1:8000/docs  

---

## 12. Tests (quality check)

```bash
cd "/Users/abrokwah/Downloads/pdf suite"
source .venv/bin/activate
APP_ENV=development pytest -q
```

You want to see all tests **passed**. That means core APIs and conversion helpers still work after changes.

---

## 13. Git workflow used on this project

Repository: **https://github.com/abrokwah07/PDFsuite** (public)

Notable releases:

| Version | Theme |
|---------|--------|
| **1.4.0** | Reliable Word/Excel for scanned salary PDFs (table parser, scoring, OCR rescue) |
| **1.5.0** | Batch, presets, table preview, job history, audit log, ⌘K palette, engine badges |

Typical commands for the maintainer:

```bash
cd "/Users/abrokwah/Downloads/pdf suite"
git status
git add ...
git commit -m "message"
git push origin main
```

---

## 14. Troubleshooting (common problems)

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Page won’t open | App not running | Run `./run.sh` again |
| “Partial” status / orange badges | Missing Tesseract / Ghostscript / LibreOffice | Install tools; restart app |
| Excel is messy / wrong columns | Bad scan / weak OCR | Run **OCR** first, then **Preview tables**, then Excel |
| OCR fails | Tesseract or ocrmypdf missing | `brew install tesseract`; reinstall Python deps |
| Compress does little | No Ghostscript | Install `gs` |
| Office → PDF fails | No LibreOffice | Install LibreOffice; ensure `soffice` on PATH |
| Job download says expired | Past ~2 hour TTL | Run the job again |
| Upload rejected | File too large or wrong type | Check size limit; use PDF/Office types |
| 401 on API | API key required | Send `X-API-Key` header or unset `API_KEY` for local-only |
| Port already in use | Something else on 8000 | Change `PORT` in `.env` |

---

## 15. Security & privacy (non-negotiables)

- Files are processed **on the host** running the app.  
- Do not deploy to the public internet without **API key**, **HTTPS**, and network restriction.  
- Temp files are cleaned after jobs (history results last until TTL).  
- Audit log stores **filenames and actions**, not full document contents — still treat the host as sensitive.  
- Never commit a real `.env` with secrets to GitHub.

---

## 16. Limits to be honest about

- OCR is not perfect; always **preview tables** for salary runs.  
- Content editor is **not** full Adobe reflow — coordinates are page-based.  
- Huge multi-hundred-page OCR jobs should be split; respect `MAX_OCR_PAGES` / `MAX_AUTO_OCR_PAGES`.  
- Login page for the UI was discussed but **not implemented yet** (API key protects `/api/*` only when set).

---

## 17. Day-one checklist for a new person

1. [ ] Clone GitHub **or** unzip backup  
2. [ ] Create venv + `pip install -r requirements.txt` (unless using full backup venv)  
3. [ ] Optional: install Tesseract, Ghostscript, LibreOffice  
4. [ ] Run `./run.sh`  
5. [ ] Open http://127.0.0.1:8000  
6. [ ] Confirm green/ready badges in sidebar  
7. [ ] Try **Smart Convert** with a sample PDF  
8. [ ] Try **Preview tables** on a salary schedule  
9. [ ] Try one **Workflow preset**  
10. [ ] Note **Job history** and **Audit log** after a successful run  

---

## 18. Glossary (jargon → English)

| Term | Meaning |
|------|---------|
| **PDF** | Portable Document Format — the usual “document print file” |
| **OCR** | Optical Character Recognition — computer reading text from images/scans |
| **API** | How programs talk to the server (the UI uses these under the hood) |
| **venv** | Private Python bubble so packages don’t mess up the rest of the Mac |
| **Docker** | Box that runs the app with its dependencies isolated |
| **Job** | Background task with progress bar and optional cancel |
| **TTL** | Time to live — how long a downloadable result is kept |
| **Preset** | Saved recipe of steps (e.g. salary → Excel) |
| **Batch** | Do the same action on many files at once |
| **Audit log** | Local list of who/what ran (here: actions + filenames) |

---

## 19. Contact / ownership notes

- GitHub owner path used for this work: **abrokwah07/PDFsuite**  
- Project name in UI: **Local PDF Suite / PDFSuite**  
- Intended use: **internal / private** document processing  

If you are reading this years later: open the GitHub repo README for the latest short notes, and keep this guide for the full story.

---

## 20. Version history of this guide

| Date | Guide covers |
|------|----------------|
| 2026-08-05 | App **v1.5.0** — batch, presets, history, audit, preview, palette, badges, scan conversion |

---

**End of guide.**  
You should now be able to run, use, explain, and hand this project to someone else without tribal knowledge.
