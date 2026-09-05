import os
import json
from datetime import datetime

from core.paths import LOG_DIR


class GatewayLogger:


    def __init__(
        self,
        max_size=10 * 1024 * 1024
    ):

        self.max_size = max_size

        os.makedirs(
            LOG_DIR,
            exist_ok=True
        )


    def _file(
        self,
        name
    ):

        return f"{LOG_DIR}/{name}.log"



    def _rotate(
        self,
        path
    ):

        if not os.path.exists(path):

            return


        if os.path.getsize(path) < self.max_size:

            return


        backup = (
            path +
            "." +
            datetime.now().strftime(
                "%Y%m%d%H%M%S"
            )
        )


        os.rename(
            path,
            backup
        )



    def write(
        self,
        name,
        data
    ):

        path = self._file(name)

        self._rotate(path)


        record = {

            "time":
                datetime.now().strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),

            **data
        }


        with open(
            path,
            "a"
        ) as f:

            f.write(
                json.dumps(
                    record,
                    ensure_ascii=False
                )
                +
                "\n"
            )



    def access(
        self,
        data
    ):

        self.write(
            "access",
            data
        )



    def error(
        self,
        data
    ):

        self.write(
            "error",
            data
        )
