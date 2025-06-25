# main.py - Optimized FastAPI MCP
from fastapi import FastAPI, HTTPException, Depends, Security, BackgroundTasks
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
import redis.asyncio as redis
import json
import asyncio
from typing import Dict, Optional, List
from datetime import datetime, timedelta
import logging
from contextlib import asynccontextmanager
import os
from dataclasses import dataclass
import uuid
import hashlib
import aiohttp

# Configuration
@dataclass
class ClientConfig:
    client_id: str
    name: str
    api_key: str
    webhook_url: Optional[str]
    testmyprompt_id: str
    channels: List[str]
    rate_limit: int  # requests per minute
    features: Dict[str, bool]
    created_at: datetime
    is_active: bool

class ConfigManager:
    def __init__(self):
        self.clients: Dict[str, ClientConfig] = {}
        self.last_reload = datetime.now()
    
    async def load_clients(self):
        """Load client configurations from multiple sources"""
        
        # 1. Load from clients.json
        try:
            with open('config/clients.json', 'r') as f:
                clients_data = json.load(f)
                
            for client_data in clients_data.get('clients', []):
                config = ClientConfig(
                    client_id=client_data['client_id'],
                    name=client_data['name'],
                    api_key=client_data['api_key'],
                    webhook_url=client_data.get('webhook_url'),
                    testmyprompt_id=client_data['testmyprompt_id'],
                    channels=client_data.get('channels', []),
                    rate_limit=client_data.get('rate_limit', 60),
                    features=client_data.get('features', {}),
                    created_at=datetime.fromisoformat(client_data.get('created_at', datetime.now().isoformat())),
                    is_active=client_data.get('is_active', True)
                )
                self.clients[client_data['client_id']] = config
                
        except FileNotFoundError:
            logging.warning("clients.json not found, using environment variables")
        
        # 2. Load from environment variables (for Railway deployment)
        env_clients = os.getenv('MCP_CLIENTS')
        if env_clients:
            try:
                env_data = json.loads(env_clients)
                for client_data in env_data:
                    # Same processing as above
                    pass
            except json.JSONDecodeError:
                logging.error("Invalid MCP_CLIENTS environment variable")
        
        # 3. Load from Redis (for dynamic client management)
        await self.load_from_redis()
        
        self.last_reload = datetime.now()
        logging.info(f"Loaded {len(self.clients)} clients")
    
    async def load_from_redis(self):
        """Load dynamic clients from Redis"""
        try:
            redis_client = await redis.from_url(os.getenv('REDIS_URL'))
            clients_keys = await redis_client.keys('client_config:*')
            
            for key in clients_keys:
                client_data = await redis_client.hgetall(key)
                if client_data:
                    # Process client data from Redis
                    pass
                    
        except Exception as e:
            logging.error(f"Failed to load clients from Redis: {e}")
    
    def get_client(self, client_id: str) -> Optional[ClientConfig]:
        return self.clients.get(client_id)
    
    def get_client_by_api_key(self, api_key: str) -> Optional[ClientConfig]:
        for client in self.clients.values():
            if client.api_key == api_key and client.is_active:
                return client
        return None

# Global instances
config_manager = ConfigManager()

# Lifespan management
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await config_manager.load_clients()
    await initialize_redis()
    await start_background_tasks()
    yield
    # Shutdown
    await cleanup_resources()

app = FastAPI(
    title="AI Agent MCP Multi-Tenant",
    description="Scalable AI Agent with Model Context Protocol",
    version="2.0.0",
    lifespan=lifespan
)

# Middleware
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Security
security = HTTPBearer()

class SecurityManager:
    @staticmethod
    async def verify_api_key(credentials: HTTPAuthorizationCredentials = Security(security)) -> ClientConfig:
        """Verify API key and return client config"""
        api_key = credentials.credentials
        
        client = config_manager.get_client_by_api_key(api_key)
        if not client:
            raise HTTPException(status_code=401, detail="Invalid API key")
        
        if not client.is_active:
            raise HTTPException(status_code=403, detail="Client account suspended")
        
        return client

