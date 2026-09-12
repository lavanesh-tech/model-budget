variable "aws_region" {
  description = "AWS region for ModelBudget."
  type        = string
  default     = "us-east-1"
}

variable "aws_profile" {
  description = "Local AWS CLI profile used by Terraform."
  type        = string
  default     = "modelbudget"
}

variable "instance_type" {
  description = "Small demo host; stop it when no recruiter demo is active."
  type        = string
  default     = "t3.small"
}

variable "repository_url" {
  description = "Public Git repository cloned by the EC2 host."
  type        = string
  default     = "https://github.com/lavanesh-tech/model-budget.git"
}

variable "repository_ref" {
  description = "Git branch deployed on the host."
  type        = string
  default     = "main"
}
