from app.core.model_hub.base import BaseLLM
from app.core.model_hub.anthropic import AnthropicLLM
from app.core.model_hub.openai import OpenAILLM

_OPENAI_COMPATIBLE = {"openai", "openai_compatible", "azure_openai", "ollama"}


class ModelHub:
    @staticmethod
    def get(model_config: dict, provider_type: str, api_key: str, base_url: str | None = None) -> BaseLLM:
        if provider_type == "anthropic":
            return AnthropicLLM(api_key=api_key, base_url=base_url)
        elif provider_type in _OPENAI_COMPATIBLE:
            return OpenAILLM(api_key=api_key, base_url=base_url)
        raise ValueError(f"Unsupported provider: {provider_type}")
