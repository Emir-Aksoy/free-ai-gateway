from .base import BaseProvider


class AgnesProvider(BaseProvider):
    name = "agnes"
    base_url = "https://apihub.agnes-ai.com/v1"
    env_file = "/etc/agnes.env"
    env_var = "AGNES_API_KEY"
