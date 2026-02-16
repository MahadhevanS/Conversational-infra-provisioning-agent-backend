variable "region" {
  type    = string
  default = "us-east-1"
}

variable "environment" {
  type = string
}

# Enable flags
variable "enable_ec2" {
  type    = bool
  default = false
}

variable "enable_rds" {
  type    = bool
  default = false
}

variable "enable_s3" {
  type    = bool
  default = false
}

variable "enable_eks" {
  type    = bool
  default = false
}

# EC2
variable "ec2_ami" {
  type    = string
  default = "ami-0c02fb55956c7d316"
}

variable "ec2_instance_type" {
  type    = string
  default = "t2.micro"
}

# RDS
variable "rds_instance_type" {
  type    = string
  default = "db.t3.micro"
}

# S3
variable "s3_bucket_name" {
  type    = string
  default = "my-demo-bucket-12345"
}

variable "s3_versioning" {
  type    = bool
  default = false
}

# EKS
variable "eks_cluster_name" {
  type    = string
  default = "demo-eks-cluster"
}

variable "eks_min_nodes" {
  type    = number
  default = 1
}

variable "eks_max_nodes" {
  type    = number
  default = 2
}
