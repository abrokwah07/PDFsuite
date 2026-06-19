from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pypdf import PdfWriter, PdfReader
from pypdf.constants import UserAccessPermissions
from pdf2docx import Converter
from reportlab.pdfgen import canvas
from typing import List
import base64
import os
import tempfile
import subprocess
import ocrmypdf
import zipfile
import pdfplumber
import pandas as pd
import io
import camelot
import json

app = FastAPI()

# 1. Serve the Frontend HTML
@app.get("/")
async def read_index():
    with open("index.html", "r") as f:
        html_content = f.read()
    return HTMLResponse(content=html_content)

# 2. Handle the PDF Merging
@app.post("/api/merge")
async def merge_pdfs(files: List[UploadFile] = File(...)):
    merger = PdfWriter()
    
    for file in files:
        contents = await file.read()
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_pdf:
            temp_pdf.write(contents)
            temp_path = temp_pdf.name
        
        merger.append(temp_path)
        os.unlink(temp_path)

    output_path = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf").name
    merger.write(output_path)
    merger.close()

    return FileResponse(path=output_path, filename="merged_document.pdf", media_type='application/pdf')

# 3. Handle PDF Compression (Upgraded for Batch Processing & Levels)
@app.post("/api/compress")
async def compress_pdf(files: List[UploadFile] = File(...), level: str = Form("medium")): 
    processed_files = []
    
    # Map the requested compression level to Ghostscript settings
    # /screen = 72 dpi (High compression, low quality)
    # /ebook = 150 dpi (Medium compression, standard quality)
    # /printer = 300 dpi (Low compression, high print quality)
    gs_settings = {
        "high": "/screen",
        "medium": "/ebook",
        "low": "/printer"
    }
    pdf_setting = gs_settings.get(level.lower(), "/ebook")
    
    try:
        for file in files:
            contents = await file.read()
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_in:
                temp_in.write(contents)
                input_path = temp_in.name
            
            output_path = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf").name

            gs_command = [
                "gs", "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.4",
                f"-dPDFSETTINGS={pdf_setting}", "-dNOPAUSE", "-dQUIET", "-dBATCH",
                f"-sOutputFile={output_path}", input_path
            ]
            
            subprocess.run(gs_command, check=True, capture_output=True, text=True)
            os.unlink(input_path) 
            processed_files.append((file.filename, output_path))
            
        if len(processed_files) == 1:
            orig_name, out_path = processed_files[0]
            return FileResponse(path=out_path, filename=f"compressed_{orig_name}", media_type='application/pdf')
            
        zip_path = tempfile.NamedTemporaryFile(delete=False, suffix=".zip").name
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for orig_name, out_path in processed_files:
                zipf.write(out_path, arcname=f"compressed_{orig_name}")
                os.unlink(out_path)

        return FileResponse(path=zip_path, filename="compressed_batch.zip", media_type='application/zip')

    except Exception as e:
        print(f"--- BATCH COMPRESS ERROR ---: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to compress files.")
    
# 4. Handle PDF Splitting / Extraction
@app.post("/api/split")
async def split_pdf(file: UploadFile = File(...), pages: str = Form(...)):
    try:
        page_indices = set()
        for part in pages.replace(" ", "").split(","):
            if "-" in part:
                start, end = part.split("-")
                for p in range(int(start), int(end) + 1):
                    page_indices.add(p - 1) 
            else:
                page_indices.add(int(part) - 1)
        
        sorted_indices = sorted(list(page_indices))

        contents = await file.read()
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_in:
            temp_in.write(contents)
            input_path = temp_in.name

        reader = PdfReader(input_path)
        writer = PdfWriter()
        total_pages = len(reader.pages)

        for idx in sorted_indices:
            if 0 <= idx < total_pages:
                writer.add_page(reader.pages[idx])

        output_path = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf").name
        writer.write(output_path)
        writer.close()
        os.unlink(input_path) 

        return FileResponse(path=output_path, filename=f"extracted_{file.filename}", media_type='application/pdf')
    except Exception as e:
        print(f"--- SPLIT ERROR ---: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to extract pages.")
    
