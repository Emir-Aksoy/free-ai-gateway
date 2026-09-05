import json
import os

from core.paths import CAPABILITY_FILE


class CapabilityManager:

    def __init__(self):
        self.data = self.load()


    def load(self):

        if not os.path.exists(CAPABILITY_FILE):

            return {
                "models": {}
            }

        with open(
            CAPABILITY_FILE,
            "r"
        ) as f:

            return json.load(f)


    def get(self, model):

        return self.data.get(
            "models",
            {}
        ).get(
            model,
            {
                "agent": 50,
                "thinking": 50,
                "coding": 50
            }
        )
