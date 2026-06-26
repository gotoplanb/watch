# Root Terragrunt config — remote state + provider generation, inherited by each
# stack via `include "root"`. (ADR-006)

remote_state {
  backend = "s3"
  generate = {
    path      = "backend.tf"
    if_exists = "overwrite_terragrunt"
  }
  config = {
    bucket         = "watch-tfstate-${get_aws_account_id()}"
    key            = "${path_relative_to_include()}/terraform.tfstate"
    region         = "us-east-1"
    encrypt        = true
    dynamodb_table = "watch-tflocks"
  }
}

generate "provider" {
  path      = "provider.tf"
  if_exists = "overwrite_terragrunt"
  contents  = <<EOF
provider "aws" {
  region = "us-east-1"
  default_tags { tags = { project = "watch", managed_by = "terragrunt" } }
}
EOF
}

inputs = {
  project = "watch"
}
