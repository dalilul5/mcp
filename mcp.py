import os
import json
import logging
from fastapi import FastAPI, HTTPException, BackgroundTasks, Header
from pydantic import BaseModel
import redis
import aiohttp
import asyncio

app = FastAPI()

# Konfigurasi logging
logger = logging.getLogger("mcp")
logging.basicConfig(level=logging.INFO)

# Konfigurasi Redis dari environment variables
redis_host = os.getenv("REDIS_HOST", "localhost")
redis_port = int(os.getenv("REDIS_PORT", 6379))
redis_password = os.getenv("REDIS_PASSWORD", "")
redis_client = redis.Redis(host=redis_host, port=redis_port, password=redis_password)

# Load konfigurasi klien dari clients.json
with open("clients.json", "r") as f:
    clients_config = json.load(f)

class ChatRequest(BaseModel):
    client_id: str
    user_id: str
    message: str

async def send_to_webhook(url: str, data: dict):
    """Mengirim data ke webhook klien secara asinkronus."""
    async with aiohttp.ClientSession() as session:
        await session.post(url, json=data)

async def get_context(key: str):
    """Mengambil konteks dari Redis secara asinkronus."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, redis_client.get, key)

async def set_context(key: str, value: str, ttl: int):
    """Menyimpan konteks di Redis dengan TTL secara asinkronus."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, redis_client.setex, key, ttl, value)

@app.post("/chat")
async def chat(request: ChatRequest, background_tasks: BackgroundTasks, x_api_key: str = Header(None)):
    """Menangani permintaan chat, mengelola konteks, dan berinteraksi dengan penyedia AI."""
    # Autentikasi permintaan
    expected_auth_key = os.getenv(f"CLIENT_{request.client_id}_AUTH_KEY")
    if not expected_auth_key or x_api_key != expected_auth_key:
        raise HTTPException(status_code=403, detail="Invalid API key")

    # Ambil konfigurasi klien
    client_config = clients_config.get("clients", {}).get(request.client_id)
    if not client_config:
        raise HTTPException(status_code=404, detail="Client not found")

    # Ambil konteks dari Redis
    context_key = f"{request.client_id}:{request.user_id}"
    context = await get_context(context_key)
    context = json.loads(context) if context else []

    # Siapkan pesan untuk AI
    system_message = {"role": "system", "content": client_config["prompt"]}
    conversation = context + [{"role": "user", "content": request.message}]

    # Dapatkan respons AI
    provider = client_config["provider"]
    model = client_config["model"]
    api_key = os.getenv(f"CLIENT_{request.client_id}_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="API key not configured")

    if provider == "openai":
        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": model,
                "messages": [system_message] + conversation
            }
            async with session.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload) as resp:
                if resp.status != 200:
                    raise HTTPException(status_code=500, detail="AI API error")
                response = await resp.json()
                ai_response = response["choices"][0]["message"]["content"]
    elif provider == "testmyprompt":
        # Placeholder untuk API Testmyprompt.com
        ai_response = "Test response"  # Ganti dengan panggilan API aktual
    else:
        raise HTTPException(status_code=400, detail="Unknown provider")

    # Perbarui konteks (simpan 10 interaksi terakhir)
    context.append({"role": "user", "content": request.message})
    context.append({"role": "assistant", "content": ai_response})
    if len(context) > 20:  # 10 interaksi (user + assistant)
        context = context[-20:]
    await set_context(context_key, json.dumps(context), 604800)  # TTL 7 hari

    # Kirim respons ke webhook di latar belakang
    webhook_data = {"user_id": request.user_id, "response": ai_response}
    background_tasks.add_task(send_to_webhook, client_config["webhook_url"], webhook_data)

    return {"response": ai_response}
