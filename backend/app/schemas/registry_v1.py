from urllib.parse import urlsplit
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


def _validate_model_capabilities(value: dict | None) -> dict | None:
    if value is None:
        return None
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
