# aws/bootstrap -- once per account, never destroyed by `make aws-down`.
#
# What a session needs to find already there (deployment-plan.md §4): the
# Terraform state bucket for session/, the lake, the MWAA bucket, the image
# repositories, the budget and the forgotten-session reminder. Everything that
# bills by the hour lives in session/ instead, so destroying a session can never
# take the lake with it.
#
# THIS STATE IS LOCAL (aws/bootstrap/terraform.tfstate, gitignored): the bucket
# it would live in is created here. Copy it to the state bucket after an apply
# (`make aws-bootstrap` does).

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 6.0" }
    archive = { source = "hashicorp/archive", version = "~> 2.7" }
  }
}

provider "aws" {
  region  = var.region
  profile = var.profile

  # THE WRONG-ACCOUNT GUARD, in the provider itself: Terraform refuses to plan
  # against any other account, the university one behind `kea-energy` included.
  allowed_account_ids = [var.account_id]

  default_tags {
    tags = { project = "kea", stack = "bootstrap" }
  }
}

locals {
  # Bucket names are global; the account id makes them this account's.
  prefix = "kea-${var.account_id}"
}
