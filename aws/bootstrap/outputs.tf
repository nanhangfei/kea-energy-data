output "state_bucket" { value = aws_s3_bucket.state.id }
output "lake_bucket" { value = aws_s3_bucket.lake.id }
output "mwaa_bucket" { value = aws_s3_bucket.mwaa.id }
output "alerts_topic_arn" { value = aws_sns_topic.alerts.arn }
output "ecr_repositories" { value = { for k, r in aws_ecr_repository.image : k => r.repository_url } }
