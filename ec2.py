from textwrap import dedent
from typing import List, Optional, Union

import boto3
from botocore.client import BaseClient


def launch_ec2_instance(
    ec2_client: Union[BaseClient, boto3.client],
    ami_id: str,
    iam_instance_profile: str,
    instance_type: str,
    key_name: str,
    security_group_ids: List[str],
    subnet_id: str,
    efs_ip: Optional[str] = None,
    sms_user_name: Optional[str] = None,
    sms_domain_id: Optional[str] = None,
    volume_id: Optional[str] = None,
    startup_script: Optional[str] = None,
    mount_option: Optional[str] = None,
) -> str:
    """
    Launches an EC2 instance with the given parameters.

    :param ec2_client: The boto3 EC2 client.
    :param ami_id: The ID of the Amazon Machine Image (AMI).
    :param iam_instance_profile: The IAM instance profile.
    :param instance_type: The type of instance.
    :param key_name: The name of the key pair.
    :param security_group_ids: The IDs of the security groups.
    :param subnet_id: The ID of the subnet.
    :param efs_ip: The IP address of the EFS (optional).
    :param sms_user_name: The name of the SageMaker Studio user (optional).
    :param sms_domain_id: The ID of the SageMaker Studio domain (optional).
    :param volume_id: The ID of the volume (optional).
    :param startup_script: The startup script (optional).
    :param mount_option: The mount option ("efs" or "ebs") (optional).
    :return: The ID of the launched instance.
    """
    user_data_script = dedent(
        """\
        #!/bin/bash

        USER=ubuntu
        HOME=/home/$USER

        # Install Docker engine
        sudo mkdir -p /etc/apt/keyrings
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
        sudo apt-get update
        sudo DEBIAN_FRONTEND=noninteractive apt-get install docker-ce docker-ce-cli containerd.io docker-compose-plugin -y
        sudo usermod -aG docker $USER
        sudo systemctl enable docker
        """
    )

    if mount_option == "efs":
        user_id = boto3.client("sagemaker").describe_user_profile(
            DomainId=sms_domain_id, UserProfileName=sms_user_name
        )["HomeEfsFileSystemUid"]

        user_data_script += dedent(
            f"""\
            # Install NFS client
            sudo apt-get update
            sudo DEBIAN_FRONTEND=noninteractive apt-get install nfs-common -y

            # Move the authorized_keys file out of the way
            sudo mv $HOME/.ssh/authorized_keys /tmp/authorized_keys

            # Mount the EFS file system
            sudo groupmod -g 1001 users
            sudo usermod -u {user_id} -g users $USER
            echo "{efs_ip}:/{user_id} $HOME nfs nfsvers=4.1,rsize=1048576,wsize=1048576,hard,timeo=600,retrans=2,noresvport 0 0" | sudo tee -a /etc/fstab
            sudo mount $HOME

            # Merge the authorized_keys files
            sudo mkdir -p $HOME/.ssh
            sudo touch $HOME/.ssh/authorized_keys
            sudo chmod 600 $HOME/.ssh/authorized_keys
            sudo cat /tmp/authorized_keys $HOME/.ssh/authorized_keys | sort | uniq | sudo tee $HOME/.ssh/authorized_keys
            sudo chown $USER:users $HOME/.ssh/authorized_keys
            """
        )

    elif mount_option == "ebs":
        user_data_script += dedent(
            """\
            if [ -e /dev/xvdh ]; then
                device=/dev/xvdh
            else
                device=/dev/nvme1n1
            fi

            # Format the EBS if necessary
            if ! file -s $device | grep -q "filesystem"; then
                sudo mkfs -t ext4 $device
                sudo mount $device /mnt
                sudo rsync -aXv /home/ /mnt/
                sudo umount /mnt
            fi

            # Move the authorized_keys file out of the way
            sudo mv $HOME/.ssh/authorized_keys /tmp/authorized_keys

            # Mount the EBS
            sudo mount $device /home
            echo "$device /home ext4 defaults,nofail 0 2" >> /etc/fstab

            # Merge the authorized_keys files
            sudo mkdir -p $HOME/.ssh
            sudo touch $HOME/.ssh/authorized_keys
            sudo chmod 600 $HOME/.ssh/authorized_keys
            sudo cat /tmp/authorized_keys $HOME/.ssh/authorized_keys | sort | uniq | sudo tee $HOME/.ssh/authorized_keys
            sudo chown $USER:users $HOME/.ssh/authorized_keys
            """
        )

    if startup_script is not None:
        user_data_script += startup_script

    response = ec2_client.run_instances(
        IamInstanceProfile={"Name": iam_instance_profile},
        ImageId=ami_id,
        InstanceType=instance_type,
        KeyName=key_name,
        MinCount=1,
        MaxCount=1,
        SecurityGroupIds=security_group_ids,
        SubnetId=subnet_id,
        TagSpecifications=[
            {
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "User", "Value": key_name},
                ],
            }
        ],
        UserData=user_data_script,
    )

    instance_id = response["Instances"][0]["InstanceId"]
    waiter = ec2_client.get_waiter("instance_running")
    waiter.wait(InstanceIds=[instance_id])

    if mount_option == "ebs":
        ec2_client.attach_volume(
            Device="/dev/sdh",
            InstanceId=instance_id,
            VolumeId=volume_id,
        )

    return instance_id
