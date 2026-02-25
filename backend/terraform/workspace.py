import os
import json
import uuid


BASE_DIR = "workspaces"
os.makedirs(BASE_DIR, exist_ok=True)

def generate_tf_from_blueprint(blueprint: dict):

    job_id = str(uuid.uuid4())
    working_dir = os.path.join(BASE_DIR, job_id)

    os.makedirs(working_dir, exist_ok=True)

    # Generate main.tf
    main_tf = generate_main_tf(blueprint)

    with open(os.path.join(working_dir, "main.tf"), "w") as f:
        f.write(main_tf)

    return working_dir

def generate_main_tf(blueprint: dict):

    if blueprint["type"] == "ec2":

        return f"""
          provider "aws" {{
            region = "us-east-1"
          }}

          resource "aws_instance" "app" {{
            ami           = "{blueprint['ami']}"
            instance_type = "{blueprint['instance_type']}"

            tags = {{
              Name = "{blueprint['name']}"
            }}
          }}
          """
