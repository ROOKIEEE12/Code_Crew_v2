"""Provider-agnostic LLM layer. Free tiers are the default, by design."""

from .base import (
    Completion,
    LLMError,
    Message,
    Provider,
    ProviderUnavailable,
    RateLimited,
    Usage,
)
from .client import (
    DEFAULT_TIERS,
    LLMClient,
    LLMConfig,
    ModelTiers,
    TaskKind,
    provider_status,
    select_provider,
)
from .providers import (
    AUTO_ORDER,
    PROVIDERS,
    AnthropicProvider,
    GeminiProvider,
    GroqProvider,
    OllamaProvider,
    ScriptedProvider,
)

__all__ = [
    "Completion",
    "LLMError",
    "Message",
    "Provider",
    "ProviderUnavailable",
    "RateLimited",
    "Usage",
    "LLMClient",
    "LLMConfig",
    "ModelTiers",
    "TaskKind",
    "DEFAULT_TIERS",
    "select_provider",
    "provider_status",
    "PROVIDERS",
    "AUTO_ORDER",
    "GroqProvider",
    "GeminiProvider",
    "OllamaProvider",
    "AnthropicProvider",
    "ScriptedProvider",
]