# 5. Handle OCR (Make PDF Searchable)
@app.post("/api/ocr")
async def ocr_pdf(file: UploadFile = File(...)):
    contents = await file.read()
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_in:
        temp_in.write(contents)
        input_path = temp_in.name

    output_path = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf").name

    try:
        ocrmypdf.ocr(input_path, output_path, force_ocr=True)
        os.unlink(input_path) 
        return FileResponse(path=output_path, filename=f"searchable_{file.filename}", media_type='application/pdf')
    except Exception as e:
        print(f"--- OCR ERROR ---: {str(e)}")
        os.unlink(input_path)
        raise HTTPException(status_code=500, detail="OCR processing failed.")
    
# 6. Handle PDF Security (Advanced Permissions)
@app.post("/api/protect")
async def protect_pdf(
    file: UploadFile = File(...), 
    open_password: str = Form(""),
    owner_password: str = Form(""),
    disable_print: bool = Form(False),
    disable_copy: bool = Form(False),
    disable_modify: bool = Form(False)
):
    try:
        contents = await file.read()
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_in:
            temp_in.write(contents)
            input_path = temp_in.name

        reader = PdfReader(input_path)
        writer = PdfWriter()

        for page in reader.pages:
            writer.add_page(page)

        permissions = -1 
        if disable_print:
            permissions &= ~(UserAccessPermissions.PRINT | UserAccessPermissions.PRINT_TO_REPRESENTATION)
        if disable_copy:
            permissions &= ~(UserAccessPermissions.EXTRACT | UserAccessPermissions.EXTRACT_TEXT_AND_GRAPHICS)
        if disable_modify:
            permissions &= ~(UserAccessPermissions.MODIFY | UserAccessPermissions.ADD_OR_MODIFY | UserAccessPermissions.FILL_FORM_FIELDS | UserAccessPermissions.ASSEMBLE_DOC)

        final_owner = owner_password if owner_password else open_password

        writer.encrypt(user_password=open_password, owner_password=final_owner, permissions_flag=permissions)

        output_path = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf").name
        writer.write(output_path)
        writer.close()
        os.unlink(input_path) 

        return FileResponse(path=output_path, filename=f"protected_{file.filename}", media_type='application/pdf')
    except Exception as e:
        print(f"--- SECURITY ERROR ---: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to protect the PDF.")
    
# 7. Handle Office to PDF Conversion
@app.post("/api/convert/to-pdf")
async def convert_to_pdf(file: UploadFile = File(...)):
    try:
        temp_dir = tempfile.mkdtemp()
        input_path = os.path.join(temp_dir, file.filename)
        
        contents = await file.read()
        with open(input_path, "wb") as f:
            f.write(contents)

        subprocess.run([
            "libreoffice", "--headless", "--nologo", "--nofirststartwizard", 
            "--convert-to", "pdf", "--outdir", temp_dir, input_path
        ], check=True, capture_output=True)

        base_name = os.path.splitext(file.filename)[0]
        output_pdf = os.path.join(temp_dir, f"{base_name}.pdf")

        return FileResponse(path=output_pdf, filename=f"{base_name}.pdf", media_type='application/pdf')
    except Exception as e:
        print(f"--- CONVERT TO PDF ERROR ---: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to convert to PDF.")

