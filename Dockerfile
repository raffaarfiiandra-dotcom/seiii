FROM python:3.12-slim

# Instal ca-certificates agar Python bisa melakukan jabat tangan SSL (HTTPS) ke Discord
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Salin daftar dependency dan instal
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Salin seluruh kode program
COPY . .

# Jalankan bot
CMD ["python", "-u", "seii_bot.py"]
