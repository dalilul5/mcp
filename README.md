# Minimal MCP Backend (FastAPI + Redis)

Simple MCP backend that stores user context per client with Redis.

## Features
- FastAPI endpoint `/chat`
- Stores last 10 messages per client+user
- Ready to deploy to Railway

## How to Run
```
pip install -r requirements.txt
uvicorn main:app --reload
```

## Deploy to Railway
- Push to GitHub
- Connect Redis plugin
- Add `REDIS_URL` in environment