# 8. Handle PDF to Word Conversion
@app.post("/api/convert/to-word")
async def convert_to_word(file: UploadFile = File(...)):
    try:
        contents = await file.read()
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_in:
            temp_in.write(contents)
            input_path = temp_in.name

        output_docx = tempfile.NamedTemporaryFile(delete=False, suffix=".docx").name

        cv = Converter(input_path)
        cv.convert(output_docx)
        cv.close()
        os.unlink(input_path) 

        base_name = os.path.splitext(file.filename)[0]
        return FileResponse(path=output_docx, filename=f"{base_name}.docx", media_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document')
    except Exception as e:
        print(f"--- CONVERT TO WORD ERROR ---: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to convert PDF to Word.")

# 9. Handle PDF to Excel Conversion (Batch Pipeline with OCR Rescue)
@app.post("/api/convert/to-excel")
async def convert_to_excel(files: List[UploadFile] = File(...)):
    try:
        excel_files = []
        
        for file in files:
            contents = await file.read()
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_in:
                temp_in.write(contents)
                input_path = temp_in.name

            all_tables = []

            # Strategy 1: Camelot
            try:
                tables = camelot.read_pdf(input_path, pages='all', flavor='stream')
                for table in tables:
                    if not table.df.empty:
                        all_tables.append(table.df)
            except Exception:
                pass

            # Strategy 2: pdfplumber fallback
            if not all_tables:
                try:
                    with pdfplumber.open(input_path) as pdf:
                        for page in pdf.pages:
                            tables = page.extract_tables({"vertical_strategy": "text", "horizontal_strategy": "text"})
                            for table in tables:
                                all_tables.append(pd.DataFrame(table))
                except Exception:
                    pass

            # Strategy 3: OCR Rescue
            if not all_tables:
                ocr_path = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf").name
                try:
                    ocrmypdf.ocr(input_path, ocr_path, force_ocr=True, output_type='pdf', deskew=True)
                    with pdfplumber.open(ocr_path) as pdf:
                        settings = {"vertical_strategy": "text", "horizontal_strategy": "text", "snap_tolerance": 5, "join_tolerance": 5}
                        for page in pdf.pages:
                            tables = page.extract_tables(settings)
                            for table in tables:
                                all_tables.append(pd.DataFrame(table))
                except Exception:
                    pass
                finally:
                    if os.path.exists(ocr_path): os.unlink(ocr_path)

            os.unlink(input_path)

            if all_tables:
                for i in range(len(all_tables)):
                    all_tables[i].columns = range(all_tables[i].shape[1])
                
                master_df = pd.concat(all_tables, ignore_index=True)
                master_df.dropna(how='all', inplace=True)

                out_path = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx").name
                master_df.to_excel(out_path, index=False, header=False, sheet_name="Master_Data")
                excel_files.append((f"{os.path.splitext(file.filename)[0]}.xlsx", out_path))

        if not excel_files:
            raise HTTPException(status_code=400, detail="No readable tables found in any files.")

        if len(excel_files) == 1:
            return FileResponse(path=excel_files[0][1], filename=excel_files[0][0], media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

        zip_path = tempfile.NamedTemporaryFile(delete=False, suffix=".zip").name
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for arcname, file_path in excel_files:
                zipf.write(file_path, arcname)
                os.unlink(file_path)

        return FileResponse(path=zip_path, filename="Batch_Excel_Extraction.zip", media_type='application/zip')

    except Exception as e:
        print(f"--- CONVERT TO EXCEL ERROR ---: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to batch convert.")

# 10. Handle Watermarking
@app.post("/api/watermark")
async def watermark_pdf(file: UploadFile = File(...), text: str = Form(...)):
    try:
        contents = await file.read()
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_in:
            temp_in.write(contents)
            input_path = temp_in.name

        reader = PdfReader(input_path)
        writer = PdfWriter()

        packet = io.BytesIO()
        can = canvas.Canvas(packet)
        can.setFont("Helvetica-Bold", 72)
        can.setFillColorRGB(0.5, 0.5, 0.5, alpha=0.3) 
        can.translate(300, 400)
        can.rotate(45)
        can.drawCentredString(0, 0, text)
        can.save()
        
        packet.seek(0)
        watermark = PdfReader(packet)

        for page in reader.pages:
            page.merge_page(watermark.pages[0])
            writer.add_page(page)

        output_path = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf").name
        writer.write(output_path)
        writer.close()
        os.unlink(input_path)

        base_name = os.path.splitext(file.filename)[0]
        return FileResponse(path=output_path, filename=f"watermarked_{base_name}.pdf", media_type='application/pdf')
    except Exception as e:
        print(f"--- WATERMARK ERROR ---: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to add watermark.")
    
# 11. Handle Metadata Scrubbing
@app.post("/api/scrub")
async def scrub_metadata(file: UploadFile = File(...)):
    try:
        contents = await file.read()
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_in:
            temp_in.write(contents)
            input_path = temp_in.name

        reader = PdfReader(input_path)
        writer = PdfWriter()

        for page in reader.pages:
            writer.add_page(page)

        writer.add_metadata({})

        output_path = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf").name
        writer.write(output_path)
        writer.close()
        os.unlink(input_path)

        base_name = os.path.splitext(file.filename)[0]
        return FileResponse(path=output_path, filename=f"scrubbed_{base_name}.pdf", media_type='application/pdf')
    except Exception as e:
        print(f"--- SCRUB ERROR ---: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to scrub metadata.")
    
# 12. Handle Document Preview (Multi-Page Grid with Pagination)
@app.post("/api/preview")
async def preview_pdf(file: UploadFile = File(...), page_start: int = Form(0)):
    try:
        contents = await file.read()
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_in:
            temp_in.write(contents)
            input_path = temp_in.name

        preview_images = []
        with pdfplumber.open(input_path) as pdf:
            total_pages = len(pdf.pages)
            
            start = min(page_start, total_pages)
            end = min(start + 30, total_pages)
            
            for i in range(start, end):
                page = pdf.pages[i]
                img = page.to_image(resolution=48) 
                buffer = io.BytesIO()
                img.original.save(buffer, format="JPEG")
                base64_encoded = base64.b64encode(buffer.getvalue()).decode('utf-8')
                preview_images.append(f"data:image/jpeg;base64,{base64_encoded}")

        os.unlink(input_path)
        
        return JSONResponse(content={
            "filename": file.filename,
            "total_pages": total_pages,
            "preview_images": preview_images,
            "current_start": start,
            "current_end": end,
            "has_more": end < total_pages
        })

    except Exception as e:
        print(f"--- PREVIEW ERROR ---: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to generate preview.")

# 13. Handle Visual Page Manipulation (Rotate & Delete)
@app.post("/api/modify-pages")
async def modify_pages(file: UploadFile = File(...), modifications: str = Form(...)):
    try:
        contents = await file.read()
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_in:
            temp_in.write(contents)
            input_path = temp_in.name

        mods = json.loads(modifications)
        rotations = mods.get("rotations", {})
        deletions = set(mods.get("deletions", []))

        reader = PdfReader(input_path)
        writer = PdfWriter()

        for i, page in enumerate(reader.pages):
            if i in deletions:
                continue
            
            if str(i) in rotations:
                angle = rotations[str(i)] % 360
                if angle != 0:
                    page.rotate(angle)
            
            writer.add_page(page)

        output_path = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf").name
        writer.write(output_path)
        writer.close()
        os.unlink(input_path)

        base_name = os.path.splitext(file.filename)[0]
        return FileResponse(path=output_path, filename=f"modified_{base_name}.pdf", media_type='application/pdf')
    except Exception as e:
        print(f"--- MODIFY ERROR ---: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to modify pages.")
    
# 14. Batch Merge Preview
@app.post("/api/preview-merge")
async def preview_merge(files: List[UploadFile] = File(...)):
    try:
        preview_images = []
        for file in files[:20]:
            contents = await file.read()
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_in:
                temp_in.write(contents)
                input_path = temp_in.name

            with pdfplumber.open(input_path) as pdf:
                if len(pdf.pages) > 0:
                    img = pdf.pages[0].to_image(resolution=48)
                    buffer = io.BytesIO()
                    img.original.save(buffer, format="JPEG")
                    base64_encoded = base64.b64encode(buffer.getvalue()).decode('utf-8')
                    preview_images.append({
                        "filename": file.filename,
                        "image": f"data:image/jpeg;base64,{base64_encoded}"
                    })
            os.unlink(input_path)
            
        return JSONResponse(content={"previews": preview_images})
    except Exception as e:
        print(f"--- MERGE PREVIEW ERROR ---: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to preview merge files.")

        # 15. Handle PDF Unlocking (Remove Password)
@app.post("/api/unlock")
async def unlock_pdf(file: UploadFile = File(...), password: str = Form(...)):
    try:
        contents = await file.read()
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_in:
            temp_in.write(contents)
            input_path = temp_in.name

        reader = PdfReader(input_path)
        
        # Check if the file actually has a password
        if not reader.is_encrypted:
            os.unlink(input_path)
            raise HTTPException(status_code=400, detail="This PDF is not encrypted.")

        # Attempt to decrypt it
        decrypted = reader.decrypt(password)
        if decrypted == 0: # 0 means the password failed
            os.unlink(input_path)
            raise HTTPException(status_code=401, detail="Incorrect password.")

        # If successful, write the unlocked pages to a new file
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)

        output_path = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf").name
        writer.write(output_path)
        writer.close()
        os.unlink(input_path)

        base_name = os.path.splitext(file.filename)[0]
        return FileResponse(path=output_path, filename=f"unlocked_{base_name}.pdf", media_type='application/pdf')
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"--- UNLOCK ERROR ---: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to unlock PDF.")