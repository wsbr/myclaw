from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.deps import get_current_user
from app.models.user import User
from app.services.mcp_service import McpService
from app.schemas.mcp_server import McpServerCreate, McpServerUpdate, McpServerResponse

router = APIRouter(prefix="/mcp-servers", tags=["mcp-servers"])


@router.get("", response_model=list[McpServerResponse])
async def list_mcp_servers(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    return await McpService.list(db)


@router.post("", response_model=McpServerResponse, status_code=201)
async def create_mcp_server(
    body: McpServerCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await McpService.create(db, current_user, body)


@router.put("/{server_id}", response_model=McpServerResponse)
async def update_mcp_server(
    server_id: str,
    body: McpServerUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await McpService.update(db, server_id, current_user, body)


@router.delete("/{server_id}", status_code=204)
async def delete_mcp_server(
    server_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await McpService.delete(db, server_id, current_user)


@router.post("/{server_id}/health")
async def check_health(
    server_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await McpService.health_check(db, server_id)
