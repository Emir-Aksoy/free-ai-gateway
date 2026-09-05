from .base import BaseProvider


class OpenRouterProvider(BaseProvider):
    name = "openrouter"
    base_url = "https://openrouter.ai/api/v1"
    env_file = "/etc/openrouter.env"
    env_var = "OPENROUTER_API_KEY"
