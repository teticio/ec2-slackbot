"""
This module contains the AWSHandler class, which is responsible
for handling AWS-related operations.
"""

from datetime import datetime
from textwrap import dedent
from time import sleep
from typing import Any, Dict, List, Optional

import boto3


class AWSHandler:
    """
    A class to handle AWS-related operations.
    """

    def __init__(
        self, config: Dict[str, Any], endpoint_url: Optional[str] = None
    ) -> None:
        self.ec2_client = boto3.client(
            "ec2", region_name=config["region"], endpoint_url=endpoint_url
        )
        self.sagemaker_client = boto3.client(
            "sagemaker", region_name=config["region"], endpoint_url=endpoint_url
        )
        self.config = config

    def get_instances_for_user(
        self, user_name: str, states: List[str]
    ) -> Dict[str, Any]:
        """
        Retrieves the instances for a given user in the given states.
        """
        return self.ec2_client.describe_instances(
            Filters=[
                {"Name": "tag:User", "Values": [user_name]},
                {"Name": "instance-state-name", "Values": states},
            ]
        )

    def get_instance_type(self, instance_id: str) -> str:
        """
        Retrieves the instance type with the given ID.
        """
        return self.ec2_client.describe_instances(InstanceIds=[instance_id])[
            "Reservations"
        ][0]["Instances"][0]["InstanceType"]

    def get_running_instance_details(self) -> List[Dict[str, Any]]:
        """
        Retrieves the details of all running instances.
        """
        running_instances = self.ec2_client.describe_instances(
            Filters=[{"Name": "instance-state-name", "Values": ["running"]}]
        )

        instance_details = []
        for reservation in running_instances["Reservations"]:
            for instance in reservation["Instances"]:
                launch_time = instance["LaunchTime"]
                instance_id = instance["InstanceId"]
                instance_type = instance["InstanceType"]
                instance_cost = self.config["instance_types"].get(
                    instance_type, self.config["large_instance_cost_threshold"]
                )
                user_name = next(
                    (
                        tag["Value"]
                        for tag in instance.get("Tags", [])
                        if tag["Key"] == "User"
                    ),
                    None,
                )
                running_days = (datetime.now(launch_time.tzinfo) - launch_time).days
                instance_details.append(
                    {
                        "instance_id": instance_id,
                        "instance_type": instance_type,
                        "instance_cost": instance_cost,
                        "user_name": user_name,
                        "running_days": running_days,
                    }
                )

        return instance_details

    def get_volume_for_user(self, user_name: str) -> Optional[Dict[str, Any]]:
        """
        Retrieves the volume ID, size and attachments for a given user's volume.
        """
        volumes = self.ec2_client.describe_volumes(
            Filters=[{"Name": "tag:User", "Values": [user_name]}]
        )
        if not volumes["Volumes"]:
            return None
        volume = volumes["Volumes"][0]
        return {
            "id": volume["VolumeId"],
            "size": volume["Size"],
            "attachments": volume.get("Attachments", []),
        }

    def get_sagemaker_studio_uid(self, user_name: str) -> Optional[str]:
        """
        Retrieves the SageMaker Studio UID for a given user.
        """
        if "sagemaker_studio_domain_id" not in self.config:
            return None

        try:
            return self.sagemaker_client.describe_user_profile(
                DomainId=self.config["sagemaker_studio_domain_id"],
                UserProfileName=user_name.replace(".", "-"),
            )["HomeEfsFileSystemUid"]
        except self.sagemaker_client.exceptions.ResourceNotFound:
            return None

    def create_key_pair(self, user_name: str, public_key: str) -> None:
        """
        Creates a public key pair for the user.
        """
        try:
            self.ec2_client.delete_key_pair(KeyName=user_name)
        except self.ec2_client.exceptions.ClientError:
            pass
        self.ec2_client.import_key_pair(KeyName=user_name, PublicKeyMaterial=public_key)

    def get_tags_for_user(self, user_name: str) -> List[Dict[str, str]]:
        """
        Retrieves the tags for a given user.
        """
        tags = [{"Key": "User", "Value": user_name}]
        tags += [
            {"Key": key, "Value": value}
            for key, value in self.config.get("default_tags", []).items()
        ]
        return tags

    def launch_ec2_instance(
        self,
        ami_id: str,
        ami_user: str,
        instance_type: str,
        root_ebs_size: int,
        user_name: str,
        startup_script: Optional[str] = None,
        mount_option: Optional[str] = None,
        volume_id: Optional[str] = None,
    ) -> str:
        """
        Launches an EC2 instance with the given parameters.

        :param ami_id: The ID of the Amazon Machine Image (AMI).
        :param ami_user: The default user of the AMI.
        :param instance_type: The type of instance.
        :param root_ebs_size: The size of the root EBS volume.
        :param user_name: The name of the user.
        :param startup_script: The startup script (optional).
        :param mount_option: The mount option ("efs" or "ebs") (optional).
        :param volume_id: The ID of the EBS volume to mount (optional).
        :return: The ID of the launched instance.
        """
        user_data_script = dedent(
            f"""\
            #!/bin/bash
            set -v

            USER={ami_user}
            HOME=/home/$USER

            # Alias sudo to run commands directly if sudo is not available
            if ! command -v sudo &> /dev/null; then
                alias sudo=''
            fi
            """
        )

        if mount_option == "efs" or mount_option == "efs_root":
            uid = self.get_sagemaker_studio_uid(user_name)
            user_data_script += dedent(
                f"""\
                # Install NFS client
                if command -v apt-get &> /dev/null; then
                    sudo apt-get update
                    sudo DEBIAN_FRONTEND=noninteractive apt-get install nfs-common bindfs -y
                elif command -v yum &> /dev/null; then
                    sudo yum install -y nfs-utils bindfs
                elif command -v zypper &> /dev/null; then
                    sudo zypper install -y nfs-client bindfs
                else
                    echo "Unsupported package manager. Please install nfs client and bindfs manually."
                    exit 1
                fi

                # Save authorized_key
                read -r authorized_key < $HOME/.ssh/authorized_keys

                # Mount the EFS file system
                sudo groupmod -g 1001 users
                sudo usermod -u {uid} -g users $USER
                echo "{self.config.get('efs_ip')}:/{uid} $HOME nfs nfsvers=4.1,rsize=1048576,\
wsize=1048576,hard,timeo=600,retrans=2,noresvport 0 0" | sudo tee -a /etc/fstab
                sudo mount $HOME

                # Restore authorized_key
                sudo mkdir -p $HOME/.ssh
                sudo touch $HOME/.ssh/authorized_keys
                sudo chmod 600 $HOME/.ssh/authorized_keys
                if ! grep -Fxq "$authorized_key" $HOME/.ssh/authorized_keys; then
                    echo "$authorized_key" >> $HOME/.ssh/authorized_keys
                fi
                sudo chown -R $USER:users $HOME/.ssh
                """
            )
            if mount_option == "efs_root":
                user_data_script += dedent(
                    """\
                    sudo bindfs --map=ubuntu/root -o nonempty /home/ubuntu /root
                    """
                )

        elif mount_option == "ebs":
            user_data_script += dedent(
                """\
                if [ -e /dev/xvdh ]; then
                    device=/dev/xvdh
                elif [ -e /dev/nvme2n1 ]; then
                    device=/dev/nvme2n1
                else
                    device=/dev/nvme1n1
                fi

                # Format the EBS volume if necessary
                if ! sudo file -s $device | grep -q "filesystem"; then
                    sudo mkfs -L ebs_volume -t ext4 $device
                    sudo mount $device /mnt
                    sudo rsync -aXv $HOME/ /mnt/
                    sudo umount /mnt
                fi

                # Save authorized_key
                read -r authorized_key < $HOME/.ssh/authorized_keys

                # Mount the EBS volume
                echo "LABEL=ebs_volume $HOME ext4 defaults,nofail 0 2" | sudo tee -a /etc/fstab
                sudo mount $HOME

                # Restore authorized_key
                sudo mkdir -p $HOME/.ssh
                sudo touch $HOME/.ssh/authorized_keys
                sudo chmod 600 $HOME/.ssh/authorized_keys
                if ! grep -Fxq "$authorized_key" $HOME/.ssh/authorized_keys; then
                    echo "$authorized_key" >> $HOME/.ssh/authorized_keys
                fi
                sudo chown -R $USER:$USER $HOME/.ssh
                """
            )

        if startup_script is not None:
            user_data_script += dedent(
                f"""\
                cd $HOME
                sudo su $USER -c 'bash -s' <<'UNLIKELY_STRING'
                {startup_script}
                """
                + "UNLIKELY_STRING"
            )

        params = {
            "ImageId": ami_id,
            "InstanceType": instance_type,
            "KeyName": user_name,
            "MinCount": 1,
            "MaxCount": 1,
            "BlockDeviceMappings": [
                {
                    "DeviceName": "/dev/sda1",
                    "Ebs": {"VolumeSize": root_ebs_size, "VolumeType": "gp2"},
                }
            ],
            "TagSpecifications": [
                {"ResourceType": "instance", "Tags": self.get_tags_for_user(user_name)}
            ],
            "UserData": user_data_script,
        }
        if "iam_instance_profile" in self.config:
            params["IamInstanceProfile"] = {"Name": self.config["iam_instance_profile"]}
        if "security_group_ids" in self.config:
            params["SecurityGroupIds"] = self.config["security_group_ids"]
        if "subnet_id" in self.config:
            params["SubnetId"] = self.config["subnet_id"]
        response = self.ec2_client.run_instances(**params)

        instance_id = response["Instances"][0]["InstanceId"]
        waiter = self.ec2_client.get_waiter("instance_running")
        waiter.wait(InstanceIds=[instance_id])

        if mount_option == "ebs":
            self.ec2_client.attach_volume(
                Device="/dev/sdh", InstanceId=instance_id, VolumeId=volume_id
            )
            waiter = self.ec2_client.get_waiter("volume_in_use")
            waiter.wait(VolumeIds=[volume_id])

        waiter = self.ec2_client.get_waiter("instance_status_ok")
        waiter.wait(InstanceIds=[instance_id])
        return instance_id

    def terminate_ec2_instances(self, instance_ids: List[str]) -> None:
        """
        Terminates the EC2 instances with the given IDs.
        """
        self.ec2_client.terminate_instances(InstanceIds=instance_ids)
        waiter = self.ec2_client.get_waiter("instance_terminated")
        waiter.wait(InstanceIds=instance_ids)

    def stop_ec2_instances(self, instance_ids: List[str]) -> None:
        """
        Stops the EC2 instances with the given IDs.
        """
        self.ec2_client.stop_instances(InstanceIds=instance_ids)
        waiter = self.ec2_client.get_waiter("instance_stopped")
        waiter.wait(InstanceIds=instance_ids)

    def start_ec2_instances(self, instance_ids: List[str]) -> None:
        """
        Starts the EC2 instances with the given IDs.
        """
        self.ec2_client.start_instances(InstanceIds=instance_ids)
        waiter = self.ec2_client.get_waiter("instance_running")
        waiter.wait(InstanceIds=instance_ids)

    def change_instance_type(self, instance_id: str, instance_type: str) -> None:
        """
        Changes the instance type of the EC2 instance with the given ID.
        """
        self.stop_ec2_instances([instance_id])
        self.ec2_client.modify_instance_attribute(
            InstanceId=instance_id, Attribute="instanceType", Value=instance_type
        )
        self.start_ec2_instances([instance_id])

    def create_volume(self, user_name: str, size: int) -> str:
        """
        Creates a volume with the given user name and size.
        """
        create_volume_params = {
            "Size": size,
            "AvailabilityZone": self.config["zone"],
            "TagSpecifications": [
                {"ResourceType": "volume", "Tags": self.get_tags_for_user(user_name)}
            ],
            "VolumeType": "gp2",
        }
        response = self.ec2_client.create_volume(**create_volume_params)
        waiter = self.ec2_client.get_waiter("volume_available")
        waiter.wait(VolumeIds=[response["VolumeId"]])
        return response["VolumeId"]

    def resize_volume(self, volume_id: str, size: int) -> None:
        """
        Resizes the volume with the given ID.
        """
        self.ec2_client.modify_volume(VolumeId=volume_id, Size=size)
        for _ in range(60):
            response = self.ec2_client.describe_volumes_modifications(
                VolumeIds=[volume_id]
            )
            if not response["VolumesModifications"]:
                break
            if response["VolumesModifications"][0]["ModificationState"] == "completed":
                break
            sleep(1)

    def attach_volume(self, instance_id: str, volume_id: str) -> None:
        """
        Attaches the volume with the given ID to the instance with the given ID.
        """
        self.ec2_client.attach_volume(
            Device="/dev/sdh", InstanceId=instance_id, VolumeId=volume_id
        )
        waiter = self.ec2_client.get_waiter("volume_in_use")
        waiter.wait(VolumeIds=[volume_id])

    def detach_volume(self, volume_id: str, attachments: List[Dict[str, Any]]) -> None:
        """
        Detaches the volume with the given user ID.
        """
        for attachment in attachments:
            self.ec2_client.detach_volume(
                VolumeId=volume_id,
                InstanceId=attachment["InstanceId"],
                Device=attachment["Device"],
                Force=False,
            )
        waiter = self.ec2_client.get_waiter("volume_available")
        waiter.wait(VolumeIds=[volume_id])

    def destroy_volume(self, volume_id: str) -> None:
        """
        Destroys the volume with the given ID.
        """
        self.ec2_client.delete_volume(VolumeId=volume_id)
        waiter = self.ec2_client.get_waiter("volume_deleted")
        waiter.wait(VolumeIds=[volume_id])
