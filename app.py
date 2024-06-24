# TODO:
# attach EBS volume
# warnings about long-running instances

import json
import os
from typing import Dict, Any

import boto3
import yaml
from flask import Flask, Response, abort, jsonify, request
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.signature import SignatureVerifier

from ec2 import launch_ec2_instance

config = yaml.safe_load(open("config.yaml"))

app = Flask(__name__)
client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
verifier = SignatureVerifier(os.environ["SLACK_SIGNING_SECRET"])
ec2_client = boto3.client("ec2", region_name=config["region"])


@app.before_request
def verify_slack_signature() -> None:
    """
    Verifies the slack signature before each request.
    """
    if request.path.startswith("/slack/") and not verifier.is_valid_request(
        body=request.get_data(), headers=request.headers
    ):
        abort(response="Invalid Slack signature", status=400)


@app.route("/slack/events", methods=["POST"])
def slack_events() -> Response:
    """
    Handles slack events.
    """
    if request.content_type == "application/json":
        data = request.json
    elif request.content_type == "application/x-www-form-urlencoded":
        data = request.form
    else:
        return Response(response="Unsupported Media Type", status=415)

    if "payload" in data:
        payload = json.loads(data["payload"])
        event_type = payload.get("type")

        if event_type == "view_submission":
            handle_interactions(payload=payload)

    if "challenge" in data:
        return jsonify({"challenge": data["challenge"]})

    return Response(status=200)


@app.route("/slack/commands", methods=["POST"])
def handle_commands() -> Response:
    """
    Handles slack commands.
    """
    data = request.form
    command = data.get("command")
    sub_command = data.get("text")
    trigger_id = data.get("trigger_id")
    user_name = data.get("user_name")

    if command == "/ec2":
        if sub_command == "key":
            return open_key_modal(trigger_id=trigger_id)

        elif sub_command == "up":
            try:
                ec2_client.describe_key_pairs(KeyNames=[user_name])
            except ec2_client.exceptions.ClientError:
                return jsonify(
                    response_type="ephemeral",
                    text="Please upload your public key first with /ec2 key.",
                )
            return open_instance_launch_modal(trigger_id=trigger_id)

        elif sub_command == "down":
            return open_instance_terminate_modal(
                trigger_id=trigger_id, user_name=user_name
            )

        else:
            return jsonify(
                response_type="ephemeral",
                text="Command must be one of: key, up, down.",
            )

    return jsonify(response_type="ephemeral", text="Command not recognized.")


def open_key_modal(trigger_id: str) -> Response:
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
                "label": {
                    "type": "plain_text",
                    "text": "Public Key",
                },
            }
        ],
    }

    try:
        client.views_open(trigger_id=trigger_id, view=modal)
    except SlackApiError as e:
        print(f"Error opening modal: {e.response['error']}")

    return Response(status=200)


def open_instance_launch_modal(trigger_id: str) -> Response:
    """
    Opens the instance launch modal.
    """
    ami_options = [
        {"text": {"type": "plain_text", "text": ami["name"]}, "value": ami["id"]}
        for ami in config["amis"]
    ]
    instance_type_options = [
        {"text": {"type": "plain_text", "text": type}, "value": type}
        for type in config["instance_types"]
    ]
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
                "type": "section",
                "block_id": "options",
                "text": {"type": "mrkdwn", "text": "Options"},
                "accessory": {
                    "type": "checkboxes",
                    "action_id": "efs_mount_check",
                    "options": [
                        {
                            "text": {
                                "type": "plain_text",
                                "text": "Mount SageMaker Studio EFS",
                            },
                            "value": "mount_efs",
                        }
                    ],
                },
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
                "label": {"type": "plain_text", "text": "Startup Script (Optional)"},
                "optional": True,
            },
        ],
    }

    try:
        client.views_open(trigger_id=trigger_id, view=modal)
    except SlackApiError as e:
        return jsonify(status=500, text=f"Failed to open modal: {e.response['error']}")

    return Response(status=200)


