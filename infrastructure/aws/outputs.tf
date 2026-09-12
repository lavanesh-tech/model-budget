output "application_url" {
  description = "Public HTTPS URL supplied by CloudFront."
  value       = "https://${aws_cloudfront_distribution.app.domain_name}"
}

output "cloudfront_domain" {
  value = aws_cloudfront_distribution.app.domain_name
}

output "instance_id" {
  value = aws_instance.app.id
}

output "instance_state" {
  value = aws_instance.app.instance_state
}
