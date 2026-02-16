# import os

# def generate_tfvars(blueprint, terraform_dir):

#     tfvars_path = os.path.join(terraform_dir, "terraform.tfvars")

#     instance_type = None
#     environment = blueprint.get("environment", "dev")

#     # ------------------------------------------------
#     # Extract from components[]
#     # ------------------------------------------------
#     for comp in blueprint.get("components", []):

#         if comp["type"] == "compute":
#             instance_type = comp["compute"]["instance_type"]

#     # ------------------------------------------------
#     # Write tfvars
#     # ------------------------------------------------
#     with open(tfvars_path, "w") as f:

#         if instance_type:
#             f.write(f'instance_type = "{instance_type}"\n')

#         f.write(f'environment = "{environment}"\n')

#     return tfvars_path

import os
import json


def generate_tfvars(blueprint, terraform_dir):

    tfvars_path = os.path.join(terraform_dir, "terraform.tfvars.json")

    # ---------------------------
    # Base structure
    # ---------------------------

    tfvars = {
        "environment": blueprint.get("environment", "development"),
        "region": blueprint.get("region", "us-east-1"),

        # Enable flags (default OFF)
        "enable_ec2": False,
        "enable_rds": False,
        "enable_s3": False,
        "enable_eks": False,

        # Defaults
        "ec2_instance_type": "t2.micro",
        "rds_instance_type": "db.t3.micro",
        "s3_versioning": False,
        "eks_min_nodes": 1,
        "eks_max_nodes": 2
    }

    # ---------------------------
    # Parse components[]
    # ---------------------------

    for comp in blueprint.get("components", []):

        service = comp.get("service")

        # =====================
        # EC2
        # =====================
        if service == "ec2":
            tfvars["enable_ec2"] = True

            tfvars["ec2_instance_type"] = \
                comp.get("compute", {}).get(
                    "instance_type",
                    tfvars["ec2_instance_type"]
                )

        # =====================
        # RDS
        # =====================
        if service == "rds":
            tfvars["enable_rds"] = True

            tfvars["rds_instance_type"] = \
                comp.get("database", {}).get(
                    "instance_type",
                    tfvars["rds_instance_type"]
                )

        # =====================
        # S3
        # =====================
        if service == "s3":
            tfvars["enable_s3"] = True

            versioning = comp.get("storage", {}).get("versioning", "false")
            tfvars["s3_versioning"] = str(versioning).lower() == "true"

        # =====================
        # EKS
        # =====================
        if service == "eks":
            tfvars["enable_eks"] = True

            container = comp.get("container", {})

            tfvars["eks_min_nodes"] = int(container.get("min_nodes", 1))
            tfvars["eks_max_nodes"] = int(container.get("max_nodes", 2))

    # ---------------------------
    # Write JSON tfvars
    # ---------------------------

    with open(tfvars_path, "w") as f:
        json.dump(tfvars, f, indent=2)

    return tfvars_path
