import os
import json
from datetime import datetime, timedelta

from core.apikey import key_id, mask_key
from core.paths import BASE_DIR as BASE, REPORT_DIR


class ReportManager:


    def __init__(
        self,
        keep_days=30
    ):

        self.keep_days = keep_days

        os.makedirs(
            REPORT_DIR,
            exist_ok=True
        )


    def today(self):

        return datetime.now().strftime(
            "%Y-%m-%d"
        )



    def load_json(
        self,
        path
    ):

        if not os.path.exists(path):

            return {}

        with open(path, "r") as f:

            return json.load(f)



    def generate(self):

        state = self.load_json(
            f"{BASE}/data/state.json"
        )

        quota = self.load_json(
            f"{BASE}/data/quota.json"
        )

        keys = self.load_json(
            f"{BASE}/data/apikeys.json"
        )


        report = {

            "date": self.today(),

            "total_calls": 0,

            "providers":
                quota.get(
                    "providers",
                    {}
                ),

            "models":
                state.get(
                    "models",
                    {}
                ),

            # 只留短标识与脱敏值，日报里绝不能出现完整密钥
            "keys": {
                key_id(key): {
                    "masked": mask_key(key),
                    "name": info.get("name"),
                    "enabled": bool(info.get("enabled", False)),
                    "calls": info.get("calls", 0),
                    "created": info.get("created"),
                }
                for key, info in keys.get("keys", {}).items()
                if isinstance(info, dict)
            }
        }


        for item in report["models"].values():

            report["total_calls"] += item.get(
                "calls",
                0
            )


        filename = (
            f"{REPORT_DIR}/daily-{self.today()}.json"
        )


        with open(
            filename,
            "w"
        ) as f:

            json.dump(
                report,
                f,
                indent=2,
                ensure_ascii=False
            )


        self.cleanup()


        return filename



    def cleanup(self):

        limit = datetime.now() - timedelta(
            days=self.keep_days
        )


        for file in os.listdir(REPORT_DIR):

            if not file.startswith(
                "daily-"
            ):

                continue


            date = file[6:16]


            try:

                day = datetime.strptime(
                    date,
                    "%Y-%m-%d"
                )


                if day < limit:

                    os.remove(
                        f"{REPORT_DIR}/{file}"
                    )

            except:

                pass
