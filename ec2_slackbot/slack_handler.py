"""
This module contains the SlackHandler class, which is responsible
for handling Slack events and commands.
"""

import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional

from flask import Request, Response, jsonify
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.signature import SignatureVerifier

from .aws_handler import AWSHandler


class SlackHandler:
    """
    A class to handle Slack events and commands.
    """

    def __init__(
        self,
        config: Dict[str, Any],
        token: str,
        signing_secret: str,
        aws_handler: AWSHandler,
    ) -> None:
        self.client = WebClient(token=token)
        self.config = config
        self.aws_handler = aws_handler
        self.verifier = SignatureVerifier(signing_secret=signing_secret)

    def get_all_user_ids(self) -> Dict[str, str]:
        """
        Get all user IDs from Slack.
        """
        try:
            response = self.client.users_list()
            if response["ok"]:
                return {
                    user["name"]: user["id"]
                    for user in response["members"]
                    if not user["is_bot"] and not user["deleted"]
                }
            logging.error("Error fetching users: %s", response["error"])
        except SlackApiError as e:
            logging.error("Error fetching users: %s", e.response["error"])
        return {}

    def send_warning(
        self, user_id: str, instance_id: str, instance_type: str, running_days: int
    ) -> None:
        """
        Send a warning message to the user.
        """
        try:
            self.client.chat_postMessage(
                channel=user_id,
                text=(
                    f"Warning: Your instance {instance_id} ({instance_type}) "
                    f"has been running for {running_days} days. "
                    "Please consider terminating it with /ec2 down."
                ),
            )
        except SlackApiError as e:
            logging.error("Error sending warning: %s", e.response["error"])

    def get_request_data(self, request: Request) -> Optional[Dict[str, Any]]:
        """
        Get the data from the request based on the content type.
        """
        if request.content_type == "application/json":
            return request.json
        if request.content_type == "application/x-www-form-urlencoded":
            return request.form
        return None

    def handle_events(self, data: Dict[str, Any]) -> Response:
        """
        Handle incoming events from Slack.
        """
        if "payload" in data:
            payload = json.loads(data["payload"])
            event_type = payload.get("type")
            if event_type == "view_submission":
                self.handle_interactions(payload)
        if "challenge" in data:
            return jsonify({"challenge": data["challenge"]})
        return Response(status=200)

    def handle_commands(self, data: Dict[str, Any]) -> Response:
        """
        Handle incoming commands from Slack.
        """
        stage = os.getenv("EC2_SLACKBOT_STAGE", "")
        command = data.get("command")
        sub_command = data.get("text")
        trigger_id = data.get("trigger_id")
        user_id = data.get("user_id")
        user_name = data.get("user_name")

        if command == f"/{stage}ec2":
            return self.handle_ec2_commands(sub_command, trigger_id, user_name)

        if command == f"/{stage}ebs":
            return self.handle_ebs_commands(sub_command, trigger_id, user_id, user_name)

        return jsonify(response_type="ephemeral", text="Command not recognized.")

    def handle_ec2_commands(self, sub_command, trigger_id, user_name) -> Response:
        """
        Handles EC2-related commands.
        """
        if sub_command == "key":
            return self.open_key_modal(trigger_id)

        if sub_command == "up":
            try:
                self.aws_handler.ec2_client.describe_key_pairs(KeyNames=[user_name])
            except self.aws_handler.ec2_client.exceptions.ClientError:
                return jsonify(
                    response_type="ephemeral",
                    text="Please upload your public key first with /ec2 key.",
                )
            return self.open_instance_launch_modal(trigger_id, user_name)

        if sub_command in ["down", "start", "stop"]:
            return self.open_instance_operate_modal(
                trigger_id, user_name, sub_command.replace("down", "terminate")
            )

        if sub_command == "change":
            return self.open_instance_change_modal(trigger_id, user_name)

        return jsonify(
            response_type="ephemeral",
            text="Command must be one of: key, up, down, change, stop, start.",
        )

    def handle_ebs_commands(
        self, sub_command, trigger_id, user_id, user_name
    ) -> Response:
        """
        Handles EBS-related commands.
        """
        if sub_command.split()[0] not in [
            "create",
            "resize",
            "attach",
            "detach",
            "destroy",
        ]:
            return jsonify(
                response_type="ephemeral",
                text="Command must be one of: create, resize, attach, detach or destroy.",
            )

        if sub_command == "create":
            return self.open_volume_create_modal(trigger_id, user_name)

        volume = self.aws_handler.get_volume_for_user(user_name)
        if volume is None:
            return jsonify(
                response_type="ephemeral",
                text="No EBS volume found. Please /ebs create first.",
            )

        if sub_command == "resize":
            return self.open_volume_resize_modal(trigger_id, user_name)

        if sub_command == "attach":
            return self.open_volume_attach_modal(trigger_id, user_name)

        if sub_command == "detach":
            self.handle_aws_command(
                function=self.aws_handler.detach_volume,
                user_id=user_id,
                success_message="EBS volume detached successfully.",
                error_message="Error detaching EBS volume: {}",
                volume_id=volume["id"],
                attachments=volume["attachments"],
            )
            return jsonify(response_type="ephemeral", text="Detaching EBS volume...")

        if sub_command == "destroy":
            return jsonify(
                response_type="ephemeral",
                text=(
                    "If you are sure you want to destroy the EBS volume, "
                    "please type: /ebs destroy please."
                ),
            )

        if sub_command == "destroy please":
            self.handle_aws_command(
                function=self.aws_handler.destroy_volume,
                user_id=user_id,
                success_message="EBS volume destroyed successfully.",
                error_message="Error destroying EBS volume: {}",
                volume_id=volume["id"],
            )
            return jsonify(response_type="ephemeral", text="Destroying EBS volume...")

        assert False, "Unhandled EBS command"

    def get_instance_type_options(self) -> List:
        """
        Retrieves instance type options.
        """
        return [
            {
                "text": {"type": "plain_text", "text": f"{instance_type} (${cost}/hr)"},
                "value": instance_type,
            }
            for instance_type, cost in self.config["instance_types"].items()
        ]

    def get_instance_options(self, user_name: str, states: List[str]) -> List:
        """
        Retrieves instance options for a given user.
        """
        instances = self.aws_handler.get_instances_for_user(user_name, states)
        return [
            {
                "text": {
                    "type": "plain_text",
                    "text": (
                        f"{instance['InstanceId']} ({instance['InstanceType']}) "
                        f"- {instance['State']['Name']}"
                    ),
                },
                "value": instance["InstanceId"],
            }
            for reservation in instances["Reservations"]
            for instance in reservation["Instances"]
        ]

    def open_key_modal(self, trigger_id: str) -> Response:
        """
        Opens the key modal.
        """
        modal = {
            "type": "modal",
            "callback_id": "submit_key",
            "title": {"type": "plain_text", "text": "Upload EC2 Key"},
            "submit": {"type": "plain_text", "text": "Submit"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "key_input",
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "public_key",
                        "multiline": True,
                        "placeholder": {
                            "type": "plain_text",
                            "text": "Paste your SSH public key here.",
                        },
                    },
                    "label": {"type": "plain_text", "text": "Public Key"},
                }
            ],
        }

        try:
            self.client.views_open(trigger_id=trigger_id, view=modal)
        except SlackApiError as e:
            logging.error("Error opening modal: %s", e.response["error"])

        return Response(status=200)

    def open_instance_launch_modal(self, trigger_id: str, user_name: str) -> Response:
        """
        Opens the instance launch modal.
        """
        ami_options = [
            {"text": {"type": "plain_text", "text": ami}, "value": ami}
            for ami in self.config["amis"]
        ]
        instance_type_options = self.get_instance_type_options()
        mount_options = [
            {"text": {"type": "plain_text", "text": "None"}, "value": "none"}
        ]

        uid = self.aws_handler.get_sagemaker_studio_uid(user_name)
        if uid is not None:
            mount_options.extend(
                [
                    {
                        "text": {
                            "type": "plain_text",
                            "text": "Mount SageMaker Studio EFS at $HOME",
                        },
                        "value": "efs",
                    },
                    {
                        "text": {
                            "type": "plain_text",
                            "text": "Mount SageMaker Studio EFS at /root",
                        },
                        "value": "efs_root",
                    },
                ]
            )
        if self.aws_handler.get_volume_for_user(user_name) is not None:
            mount_options.append(
                {
                    "text": {"type": "plain_text", "text": "Mount EBS Volume at $HOME"},
                    "value": "ebs",
                }
            )

        modal = {
            "type": "modal",
            "callback_id": "launch_instance",
            "title": {"type": "plain_text", "text": "Launch EC2 Instance"},
            "submit": {"type": "plain_text", "text": "Launch"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "ami_choice",
                    "element": {
                        "type": "static_select",
                        "action_id": "ami",
                        "placeholder": {"type": "plain_text", "text": "Select an AMI"},
                        "options": ami_options,
                    },
                    "label": {"type": "plain_text", "text": "AMI"},
                },
                {
                    "type": "input",
                    "block_id": "instance_type_choice",
                    "element": {
                        "type": "static_select",
                        "action_id": "instance_type",
                        "placeholder": {
                            "type": "plain_text",
                            "text": "Select Instance Type",
                        },
                        "options": instance_type_options,
                    },
                    "label": {"type": "plain_text", "text": "Instance Type"},
                },
                {
                    "type": "input",
                    "block_id": "root_ebs_size",
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "root_ebs_size_input",
                        "placeholder": {
                            "type": "plain_text",
                            "text": "Enter root volume size in GiB",
                        },
                        "initial_value": "20",
                    },
                    "label": {
                        "type": "plain_text",
                        "text": f"Root Volume Size in GiB (max {self.config['max_volume_size']})",
                    },
                },
                {
                    "type": "input",
                    "block_id": "mount_options",
                    "element": {
                        "type": "static_select",
                        "action_id": "mount_input",
                        "placeholder": {
                            "type": "plain_text",
                            "text": "Select Mount Option",
                        },
                        "options": mount_options,
                        "initial_option": {
                            "text": {"type": "plain_text", "text": "None"},
                            "value": "none",
                        },
                    },
                    "label": {"type": "plain_text", "text": "Mount Options"},
                },
                {
                    "type": "input",
                    "block_id": "startup_script",
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "startup_script_input",
                        "multiline": True,
                        "placeholder": {
                            "type": "plain_text",
                            "text": "Enter startup script (optional)",
                        },
                    },
                    "label": {
                        "type": "plain_text",
                        "text": "Startup Script",
                    },
                    "optional": True,
                },
            ],
        }

        try:
            self.client.views_open(trigger_id=trigger_id, view=modal)
        except SlackApiError as e:
            return jsonify(
                status=500, text=f"Failed to open modal: {e.response['error']}"
            )

        return Response(status=200)

    def open_instance_operate_modal(
        self, trigger_id: str, user_name: str, command: str
    ) -> Response:
        """
        Opens the instance operation modal.
        """
        states = {
            "stop": ["running"],
            "start": ["stopped"],
            "terminate": ["pending", "running", "stopping", "stopped"],
        }
        instance_options = self.get_instance_options(user_name, states[command])
        if not instance_options:
            return jsonify(
                response_type="ephemeral", text=f"No EC2 instances to {command}."
            )

        modal = {
            "type": "modal",
            "callback_id": f"{command}_instance",
            "title": {
                "type": "plain_text",
                "text": f"{command.capitalize()} EC2 Instances",
            },
            "submit": {"type": "plain_text", "text": command.capitalize()},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "instance_selection",
                    "element": {
                        "type": "checkboxes",
                        "action_id": "selected_instances",
                        "options": instance_options,
                    },
                    "label": {"type": "plain_text", "text": "Select Instances"},
                }
            ],
        }

        try:
            self.client.views_open(trigger_id=trigger_id, view=modal)
        except SlackApiError as e:
            return jsonify(
                status=500, text=f"Failed to open modal: {e.response['error']}"
            )

        return Response(status=200)

    def open_instance_change_modal(self, trigger_id: str, user_name: str) -> Response:
        """
        Opens the instance change modal.
        """
        instance_options = self.get_instance_options(user_name, ["pending", "running"])
        if not instance_options:
            return jsonify(
                response_type="ephemeral", text="No EC2 instances to change."
            )
        instance_type_options = self.get_instance_type_options()

        modal = {
            "type": "modal",
            "callback_id": "change_instance",
            "title": {"type": "plain_text", "text": "Change EC2 Instance Type"},
            "submit": {"type": "plain_text", "text": "Change"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "instance_selection",
                    "element": {
                        "type": "static_select",
                        "action_id": "selected_instances",
                        "options": instance_options,
                    },
                    "label": {"type": "plain_text", "text": "Select Instance"},
                },
                {
                    "type": "input",
                    "block_id": "instance_type_choice",
                    "element": {
                        "type": "static_select",
                        "action_id": "instance_type",
                        "placeholder": {
                            "type": "plain_text",
                            "text": "Select Instance Type",
                        },
                        "options": instance_type_options,
                    },
                    "label": {"type": "plain_text", "text": "Instance Type"},
                },
            ],
        }

        try:
            self.client.views_open(trigger_id=trigger_id, view=modal)
        except SlackApiError as e:
            return jsonify(
                status=500, text=f"Failed to open modal: {e.response['error']}"
            )

        return Response(status=200)

    def open_volume_create_modal(self, trigger_id: str, user_name: str) -> Response:
        """
        Opens the volume creation modal.
        """
        volume = self.aws_handler.get_volume_for_user(user_name)
        if volume is not None:
            return jsonify(
                response_type="ephemeral",
                text="EBS volume already exists, please /ebs destroy first.",
            )

        modal = {
            "type": "modal",
            "callback_id": "create_volume",
            "title": {"type": "plain_text", "text": "Create EBS Volume"},
            "submit": {"type": "plain_text", "text": "Create"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "volume_size",
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "volume_size_input",
                        "placeholder": {
                            "type": "plain_text",
                            "text": "Enter volume size in GiB",
                        },
                        "initial_value": "20",
                    },
                    "label": {
                        "type": "plain_text",
                        "text": f"Volume Size in GiB (max {self.config['max_volume_size']})",
                    },
                },
            ],
        }

        try:
            self.client.views_open(trigger_id=trigger_id, view=modal)
        except SlackApiError as e:
            return jsonify(
                status=500, text=f"Failed to open modal: {e.response['error']}"
            )

        return Response(status=200)

    def open_volume_resize_modal(self, trigger_id: str, user_name: str) -> Response:
        """
        Opens the volume resize modal.
        """
        volume = self.aws_handler.get_volume_for_user(user_name)
        if volume is None:
            return jsonify(
                response_type="ephemeral",
                text="No EBS volume found. Please /ebs create first.",
            )

        modal = {
            "type": "modal",
            "callback_id": "resize_volume",
            "title": {"type": "plain_text", "text": "Resize EBS Volume"},
            "submit": {"type": "plain_text", "text": "Resize"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "volume_size",
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "volume_size_input",
                        "placeholder": {
                            "type": "plain_text",
                            "text": "Enter volume size in GiB",
                        },
                        "initial_value": str(volume["size"]),
                    },
                    "label": {
                        "type": "plain_text",
                        "text": f"Volume Size in GiB (max {self.config['max_volume_size']})",
                    },
                },
            ],
        }

        try:
            self.client.views_open(trigger_id=trigger_id, view=modal)
        except SlackApiError as e:
            return jsonify(
                status=500, text=f"Failed to open modal: {e.response['error']}"
            )

        return Response(status=200)

    def open_volume_attach_modal(self, trigger_id: str, user_name: str) -> Response:
        """
        Opens the volume attachment modal.
        """
        instance_options = self.get_instance_options(user_name, ["running"])
        if not instance_options:
            return jsonify(response_type="ephemeral", text="No instances to attach to.")

        modal = {
            "type": "modal",
            "callback_id": "attach_volume",
            "title": {"type": "plain_text", "text": "Attach EBS Volume"},
            "submit": {"type": "plain_text", "text": "Attach"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "instance_selection",
                    "element": {
                        "type": "static_select",
                        "action_id": "selected_instance",
                        "placeholder": {
                            "type": "plain_text",
                            "text": "Select Instance",
                        },
                        "options": instance_options,
                    },
                    "label": {"type": "plain_text", "text": "Select Instance"},
                },
            ],
        }

        try:
            self.client.views_open(trigger_id=trigger_id, view=modal)
        except SlackApiError as e:
            return jsonify(
                status=500, text=f"Failed to open modal: {e.response['error']}"
            )

        return Response(status=200)

    def handle_interactions(self, payload: Dict[str, Any]) -> None:
        """
        Handles interactions.
        """
        user_id = payload["user"]["id"]
        user_name = payload["user"]["username"]
        callback_id = payload.get("view", {}).get("callback_id")
        values = payload.get("view", {}).get("state", {}).get("values")
        volume = self.aws_handler.get_volume_for_user(user_name=user_name)

        if callback_id == "submit_key":
            public_key = values["key_input"]["public_key"]["value"]
            function = self.aws_handler.create_key_pair
            kwargs = {
                "user_name": user_name,
                "public_key": public_key,
            }
            success_message = "Public key updated successfully."
            error_message = "Failed to store public key: {}"

        elif callback_id == "launch_instance":
            ami = self.config["amis"][
                values["ami_choice"]["ami"]["selected_option"]["value"]
            ]
            instance_type = values["instance_type_choice"]["instance_type"][
                "selected_option"
            ]["value"]
            root_ebs_size = int(values["root_ebs_size"]["root_ebs_size_input"]["value"])
            root_ebs_size = min(root_ebs_size, self.config["max_volume_size"])
            mount_option = values["mount_options"]["mount_input"]["selected_option"][
                "value"
            ]
            startup_script = values["startup_script"]["startup_script_input"]["value"]
            function = self.aws_handler.launch_ec2_instance
            kwargs = {
                "ami_id": ami["id"],
                "ami_user": ami["user"],
                "instance_type": instance_type,
                "root_ebs_size": root_ebs_size,
                "user_name": user_name,
                "startup_script": ami.get("startup_script", "")
                + (startup_script or ""),
                "mount_option": mount_option,
                "volume_id": volume["id"] if volume is not None else None,
            }
            user = ami["user"] if mount_option != "efs_root" else "root"
            success_message = (
                "EC2 instance {0} launched successfully. You can now run: "
                f"ssh {user}@{{0}}"
            )
            error_message = "Error launching EC2 instance: {}"

        elif callback_id in ["terminate_instance", "stop_instance", "start_instance"]:
            selected_instances = [
                option["value"]
                for option in values["instance_selection"]["selected_instances"][
                    "selected_options"
                ]
            ]
            command = callback_id.split("_")[0]
            function = getattr(self.aws_handler, f"{command}_ec2_instances")
            kwargs = {
                "instance_ids": selected_instances,
            }
            success_message = {
                "terminate_instance": f"Terminated instances: {', '.join(selected_instances)}",
                "stop_instance": f"Stopped instances: {', '.join(selected_instances)}",
                "start_instance": f"Started instances: {', '.join(selected_instances)}",
            }[callback_id]
            error_message = f"Error {command}ing instances: {{}}"

        elif callback_id == "change_instance":
            instance_id = values["instance_selection"]["selected_instances"][
                "selected_option"
            ]["value"]
            new_instance_type = values["instance_type_choice"]["instance_type"][
                "selected_option"
            ]["value"]
            instance_type = self.aws_handler.get_instance_type(instance_id)
            if new_instance_type == instance_type:
                self.client.chat_postMessage(
                    channel=user_id,
                    text=f"Instance {instance_id} is already of type {new_instance_type}.",
                )
                return
            function = self.aws_handler.change_instance_type
            kwargs = {
                "instance_id": instance_id,
                "instance_type": new_instance_type,
            }
            success_message = (
                f"Changed instance {instance_id} to type "
                f"{new_instance_type} successfully."
            )
            error_message = "Error changing instance type: {}"

        elif callback_id == "create_volume":
            volume_size = int(values["volume_size"]["volume_size_input"]["value"])
            volume_size = min(volume_size, self.config["max_volume_size"])
            function = self.aws_handler.create_volume
            kwargs = {
                "user_name": user_name,
                "size": volume_size,
            }
            success_message = f"EBS volume of {volume_size} GiB created successfully."
            error_message = "Error creating EBS volume: {}"

        elif callback_id == "resize_volume":
            volume_size = int(values["volume_size"]["volume_size_input"]["value"])
            volume_size = min(volume_size, self.config["max_volume_size"])
            function = self.aws_handler.resize_volume
            kwargs = {
                "volume_id": volume["id"] if volume is not None else None,
                "size": volume_size,
            }
            success_message = (
                f"EBS volume resized to {volume_size} GiB successfully. "
                "Remember to run resize2fs to resize the filesystem."
            )
            error_message = "Error resizing EBS volume: {}"

        elif callback_id == "attach_volume":
            instance_id = values["instance_selection"]["selected_instance"][
                "selected_option"
            ]["value"]
            function = self.aws_handler.attach_volume
            kwargs = {
                "volume_id": volume["id"] if volume is not None else None,
                "instance_id": instance_id,
            }
            success_message = (
                f"EBS volume attached to instance {instance_id} successfully."
            )
            error_message = "Error attaching EBS volume: {}"

        else:
            logging.warning("Unhandled callback_id: %s", callback_id)
            return

        self.handle_aws_command(
            function=function,
            user_id=user_id,
            success_message=success_message,
            error_message=error_message,
            **kwargs,
        )

    def handle_aws_command(
        self,
        function: Any,
        user_id: str,
        success_message: str,
        error_message: str,
        **kwargs: Any,
    ) -> None:
        """
        Handles AWS commands in a separate thread.
        """

        def run_command() -> None:
            """
            Run the AWS command.
            """
            try:
                response = function(**kwargs)
                self.client.chat_postMessage(
                    channel=user_id, text=success_message.format(response)
                )
            except self.aws_handler.ec2_client.exceptions.ClientError as e:
                self.client.chat_postMessage(
                    channel=user_id, text=error_message.format(e)
                )

        threading.Thread(target=run_command).start()
