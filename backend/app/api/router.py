from fastapi import APIRouter
from app.api import accounts, artifacts, audit, auth, agents, agent_options, clarifications, groups, identity, memories, plans, registries_v1, required_actions, skills, sessions, runs, admin, notifications

api_router = APIRouter(prefix="/api/v1")

api_router.include_router(auth.router)
api_router.include_router(accounts.router)
api_router.include_router(audit.router)
api_router.include_router(identity.router)
api_router.include_router(plans.router)
api_router.include_router(groups.router)
api_router.include_router(artifacts.router)
api_router.include_router(memories.router)
api_router.include_router(agents.router)
api_router.include_router(agent_options.router)
api_router.include_router(registries_v1.router)
api_router.include_router(required_actions.router)
api_router.include_router(clarifications.router)
api_router.include_router(skills.router)
api_router.include_router(sessions.router)
api_router.include_router(runs.router)
api_router.include_router(notifications.router)
api_router.include_router(admin.router)
