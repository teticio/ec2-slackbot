# Slackbot for AWS EC2 Management

This repository contains a Slackbot that allows you to manage AWS EC2 instances directly from Slack. The bot is built with Python, using Flask for the web server and the `slack-sdk` for interacting with the Slack API.

## Features

- Launch EC2 instances with specified parameters.
- Terminate running EC2 instances.
- Upload SSH public keys for EC2 instances.
- Create, attach, detach and destroy EBS volumes.
- Optionally mount SageMaker Studio EFS or EBS volume.
- Warn users to consider terminating long-running EC2 instances.

## Usage

The bot is designed to be used with Slack slash commands. The following commands are supported:

- `/ec2 key`: Upload your public SSH key for EC2 instances.
- `/ec2 up`: Launch an EC2 instance. This opens a modal where you can select the AMI, instance type, and other options.
- `/ec2 down`: Terminate running EC2 instances. This opens a modal where you can select the instances to terminate.
- `/ec2 create_volume`: Create a new EBS volume (limited to one per user).
- `/ec2 resize_volume`: Resize an existing EBS volume.
- `/ec2 attach_volume`: Attach an EBS volume to an EC2 instance. This opens a modal where you can select the volume and the instance.
- `/ec2 detach_volume`: Detach an EBS volume from an EC2 instance. This opens a modal where you can select the volume and the instance.
- `/ec2 destroy_volume`: Destroy an existing EBS volume. This opens a modal where you can select the volume to destroy.

## Configuration

The bot's configuration is stored in a `config.yaml` file. An example configuration is provided in `config.yaml.example`. The configuration includes AWS region, subnet and security group details, as well as AMI and instance type options.

## SSM (Simple Systems Manager)

The instances establish a connection using SSH over SSM.

For AWS, you need to perform the following steps:

1. Create a role and attach the `AmazonSSMManagedInstanceCore` policy to it. Then, set the `iam_instance_profile` in `config.yaml` to the name of this profile.
2. If your `subnet` is private, you will need to [configure your VPC endpoints](https://repost.aws/knowledge-center/ec2-systems-manager-vpc-endpoints) to allow SSM connections.
3. Make sure your AWS account is set to have an "advanced activation tier":

    ```bash
    aws ssm update-service-setting \
        --setting-id arn:aws:ssm:<region>:<account>:servicesetting/ssm/managed-instance/activation-tier \
        --setting-value advanced
    ```

4. Verify that the user's IAM policy includes:

    ```json
    {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": "ssm:StartSession",
                "Resource": [
                    "arn:aws:ec2:*",
                    "arn:aws:ssm:*:*:document/AWS-StartSSHSession"
                ],
                "Condition": {
                    "BoolIfExists": {
                        "ssm:SessionDocumentAccessCheck": "true"
                    }
                }
            }
        ]
    }
    ```

For your local machine, you need to:

1. [Install the Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html).
2. Insert the following lines into your `~/.ssh/config`:

    ```bash
    # >>> AWS SSM config >>>
    Host i-* mi-*
        StrictHostKeyChecking accept-new
        ForwardAgent yes
        ProxyCommand bash -c 'export PATH=$PATH:/usr/local/bin; aws ssm start-session --target %h --document-name AWS-StartSSHSession --parameters "portNumber=%p"'
    # <<< AWS SSM config <<<
    ```

After these configurations, users can SSH into instances using:

```bash
ssh ubuntu@i-...  # i-... is the instance id
```

## Mount SageMaker Studio EFS

The "classic" version of SageMaker Studio mounts a shared EFS drive on all instances. One key advantage of using a regular EC2 instance is the ability to run `docker` directly, unlike SageMaker Studio apps which operate within a docker container.

In order to mount the EFS folder associated with the Slack user, you need to specify the `efs_ip` of the EFS that corresponds to the `subnet`, and the `sagemaker_studio_domain_id` in the `config.yaml` file. Additionally, the `security_groups` should incorporate the one used by SageMaker Studio for NFS ingress.

## Mount EBS

Every Slack user can create an EBS volume with the `/ec2 create_volume` command, which they can mount at `/home`. During the initial setup, the volume will be formatted, and the `/home` directory will be configured. EBS volumes offer higher performance compared to EFS due to their non-networked nature, but they are typically limited to being attached to a single EC2 instance at a time.

If you choose not to mount the EBS at `/home`, you can use it as an additional device. For more details, refer to the section "Common Operations with EBS Volumes".

**Note:** EBS volumes of type `io1` and `io2` support multi-attach, but this requires a cluster setup.

## Deployment Steps

1. Install the necessary dependencies by running `poetry install --no-root` in your terminal.
2. Create a new [Slack app](https://api.slack.com/apps). This will be used to interact with your deployment.
3. Update the `.env` file with your `SLACK_BOT_TOKEN` and `SLACK_SIGNING_SECRET`. These are essential for the Slack app to function correctly.
4. Start the application by executing `python app.py` in your terminal. This will start the server on port 3000. To make the server accessible publicly, you can use a tool like `ngrok` to forward the port.
5. Lastly, configure your Slack app. Make sure that the manifest file includes the following settings:

    ```yaml
    ...
    features:
      bot_user:
        display_name: EC2
        always_online: false
      slash_commands:
        - command: /ec2
          url: https://<your-url>/slack/commands
          description: EC2
          should_escape: false
    oauth_config:
      scopes:
        bot:
          - chat:write
          - commands
          - im:write
          - users:read
    settings:
      interactivity:
        is_enabled: true
        request_url: https://<your-url>/slack/events
    ...
    ```

## Common Operations with EBS Volumes

The EBS device will either be `/dev/xvdh` or `/dev/nvme1n1` depending on the type of the EC2 instance.

```bash
if [ -e /dev/xvdh ]; then
    device=/dev/xvdh
else
    device=/dev/nvme1n1
fi
```

To format the EBS volume:

```bash
sudo mkfs -t ext4 $device
```

To mount the EBS volume at `/mnt` and ensure it is mounted automatically after a reboot:

```bash
mount=/mnt
mount $device $mount
sudo chown $USER:$USER $mount
echo "$device $mount ext4 defaults,nofail 0 2" >> /etc/fstab
```

If you resize the EBS volume with `/ec2 resize_volume` then you will need to run

```bash
sudo resize2fs $device
```
