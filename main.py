from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pypdf import PdfWriter, PdfReader
from pypdf.constants import UserAccessPermissions
from pdf2docx import Converter
from reportlab.pdfgen import canvas
from fastapi.responses import JSONResponse
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
async def merge_pdfs(files: list[UploadFile] = File(...)):
    merger = PdfWriter()
    
    # Save uploaded files to temporary memory and add them to the merger
    for file in files:
        contents = await file.read()
        # Create a temporary file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_pdf:
            temp_pdf.write(contents)
            temp_path = temp_pdf.name
        
        # Add to merger
        merger.append(temp_path)
        # Delete the temp file to save space
        os.unlink(temp_path)

    # Create a final output temporary file
    output_path = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf").name
    merger.write(output_path)
    merger.close()

    # Send the merged PDF back to the user
    return FileResponse(
        path=output_path, 
        filename="merged_document.pdf", 
        media_type='application/pdf'
    )

# 3. Handle PDF Compression (Upgraded for Batch Processing)
@app.post("/api/compress")
async def compress_pdf(files: list[UploadFile] = File(...)): 
    processed_files = []
    
    try:
        # Loop through every uploaded file
        for file in files:
            contents = await file.read()
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_in:
                temp_in.write(contents)
                input_path = temp_in.name
            
            output_path = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf").name

            gs_command = [
                "gs", "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.4",
                "-dPDFSETTINGS=/screen", "-dNOPAUSE", "-dQUIET", "-dBATCH",
                f"-sOutputFile={output_path}", input_path
            ]
            
            # Compress it
            subprocess.run(gs_command, check=True, capture_output=True, text=True)
            os.unlink(input_path) 
            
            # Save the result to our list
            processed_files.append((file.filename, output_path))
            
        # If they only uploaded ONE file, just return the normal PDF
        if len(processed_files) == 1:
            orig_name, out_path = processed_files[0]
            return FileResponse(path=out_path, filename=f"compressed_{orig_name}", media_type='application/pdf')
            
        # If they uploaded MULTIPLE files, zip them up!
        zip_path = tempfile.NamedTemporaryFile(delete=False, suffix=".zip").name
        with zipfile.ZipFile(zip_path, 'w') as zipf:
            for orig_name, out_path in processed_files:
                # Add to zip and delete the temporary PDF
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
        # 1. Parse the text input (e.g., "1, 3, 5-7") into a list of numbers
        page_indices = set()
        # Remove spaces and split by commas
        for part in pages.replace(" ", "").split(","):
            if "-" in part:
                start, end = part.split("-")
                # Add all numbers in the range
                for p in range(int(start), int(end) + 1):
                    page_indices.add(p - 1) 
            else:
                page_indices.add(int(part) - 1)
        
        sorted_indices = sorted(list(page_indices))

        # 2. Save the uploaded file temporarily
        contents = await file.read()
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_in:
            temp_in.write(contents)
            input_path = temp_in.name

        # 3. Read the PDF and extract the requested pages
        reader = PdfReader(input_path)
        writer = PdfWriter()
        total_pages = len(reader.pages)

        for idx in sorted_indices:
            # Ensure the user didn't ask for a page that doesn't exist
            if 0 <= idx < total_pages:
                writer.add_page(reader.pages[idx])

        # 4. Save and return the new PDF
        output_path = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf").name
        writer.write(output_path)
        writer.close()
        os.unlink(input_path) 

        return FileResponse(
            path=output_path, 
            filename=f"extracted_{file.filename}", 
            media_type='application/pdf'
        )
    except Exception as e:
        print(f"--- SPLIT ERROR ---: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to extract pages.")
    
