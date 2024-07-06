"""
This module contains the WebServer class, which is responsible
for handling the web server for the application.
"""

from typing import Any, Dict

from flask import Flask, Response, abort, request

from .instance_checker import InstanceChecker
from .slack_handler import SlackHandler


class WebServer:
    """
    A class to handle the web server for the application.
    """

    def __init__(self, config: Dict[str, Any], slack_handler: SlackHandler) -> None:
        self.app = Flask(__name__)
        self.config = config
        self.slack_handler = slack_handler
        self.setup_routes()
        self.instance_checker = InstanceChecker(
            config=config, slack_handler=slack_handler
        )
        self.instance_checker.start_periodic_checks(
            interval=config["check_interval_seconds"]
        )

    def setup_routes(self) -> None:
        """
        Set up the routes for the web server.
        """

        @self.app.before_request
        def verify_slack_signature() -> None:
            """
            Verify the Slack signature before processing the request.
            """
            if request.path.startswith(
                "/slack/"
            ) and not self.slack_handler.verifier.is_valid_request(
                body=request.get_data(), headers=request.headers
            ):
                abort(Response(response="Invalid Slack signature", status=400))

        @self.app.route("/slack/events", methods=["POST"])
        def slack_events() -> Response:
            """
            Handle incoming events from Slack.
            """
            data = self.slack_handler.get_request_data(request)
            if data is None:
                return Response(response="Unsupported Media Type", status=415)
            return self.slack_handler.handle_events(data)

        @self.app.route("/slack/commands", methods=["POST"])
        def handle_commands() -> Response:
            """
            Handle incoming commands from Slack.
            """
            data = request.form
            return self.slack_handler.handle_commands(data)

    def run(self) -> None:
        """
        Run the web server.
        """
        self.app.run(
            host=self.config.get("host", "127.0.0.1"),
            port=self.config.get("port", 3000),
        )
