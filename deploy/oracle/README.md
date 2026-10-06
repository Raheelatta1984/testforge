# Oracle Cloud deployment

`provision.sh` creates the base OCI infrastructure for TestForge from Oracle Cloud Shell:

- Always Free `VM.Standard.A1.Flex` instance (default: 1 OCPU and 6 GB RAM)
- Ubuntu 24.04 ARM image
- 100 GB boot volume
- VCN, internet gateway, public subnet, and route table
- ingress for SSH (22), HTTP (80), and HTTPS (443)
- FIPS-compatible RSA SSH key stored in the user's private Cloud Shell home directory

## Run from OCI Cloud Shell

```bash
git clone --depth 1 --branch arena/0fd55ef4-testforge \
  https://github.com/Raheelatta1984/testforge.git
cd testforge
bash deploy/oracle/provision.sh
```

The script obtains the tenancy, region, and home-region information from the authenticated OCI Cloud Shell configuration. It refuses resource settings above the current Always Free A1 allowance of 2 OCPUs and 12 GB RAM.

The script is idempotent by display name and reuses infrastructure it previously created. Its default names all begin with `testforge-`.

If Sydney has no A1 host capacity, the script retries every 10 minutes until Oracle accepts the launch request. HTTP 429 rate limits are retried every two minutes. Keep the Cloud Shell session active while it waits. Set `TF_OCI_MAX_LAUNCH_ATTEMPTS` to a nonzero number to impose a retry limit.

## Optional sizing

Set these variables before running the script. Do not exceed the allowances shown in the OCI console.

```bash
export TF_OCI_OCPUS=2
export TF_OCI_MEMORY_GB=12
export TF_OCI_BOOT_GB=100
bash deploy/oracle/provision.sh
```

## Important

This script provisions infrastructure. Application installation is a separate step because TestForge first needs an ARM-compatible production container and fixes for the current recording/execution inconsistencies.
