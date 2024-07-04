"""
This module contains the InstanceChecker class, which is responsible
for periodically checking the running instances.
"""

import threading
import time

from .slack_handler import SlackHandler


class InstanceChecker:
    """
    A class to periodically check the running instances.
    """

    def __init__(self, config, slack_handler: SlackHandler) -> None:
        self.config = config
        self.slack_handler = slack_handler
        self.aws_handler = slack_handler.aws_handler

    def periodically_check_instances(self) -> None:
        """
        Periodically check the running instances and send warnings if necessary.
        """
        user_ids = self.slack_handler.get_all_user_ids()
        instance_details = self.aws_handler.get_running_instance_details()

        for instance in instance_details:
            instance_id = instance["instance_id"]
            instance_type = instance["instance_type"]
            instance_cost = instance["instance_cost"]
            user_name = instance["user_name"]
            running_days = instance["running_days"]

            if user_name in user_ids:
                if running_days >= self.config["instance_warning_days"] or (
                    instance_cost >= self.config["large_instance_cost_threshold"]
                    and running_days >= self.config["large_instance_warning_days"]
                ):
                    self.slack_handler.send_warning(
                        user_ids[user_name],
                        instance_id,
                        instance_type,
                        running_days,
                    )

    def start_periodic_checks(self, interval: int) -> None:
        """
        Start the periodic checks with the given interval.
        """

        def run_checks() -> None:
            """
            Run the checks periodically.
            """
            while True:
                self.periodically_check_instances()
                time.sleep(interval)

        threading.Thread(target=run_checks, daemon=True).start()
