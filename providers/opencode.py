from .base import BaseProvider


class OpenCodeProvider(BaseProvider):
    name = "opencode"
    base_url = "https://opencode.ai/zen/v1"
    env_file = "/etc/opencode.env"
    env_var = "OPENCODE_API_KEY"
