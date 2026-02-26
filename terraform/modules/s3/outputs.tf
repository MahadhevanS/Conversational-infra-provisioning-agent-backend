########################################
# S3 Outputs
########################################

output "bucket_name" {
  description = "S3 Bucket Name"
  value       = aws_s3_bucket.this.bucket
}

output "bucket_arn" {
  description = "S3 Bucket ARN"
  value       = aws_s3_bucket.this.arn
}

output "bucket_region" {
  description = "S3 Bucket Region"
  value       = aws_s3_bucket.this.region
}

output "bucket_domain_name" {
  description = "S3 Bucket Domain Name"
  value       = aws_s3_bucket.this.bucket_domain_name
}