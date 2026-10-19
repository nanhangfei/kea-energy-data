#!/usr/bin/env bash
# Refuse to touch AWS unless the profile resolves to the personal account.
#
#   aws/bin/check-account.sh          # exits 0 and prints the account, or exits 1
#
# Every aws-* target runs this first (deployment-plan.md §5). The university
# account behind the `kea-energy` profile is the one this exists to keep out:
# it cannot create IAM roles and its budget is shared.
set -euo pipefail

profile="${AWS_PROFILE:-kea-personal}"
expected="${KEA_AWS_ACCOUNT_ID:-}"

if [ -z "$expected" ]; then
  echo "KEA_AWS_ACCOUNT_ID is not set in .env -- the personal account's 12-digit id" >&2
  exit 1
fi

if ! actual=$(aws sts get-caller-identity --profile "$profile" --query Account --output text 2>/dev/null); then
  echo "profile '$profile' has no valid session -- run: aws login --profile $profile" >&2
  exit 1
fi

if [ "$actual" != "$expected" ]; then
  echo "profile '$profile' is account $actual, not KEA_AWS_ACCOUNT_ID $expected -- refusing" >&2
  exit 1
fi

echo "AWS account $actual via profile '$profile'"
