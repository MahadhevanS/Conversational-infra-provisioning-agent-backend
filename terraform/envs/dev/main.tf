terraform {
  required_version = ">= 1.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region
}

# =====================
# NETWORK (Always On)
# =====================

module "network" {
  source = "../../modules/network"
}

# =====================
# EC2
# =====================

module "ec2" {
  source = "../../modules/ec2"
  count  = var.enable_ec2 ? 1 : 0

  ami               = var.ec2_ami
  instance_type     = var.ec2_instance_type
  subnet_id         = module.network.subnet_ids[0]
  security_group_id = module.network.security_group_id
  environment       = var.environment
}

# =====================
# RDS
# =====================

module "rds" {
  source = "../../modules/rds"
  count  = var.enable_rds ? 1 : 0

  instance_type     = var.rds_instance_type
  subnet_ids        = module.network.subnet_ids
  security_group_id = module.network.security_group_id
  environment       = var.environment
}


# =====================
# S3
# =====================

module "s3" {
  source = "../../modules/s3"
  count  = var.enable_s3 ? 1 : 0

  bucket_name = var.s3_bucket_name
  environment = var.environment
}

# =====================
# EKS
# =====================

module "eks" {
  source = "../../modules/eks"
  count  = var.enable_eks ? 1 : 0

  cluster_name = var.eks_cluster_name
  subnet_ids   = module.network.subnet_ids
}
