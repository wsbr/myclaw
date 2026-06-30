from fastapi import APIRouter
from app.api import auth, agents, tools, skills, mcp_servers, model_providers, tasks, sessions, admin

api_router = APIRouter(prefix="/api/v1")

api_router.include_router(auth.router)
api_router.include_router(agents.router)
api_router.include_router(tools.router)
api_router.include_router(skills.router)
api_router.include_router(mcp_servers.router)
api_router.include_router(model_providers.router)
api_router.include_router(tasks.router)
api_router.include_router(sessions.router)
api_router.include_router(admin.router)
