# Gunakan image Python yang ringan
FROM python:3.11-slim

# Buat direktori kerja di dalam container
WORKDIR /app

# Salin semua file ke dalam container
COPY . /app

# Install semua dependensi dari requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Buka port 8000 (wajib agar Railway bisa akses)
EXPOSE 8000

# Jalankan server FastAPI menggunakan uvicorn
CMD ["uvicorn", "main:app", "--host=0.0.0.0", "--port=8000"]
