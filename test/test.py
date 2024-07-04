"""
A module to test the SlackHandler class. It uses localstack to mock the AWS services.
"""

import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
import unittest
from argparse import Namespace
from typing import Any, Dict, Optional
from unittest.mock import Mock, patch

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from flask import Flask
from requests.auth import AuthBase
from werkzeug.serving import make_server

from app import create_web_server

os.environ.update(
    {
        "SLACK_BOT_TOKEN": "xoxb-12345",
        "SLACK_SIGNING_SECRET": "12345",
        "AWS_ACCESS_KEY_ID": "test",
        "AWS_SECRET_ACCESS_KEY": "test",
        "AWS_ENDPOINT_URL": "http://localhost:4566",
    }
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class SlackAuth(AuthBase):
    """
    A class to authenticate requests to the Slack API.
    """

    def __init__(self, secret: Optional[str] = None) -> None:
        self.secret = secret or os.environ["SLACK_SIGNING_SECRET"]

    def __call__(self, request: requests.PreparedRequest) -> requests.PreparedRequest:
        """
        Authenticate the request.
        """
        payload = request.body or ""
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        timestamp = str(int(time.time()))
        basestring = f"v0:{timestamp}:{payload}"
        signature = (
            "v0="
            + hmac.new(
                self.secret.encode(), basestring.encode(), hashlib.sha256
            ).hexdigest()
        )
        request.headers["X-Slack-Request-Timestamp"] = timestamp
        request.headers["X-Slack-Signature"] = signature
        return request


class ServerThread(threading.Thread):
    """
    A class to run the Flask server in a separate thread.
    """

    def __init__(self, app: Flask, port: int) -> None:
        threading.Thread.__init__(self)
        self.server = make_server("localhost", port, app)
        self.ctx = app.app_context()
        self.ctx.push()

    def run(self) -> None:
        """
        Run the server.
        """
        self.server.serve_forever()

    def shutdown(self) -> None:
        """
        Shutdown the server.
        """
        self.server.shutdown()


class TestSlackHandler(unittest.TestCase):
    """
    A class to test the SlackHandler class.
    """

    @classmethod
    def setUpClass(cls):
        """
        Set up the server and port before any tests run.
        """
        cls.slack_auth = SlackAuth()
        cls.web_server = create_web_server(Namespace(config="test/test_config.yaml"))
        cls.port = cls.web_server.config.get("port", 3000)
        cls.server = ServerThread(cls.web_server.app, cls.port)
        cls.server.start()

        healthz_url = f"http://localhost:{cls.port}/healthz"
        while True:
            try:
                _response = requests.get(healthz_url, timeout=1)
                if _response.status_code == 200:
                    break
            except requests.ConnectionError:
                pass
            time.sleep(1)

    @classmethod
    def tearDownClass(cls):
        """
        Shutdown the server after all tests run.
        """
        cls.server.shutdown()

    def setUp(self) -> None:
        """
        Set up the SlackHandler instance before each test.
        """
        self.post_message_event = threading.Event()
        self.text = ""
        self.port = self.__class__.port

    def mock_post_message(self, mock_chat_post_message: Mock) -> None:
        """
        Mock the chat_postMessage method and set the event.
        """

        def side_effect(**kwargs):
            """
            Side effect for the chat_postMessage method.
            """
            self.text = kwargs.get("text")
            self.post_message_event.set()
            return {"ok": True}

        mock_chat_post_message.side_effect = side_effect

    def post_event(self, payload: Dict[str, Any], timeout: int) -> None:
        """
        Post an event to the server and wait for the response.
        """
        self.post_message_event.clear()
        self.text = ""

        response = requests.post(
            f"http://localhost:{self.port}/slack/events",
            json={"payload": json.dumps(payload)},
            auth=self.slack_auth,
            timeout=5,
        )

        self.post_message_event.wait(timeout=timeout)
        self.assertEqual(response.status_code, 200)

    def post_command(self, payload: Dict[str, Any], timeout: int) -> requests.Response:
        """
        Post a command to the server and wait for the response.
        """
        self.post_message_event.clear()
        self.text = ""

        response = requests.post(
            f"http://localhost:{self.port}/slack/commands",
            data=payload,
            auth=self.slack_auth,
            timeout=5,
        )

        self.post_message_event.wait(timeout=timeout)
        self.assertEqual(response.status_code, 200)
        return response

    @staticmethod
    def generate_dummy_ssh_public_key() -> str:
        """
        Generate a dummy SSH public key.
        """
        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        public_key = private_key.public_key()
        ssh_public_key = public_key.public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )
        return ssh_public_key.decode("utf-8")

    @patch("slack_sdk.WebClient.views_open")
    @patch("slack_sdk.WebClient.chat_postMessage")
    def test_instance_and_volume_operations(
        self, mock_chat_post_message: Mock, mock_views_open: Mock
    ) -> None:
        """
        Test operations on instances and volumes.
        """
        trigger_id = "12345.12345.12345"
        user_name = "testuser"
        user_id = "U12345"

        command_payload = {
            "trigger_id": trigger_id,
            "user_id": user_id,
            "user_name": user_name,
        }

        logger.info("Testing /ec2 key")
        command_payload["command"] = "/ec2"
        command_payload["text"] = "key"
        response = self.post_command(command_payload, timeout=0)
        self.assertEqual(response.text, "")
        mock_views_open.assert_called_once()

        logger.info("Testing creating a public key")
        payload = {
            "type": "view_submission",
            "user": {"id": user_id, "username": user_name},
            "view": {
                "callback_id": "submit_key",
                "state": {
                    "values": {
                        "key_input": {
                            "public_key": {
                                "value": self.generate_dummy_ssh_public_key()
                            }
                        }
                    }
                },
            },
        }

        self.mock_post_message(mock_chat_post_message)
        self.post_event(payload, timeout=10)
        mock_chat_post_message.assert_called_once_with(
            channel=user_id, text="Public key updated successfully."
        )

        logger.info("Testing opening the instance launch modal")
        command_payload["text"] = "up"
        response = self.post_command(command_payload, timeout=0)
        self.assertEqual(response.text, "")
        self.assertEqual(mock_views_open.call_count, 2)

        logger.info("Testing launching an instance")
        launch_payload = {
            "type": "view_submission",
            "user": {"id": user_id, "username": user_name},
            "view": {
                "callback_id": "launch_instance",
                "state": {
                    "values": {
                        "ami_choice": {
                            "ami": {"selected_option": {"value": "Ubuntu 22.04"}}
                        },
                        "instance_type_choice": {
                            "instance_type": {"selected_option": {"value": "t2.micro"}}
                        },
                        "mount_options": {
                            "mount_input": {"selected_option": {"value": "efs"}}
                        },
                        "startup_script": {"startup_script_input": {"value": ""}},
                    }
                },
            },
        }

        self.mock_post_message(mock_chat_post_message)
        self.post_event(launch_payload, timeout=10)
        self.assertIn("launched successfully.", self.text)
        match = re.search(r"i-[0-9a-fA-F]{17}", self.text)
        instance_id = match.group(0) if match else ""

        logger.info("Testing /ec2 down")
        command_payload["text"] = "down"
        response = self.post_command(command_payload, timeout=0)
        self.assertEqual(response.text, "")
        self.assertEqual(mock_views_open.call_count, 3)

        logger.info("Testing /ec2 stop")
        command_payload["text"] = "stop"
        response = self.post_command(command_payload, timeout=0)
        self.assertEqual(response.text, "")
        self.assertEqual(mock_views_open.call_count, 4)

        logger.info("Testing stopping the instance")
        stop_payload = {
            "type": "view_submission",
            "user": {"id": user_id, "username": user_name},
            "view": {
                "callback_id": "stop_instance",
                "state": {
                    "values": {
                        "instance_selection": {
                            "selected_instances": {
                                "selected_options": [{"value": instance_id}]
                            }
                        }
                    }
                },
            },
        }

        self.post_event(stop_payload, timeout=10)
        mock_chat_post_message.assert_called_with(
            channel=user_id, text=f"Stopped instances: {instance_id}"
        )

        logger.info("Testing /ec2 start")
        command_payload["text"] = "start"
        response = self.post_command(command_payload, timeout=0)
        self.assertEqual(response.text, "")
        self.assertEqual(mock_views_open.call_count, 5)

        logger.info("Testing starting the instance")
        start_payload = {
            "type": "view_submission",
            "user": {"id": user_id, "username": user_name},
            "view": {
                "callback_id": "start_instance",
                "state": {
                    "values": {
                        "instance_selection": {
                            "selected_instances": {
                                "selected_options": [{"value": instance_id}]
                            }
                        }
                    }
                },
            },
        }

        self.post_event(start_payload, timeout=10)
        mock_chat_post_message.assert_called_with(
            channel=user_id, text=f"Started instances: {instance_id}"
        )

        logger.info("Testing /ec2 change")
        command_payload["text"] = "change"
        response = self.post_command(command_payload, timeout=0)
        self.assertEqual(response.text, "")
        self.assertEqual(mock_views_open.call_count, 6)

        logger.info("Testing changing the instance type")
        change_payload = {
            "type": "view_submission",
            "user": {"id": user_id, "username": user_name},
            "view": {
                "callback_id": "change_instance",
                "state": {
                    "values": {
                        "instance_selection": {
                            "selected_instances": {
                                "selected_option": {"value": instance_id}
                            }
                        },
                        "instance_type_choice": {
                            "instance_type": {"selected_option": {"value": "t3.medium"}}
                        },
                    }
                },
            },
        }

        self.post_event(change_payload, timeout=10)
        mock_chat_post_message.assert_called_with(
            channel=user_id,
            text=f"Changed instance {instance_id} to type t3.medium successfully.",
        )

        logger.info("Testing /ebs create")
        command_payload["command"] = "/ebs"
        command_payload["text"] = "create"
        response = self.post_command(command_payload, timeout=0)
        self.assertEqual(response.text, "")
        self.assertEqual(mock_views_open.call_count, 7)

        logger.info("Testing creating a volume")
        create_volume_payload = {
            "type": "view_submission",
            "user": {"id": user_id, "username": user_name},
            "view": {
                "callback_id": "create_volume",
                "state": {
                    "values": {"volume_size": {"volume_size_input": {"value": "1"}}}
                },
            },
        }

        self.post_event(create_volume_payload, timeout=10)
        mock_chat_post_message.assert_called_with(
            channel=user_id, text="EBS volume of 1 GiB created successfully."
        )

        logger.info("Testing /ebs resize")
        command_payload["text"] = "resize"
        response = self.post_command(command_payload, timeout=0)
        self.assertEqual(response.text, "")
        self.assertEqual(mock_views_open.call_count, 8)

        # Resizing a volume with localstack doesn't work properly

        logger.info("Testing /ebs attach")
        command_payload["text"] = "attach"
        response = self.post_command(command_payload, timeout=0)
        self.assertEqual(response.text, "")
        self.assertEqual(mock_views_open.call_count, 9)

        logger.info("Testing attaching a volume")
        attach_volume_payload = {
            "type": "view_submission",
            "user": {"id": user_id, "username": user_name},
            "view": {
                "callback_id": "attach_volume",
                "state": {
                    "values": {
                        "instance_selection": {
                            "selected_instance": {
                                "selected_option": {"value": instance_id}
                            }
                        }
                    }
                },
            },
        }

        self.post_event(attach_volume_payload, timeout=10)
        mock_chat_post_message.assert_called_with(
            channel=user_id,
            text=f"EBS volume attached to instance {instance_id} successfully.",
        )

        logger.info("Testing detaching a volume")
        command_payload["text"] = "detach"
        response = self.post_command(command_payload, timeout=10)
        self.assertEqual(response.json()["text"], "Detaching EBS volume...")
        mock_chat_post_message.assert_called_with(
            channel=user_id, text="EBS volume detached successfully."
        )

        logger.info("Testing destroying a volume")
        command_payload["text"] = "destroy please"
        response = self.post_command(command_payload, timeout=10)
        self.assertEqual(response.json()["text"], "Destroying EBS volume...")
        mock_chat_post_message.assert_called_with(
            channel=user_id, text="EBS volume destroyed successfully."
        )

        logger.info("Testing terminating the instance")
        terminate_payload = {
            "type": "view_submission",
            "user": {"id": user_id, "username": user_name},
            "view": {
                "callback_id": "terminate_instance",
                "state": {
                    "values": {
                        "instance_selection": {
                            "selected_instances": {
                                "selected_options": [{"value": instance_id}]
                            }
                        }
                    }
                },
            },
        }

        self.post_event(terminate_payload, timeout=10)
        mock_chat_post_message.assert_called_with(
            channel=user_id, text=f"Terminated instances: {instance_id}"
        )

    @patch("slack_sdk.WebClient.chat_postMessage")
    def test_periodically_check_instances(self, mock_chat_post_message: Mock) -> None:
        """
        Test the instance checker.
        """
        logger.info("Testing the instance checker")
        user_name = "testuser"
        user_id = "U12345"
        small_instance_id = "i-1234567890abcdef0"
        small_instance_type = "t2.micro"
        small_running_days = 8
        large_instance_id = "i-0987654321fedcba0"
        large_instance_type = "t3.large"
        large_running_days = 2

        slack_handler = self.web_server.slack_handler
        slack_handler.get_all_user_ids = Mock(return_value={user_name: user_id})
        instance_checker = self.web_server.instance_checker
        instance_checker.aws_handler.get_running_instance_details = Mock(
            return_value=[
                {
                    "instance_id": small_instance_id,
                    "instance_type": small_instance_type,
                    "instance_cost": 0.0001,
                    "user_name": user_name,
                    "running_days": small_running_days,
                },
                {
                    "instance_id": large_instance_id,
                    "instance_type": large_instance_type,
                    "instance_cost": 100.0,
                    "user_name": user_name,
                    "running_days": large_running_days,
                },
            ]
        )
        instance_checker.periodically_check_instances()

        mock_chat_post_message.assert_any_call(
            channel=user_id,
            text=(
                f"Warning: Your instance {small_instance_id} ({small_instance_type}) "
                f"has been running for {small_running_days} days. "
                "Please consider terminating it with /ec2 down."
            ),
        )
        mock_chat_post_message.assert_any_call(
            channel=user_id,
            text=(
                f"Warning: Your instance {large_instance_id} ({large_instance_type}) "
                f"has been running for {large_running_days} days. "
                "Please consider terminating it with /ec2 down."
            ),
        )


if __name__ == "__main__":
    unittest.main()
