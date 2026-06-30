from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.deps import get_current_user, get_current_admin
from app.models.user import User
from app.services.tool_service import ToolService
from app.schemas.tool import ToolCreate, ToolUpdate, ToolResponse

router = APIRouter(prefix="/tools", tags=["tools"])


@router.get("", response_model=list[ToolResponse])
async def list_tools(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    return await ToolService.list(db)


@router.post("", response_model=ToolResponse, status_code=201)
async def create_tool(
    body: ToolCreate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    return await ToolService.create(db, admin, body)


@router.put("/{tool_id}", response_model=ToolResponse)
async def update_tool(
    tool_id: str,
    body: ToolUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    return await ToolService.update(db, tool_id, body)


@router.delete("/{tool_id}", status_code=204)
async def delete_tool(
    tool_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    await ToolService.delete(db, tool_id)
