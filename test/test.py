"""
A module to test the SlackHandler class. It uses localstack to mock the AWS services.
"""

import hashlib
import hmac
import json
import os
import re
import threading
import time
import unittest
from argparse import Namespace
from typing import Any, Dict, Optional
from unittest.mock import patch

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from flask import Flask
from requests.auth import AuthBase
from werkzeug.serving import make_server

from app import create_web_server

os.environ["AWS_ENDPOINT_URL"] = "http://localhost:4566"


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

        @cls.web_server.app.route("/healthz", methods=["GET"])
        def healthz() -> Dict[str, str]:
            """
            Health check endpoint.
            """
            return {"status": "ok"}

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

    def mock_post_message(self, mock_chat_post_message) -> None:
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

    def post_event(self, payload: Any, timeout: int) -> None:
        """
        Post an event to the server and wait for the response.
        """
        self.post_message_event.clear()
        self.text = ""

        response = requests.post(
            f"http://localhost:{self.port}/slack/events",
            json={"payload": json.dumps(payload)},
            auth=self.slack_auth,
            timeout=timeout,
        )

        self.post_message_event.wait(timeout=timeout)
        self.assertEqual(response.status_code, 200)

    def post_command(self, payload: Any, timeout: int) -> None:
        """
        Post a command to the server and wait for the response.
        """
        self.post_message_event.clear()
        self.text = ""

        response = requests.post(
            f"http://localhost:{self.port}/slack/commands",
            data=payload,
            auth=self.slack_auth,
            timeout=timeout,
        )

        self.post_message_event.wait(timeout=timeout)
        self.assertEqual(response.status_code, 200)

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

    @patch("slack_sdk.WebClient.chat_postMessage")
    def test_create_public_key(self, mock_chat_post_message):
        """
        Test the create_public_key method.
        """
        payload = {
            "type": "view_submission",
            "user": {"id": "U12345", "username": "testuser"},
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
            channel="U12345", text="Public key updated successfully."
        )

    @patch("slack_sdk.WebClient.chat_postMessage")
    def test_instances_and_volumes(self, mock_chat_post_message):
        """
        Test operations on instances and volumes.
        """
        # Test launching an instance
        launch_payload = {
            "type": "view_submission",
            "user": {"id": "U12345", "username": "testuser"},
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
                            "mount_input": {"selected_option": {"value": "none"}}
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

        # Test stopping the instance
        stop_payload = {
            "type": "view_submission",
            "user": {"id": "U12345", "username": "testuser"},
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
            channel="U12345", text=f"Stopped instances: {instance_id}"
        )

        # Test starting the instance
        start_payload = {
            "type": "view_submission",
            "user": {"id": "U12345", "username": "testuser"},
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
            channel="U12345", text=f"Started instances: {instance_id}"
        )

        # Test changing the instance type
        change_payload = {
            "type": "view_submission",
            "user": {"id": "U12345", "username": "testuser"},
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
            channel="U12345",
            text=f"Changed instance {instance_id} to type t3.medium successfully.",
        )

        # Test creating a volume
        create_volume_payload = {
            "type": "view_submission",
            "user": {"id": "U12345", "username": "testuser"},
            "view": {
                "callback_id": "create_volume",
                "state": {
                    "values": {"volume_size": {"volume_size_input": {"value": "1"}}}
                },
            },
        }

        self.post_event(create_volume_payload, timeout=10)
        mock_chat_post_message.assert_called_with(
            channel="U12345", text="EBS volume of 1 GiB created successfully."
        )

        # Resizing a volume with localstack doesn't work properly

        # Test attaching a volume
        attach_volume_payload = {
            "type": "view_submission",
            "user": {"id": "U12345", "username": "testuser"},
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
            channel="U12345",
            text=f"EBS volume attached to instance {instance_id} successfully.",
        )

        # Test detaching a volume
        detach_volume_payload = {
            "command": "/ebs",
            "text": "detach",
            "trigger_id": "12345.12345.12345",
            "user_id": "U12345",
            "user_name": "testuser",
        }

        self.post_command(detach_volume_payload, timeout=10)
        mock_chat_post_message.assert_called_with(
            channel="U12345", text=f"EBS volume detached successfully."
        )

        # Test destroying a volume
        destroy_volume_payload = {
            "command": "/ebs",
            "text": "destroy please",
            "trigger_id": "12345.12345.12345",
            "user_id": "U12345",
            "user_name": "testuser",
        }

        self.post_command(destroy_volume_payload, timeout=10)
        mock_chat_post_message.assert_called_with(
            channel="U12345", text="EBS volume destroyed successfully."
        )

        # Test terminating the instance
        terminate_payload = {
            "type": "view_submission",
            "user": {"id": "U12345", "username": "testuser"},
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
            channel="U12345", text=f"Terminated instances: {instance_id}"
        )


if __name__ == "__main__":
    unittest.main()
