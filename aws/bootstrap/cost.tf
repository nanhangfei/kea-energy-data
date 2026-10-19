# Two guards against a forgotten `make aws-down` (deployment-plan.md §5).

resource "aws_sns_topic" "alerts" {
  name = "kea-alerts"
}

# Confirm the subscription from the email AWS sends; until then nothing arrives.
resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# 1 THE BUDGET, counted BEFORE credits. With credits included the net bill is
#   zero and no alert ever fires; this one says what the sessions cost.
resource "aws_budgets_budget" "monthly" {
  name         = "kea-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_types {
    include_credit = false
    include_refund = false
  }

  dynamic "notification" {
    for_each = [
      { pct = 50, type = "ACTUAL" },
      { pct = 80, type = "ACTUAL" },
      { pct = 100, type = "FORECASTED" },
    ]
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value.pct
      threshold_type             = "PERCENTAGE"
      notification_type          = notification.value.type
      subscriber_email_addresses = [var.alert_email]
    }
  }
}

# 2 THE HOURLY REMINDER. It names what has run too long and deletes nothing:
#   tearing a session down is the operator's call.
data "archive_file" "guard" {
  type        = "zip"
  source_file = "${path.module}/session_guard.py"
  output_path = "${path.module}/.build/session_guard.zip"
}

resource "aws_iam_role" "guard" {
  name = "kea-session-guard"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "guard" {
  role = aws_iam_role.guard.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ec2:DescribeNatGateways",
          "rds:DescribeDBInstances",
          "airflow:ListEnvironments",
          "airflow:GetEnvironment",
        ]
        Resource = "*"
      },
      { Effect = "Allow", Action = "sns:Publish", Resource = aws_sns_topic.alerts.arn },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.region}:${var.account_id}:*"
      },
    ]
  })
}

resource "aws_lambda_function" "guard" {
  function_name    = "kea-session-guard"
  role             = aws_iam_role.guard.arn
  runtime          = "python3.13"
  architectures    = ["arm64"]
  handler          = "session_guard.handler"
  filename         = data.archive_file.guard.output_path
  source_code_hash = data.archive_file.guard.output_base64sha256
  timeout          = 30
  environment {
    variables = {
      TOPIC_ARN = aws_sns_topic.alerts.arn
      MAX_HOURS = tostring(var.session_max_hours)
    }
  }
}

resource "aws_cloudwatch_log_group" "guard" {
  name              = "/aws/lambda/${aws_lambda_function.guard.function_name}"
  retention_in_days = 14
}

resource "aws_iam_role" "scheduler" {
  name = "kea-session-guard-scheduler"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "scheduler" {
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "lambda:InvokeFunction", Resource = aws_lambda_function.guard.arn }]
  })
}

resource "aws_scheduler_schedule" "guard" {
  name                = "kea-session-guard"
  schedule_expression = "rate(1 hour)"
  flexible_time_window { mode = "OFF" }
  target {
    arn      = aws_lambda_function.guard.arn
    role_arn = aws_iam_role.scheduler.arn
  }
}
