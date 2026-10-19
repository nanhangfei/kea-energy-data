# Three buckets, each private and encrypted, each with one job.

resource "aws_s3_bucket" "state" {
  bucket = "${local.prefix}-tfstate"
}

# The lake: bronze/, bronze/parsed/, export/ -- KEA_LAKE_BUCKET on AWS.
resource "aws_s3_bucket" "lake" {
  bucket = "${local.prefix}-lake"
}

# MWAA reads dags/, requirements.txt and plugins from here, and requires
# versioning on it.
resource "aws_s3_bucket" "mwaa" {
  bucket = "${local.prefix}-mwaa"
}

locals {
  buckets   = { state = aws_s3_bucket.state.id, lake = aws_s3_bucket.lake.id, mwaa = aws_s3_bucket.mwaa.id }
  versioned = { state = aws_s3_bucket.state.id, mwaa = aws_s3_bucket.mwaa.id }
}

resource "aws_s3_bucket_public_access_block" "all" {
  for_each                = local.buckets
  bucket                  = each.value
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "all" {
  for_each = local.buckets
  bucket   = each.value
  rule { object_ownership = "BucketOwnerEnforced" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "all" {
  for_each = local.buckets
  bucket   = each.value
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

# The lake is NOT versioned: bronze is immutable by convention and a re-run
# rewrites a partition on purpose (FORCE=1); versions would only bill for it.
resource "aws_s3_bucket_versioning" "versioned" {
  for_each = local.versioned
  bucket   = each.value
  versioning_configuration { status = "Enabled" }
}

# Spark's event logs and scratch space are not evidence; a week is plenty.
resource "aws_s3_bucket_lifecycle_configuration" "lake" {
  bucket = aws_s3_bucket.lake.id
  rule {
    id     = "spark-events"
    status = "Enabled"
    filter { prefix = "_spark/" }
    expiration { days = 7 }
  }
  rule {
    id     = "tmp"
    status = "Enabled"
    filter { prefix = "tmp/" }
    expiration { days = 7 }
  }
  rule {
    id     = "abort-multipart"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload { days_after_initiation = 1 }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "versioned" {
  for_each = local.versioned
  bucket   = each.value
  rule {
    id     = "old-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration { noncurrent_days = 30 }
  }
}

# The three images a session runs (deployment-plan.md §3.2), tagged git-<sha>.
resource "aws_ecr_repository" "image" {
  for_each             = toset(["dbt", "soda", "spark"])
  name                 = "kea/${each.key}"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
}

resource "aws_ecr_lifecycle_policy" "image" {
  for_each   = aws_ecr_repository.image
  repository = each.value.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep the last five releases"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 5 }
      action       = { type = "expire" }
    }]
  })
}