def open_instance_terminate_modal(trigger_id: str, user_name: str) -> Response:
    """
    Opens the instance termination modal.
    """
    instances = ec2_client.describe_instances(
        Filters=[
            {"Name": "tag:User", "Values": [user_name]},
            {"Name": "instance-state-name", "Values": ["pending", "running"]},
        ]
    )
    instance_options = [
        {
            "text": {"type": "plain_text", "text": instance["InstanceId"]},
            "value": instance["InstanceId"],
        }
        for reservation in instances["Reservations"]
        for instance in reservation["Instances"]
    ]
    if len(instance_options) == 0:
        return jsonify(response_type="ephemeral", text="No instances to terminate.")

    modal = {
        "type": "modal",
        "callback_id": "terminate_instance",
        "title": {"type": "plain_text", "text": "Terminate EC2 Instances"},
        "submit": {"type": "plain_text", "text": "Terminate"},
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
        client.views_open(trigger_id=trigger_id, view=modal)
    except SlackApiError as e:
        return jsonify(status=500, text=f"Failed to open modal: {e.response['error']}")

    return Response(status=200)


def handle_interactions(payload: Dict[str, Any]) -> None:
    """
    Handles interactions.
    """
    user_id = payload["user"]["id"]
    user_name = payload["user"]["username"]
    callback_id = payload.get("view", {}).get("callback_id")
    values = payload.get("view", {}).get("state", {}).get("values")

    if callback_id == "submit_key":
        public_key = values["key_input"]["public_key"]["value"]
        handle_key_submission(user_id=user_id, public_key=public_key)

    elif callback_id == "launch_instance":
        ami_id = values["ami_choice"]["ami"]["selected_option"]["value"]
        instance_type = values["instance_type_choice"]["instance_type"][
            "selected_option"
        ]["value"]
        mount_efs = any(
            option["value"] == "mount_efs"
            for option in values["options"]["efs_mount_check"]["selected_options"]
        )
        startup_script = values["startup_script"]["startup_script_input"]["value"]
        handle_instance_launch(
            user_id=user_id,
            user_name=user_name,
            ami_id=ami_id,
            instance_type=instance_type,
            mount_efs=mount_efs,
            startup_script=startup_script,
        )

    elif callback_id == "terminate_instance":
        selected_instances = [
            option["value"]
            for option in values["instance_selection"]["selected_instances"][
                "selected_options"
            ]
        ]
        handle_instance_termination(user_id=user_id, instance_ids=selected_instances)


def handle_key_submission(user_id: str, public_key: str) -> None:
    """
    Handles key submission.
    """
    try:
        ec2_client.delete_key_pair(KeyName=user_id)
    except ec2_client.exceptions.ClientError as e:
        pass

    try:
        ec2_client.import_key_pair(KeyName=user_id, PublicKeyMaterial=public_key)
        client.chat_postMessage(
            channel=user_id, text="Public key has been updated successfully."
        )
    except ec2_client.exceptions.ClientError as e:
        client.chat_postMessage(
            channel=user_id,
            text=f"Failed to store public key: {e.response['Error']['Message']}",
        )


def handle_instance_launch(
    user_id: str,
    user_name: str,
    ami_id: str,
    instance_type: str,
    mount_efs: bool,
    startup_script: str,
) -> None:
    """
    Handles instance launch.
    """
    try:
        instance = launch_ec2_instance(
            ec2_client=ec2_client,
            ami_id=ami_id,
            iam_instance_profile=config["iam_instance_profile"],
            instance_type=instance_type,
            key_name=user_name,
            security_group_ids=config["security_group_ids"],
            subnet_id=config["subnet_id"],
            efs_ip=config["efs_ip"] if mount_efs else None,
            sms_user_name=user_name.replace(".", "-"),
            sms_domain_id=config["sagemaker_studio_domain_id"],
            startup_script=startup_script,
        )
        client.chat_postMessage(
            channel=user_id, text=f"EC2 instance {instance} launched successfully."
        )
    except ec2_client.exceptions.ClientError as e:
        client.chat_postMessage(
            channel=user_id,
            text=f"Error launching EC2 instance: {e.response['Error']['Message']}",
        )


def handle_instance_termination(user_id: str, instance_ids: list) -> None:
    """
    Handles instance termination.
    """
    try:
        ec2_client.terminate_instances(InstanceIds=instance_ids)
        terminated_instances = ", ".join(instance_ids)
        client.chat_postMessage(
            channel=user_id, text=f"Terminated instances: {terminated_instances}"
        )
    except ec2_client.exceptions.ClientError as e:
        client.chat_postMessage(
            channel=user_id,
            text=f"Error terminating instances: {e.response['Error']['Message']}",
        )


if __name__ == "__main__":
    app.run(port=3000)
