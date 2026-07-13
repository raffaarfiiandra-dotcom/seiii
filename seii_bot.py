import os
import sys
import subprocess

def install_pkg(package):
    subprocess.check_call([sys.executable, "-m", "pip", "install", package])

try:
    from ddgs import DDGS
except ImportError:
    print(f"[SYSTEM] Menginstal module ddgs yang hilang...")
    install_pkg("ddgs")
    from ddgs import DDGS

import discord
import http.server
import socketserver
import threading
import socket
import aiohttp
import gc
import base64
import datetime
import json
import re

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
GROQ_KEY_FILE = os.path.join(BASE_DIR, 'groq_key.txt')
SYSTEM_PROMPT_FILE = os.path.join(BASE_DIR, 'system_prompt.txt')

# Dictionary untuk menyimpan riwayat chat (Memory) per Channel atau User
conversation_histories = {}
MAX_HISTORY_TURNS = 20  # Batas giliran chat

# Inisialisasi Discord Intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
client = discord.Client(intents=intents)

# Membaca Data Konfigurasi
def read_config_files():
    discord_token = os.environ.get('DISCORD_TOKEN')
    groq_key = os.environ.get('GROQ_KEY')

    if not discord_token:
        if not os.path.exists(TOKEN_FILE):
            print(f"Error: Token tidak ditemukan di Env maupun file '{TOKEN_FILE}'!")
            sys.exit(1)
        with open(TOKEN_FILE, 'r', encoding='utf-8') as f:
            discord_token = f.read().strip()

    if not groq_key:
        if os.path.exists(GROQ_KEY_FILE):
            with open(GROQ_KEY_FILE, 'r', encoding='utf-8') as f:
                groq_key = f.read().strip()
        else:
            print(f"Error: API Key tidak ditemukan di Env maupun file '{GROQ_KEY_FILE}'!")
            sys.exit(1)

    system_prompt = ""
    if os.path.exists(SYSTEM_PROMPT_FILE):
        with open(SYSTEM_PROMPT_FILE, 'r', encoding='utf-8') as f:
            system_prompt = f.read().strip()
    else:
        system_prompt = "Kamu adalah asisten discord."

    return discord_token, groq_key, system_prompt

DISCORD_TOKEN, GROQ_KEY, SYSTEM_PROMPT = read_config_files()

import asyncio

# Fungsi untuk memanggil Groq API
async def generate_groq_content(messages, has_image=False):
    current_time = datetime.datetime.now().strftime("%d %B %Y, %H:%M:%S")
    dynamic_prompt = f"{SYSTEM_PROMPT}\n\n[INFO SISTEM CRITICAL]\nHari ini adalah tanggal {current_time}. Tahun ini adalah 2026. Kamu sekarang berjalan menggunakan Groq API."
    
    # Pilih model: Jika ada gambar, pakai vision. Jika teks saja, pakai versatile.
    model_name = "meta-llama/llama-4-scout-17b-16e-instruct" if has_image else "llama-3.3-70b-versatile"
    
    # Sanitize history untuk model teks (Llama 3.3 Versatile menolak list content)
    sanitized_messages = []
    for msg in messages:
        msg_copy = msg.copy()
        if not has_image and isinstance(msg_copy.get("content"), list):
            # Ekstrak teks saja, buang gambarnya dari memori
            text_parts = [item["text"] for item in msg_copy["content"] if item.get("type") == "text"]
            msg_copy["content"] = " ".join(text_parts) if text_parts else "[Gambar telah dihapus dari memori untuk menghemat token]"
        sanitized_messages.append(msg_copy)
    
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_KEY}",
        "Content-Type": "application/json"
    }
    
    # Masukkan system prompt ke dalam list messages
    full_messages = [{"role": "system", "content": dynamic_prompt}] + sanitized_messages
    
    payload = {
        "model": model_name,
        "messages": full_messages,
        "tools": [{
            "type": "function",
            "function": {
                "name": "search_web",
                "description": "Gunakan fungsi ini SECARA PROAKTIF untuk mencari informasi, berita, atau harga terbaru di internet jika kamu tidak yakin.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Kata kunci pencarian internet (contoh: 'harga ram ddr5 32gb 2026' atau 'berita hari ini')"
                        }
                    },
                    "required": ["query"]
                }
            }
        }],
        "tool_choice": "auto"
    }
    
    max_retries = 5
    for attempt in range(max_retries):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=payload) as response:
                    if response.status == 200:
                        data = await response.json()
                        message_data = data['choices'][0]['message']
                        
                        # Cek apakah model ingin memanggil tool (search_web)
                        if message_data.get('tool_calls'):
                            tool_call = message_data['tool_calls'][0]
                            if tool_call['function']['name'] == "search_web":
                                try:
                                    args = json.loads(tool_call['function']['arguments'])
                                    query = args.get('query', '')
                                    print(f"[INFO] Groq ingin mencari di internet: '{query}'")
                                    
                                    def do_search():
                                        results = DDGS().text(query, max_results=3)
                                        return list(results) if results else []
                                    
                                    results_list = await asyncio.to_thread(do_search)
                                    search_results = []
                                    for r in results_list:
                                        search_results.append(f"Title: {r.get('title', '')}\nLink: {r.get('href', '')}\nBody: {r.get('body', '')}")
                                    search_result_text = "\n\n".join(search_results)
                                    
                                    if not search_result_text.strip():
                                        search_result_text = "Tidak ada hasil pencarian."
                                except Exception as e:
                                    search_result_text = f"Error saat mencari: {e}"
                                
                                print(f"[INFO] Hasil pencarian didapat, mengirim kembali ke Groq...")
                                
                                # Rekursif panggil API lagi dengan menyertakan hasil pencarian
                                new_messages = messages.copy()
                                new_messages.append(message_data) # Tambahkan assistant's tool_call request
                                new_messages.append({
                                    "role": "tool",
                                    "tool_call_id": tool_call['id'],
                                    "name": "search_web",
                                    "content": search_result_text
                                })
                                return await generate_groq_content(new_messages, has_image)
                        
                        # Jika tidak ada tool calls, berarti itu adalah balasan teks biasa
                        return message_data.get('content', '')
                    else:
                        error_text = await response.text()
                        
                        # Fallback Anti-Hallucination untuk Groq Llama 3 tool calling
                        if response.status == 400 and "tool_use_failed" in error_text:
                            try:
                                error_json = json.loads(error_text)
                                failed_gen = error_json.get("error", {}).get("failed_generation", "")
                                if "search_web" in failed_gen:
                                    match = re.search(r'\{.*\}', failed_gen)
                                    if match:
                                        args = json.loads(match.group(0))
                                        query = args.get('query', '')
                                        print(f"[INFO] Fallback tool call: Mencegat query '{query}' dari error 400 Groq.")
                                        
                                        def do_search():
                                            results = DDGS().text(query, max_results=3)
                                            return list(results) if results else []
                                        
                                        results_list = await asyncio.to_thread(do_search)
                                        search_results = []
                                        for r in results_list:
                                            search_results.append(f"Title: {r.get('title', '')}\nLink: {r.get('href', '')}\nBody: {r.get('body', '')}")
                                        search_result_text = "\n\n".join(search_results)
                                        
                                        if not search_result_text.strip():
                                            search_result_text = "Tidak ada hasil pencarian."
                                            
                                        print(f"[INFO] Hasil pencarian didapat (Fallback), mengirim kembali ke Groq...")
                                        new_messages = messages.copy()
                                        new_messages.append({
                                            "role": "assistant", 
                                            "content": f"Baik, saya akan mencari '{query}' di internet."
                                        })
                                        new_messages.append({
                                            "role": "user", 
                                            "content": f"[HASIL PENCARIAN INTERNET UNTUK '{query}']:\n{search_result_text}\n\nTolong jawab pertanyaan asliku menggunakan data tersebut, dan sertakan link sumbernya!"
                                        })
                                        return await generate_groq_content(new_messages, has_image)
                            except Exception as parse_e:
                                print(f"[WARNING] Gagal mengekstrak fallback JSON: {parse_e}")
                        
                        raise Exception(f"Groq API mengembalikan status {response.status}: {error_text}")
        except Exception as e:
            if attempt < max_retries - 1:
                wait_time = 3 * (attempt + 1)
                print(f"      [WARNING] Groq API gagal. Mencoba ulang dalam {wait_time} detik... ({e})")
                await asyncio.sleep(wait_time)
                continue
            raise e

