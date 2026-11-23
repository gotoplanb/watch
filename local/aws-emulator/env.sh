# Point the AWS CLI / boto3 in THIS shell at the local AWS Emulator (ADR-067):
#   source local/aws-emulator/env.sh
# Real AWS credentials are unset, so nothing in this shell can reach a real account.
_port="${EMULATOR_PORT:-9297}"
_dir="$HOME/.aws/emulator/$_port"
export AWS_ENDPOINT_URL="https://127.0.0.1:$_port"
export AWS_SHARED_CREDENTIALS_FILE="$_dir/emulator_credentials"
export AWS_CONFIG_FILE="$_dir/config"
export AWS_CA_BUNDLE="$_dir/ca.pem"
export AWS_EC2_METADATA_DISABLED=true
export AWS_DISABLE_HOST_PREFIX_INJECTION=true
export AWS_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_PROFILE _port _dir