# Redis Manager
class RedisManager:
    def __init__(self):
        self.redis_client = None
        self.connection_pool = None
    
    async def initialize(self):
        """Initialize Redis connection"""
        self.redis_client = await redis.from_url(
            os.getenv('REDIS_URL'),
            encoding="utf-8",
            decode_responses=True,
            retry_on_timeout=True,
            health_check_interval=30
        )
        
        # Test connection
        await self.redis_client.ping()
        logging.info("Redis connection established")
    
    async def get_context(self, client_id: str, user_id: str) -> Dict:
        """Get user context for specific client"""
        try:
            key = f"context:{client_id}:{user_id}"
            context_data = await self.redis_client.hgetall(key)
            
            if not context_data:
                # Initialize new context
                default_context = {
                    "user_id": user_id,
                    "client_id": client_id,
                    "conversation_history": "[]",
                    "user_profile": "{}",
                    "session_data": "{}",
                    "created_at": datetime.now().isoformat(),
                    "updated_at": datetime.now().isoformat(),
                    "message_count": "0",
                    "last_channel": "",
                    "context_summary": ""
                }
                await self.redis_client.hset(key, mapping=default_context)
                # Set expiration (e.g., 30 days)
                await self.redis_client.expire(key, 2592000)
                return default_context
            
            return context_data
            
        except redis.RedisError as e:
            logging.error(f"Redis error getting context: {e}")
            raise HTTPException(status_code=500, detail="Context retrieval failed")
    
    async def update_context(self, client_id: str, user_id: str, context_data: Dict):
        """Update user context"""
        try:
            key = f"context:{client_id}:{user_id}"
            context_data["updated_at"] = datetime.now().isoformat()
            
            await self.redis_client.hset(key, mapping=context_data)
            await self.redis_client.expire(key, 2592000)  # 30 days
            
            # Update client usage metrics
            await self.update_usage_metrics(client_id)
            
        except redis.RedisError as e:
            logging.error(f"Redis error updating context: {e}")
            raise HTTPException(status_code=500, detail="Context update failed")
    
    async def update_usage_metrics(self, client_id: str):
        """Update client usage metrics"""
        today = datetime.now().strftime('%Y-%m-%d')
        metrics_key = f"metrics:{client_id}:{today}"
        
        await self.redis_client.hincrby(metrics_key, "requests", 1)
        await self.redis_client.expire(metrics_key, 86400 * 7)  # 7 days

redis_manager = RedisManager()

# Rate Limiter
class RateLimiter:
    def __init__(self):
        self.redis_client = None
    
    async def check_rate_limit(self, client_id: str, limit: int) -> bool:
        """Check if client has exceeded rate limit"""
        if not self.redis_client:
            self.redis_client = redis_manager.redis_client
        
        key = f"rate_limit:{client_id}:{datetime.now().strftime('%Y-%m-%d-%H-%M')}"
        
        current_count = await self.redis_client.incr(key)
        if current_count == 1:
            await self.redis_client.expire(key, 60)  # 1 minute window
        
        return current_count <= limit

rate_limiter = RateLimiter()

