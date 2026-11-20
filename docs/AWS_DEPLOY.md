# AWS deployment runbook

This deployment keeps the app and model on a private EC2 instance. The browser is reached
through an SSM port-forward; the app listens only on the instance loopback address. The
`aws` profile routes every AI task to the private `vllm_private` provider and rejects a
profile whose task routes contain a provider not marked `private: true`.

## Before provisioning

- Obtain written Skan/client authorization for the specific data and retention period.
- Use an EC2 **g6e.2xlarge** instance with an NVIDIA L40S 48 GB GPU in a private subnet.
- Attach a 200 GB encrypted EBS volume and mount it at `/data`.
- Attach an instance role scoped to one S3 bucket and its prefix. It needs only the bucket
  listing/head operations and object read/write operations for that prefix; omit delete.
- Provide S3 through a VPC gateway endpoint or an approved private route. No inbound security
  group rule is required for the app.
- Install Docker Engine / Compose and the NVIDIA container toolkit. Confirm `docker run --rm
  --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi` sees the L40S.

## Start the app

The repository directory should be available on the instance, with the EBS volume mounted at
`/data`. Set the model and backup bucket in the environment for Compose (these are names, not
credentials; boto3 obtains AWS credentials from the instance role):

```bash
export PM_PROFILE=aws
export VLLM_MODEL=Qwen/Qwen3.5-27B
export S3_BUCKET=your-approved-bucket
export S3_PREFIX=process-mining
docker compose -f docker/compose.aws.yaml up -d --build
docker compose -f docker/compose.aws.yaml ps
docker compose -f docker/compose.aws.yaml exec app python scripts/health.py
docker compose -f docker/compose.aws.yaml exec app python scripts/health.py --check-ai
```

The model container uses the NVIDIA runtime and is reachable only on the Compose network. The
app connects through `vllm.internal`, which the endpoint privacy check recognizes as an
internal host. There are no model or app public ports. The app's host binding is loopback only.
To open a temporary tunnel from an authorized workstation:

```bash
aws ssm start-session --target i-INSTANCE_ID --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["8501"],"localPortNumber":["8501"]}'
```

Then open `http://localhost:8501`. End the SSM session when finished.

## S3 backups and restore

Set `storage.s3_backup: true` in `config/settings.yaml` to back up the data folder once an
analysis reaches its completed state. Raw uploads are excluded by default. To include them,
also set `storage.s3_include_raw: true`; client authorization and retention policy still apply.
The IAM role supplies credentials automatically; do not put access keys in `.env`, Compose, or
the source tree.

Manual copy and restore are available for migrations:

```bash
docker compose -f docker/compose.aws.yaml exec app python scripts/s3_sync.py push \
  --bucket "$S3_BUCKET" --prefix "$S3_PREFIX"
docker compose -f docker/compose.aws.yaml exec app python scripts/s3_sync.py pull \
  --bucket "$S3_BUCKET" --prefix "$S3_PREFIX"
```

The sync includes jobs, process libraries and shared mappings. Raw uploads require the explicit
`--include-raw` option.

## Start/stop schedule and cost controls

Use EventBridge Scheduler or AWS Instance Scheduler for the proposed four-hours-per-day window.
Stopping the instance stops compute charges; encrypted EBS and S3 storage continue to incur
storage charges. Set the schedule timezone and holidays with the data owner, and confirm the
instance has stopped before the inactive period.

## Data and AI boundaries

- Keep raw client files and job outputs on encrypted EBS and the authorized S3 bucket only.
- The AWS settings profile sets `ai.allow_external_for_client: false`, clears custom
  `LLM_*` endpoints, and routes every task only to `vllm_private`.
- The profile startup check prints the active profile, data folder, and private status of every
  routed provider. A non-private route prevents startup.
- Do not expose Streamlit or vLLM through a public security group or load balancer. Access uses
  SSM Session Manager, with IAM logging and MFA controls set by the account owner.
- Remove any test or restore copy according to the signed retention schedule using the
  organization's approved deletion process.
