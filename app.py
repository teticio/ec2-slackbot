"""
Main entry point for the application. This file is responsible for setting up the
"""

import os

import yaml
from flask import Flask

from ec2_slackbot.aws_handler import AWSHandler
from ec2_slackbot.instance_checker import InstanceChecker
from ec2_slackbot.slack_handler import SlackHandler
from ec2_slackbot.web_server import WebServer

app = Flask(__name__)


def main() -> None:
    """
    Main entry point for the application.
    """
    config = yaml.safe_load(open("config.yaml", "r", encoding="utf-8"))
    aws_handler = AWSHandler(config=config)
    slack_handler = SlackHandler(
        config=config,
        aws_handler=aws_handler,
        token=os.environ["SLACK_BOT_TOKEN"],
        signing_secret=os.environ["SLACK_SIGNING_SECRET"],
    )
    web_server = WebServer(config=config, slack_handler=slack_handler)
    periodic_checker = InstanceChecker(
        config=config, slack_handler=slack_handler, aws_handler=aws_handler
    )
    periodic_checker.start_periodic_checks(interval=config["check_interval_seconds"])
    web_server.run()


if __name__ == "__main__":
    main()
