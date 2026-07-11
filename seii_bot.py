import discord
import os
import sys
import http.server
import socketserver
import threading
import socket
import aiohttp
import gc
import base64
import datetime
from duckduckgo_search import DDGS

# PENTING: Memaksa sistem Python menggunakan IPv4 saja (mengatasi eror IPv6 di cloud).
orig_getaddrinfo = socket.getaddrinfo
def getaddrinfo_ipv4(host, port, family=0, type=0, proto=0, flags=0):
    return orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
socket.getaddrinfo = getaddrinfo_ipv4

# PENTING: Menonaktifkan verifikasi SSL secara global di aiohttp (mengatasi eror ssl:default karena sertifikat Linux minimal).
orig_connector_init = aiohttp.TCPConnector.__init__
def patched_connector_init(self, *args, **kwargs):
    kwargs['ssl'] = False  # Matikan validasi sertifikat SSL agar koneksi langsung tembus
    orig_connector_init(self, *args, **kwargs)
aiohttp.TCPConnector.__init__ = patched_connector_init

# Konfigurasi Path File
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(BASE_DIR, 'token.txt')
GEMINI_KEY_FILE = os.path.join(BASE_DIR, 'gemini_key.txt')
SYSTEM_PROMPT_FILE = os.path.join(BASE_DIR, 'system_prompt.txt')

# Dictionary untuk menyimpan riwayat chat (Memory) per Channel atau User
conversation_histories = {}
MAX_HISTORY_TURNS = 20  # Batas maksimal giliran chat yang diingat

# Inisialisasi Discord Intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
client = discord.Client(intents=intents)

# Membaca Data Konfigurasi (Mendukung Environment Variables untuk Cloud)
def read_config_files():
    discord_token = os.environ.get('DISCORD_TOKEN')
    gemini_key = os.environ.get('GEMINI_KEY')

    if not discord_token:
        if not os.path.exists(TOKEN_FILE):
            print(f"Error: Token tidak ditemukan di Env maupun file '{TOKEN_FILE}'!")
            sys.exit(1)
        with open(TOKEN_FILE, 'r', encoding='utf-8') as f:
            discord_token = f.read().strip()

    if not gemini_key:
        if not os.path.exists(GEMINI_KEY_FILE):
            print(f"Error: API Key tidak ditemukan di Env maupun file '{GEMINI_KEY_FILE}'!")
            sys.exit(1)
        with open(GEMINI_KEY_FILE, 'r', encoding='utf-8') as f:
            gemini_key = f.read().strip()

    system_prompt = ""
    if os.path.exists(SYSTEM_PROMPT_FILE):
        with open(SYSTEM_PROMPT_FILE, 'r', encoding='utf-8') as f:
            system_prompt = f.read().strip()
    else:
        print("Warning: File 'system_prompt.txt' tidak ditemukan.")

    return discord_token, gemini_key, system_prompt

# Memuat konfigurasi
DISCORD_TOKEN, GEMINI_KEY, SYSTEM_PROMPT = read_config_files()

# Membaca model dari environment, default gemini-flash-latest (1.5 Flash) karena gemini-2.5-flash dibatasi sangat ketat (hanya 20 request per hari di free tier).
# Sedangkan gemini-flash-latest gratisan memberikan jatah 1.500 request per hari (75x lipat lebih banyak)!
GEMINI_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-flash-latest')

import asyncio

