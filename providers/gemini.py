from .base import BaseProvider


class GeminiProvider(BaseProvider):
    name = "gemini"
    base_url = "https://generativelanguage.googleapis.com/v1beta/openai"
    env_file = "/etc/gemini.env"
    env_var = "GEMINI_API_KEY"