# AI Processing
class AIProcessor:
    def __init__(self):
        self.testmyprompt_cache = {}
    
    async def process_message(self, message: str, context: Dict, client_config: ClientConfig) -> str:
        """Process message with AI using TestMyPrompt"""
        
        try:
            # Get prompt template
            prompt_template = await self.get_prompt_template(client_config.testmyprompt_id)
            
            # Build context-aware prompt
            full_prompt = await self.build_prompt(message, context, prompt_template)
            
            # Call TestMyPrompt API
            response = await self.call_testmyprompt(full_prompt, client_config.testmyprompt_id)
            
            return response
            
        except Exception as e:
            logging.error(f"AI processing error for client {client_config.client_id}: {e}")
            return "I apologize, but I'm having trouble processing your message right now. Please try again."
    
    async def get_prompt_template(self, testmyprompt_id: str) -> str:
        """Get prompt template from TestMyPrompt"""
        if testmyprompt_id in self.testmyprompt_cache:
            cached_data = self.testmyprompt_cache[testmyprompt_id]
            if datetime.now() - cached_data['timestamp'] < timedelta(hours=1):
                return cached_data['template']
        
        # Fetch from TestMyPrompt API
        # Implementation depends on TestMyPrompt API
        template = "You are a helpful AI assistant. Context: {context}\n\nUser: {message}\n\nAssistant:"
        
        self.testmyprompt_cache[testmyprompt_id] = {
            'template': template,
            'timestamp': datetime.now()
        }
        
        return template
    
    async def build_prompt(self, message: str, context: Dict, template: str) -> str:
        """Build final prompt with context"""
        conversation_history = json.loads(context.get('conversation_history', '[]'))
        user_profile = json.loads(context.get('user_profile', '{}'))
        
        context_summary = f"""
        User Profile: {user_profile}
        Recent Conversations: {len(conversation_history)} messages
        Last Channel: {context.get('last_channel', 'unknown')}
        Context Summary: {context.get('context_summary', 'New conversation')}
        """
        
        return template.format(context=context_summary, message=message)
    
    async def call_testmyprompt(self, prompt: str, testmyprompt_id: str) -> str:
        """Call TestMyPrompt API"""
        # Implementation for TestMyPrompt API call
        # This is a placeholder - implement based on actual API
        
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"https://api.testmyprompt.com/v1/prompts/{testmyprompt_id}/run",
                json={"prompt": prompt},
                headers={"Authorization": f"Bearer {os.getenv('TESTMYPROMPT_API_KEY')}"}
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    return result.get('response', 'No response generated')
                else:
                    raise Exception(f"TestMyPrompt API error: {response.status}")

ai_processor = AIProcessor()

# Webhook Manager
class WebhookManager:
    async def send_webhook(self, client_config: ClientConfig, data: Dict):
        """Send webhook to client"""
        if not client_config.webhook_url:
            return
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    client_config.webhook_url,
                    json=data,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    if response.status != 200:
                        logging.warning(f"Webhook failed for client {client_config.client_id}: {response.status}")
                        
        except Exception as e:
            logging.error(f"Webhook error for client {client_config.client_id}: {e}")

webhook_manager = WebhookManager()

# Main Chat Endpoint
@app.post("/chat")
async def chat_endpoint(
    request: Dict,
    background_tasks: BackgroundTasks,
    client: ClientConfig = Depends(SecurityManager.verify_api_key)
):
    """Main chat endpoint"""
    
    try:
        # Validate request
        if 'message' not in request or 'user_id' not in request:
            raise HTTPException(status_code=400, detail="Missing required fields")
        
        user_id = request['user_id']
        message = request['message']
        channel = request.get('channel', 'api')
        
        # Check rate limit
        if not await rate_limiter.check_rate_limit(client.client_id, client.rate_limit):
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
        
        # Get user context
        context = await redis_manager.get_context(client.client_id, user_id)
        
        # Process with AI
        ai_response = await ai_processor.process_message(message, context, client)
        
        # Update context
        conversation_history = json.loads(context.get('conversation_history', '[]'))
        conversation_history.append({
            "timestamp": datetime.now().isoformat(),
            "user": message,
            "assistant": ai_response,
            "channel": channel
        })
        
        # Keep only last 20 messages to manage memory
        if len(conversation_history) > 20:
            conversation_history = conversation_history[-20:]
        
        updated_context = {
            **context,
            "conversation_history": json.dumps(conversation_history),
            "message_count": str(int(context.get('message_count', 0)) + 1),
            "last_channel": channel,
            "context_summary": ai_response[:200] + "..." if len(ai_response) > 200 else ai_response
        }
        
        await some_async_function()
