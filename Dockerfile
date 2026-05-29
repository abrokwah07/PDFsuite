# 1. Use the lightweight Python base image
FROM python:3.11-slim

# 2. Set the working directory
WORKDIR /app

# 3. Install system dependencies and immediately clean up the apt cache to reduce image size
RUN apt-get update && apt-get install -y \
    ghostscript \
    tesseract-ocr \
    qpdf \
    libreoffice \
    && rm -rf /var/lib/apt/lists/*

# 4. Copy requirements and install Python packages securely
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. Copy the rest of the application code
COPY . .

# 6. Expose the port
EXPOSE 8000

# 7. Start the server (Notice: NO --reload flag for production to save memory)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]