# 5. Handle OCR (Make PDF Searchable)
@app.post("/api/ocr")
async def ocr_pdf(file: UploadFile = File(...)):
    # Save the uploaded file temporarily
    contents = await file.read()
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_in:
        temp_in.write(contents)
        input_path = temp_in.name

    # Create a path for the output file
    output_path = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf").name

    try:
        # Run OCR. force_ocr=True ensures it processes even if it thinks there is already text.
        ocrmypdf.ocr(input_path, output_path, force_ocr=True)
        os.unlink(input_path) 
        
        return FileResponse(
            path=output_path, 
            filename=f"searchable_{file.filename}", 
            media_type='application/pdf'
        )
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
        # Save the uploaded file temporarily
        contents = await file.read()
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_in:
            temp_in.write(contents)
            input_path = temp_in.name

        reader = PdfReader(input_path)
        writer = PdfWriter()

        for page in reader.pages:
            writer.add_page(page)

        # 1. Start with ALL permissions granted (-1 represents all 1s in binary)
        permissions = -1 

        # 2. Turn off specific permissions using Bitwise logic if requested
        if disable_print:
            permissions &= ~(UserAccessPermissions.PRINT | UserAccessPermissions.PRINT_TO_REPRESENTATION)
        if disable_copy:
            permissions &= ~(UserAccessPermissions.EXTRACT | UserAccessPermissions.EXTRACT_TEXT_AND_GRAPHICS)
        if disable_modify:
            permissions &= ~(UserAccessPermissions.MODIFY | UserAccessPermissions.ADD_OR_MODIFY | UserAccessPermissions.FILL_FORM_FIELDS | UserAccessPermissions.ASSEMBLE_DOC)

        # 3. Ensure we have an owner password to lock the permissions
        final_owner = owner_password if owner_password else open_password

        # 4. Encrypt the PDF with the flags
        writer.encrypt(
            user_password=open_password, 
            owner_password=final_owner,
            permissions_flag=permissions
        )

        output_path = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf").name
        writer.write(output_path)
        writer.close()
        os.unlink(input_path) 

        return FileResponse(
            path=output_path, 
            filename=f"protected_{file.filename}", 
            media_type='application/pdf'
        )
    except Exception as e:
        print(f"--- SECURITY ERROR ---: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to protect the PDF.")
    
# 7. Handle Office to PDF Conversion (Word, Excel, PPT)
@app.post("/api/convert/to-pdf")
async def convert_to_pdf(file: UploadFile = File(...)):
    try:
        # LibreOffice requires an actual directory to work properly
        temp_dir = tempfile.mkdtemp()
        input_path = os.path.join(temp_dir, file.filename)
        
        # Save the uploaded Office file
        contents = await file.read()
        with open(input_path, "wb") as f:
            f.write(contents)

        # Run LibreOffice in invisible "headless" mode
        subprocess.run([
            "libreoffice", 
            "--headless", 
            "--nologo", 
            "--nofirststartwizard", 
            "--convert-to", "pdf", 
            "--outdir", temp_dir, 
            input_path
        ], check=True, capture_output=True)

        # The output file will have the same name, but with a .pdf extension
        base_name = os.path.splitext(file.filename)[0]
        output_pdf = os.path.join(temp_dir, f"{base_name}.pdf")

        return FileResponse(
            path=output_pdf, 
            filename=f"{base_name}.pdf", 
            media_type='application/pdf'
        )
    except Exception as e:
        print(f"--- CONVERT TO PDF ERROR ---: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to convert to PDF.")

# 8. Handle PDF to Word Conversion
@app.post("/api/convert/to-word")
async def convert_to_word(file: UploadFile = File(...)):
    try:
        # Save the uploaded PDF
        contents = await file.read()
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_in:
            temp_in.write(contents)
            input_path = temp_in.name

        output_docx = tempfile.NamedTemporaryFile(delete=False, suffix=".docx").name

        # Convert PDF to Word
        cv = Converter(input_path)
        cv.convert(output_docx)      # all pages by default
        cv.close()
        
        os.unlink(input_path) 

        base_name = os.path.splitext(file.filename)[0]
        return FileResponse(
            path=output_docx, 
            filename=f"{base_name}.docx", 
            media_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        )
    except Exception as e:
        print(f"--- CONVERT TO WORD ERROR ---: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to convert PDF to Word.")


