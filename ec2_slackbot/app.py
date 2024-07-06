"""
Main entry point for the application. This file is responsible for setting up the
"""

import argparse
import os
from argparse import Namespace
from typing import Dict

import yaml

from .aws_handler import AWSHandler
from .slack_handler import SlackHandler
from .web_server import WebServer


def create_web_server(arguments: Namespace) -> WebServer:
    """
    Create the web server with the given arguments.
    """
    with open(arguments.config, "r", encoding="utf-8") as file:
        config = yaml.safe_load(file)
    aws_handler = AWSHandler(
        config=config, endpoint_url=os.environ.get("AWS_ENDPOINT_URL")
    )
    slack_handler = SlackHandler(
        config=config,
        aws_handler=aws_handler,
        token=os.environ["SLACK_BOT_TOKEN"],
        signing_secret=os.environ["SLACK_SIGNING_SECRET"],
    )
    web_server = WebServer(config=config, slack_handler=slack_handler)

    @web_server.app.route("/healthz", methods=["GET"])
    def healthz() -> Dict[str, str]:
        """
        Health check endpoint.
        """
        return {"status": "ok"}

    return web_server


def main() -> None:
    """
    Main entry point for the application.
    """
    parser = argparse.ArgumentParser(description="EC2 Slackbot Application")
    parser.add_argument(
        "--config",
        type=str,
        required=True,
        help="Path to the configuration file",
    )
    args = parser.parse_args()
    create_web_server(args).run()


if __name__ == "__main__":
    main()
