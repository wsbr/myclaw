from urllib.parse import urlsplit
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.model_hub.parameters import validate_model_defaults


def _validate_model_capabilities(value: dict | None) -> dict | None:
    if value is None:
        return None
    model_kind = value.get("model_kind", "chat")
    if model_kind not in {"chat", "embedding"}:
        raise ValueError("model_kind must be chat or embedding")
    if model_kind == "embedding":
        dimensions = value.get("embedding_dimensions")
        if (
            isinstance(dimensions, bool) or not isinstance(dimensions, int)
            or dimensions < 8 or dimensions > 4096
        ):
            raise ValueError("embedding_dimensions must be an integer between 8 and 4096")
        batch_size = value.get("embedding_max_batch_size", 10)
        if (
            isinstance(batch_size, bool) or not isinstance(batch_size, int)
            or batch_size < 1 or batch_size > 2048
        ):
            raise ValueError("embedding_max_batch_size must be between 1 and 2048")
        return value
    structured_output = value.get("structured_output")
    if structured_output is not None and structured_output not in {"json_schema", "json_object", "none"}:
        raise ValueError("structured_output must be json_schema, json_object, or none")
    for key in ("context_window", "max_input_tokens", "max_output_tokens"):
        limit = value.get(key)
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0
        ):
            raise ValueError(f"{key} must be a positive integer")
    context_window = value.get("context_window")
    max_input_tokens = value.get("max_input_tokens")
    max_output_tokens = value.get("max_output_tokens")
    if (
        context_window is not None and max_input_tokens is not None
        and max_input_tokens > context_window
    ):
        raise ValueError("max_input_tokens must not exceed context_window")
    if (
        context_window is not None and max_output_tokens is not None
        and max_output_tokens > context_window
    ):
        raise ValueError("max_output_tokens must not exceed context_window")
    return value


def _validate_no_embedded_secrets(value):
    sensitive = {
        "apikey", "api_key", "authorization", "bearer", "bearer_token",
        "credential", "credentials", "password", "secret", "token",
        "access_token",
    }
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).strip().casefold().replace("-", "_")
            if normalized in sensitive or normalized.replace("_", "") in sensitive:
                raise ValueError(
                    "credentials must be submitted only through the write-only api_key field"
                )
            _validate_no_embedded_secrets(item)
    elif isinstance(value, list):
        for item in value:
            _validate_no_embedded_secrets(item)
    return value


