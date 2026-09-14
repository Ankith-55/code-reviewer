from contextlib import asynccontextmanager
import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes.review import router as review_router
from core.config import settings
from database.database import init_db

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize DB tables on startup
    try:
        init_db()
        logger.info("Database schema initialized.")
    except Exception as e:
        logger.warning(f"Database initialization deferred (broker/db may not be ready yet): {e}")
    yield


app = FastAPI(
    title="Distributed AI Code Review API",
    description="Scalable, asynchronous distributed code review system using RabbitMQ and LLM analysis.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(review_router)


@app.get("/", tags=["health"])
async def root():
    return {
        "service": "Distributed AI Code Review System",
        "status": "online",
        "docs_url": "/docs",
    }


@app.get("/health", tags=["health"])
async def health():
    return {
        "status": "healthy",
        "environment": settings.APP_ENV,
    }
