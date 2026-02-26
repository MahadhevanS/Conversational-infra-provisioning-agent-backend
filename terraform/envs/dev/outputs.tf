########################################
# NETWORK
########################################

output "vpc_id" {
  value       = module.network.vpc_id
  description = "VPC ID"
}

output "subnet_ids" {
  value       = module.network.subnet_ids
  description = "Subnet IDs"
}

########################################
# EC2 (if enabled)
########################################

output "ec2_instance_id" {
  value       = var.enable_ec2 ? module.ec2[0].instance_id : null
  description = "EC2 Instance ID"
}

output "ec2_public_ip" {
  value       = var.enable_ec2 ? module.ec2[0].public_ip : null
  description = "EC2 Public IP"
}

########################################
# RDS (if enabled)
########################################

output "rds_endpoint" {
  value       = var.enable_rds ? module.rds[0].endpoint : null
  description = "RDS Endpoint"
}

output "rds_port" {
  value       = var.enable_rds ? module.rds[0].port : null
  description = "RDS Port"
}

########################################
# S3 (if enabled)
########################################

output "s3_bucket_name" {
  value       = var.enable_s3 ? module.s3[0].bucket_name : null
  description = "S3 Bucket Name"
}

output "s3_bucket_domain_name" {
  value = var.enable_s3 ? module.s3[0].bucket_domain_name : null
}

########################################
# EKS (if enabled)
########################################

output "eks_cluster_name" {
  value       = var.enable_eks ? module.eks[0].cluster_name : null
  description = "EKS Cluster Name"
}

output "eks_endpoint" {
  value       = var.enable_eks ? module.eks[0].cluster_endpoint : null
  description = "EKS Endpoint"
}