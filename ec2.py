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
    startup_script: Optional[str] = None,
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
    :param startup_script: The startup script (optional).
    :return: The ID of the launched instance.
    """
    user_data_script = dedent(
        """\
        #!/bin/bash
    """
    )

    if efs_ip is not None and sms_user_name is not None and sms_domain_id is not None:
        user_id = boto3.client("sagemaker").describe_user_profile(
            DomainId=sms_domain_id, UserProfileName=sms_user_name
        )["HomeEfsFileSystemUid"]

        user_data_script += dedent(
            f"""\
            USER=ubuntu
            HOME=/home/$USER

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
            sudo touch $HOME/.ssh/authorized_keys
            sudo chmod 600 $HOME/.ssh/authorized_keys
            sudo cat /tmp/authorized_keys $HOME/.ssh/authorized_keys | sort | uniq | sudo tee $HOME/.ssh/authorized_keys
            sudo chown ubuntu:users $HOME/.ssh/authorized_keys

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
    return instance_id
