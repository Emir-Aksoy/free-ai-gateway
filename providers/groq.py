from .base import BaseProvider


class GroqProvider(BaseProvider):
    name = "groq"
    base_url = "https://api.groq.com/openai/v1"
    env_file = "/etc/groq.env"
    env_var = "GROQ_API_KEY"
