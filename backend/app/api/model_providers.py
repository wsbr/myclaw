from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.deps import get_current_user, get_current_admin
from app.models.user import User
from app.services.model_provider_service import ModelProviderService
from app.schemas.model_provider import ModelProviderCreate, ModelProviderResponse, AvailableModelResponse

router = APIRouter(prefix="/model-providers", tags=["model-providers"])


@router.get("", response_model=list[ModelProviderResponse])
async def list_providers(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_admin)):
    return await ModelProviderService.list(db)


@router.post("", response_model=ModelProviderResponse, status_code=201)
async def create_provider(
    body: ModelProviderCreate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    return await ModelProviderService.create(db, admin, body)


@router.delete("/{provider_id}", status_code=204)
async def delete_provider(
    provider_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    await ModelProviderService.delete(db, provider_id)


@router.post("/{provider_id}/test")
async def test_provider(
    provider_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    return await ModelProviderService.test_connection(db, provider_id)


@router.patch("/{provider_id}/activate", response_model=ModelProviderResponse)
async def activate_provider(
    provider_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    return await ModelProviderService.set_active(db, provider_id, True)


@router.patch("/{provider_id}/deactivate", response_model=ModelProviderResponse)
async def deactivate_provider(
    provider_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    return await ModelProviderService.set_active(db, provider_id, False)


@router.get("/{provider_id}/models", response_model=list[AvailableModelResponse])
async def list_models(
    provider_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await ModelProviderService.list_models(db, provider_id)