@client.event
async def on_ready():
    print(f"\n=========================================")
    print(f"   SEII BOT AKTIF 100% (GROQ ENGINE)")
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

            content = []
            if clean_prompt:
                content.append({"type": "text", "text": clean_prompt})
            
            has_image = False
            
            # Proses attachment (gambar)
            for attachment in message.attachments:
                if attachment.content_type and attachment.content_type.startswith("image/"):
                    has_image = True
                    if attachment.size > 5 * 1024 * 1024:
                        await message.reply(f"Waduh, gambar `{attachment.filename}` kegedean Bro (maksimal 5MB biar otak gw ga meleduk).")
                        return
                    
                    try:
                        image_bytes = await attachment.read()
                        base64_data = base64.b64encode(image_bytes).decode('utf-8')
                        content.append({
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{attachment.content_type};base64,{base64_data}"
                            }
                        })
                    except Exception as e:
                        print(f"[ERROR] Gagal memproses gambar: {e}")

            if not content:
                await message.reply("Yo, ada yang bisa gw bantu?")
                return

            # Untuk Groq, jika hanya teks, content string biasa lebih stabil, meski list of dicts didukung.
            # Namun karena kita pakai auto-switch model, kita bisa pass list of dicts.
            if not has_image:
                final_content = clean_prompt
            else:
                final_content = content

            chat_key = message.channel.id if not is_dm else message.author.id

            if chat_key not in conversation_histories:
                conversation_histories[chat_key] = []

            history = conversation_histories[chat_key]

            history.append({
                "role": "user",
                "content": final_content
            })

            if len(history) > MAX_HISTORY_TURNS:
                conversation_histories[chat_key] = history[-MAX_HISTORY_TURNS:]
                history = conversation_histories[chat_key]

            print(f"[LOG] Memproses pesan dari {message.author} di #{message.channel if not is_dm else 'DM'}")
            print(f"      Teks: '{clean_prompt}'")

            try:
                # Memanggil Groq API
                response_text = await generate_groq_content(history, has_image)

                # Simpan respons asisten ke memory
                if response_text:
                    history.append({
                        "role": "assistant",
                        "content": response_text
                    })

                    print(f"      [OK] Respons Groq sukses didapatkan!")

                    if len(response_text) > 2000:
                        parts = [response_text[i:i+1900] for i in range(0, len(response_text), 1900)]
                        for idx, part in enumerate(parts):
                            if idx == 0:
                                await message.reply(part)
                            else:
                                await message.channel.send(part)
                    else:
                        await message.reply(response_text)
                else:
                    await message.reply("Maaf, otak gw nge-blank (respon kosong dari API).")

            except Exception as e:
                print(f"      [ERROR] Gagal memanggil Groq/mengirim chat: {e}")
                await message.reply("Aduh sori Bro, kepala gw lagi pusing nih (ada kendala koneksi ke otak AI). Coba tanya lagi bentar ya!")
            finally:
                gc.collect()

def run_dummy_server():
    PORT = int(os.environ.get('PORT', 7860))
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
