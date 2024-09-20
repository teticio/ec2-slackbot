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
from io import StringIO
from typing import Any, Dict, Optional, Tuple
from unittest.mock import Mock, patch

import paramiko
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from flask import Flask
from requests.auth import AuthBase
from werkzeug.serving import make_server

from ec2_slackbot.app import create_web_server

os.environ.update(
    {
        "SLACK_BOT_TOKEN": "xoxb-12345",
        "SLACK_SIGNING_SECRET": "12345",
    }
)

if "TEST_ON_AWS" not in os.environ:
    os.environ.update(
        {
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
        config = (
            "tests/config.yaml" if "TEST_ON_AWS" not in os.environ else "config.yaml"
        )
        cls.web_server = create_web_server(Namespace(config=config))
        cls.port = cls.web_server.config.get("port", 3000)
        cls.server = ServerThread(cls.web_server.app, cls.port)
        cls.server.start()

        healthz_url = f"http://localhost:{cls.port}/healthz"
        while True:
            try:
                response = requests.get(healthz_url, timeout=1)
                if response.status_code == 200:
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
        self.timeout = 10 if "TEST_ON_AWS" not in os.environ else 300
        self.trigger_id = "12345.12345.12345"
        self.user_name = "testuser"
        self.user_id = "U12345"
        self.command_payload = {
            "trigger_id": self.trigger_id,
            "user_id": self.user_id,
            "user_name": self.user_name,
        }
        self.instance_id = None

    def tearDown(self) -> None:
        """
        Clean up resources after each test.
        """
        ec2_client = self.web_server.slack_handler.aws_handler.ec2_client
        instance_ids = [
            instance["InstanceId"]
            for instance in ec2_client.describe_instances(
                Filters=[{"Name": "tag:User", "Values": [self.user_name]}]
            )["Reservations"]
            for instance in instance["Instances"]
        ]
        if len(instance_ids) > 0:
            ec2_client.terminate_instances(InstanceIds=instance_ids)
            waiters = ec2_client.get_waiter("instance_terminated")
            waiters.wait(InstanceIds=instance_ids)
        volume_ids = [
            volume["VolumeId"]
            for volume in ec2_client.describe_volumes(
                Filters=[{"Name": "tag:User", "Values": [self.user_name]}]
            )["Volumes"]
        ]
        if len(volume_ids) > 0:
            ec2_client.delete_volume(VolumeId=volume_ids[0])
            waiters = ec2_client.get_waiter("volume_deleted")
            waiters.wait(VolumeIds=volume_ids)
        ec2_client.delete_key_pair(KeyName=self.user_name)

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
    def generate_ssh_key_pair() -> Tuple[str, str]:
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
        ssh_private_key = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
        return ssh_public_key.decode("utf-8"), ssh_private_key.decode("utf-8")

    @patch("slack_sdk.WebClient.views_open")
    @patch("slack_sdk.WebClient.chat_postMessage")
    def test_instance_and_volume_operations(
        self, mock_chat_post_message: Mock, mock_views_open: Mock
    ) -> None:
        """
        Test operations on instances and volumes in order.
        """
        self._test_command(mock_views_open, "/ec2", "key")
        self._test_create_public_key(mock_chat_post_message)
        self._test_command(mock_views_open, "/ebs", "create")
        self._test_create_volume(mock_chat_post_message)
        self._test_command(mock_views_open, "/ec2", "up")
        self._test_launch_instance(mock_chat_post_message, mount_option="ebs")
        self._test_terminate_instance(mock_chat_post_message)
        self._test_launch_instance(mock_chat_post_message, mount_option="efs")
        self._test_command(mock_views_open, "/ec2", "down")
        self._test_command(mock_views_open, "/ec2", "stop")
        self._test_stop_instance(mock_chat_post_message)
        self._test_command(mock_views_open, "/ec2", "start")
        self._test_start_instance(mock_chat_post_message)
        self._test_command(mock_views_open, "/ec2", "change")
        self._test_change_instance_type(mock_chat_post_message)
        self._test_command(mock_views_open, "/ebs", "resize")
        self._test_resize_volume(mock_chat_post_message)
        self._test_command(mock_views_open, "/ebs", "attach")
        self._test_attach_volume(mock_chat_post_message)
        self._test_detach_volume(mock_chat_post_message)
        self._test_destroy_volume(mock_chat_post_message)
        self._test_terminate_instance(mock_chat_post_message)

    def _test_command(self, mock_views_open: Mock, command: str, text: str) -> None:
        """
        Test a command with the given text.
        """
        logger.info("Testing %s %s", command, text)
        self.command_payload["command"] = command.replace(
            "/", f"/{os.getenv('EC2_SLACKBOT_STAGE',  '')}"
        )
        self.command_payload["text"] = text
        call_count = mock_views_open.call_count
        response = self.post_command(self.command_payload, timeout=0)
        self.assertEqual(response.text, "")
        self.assertEqual(mock_views_open.call_count, call_count + 1)

    def _test_create_public_key(self, mock_chat_post_message: Mock) -> None:
        """
        Test creating a public key.
        """
        logger.info("Testing creating a public key")
        public_key, _ = self.generate_ssh_key_pair()
        payload = {
            "type": "view_submission",
            "user": {"id": self.user_id, "username": self.user_name},
            "view": {
                "callback_id": "submit_key",
                "state": {
                    "values": {"key_input": {"public_key": {"value": public_key}}}
                },
            },
        }
        self.mock_post_message(mock_chat_post_message)
        self.post_event(payload, timeout=self.timeout)
        mock_chat_post_message.assert_called_once_with(
            channel=self.user_id, text="Public key updated successfully."
        )

    def _test_launch_instance(
        self, mock_chat_post_message: Mock, mount_option: str
    ) -> None:
        """
        Test launching an instance.
        """
        logger.info("Testing launching an instance")
        launch_payload = {
            "type": "view_submission",
            "user": {"id": self.user_id, "username": self.user_name},
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
                        "root_ebs_size": {"root_ebs_size_input": {"value": "8"}},
                        "mount_options": {
                            "mount_input": {"selected_option": {"value": mount_option}}
                        },
                        "startup_script": {"startup_script_input": {"value": "ls"}},
                    }
                },
            },
        }
        self.mock_post_message(mock_chat_post_message)
        self.post_event(launch_payload, timeout=self.timeout)
        self.assertIn("launched successfully.", self.text)
        match = re.search(r"i-[0-9a-fA-F]{17}", self.text)
        self.instance_id = match.group(0) if match else ""

    def _test_stop_instance(self, mock_chat_post_message: Mock) -> None:
        """
        Test stopping the instance.
        """
        logger.info("Testing stopping the instance")
        stop_payload = {
            "type": "view_submission",
            "user": {"id": self.user_id, "username": self.user_name},
            "view": {
                "callback_id": "stop_instance",
                "state": {
                    "values": {
                        "instance_selection": {
                            "selected_instances": {
                                "selected_options": [{"value": self.instance_id}]
                            }
                        }
                    }
                },
            },
        }
        self.post_event(stop_payload, timeout=self.timeout)
        mock_chat_post_message.assert_called_with(
            channel=self.user_id, text=f"Stopped instances: {self.instance_id}"
        )

    def _test_start_instance(self, mock_chat_post_message: Mock) -> None:
        """
        Test starting the instance.
        """
        logger.info("Testing starting the instance")
        start_payload = {
            "type": "view_submission",
            "user": {"id": self.user_id, "username": self.user_name},
            "view": {
                "callback_id": "start_instance",
                "state": {
                    "values": {
                        "instance_selection": {
                            "selected_instances": {
                                "selected_options": [{"value": self.instance_id}]
                            }
                        }
                    }
                },
            },
        }
        self.post_event(start_payload, timeout=self.timeout)
        mock_chat_post_message.assert_called_with(
            channel=self.user_id, text=f"Started instances: {self.instance_id}"
        )

    def _test_change_instance_type(self, mock_chat_post_message: Mock) -> None:
        """
        Test changing the instance type.
        """
        logger.info("Testing changing the instance type")
        change_payload = {
            "type": "view_submission",
            "user": {"id": self.user_id, "username": self.user_name},
            "view": {
                "callback_id": "change_instance",
                "state": {
                    "values": {
                        "instance_selection": {
                            "selected_instances": {
                                "selected_option": {"value": self.instance_id}
                            }
                        },
                        "instance_type_choice": {
                            "instance_type": {"selected_option": {"value": "t3.medium"}}
                        },
                    }
                },
            },
        }
        self.post_event(change_payload, timeout=self.timeout)
        mock_chat_post_message.assert_called_with(
            channel=self.user_id,
            text=f"Changed instance {self.instance_id} to type t3.medium successfully.",
        )

    def _test_create_volume(self, mock_chat_post_message: Mock) -> None:
        """
        Test creating a volume.
        """
        logger.info("Testing creating a volume")
        create_volume_payload = {
            "type": "view_submission",
            "user": {"id": self.user_id, "username": self.user_name},
            "view": {
                "callback_id": "create_volume",
                "state": {
                    "values": {"volume_size": {"volume_size_input": {"value": "1"}}}
                },
            },
        }
        self.post_event(create_volume_payload, timeout=self.timeout)
        mock_chat_post_message.assert_called_with(
            channel=self.user_id, text="EBS volume of 1 GiB created successfully."
        )

    def _test_resize_volume(self, mock_chat_post_message: Mock) -> None:
        """
        Test resizing a volume.
        """
        logger.info("Testing resizing a volume")
        resize_volume_payload = {
            "type": "view_submission",
            "user": {"id": self.user_id, "username": self.user_name},
            "view": {
                "callback_id": "resize_volume",
                "state": {
                    "values": {"volume_size": {"volume_size_input": {"value": "2"}}}
                },
            },
        }
        self.post_event(resize_volume_payload, timeout=self.timeout)
        mock_chat_post_message.assert_called_with(
            channel=self.user_id,
            text=(
                "EBS volume resized to 2 GiB successfully. "
                "Remember to run resize2fs to resize the filesystem."
            ),
        )

    def _test_attach_volume(self, mock_chat_post_message: Mock) -> None:
        """
        Test attaching a volume.
        """
        logger.info("Testing attaching a volume")
        attach_volume_payload = {
            "type": "view_submission",
            "user": {"id": self.user_id, "username": self.user_name},
            "view": {
                "callback_id": "attach_volume",
                "state": {
                    "values": {
                        "instance_selection": {
                            "selected_instance": {
                                "selected_option": {"value": self.instance_id}
                            }
                        }
                    }
                },
            },
        }
        self.post_event(attach_volume_payload, timeout=self.timeout)
        mock_chat_post_message.assert_called_with(
            channel=self.user_id,
            text=f"EBS volume attached to instance {self.instance_id} successfully.",
        )

    def _test_detach_volume(self, mock_chat_post_message: Mock) -> None:
        """
        Test detaching a volume.
        """
        logger.info("Testing detaching a volume")
        self.command_payload["command"] = f"/{os.getenv('EC2_SLACKBOT_STAGE', '')}ebs"
        self.command_payload["text"] = "detach"
        response = self.post_command(self.command_payload, timeout=self.timeout)
        self.assertEqual(response.json()["text"], "Detaching EBS volume...")
        mock_chat_post_message.assert_called_with(
            channel=self.user_id, text="EBS volume detached successfully."
        )

    def _test_destroy_volume(self, mock_chat_post_message: Mock) -> None:
        """
        Test destroying a volume.
        """
        logger.info("Testing destroying a volume")
        self.command_payload["command"] = f"/{os.getenv('EC2_SLACKBOT_STAGE', '')}ebs"
        self.command_payload["text"] = "destroy please"
        response = self.post_command(self.command_payload, timeout=self.timeout)
        self.assertEqual(response.json()["text"], "Destroying EBS volume...")
        mock_chat_post_message.assert_called_with(
            channel=self.user_id, text="EBS volume destroyed successfully."
        )

    def _test_terminate_instance(self, mock_chat_post_message: Mock) -> None:
        """
        Test terminating the instance.
        """
        logger.info("Testing terminating the instance")
        terminate_payload = {
            "type": "view_submission",
            "user": {"id": self.user_id, "username": self.user_name},
            "view": {
                "callback_id": "terminate_instance",
                "state": {
                    "values": {
                        "instance_selection": {
                            "selected_instances": {
                                "selected_options": [{"value": self.instance_id}]
                            }
                        }
                    }
                },
            },
        }
        self.post_event(terminate_payload, timeout=self.timeout)
        mock_chat_post_message.assert_called_with(
            channel=self.user_id, text=f"Terminated instances: {self.instance_id}"
        )

    @patch("slack_sdk.WebClient.chat_postMessage")
    def test_periodically_check_instances(self, mock_chat_post_message: Mock) -> None:
        """
        Test the instance checker.
        """
        logger.info("Testing the instance checker")
        small_instance_id = "i-1234567890abcdef0"
        small_instance_type = "t2.micro"
        small_running_days = 8
        large_instance_id = "i-0987654321fedcba0"
        large_instance_type = "t3.large"
        large_running_days = 2

        slack_handler = self.web_server.slack_handler
        slack_handler.get_all_user_ids = Mock(
            return_value={self.user_name: self.user_id}
        )
        instance_checker = self.web_server.instance_checker
        instance_checker.aws_handler.get_running_instance_details = Mock(
            return_value=[
                {
                    "instance_id": small_instance_id,
                    "instance_type": small_instance_type,
                    "instance_cost": 0.0001,
                    "user_name": self.user_name,
                    "running_days": small_running_days,
                },
                {
                    "instance_id": large_instance_id,
                    "instance_type": large_instance_type,
                    "instance_cost": 100.0,
                    "user_name": self.user_name,
                    "running_days": large_running_days,
                },
            ]
        )
        instance_checker.periodically_check_instances()

        mock_chat_post_message.assert_any_call(
            channel=self.user_id,
            text=(
                f"Warning: Your instance {small_instance_id} ({small_instance_type}) "
                f"has been running for {small_running_days} days. "
                "Please consider terminating it with /ec2 down."
            ),
        )
        mock_chat_post_message.assert_any_call(
            channel=self.user_id,
            text=(
                f"Warning: Your instance {large_instance_id} ({large_instance_type}) "
                f"has been running for {large_running_days} days. "
                "Please consider terminating it with /ec2 down."
            ),
        )

    @staticmethod
    def ssh_connect(
        hostname: str, username: str, private_key_text: str, command: str
    ) -> Tuple[str, str]:
        """
        Connect to an SSH server via SSM.
        """
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        private_key_file = StringIO(private_key_text)
        private_key = paramiko.RSAKey.from_private_key(private_key_file)
        proxy_command = (
            f"aws ssm start-session --target {hostname} "
            f"--document-name AWS-StartSSHSession --parameters portNumber=22"
        )
        proxy = paramiko.ProxyCommand(proxy_command)
        client.connect(
            hostname, username=username, pkey=private_key, sock=proxy  # type: ignore
        )
        _, stdout, stderr = client.exec_command(command)
        output = stdout.read().decode()
        error = stderr.read().decode()
        client.close()
        return output, error

    def test_ssh(self) -> None:
        """
        Test SSH (localstack doesn't support SSM).
        """
        if "TEST_ON_AWS" not in os.environ:
            return

        logger.info("Testing SSH")
        public_key, private_key = self.generate_ssh_key_pair()
        ami = self.web_server.config["amis"]["Amazon Linux 2"]
        aws_handler = self.web_server.slack_handler.aws_handler
        aws_handler.create_key_pair(user_name=self.user_name, public_key=public_key)
        instance_id = aws_handler.launch_ec2_instance(
            ami_id=ami["id"],
            ami_user=ami["user"],
            instance_type="t2.micro",
            root_ebs_size=8,
            user_name=self.user_name,
        )
        output, _ = self.ssh_connect(instance_id, ami["user"], private_key, "uname")
        self.assertEqual(output.strip(), "Linux")
        aws_handler.terminate_ec2_instances(instance_ids=[instance_id])


if __name__ == "__main__":
    unittest.main()