# Fungsi untuk memanggil Gemini API secara langsung menggunakan aiohttp (sangat hemat RAM!)
async def generate_gemini_content(contents):
    # Menyuntikkan waktu saat ini agar bot tidak halusinasi soal tahun
    current_time = datetime.datetime.now().strftime("%d %B %Y, %H:%M:%S")
    dynamic_prompt = f"{SYSTEM_PROMPT}\n\n[INFO SISTEM CRITICAL]\nHari ini adalah tanggal {current_time}. Tahun ini adalah 2026. Jika pengguna memberikan screenshot atau info terbaru (seperti promo Google AI Pro 2026), percayalah pada data tersebut dan jangan berasumsi bahwa itu editan hanya karena database lamamu tidak mengetahuinya."
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}"
    payload = {
        "contents": contents,
        "systemInstruction": {
            "parts": [{"text": dynamic_prompt}]
        },
        "tools": [{
            "function_declarations": [{
                "name": "search_web",
                "description": "Gunakan fungsi ini SECARA PROAKTIF untuk mencari informasi, berita, atau harga terbaru di internet jika kamu tidak yakin.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "query": {
                            "type": "STRING",
                            "description": "Kata kunci pencarian internet (contoh: 'harga ram ddr5 32gb 2026' atau 'berita hari ini')"
                        }
                    },
                    "required": ["query"]
                }
            }]
        }]
    }
    
    max_retries = 5
    for attempt in range(max_retries):
        try:
            # Gunakan session aiohttp untuk melakukan request REST API
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload) as response:
                    if response.status == 200:
                        data = await response.json()
                        try:
                            part = data['candidates'][0]['content']['parts'][0]
                            
                            # Cek apakah model ingin melakukan pencarian web
                            if 'functionCall' in part:
                                func_name = part['functionCall']['name']
                                if func_name == "search_web":
                                    query = part['functionCall']['args'].get('query', '')
                                    print(f"[INFO] Gemini ingin mencari di internet: '{query}'")
                                    
                                    try:
                                        def do_search():
                                            results = DDGS().text(query, max_results=3)
                                            # DDGS returns a list of dicts in recent versions, or generator. Wrap safely.
                                            return list(results) if results else []
                                        
                                        results_list = await asyncio.to_thread(do_search)
                                        search_results = []
                                        for r in results_list:
                                            search_results.append(f"Title: {r.get('title', '')}\nBody: {r.get('body', '')}")
                                        search_result_text = "\n\n".join(search_results)
                                        
                                        if not search_result_text.strip():
                                            search_result_text = "Tidak ada hasil pencarian."
                                    except Exception as e:
                                        search_result_text = f"Error saat mencari: {e}"
                                    
                                    print(f"[INFO] Hasil pencarian didapat, mengirim kembali ke Gemini...")
                                    
                                    # Rekursif panggil API lagi dengan menyertakan hasil pencarian
                                    new_contents = contents.copy()
                                    new_contents.append({
                                        "role": "model",
                                        "parts": [{"functionCall": part['functionCall']}]
                                    })
                                    new_contents.append({
                                        "role": "function",
                                        "parts": [{"functionResponse": {
                                            "name": "search_web",
                                            "response": {"result": search_result_text}
                                        }}]
                                    })
                                    return await generate_gemini_content(new_contents)
                                else:
                                    return "Gw bingung, otak gw nyuruh panggil fungsi yang ga gw kenal."
                            elif 'text' in part:
                                return part['text']
                            else:
                                raise Exception("Tidak ada teks atau functionCall dalam response.")
                        except (KeyError, IndexError) as e:
                            raise Exception(f"Struktur respons API di luar dugaan: {data}")
                    else:
                        error_text = await response.text()
                        raise Exception(f"Gemini API mengembalikan status {response.status}: {error_text}")
        except Exception as e:
            if attempt < max_retries - 1:
                wait_time = 3 * (attempt + 1) # Tunggu makin lama: 3s, 6s, 9s, 12s
                print(f"      [WARNING] Gemini API sibuk/gagal. Mencoba ulang dalam {wait_time} detik... (Percobaan {attempt + 1}/{max_retries}) - Error: {e}")
                await asyncio.sleep(wait_time)
                continue
            raise e

@client.event
async def on_ready():
    print(f"\n=========================================")
    print(f"   SEII BOT AKTIF 100% (MOD MEMORI MINIM)")
    print(f"   Login sebagai: {client.user}")
    print(f"=========================================")
    print(f"Menunggu pesan masuk di Discord...\n")

