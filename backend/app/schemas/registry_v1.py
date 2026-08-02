from pydantic import BaseModel, Field


class ModelCreate(BaseModel):
    key: str
    provider: str
    model_name: str
    endpoint: str | None = None
    credential_ref: str | None = None
    default_parameters: dict = Field(default_factory=dict)
    capabilities: dict = Field(default_factory=dict)
    enabled: bool = True


class ModelPatch(BaseModel):
    endpoint: str | None = None
    credential_ref: str | None = None
    default_parameters: dict | None = None
    capabilities: dict | None = None
    enabled: bool | None = None


class PlatformToolPatch(BaseModel):
    display_name: str | None = None
    description: str | None = None
    enabled: bool | None = None
    confirmation_policy: str | None = None
    platform_config: dict | None = None


class PolicyPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    default_parameters: dict | None = None
    enabled: bool | None = None


class SandboxPatch(BaseModel):
    description: str | None = None
    config: dict | None = None
    enabled: bool | None = None
