from __future__ import annotations
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from fastapi import HTTPException
from app.models.tool import ModelProvider, AvailableModel
from app.models.user import User
from app.schemas.model_provider import ModelProviderCreate
import uuid
import httpx


class ModelProviderService:

    @staticmethod
    async def list(db: AsyncSession) -> list[ModelProvider]:
        result = await db.execute(select(ModelProvider).order_by(ModelProvider.name))
        return result.scalars().all()

    @staticmethod
    async def create(db: AsyncSession, admin: User, body: ModelProviderCreate) -> ModelProvider:
        provider = ModelProvider(
            id=str(uuid.uuid4()),
            name=body.name,
            provider=body.provider,
            base_url=body.base_url,
            # TODO: encrypt api_key before storing
            api_key_encrypted=body.api_key,
            created_by=admin.id,
        )
        db.add(provider)
        await db.commit()
        await db.refresh(provider)
        return provider

    @staticmethod
    async def delete(db: AsyncSession, provider_id: str) -> None:
        result = await db.execute(select(ModelProvider).where(ModelProvider.id == provider_id))
        provider = result.scalar_one_or_none()
        if not provider:
            raise HTTPException(status_code=404, detail="Provider not found")
        await db.delete(provider)
        await db.commit()

    @staticmethod
    async def set_active(db: AsyncSession, provider_id: str, is_active: bool) -> ModelProvider:
        result = await db.execute(select(ModelProvider).where(ModelProvider.id == provider_id))
        provider = result.scalar_one_or_none()
        if not provider:
            raise HTTPException(status_code=404, detail="Provider not found")
        provider.is_active = is_active
        await db.commit()
        await db.refresh(provider)
        return provider

    @staticmethod
    async def test_connection(db: AsyncSession, provider_id: str) -> dict:
        result = await db.execute(select(ModelProvider).where(ModelProvider.id == provider_id))
        provider = result.scalar_one_or_none()
        if not provider:
            raise HTTPException(status_code=404, detail="Provider not found")

        api_key = provider.api_key_encrypted or ""
        base_url = provider.base_url or ""

        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                if provider.provider in ("openai", "openai_compatible"):
                    if not base_url and provider.provider == "openai_compatible":
                        return {"ok": False, "message": "OpenAI Compatible 必须填写 Base URL"}
                    url = (base_url.rstrip("/") or "https://api.openai.com/v1") + "/models"
                    r = await c.get(url, headers={"Authorization": f"Bearer {api_key}"})
                elif provider.provider == "azure_openai":
                    url = base_url.rstrip("/") + "/openai/models?api-version=2024-02-01"
                    r = await c.get(url, headers={"api-key": api_key})
                elif provider.provider == "anthropic":
                    url = (base_url.rstrip("/") or "https://api.anthropic.com") + "/v1/models"
                    r = await c.get(url, headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                    })
                elif provider.provider == "ollama":
                    url = (base_url.rstrip("/") or "http://localhost:11434") + "/api/tags"
                    r = await c.get(url)
                else:
                    # 通用测试：GET base_url，能通就算成功
                    if not base_url:
                        return {"ok": False, "message": "未知提供商且未配置 Base URL"}
                    r = await c.get(base_url)

            if r.status_code < 400:
                return {"ok": True, "message": f"连接成功 (HTTP {r.status_code})"}
            else:
                return {"ok": False, "message": f"HTTP {r.status_code}: {r.text[:200]}"}
        except httpx.ConnectError:
            return {"ok": False, "message": "连接失败：无法访问该地址"}
        except httpx.TimeoutException:
            return {"ok": False, "message": "连接超时（10s）"}
        except Exception as e:
            return {"ok": False, "message": f"错误：{str(e)[:200]}"}

    @staticmethod
    async def list_models(db: AsyncSession, provider_id: str) -> list[AvailableModel]:
        result = await db.execute(
            select(AvailableModel).where(
                AvailableModel.provider_id == provider_id,
                AvailableModel.is_active == True,
            )
        )
        return result.scalars().all()