@client.event
async def on_message(message):
    if message.author == client.user:
        return

    is_dm = isinstance(message.channel, discord.DMChannel)
    is_mentioned = client.user.mentioned_in(message)

    if is_dm or is_mentioned:
        async with message.channel.typing():
            clean_prompt = message.content
            if is_mentioned:
                bot_mention = f"<@!{client.user.id}>"
                bot_mention_alt = f"<@{client.user.id}>"
                clean_prompt = clean_prompt.replace(bot_mention, "").replace(bot_mention_alt, "").strip()

            user_parts = []
            if clean_prompt:
                user_parts.append({"text": clean_prompt})
            
            # Proses attachment (gambar)
            for attachment in message.attachments:
                if attachment.content_type and attachment.content_type.startswith("image/"):
                    if attachment.size > 5 * 1024 * 1024:
                        await message.reply(f"Waduh, gambar `{attachment.filename}` kegedean Bro (maksimal 5MB biar otak gw ga meleduk).")
                        return
                    
                    try:
                        image_bytes = await attachment.read()
                        base64_data = base64.b64encode(image_bytes).decode('utf-8')
                        user_parts.append({
                            "inlineData": {
                                "mimeType": attachment.content_type,
                                "data": base64_data
                            }
                        })
                    except Exception as e:
                        print(f"[ERROR] Gagal memproses gambar: {e}")

            if not user_parts:
                await message.reply("Yo, ada yang bisa gw bantu?")
                return

            chat_key = message.channel.id if not is_dm else message.author.id

            if chat_key not in conversation_histories:
                conversation_histories[chat_key] = []

            history = conversation_histories[chat_key]

            history.append({
                "role": "user",
                "parts": user_parts
            })

            if len(history) > MAX_HISTORY_TURNS:
                conversation_histories[chat_key] = history[-MAX_HISTORY_TURNS:]
                history = conversation_histories[chat_key]

            print(f"[LOG] Memproses pesan dari {message.author} di #{message.channel if not is_dm else 'DM'}")
            print(f"      Teks: '{clean_prompt}'")

            try:
                # Memanggil Gemini secara direct REST API
                response_text = await generate_gemini_content(history)

                history.append({
                    "role": "model",
                    "parts": [{"text": response_text}]
                })

                print(f"      [OK] Respons Gemini sukses didapatkan!")

                if len(response_text) > 2000:
                    parts = [response_text[i:i+1900] for i in range(0, len(response_text), 1900)]
                    for idx, part in enumerate(parts):
                        if idx == 0:
                            await message.reply(part)
                        else:
                            await message.channel.send(part)
                else:
                    await message.reply(response_text)

            except Exception as e:
                print(f"      [ERROR] Gagal memanggil Gemini/mengirim chat: {e}")
                await message.reply("Aduh sori Bro, kepala gw lagi pusing nih (ada kendala koneksi ke otak AI). Coba tanya lagi bentar ya!")
            finally:
                # Paksa bersihkan RAM setiap kali selesai memproses chat
                gc.collect()

# Web Server Dummy untuk Lolos Health Check
def run_dummy_server():
    PORT = int(os.environ.get('PORT', 7860))  # Gunakan port dari Render, default 7860
    Handler = http.server.SimpleHTTPRequestHandler

    class SilentHandler(Handler):
        def log_message(self, format, *args):
            pass

    socketserver.TCPServer.allow_reuse_address = True
    try:
        with socketserver.TCPServer(("", PORT), SilentHandler) as httpd:
            print(f"Server dummy berjalan di port {PORT} untuk health check cloud.")
            httpd.serve_forever()
    except Exception as e:
        print(f"Peringatan: Gagal menjalankan server dummy: {e}")

if __name__ == "__main__":
    threading.Thread(target=run_dummy_server, daemon=True).start()

    try:
        client.run(DISCORD_TOKEN)
    except discord.errors.LoginFailure:
        print("Error: Token Discord yang dimasukkan salah!")
    except Exception as e:
        print(f"Error saat menjalankan bot: {e}")