def _validate_endpoint(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    parsed = urlsplit(normalized)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("endpoint must be an absolute HTTPS URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("endpoint must not contain credentials, query parameters, or fragments")
    if len(normalized) > 500:
        raise ValueError("endpoint is too long")
    return normalized


def _validate_default_output(parameters: dict, capabilities: dict) -> None:
    if capabilities.get("model_kind") == "embedding":
        allowed = {"dimensions", "encoding_format"}
        unexpected = sorted(set(parameters) - allowed)
        if unexpected:
            raise ValueError(
                "embedding default_parameters only support dimensions and encoding_format"
            )
        configured = parameters.get("dimensions")
        declared = capabilities.get("embedding_dimensions")
        if configured is not None and configured != declared:
            raise ValueError("embedding dimensions must equal embedding_dimensions capability")
        if parameters.get("encoding_format", "float") != "float":
            raise ValueError("embedding encoding_format must be float")
        return
    raw = parameters.get(
        "max_output_tokens", parameters.get("max_tokens", 8192),
    )
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise ValueError("default max output tokens must be a positive integer")
    declared = capabilities.get("max_output_tokens")
    if declared is not None and raw > declared:
        raise ValueError("default max output tokens must not exceed model capability")
    context_window = capabilities.get("context_window")
    if context_window is not None and raw >= context_window:
        raise ValueError("default max output tokens must be smaller than context_window")


class ModelCreate(BaseModel):
    key: str
    provider: str
    model_name: str
    endpoint: str | None = None
    api_key: str = Field(
        min_length=1,
        max_length=4096,
        repr=False,
        json_schema_extra={"writeOnly": True, "format": "password"},
    )
    default_parameters: dict = Field(default_factory=dict)
    capabilities: dict = Field(default_factory=dict)
    enabled: bool = True

    _capabilities_are_valid = field_validator("capabilities")(_validate_model_capabilities)
    _default_parameters_have_no_secrets = field_validator("default_parameters")(_validate_no_embedded_secrets)
    _default_parameters_are_safe = field_validator("default_parameters")(validate_model_defaults)
    _capabilities_have_no_secrets = field_validator("capabilities")(_validate_no_embedded_secrets)
    _endpoint_is_valid = field_validator("endpoint")(_validate_endpoint)

    @model_validator(mode="after")
    def _default_output_is_valid(self):
        _validate_default_output(self.default_parameters, self.capabilities)
        return self


class LogicalModelCreate(BaseModel):
    key: str
    name: str


class ModelVersionCreate(BaseModel):
    provider: str
    model_name: str
    endpoint: str
    api_key: str = Field(
        min_length=1,
        max_length=4096,
        repr=False,
        json_schema_extra={"writeOnly": True, "format": "password"},
    )
    default_parameters: dict = Field(default_factory=dict)
    capabilities: dict

    _capabilities_are_valid = field_validator("capabilities")(_validate_model_capabilities)
    _default_parameters_have_no_secrets = field_validator("default_parameters")(_validate_no_embedded_secrets)
    _default_parameters_are_safe = field_validator("default_parameters")(validate_model_defaults)
    _capabilities_have_no_secrets = field_validator("capabilities")(_validate_no_embedded_secrets)
    _endpoint_is_valid = field_validator("endpoint")(_validate_endpoint)

    @model_validator(mode="after")
    def _default_output_is_valid(self):
        _validate_default_output(self.default_parameters, self.capabilities)
        return self


class ModelPatch(BaseModel):
    endpoint: str | None = None
    api_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=4096,
        repr=False,
        json_schema_extra={"writeOnly": True, "format": "password"},
    )
    default_parameters: dict | None = None
    capabilities: dict | None = None
    enabled: bool | None = None

    _capabilities_are_valid = field_validator("capabilities")(_validate_model_capabilities)
    _default_parameters_have_no_secrets = field_validator("default_parameters")(_validate_no_embedded_secrets)
    _default_parameters_are_safe = field_validator("default_parameters")(validate_model_defaults)
    _capabilities_have_no_secrets = field_validator("capabilities")(_validate_no_embedded_secrets)
    _endpoint_is_valid = field_validator("endpoint")(_validate_endpoint)


class PlatformToolPatch(BaseModel):
    display_name: str | None = None
    display_name_zh: str | None = None
    enabled: bool | None = None
    confirmation_policy: str | None = None
    network_policy: dict | None = None
    workspace_policy: dict | None = None
    risk_rules: list[dict] | None = None


class ToolConnectionUpsert(BaseModel):
    connection_key: str = Field(min_length=1, max_length=191, pattern=r"^[a-z][a-z0-9_.-]*$")
    display_name: str = Field(min_length=1, max_length=200)
    connection_type: Literal["bocha", "brave", "bailian", "mcp", "browser"]
    endpoint: str
    auth_type: Literal["none", "bearer", "x_api_key"] = "none"
    api_key: str | None = Field(
        default=None, min_length=1, max_length=4096, repr=False,
        json_schema_extra={"writeOnly": True, "format": "password"},
    )
    config: dict = Field(default_factory=dict)
    enabled: bool = True
    is_primary: bool = False


class ToolConnectionPatch(BaseModel):
    display_name: str
    endpoint: str
    auth_type: Literal["none", "bearer", "x_api_key"]
    api_key: str | None = Field(
        default=None, min_length=1, max_length=4096, repr=False,
        json_schema_extra={"writeOnly": True, "format": "password"},
    )
    config: dict = Field(default_factory=dict)
    enabled: bool = True
    is_primary: bool = False


class PolicyPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    default_parameters: dict | None = None
    enabled: bool | None = None


class SandboxPatch(BaseModel):
    description: str | None = None
    config: dict | None = None
    enabled: bool | None = None
