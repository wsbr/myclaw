from pydantic import BaseModel, Field, field_validator


def _validate_model_capabilities(value: dict | None) -> dict | None:
    if value is None:
        return None
    structured_output = value.get("structured_output")
    if structured_output is not None and structured_output not in {"json_schema", "json_object", "none"}:
        raise ValueError("structured_output must be json_schema, json_object, or none")
    return value


class ModelCreate(BaseModel):
    key: str
    provider: str
    model_name: str
    endpoint: str | None = None
    credential_ref: str | None = None
    default_parameters: dict = Field(default_factory=dict)
    capabilities: dict = Field(default_factory=dict)
    enabled: bool = True

    _capabilities_are_valid = field_validator("capabilities")(_validate_model_capabilities)


class LogicalModelCreate(BaseModel):
    key: str
    name: str


class ModelVersionCreate(BaseModel):
    provider: str
    model_name: str
    endpoint: str | None = None
    credential_ref: str | None = None
    default_parameters: dict = Field(default_factory=dict)
    capabilities: dict

    _capabilities_are_valid = field_validator("capabilities")(_validate_model_capabilities)


class ModelPatch(BaseModel):
    endpoint: str | None = None
    credential_ref: str | None = None
    default_parameters: dict | None = None
    capabilities: dict | None = None
    enabled: bool | None = None

    _capabilities_are_valid = field_validator("capabilities")(_validate_model_capabilities)


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