# 9. Handle PDF to Excel Conversion (Robust Pipeline with OCR Rescue)
@app.post("/api/convert/to-excel")
async def convert_to_excel(file: UploadFile = File(...)):
    try:
        contents = await file.read()
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_in:
            temp_in.write(contents)
            input_path = temp_in.name

        output_xlsx = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx").name
        all_tables = []

        # Strategy 1: Try Camelot (Best for structural accuracy on native PDFs)
        try:
            tables = camelot.read_pdf(input_path, pages='all', flavor='stream')
            for table in tables:
                if not table.df.empty:
                    all_tables.append(table.df)
        except Exception as e:
            print(f"Camelot Strategy Skipped/Failed: {e}")

        # Strategy 2: Fallback to pdfplumber (If Camelot misses native text)
        if not all_tables:
            try:
                with pdfplumber.open(input_path) as pdf:
                    for page in pdf.pages:
                        tables = page.extract_tables({"vertical_strategy": "text", "horizontal_strategy": "text"})
                        for table in tables:
                            df = pd.DataFrame(table[1:], columns=table[0])
                            all_tables.append(df)
            except Exception as e:
                print(f"pdfplumber Strategy Failed: {e}")

        # Strategy 3: The OCR Rescue (If the PDF is a flat image/screenshot)
        if not all_tables:
            print("--- IMAGE DETECTED: Running OCR Rescue ---")
            ocr_path = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf").name
            try:
                # Force OCR to inject a text layer into the image
                ocrmypdf.ocr(input_path, ocr_path, force_ocr=True, output_type='pdf', deskew=True)
                
                # Extract using the new text layer
                with pdfplumber.open(ocr_path) as pdf:
                    # Give OCR text slightly wider tolerances
                    settings = {
                        "vertical_strategy": "text", 
                        "horizontal_strategy": "text",
                        "snap_tolerance": 5, 
                        "join_tolerance": 5
                    }
                    for page in pdf.pages:
                        tables = page.extract_tables(settings)
                        for table in tables:
                            df = pd.DataFrame(table[1:], columns=table[0])
                            all_tables.append(df)
            except Exception as e:
                print(f"OCR Rescue Failed: {e}")
            finally:
                if os.path.exists(ocr_path):
                    os.unlink(ocr_path)

        # Final check
        if not all_tables:
            os.unlink(input_path)
            raise HTTPException(status_code=400, detail="Could not detect tables, even after OCR.")

        # Save to Excel
        with pd.ExcelWriter(output_xlsx, engine='openpyxl') as writer:
            for i, df in enumerate(all_tables):
                df.to_excel(writer, sheet_name=f"Table_{i+1}", index=False)

        os.unlink(input_path)
        
        base_name = os.path.splitext(file.filename)[0]
        return FileResponse(
            path=output_xlsx, 
            filename=f"{base_name}.xlsx", 
            media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"--- CONVERT TO EXCEL ERROR ---: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to convert.")

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

        # Create the watermark PDF in memory
        packet = io.BytesIO()
        can = canvas.Canvas(packet)
        can.setFont("Helvetica-Bold", 72)
        can.setFillColorRGB(0.5, 0.5, 0.5, alpha=0.3) 
        
        # Position and rotate the text diagonally
        can.translate(300, 400)
        can.rotate(45)
        can.drawCentredString(0, 0, text)
        can.save()
        
        packet.seek(0)
        watermark = PdfReader(packet)

        # Stamp the watermark onto every page
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

        # Copy all pages
        for page in reader.pages:
            writer.add_page(page)

        # Overwrite the metadata dictionary with an empty set
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
    
    import json

# 12. Handle Document Preview (Multi-Page Grid)
@app.post("/api/preview")
async def preview_pdf(file: UploadFile = File(...)):
    try:
        contents = await file.read()
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_in:
            temp_in.write(contents)
            input_path = temp_in.name

        preview_images = []
        with pdfplumber.open(input_path) as pdf:
            total_pages = len(pdf.pages)
            # Cap at 30 pages to prevent the server from freezing on huge books
            max_pages = min(total_pages, 30)
            
            for i in range(max_pages):
                page = pdf.pages[i]
                # Resolution 48 is low-res, perfect for fast-loading UI thumbnails
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
            "limited": total_pages > 30
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

        # modifications is a JSON string: '{"rotations": {"0": 90}, "deletions": [1, 2]}'
        mods = json.loads(modifications)
        rotations = mods.get("rotations", {})
        deletions = set(mods.get("deletions", []))

        reader = PdfReader(input_path)
        writer = PdfWriter()

        for i, page in enumerate(reader.pages):
            if i in deletions:
                continue # Skip deleted pages
            
            # Check if this page needs rotation
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