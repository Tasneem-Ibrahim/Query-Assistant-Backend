.venv\Scripts\activate
& "C:\Program Files\Tesseract-OCR\tesseract.exe" --version
python --version
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
pip list
pip install fastapi uvicorn python-multipart
uvicorn api_server:app --port 8000 / python -m uvicorn api_server:app --port 8000

http://localhost:8000/api/health