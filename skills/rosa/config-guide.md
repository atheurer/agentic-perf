# ROSA (Red Hat OpenShift Service on AWS)

> **⚠️ UNTESTED CONTRIBUTION**: This skill has not been tested in production. It is contributed as-is for community evaluation and testing. Use at your own risk.

## Overview

Automates deployment of ROSA (Red Hat OpenShift Service on AWS) HCP (Hosted Control Plane) clusters with kube-burner-ocp pre-installed for performance testing.

### What This Skill Does

1. **Provisions AWS Infrastructure**
   - VPC with public/private subnets across 3 availability zones
   - Optional bastion EC2 instance for cluster access

2. **Deploys ROSA Cluster**
   - Hosted Control Plane (HCP) architecture
   - Configurable worker node count and instance types
   - Automated OIDC and IAM role setup

3. **Installs kube-burner-ocp**
   - Pre-configured on bastion instance
   - Ready-to-run performance tests
   - Wrapper scripts for common workloads

## Prerequisites

### Required Accounts

- **AWS Account** with sufficient permissions and quotas
- **Red Hat Account** with ROSA entitlement/subscription
- **ROSA Token** from https://console.redhat.com/openshift/token/rosa

### Required CLI Tools

The skill auto-verifies these tools but they must be installed:

- `rosa` CLI (1.2.0+)
- `aws` CLI (2.0.0+)
- `oc` CLI (4.12.0+)
- `jq` (1.5+)

#### Quick Dependency Check

```bash
# Verify all tools installed
rosa version && aws --version && oc version --client && jq --version

# Verify credentials configured
aws sts get-caller-identity && rosa whoami
```

#### Installation

**ROSA CLI (1.2.0+)**

```bash
# Linux
curl -LO https://mirror.openshift.com/pub/openshift-v4/clients/rosa/latest/rosa-linux.tar.gz
tar xzf rosa-linux.tar.gz
sudo mv rosa /usr/local/bin/rosa
sudo chmod +x /usr/local/bin/rosa

# Verify
rosa version
```

**AWS CLI v2 (2.0.0+)**

```bash
# Linux
curl 'https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip' -o 'awscliv2.zip'
unzip awscliv2.zip
sudo ./aws/install

# Verify
aws --version
```

**OpenShift CLI (4.12.0+)**

```bash
# Linux
curl -LO https://mirror.openshift.com/pub/openshift-v4/clients/ocp/stable/openshift-client-linux.tar.gz
tar xzf openshift-client-linux.tar.gz
sudo mv oc kubectl /usr/local/bin/
sudo chmod +x /usr/local/bin/oc /usr/local/bin/kubectl

# Verify
oc version --client
```

**jq (1.5+)**

```bash
# Debian/Ubuntu
sudo apt-get install -y jq

# RHEL/CentOS/Fedora
sudo yum install -y jq

# Verify
jq --version
```

#### All-in-One Installation Script

```bash
#!/bin/bash
set -e

echo "Installing ROSA deployment dependencies..."

# System packages
sudo yum install -y curl tar unzip jq || \
sudo apt-get update && sudo apt-get install -y curl tar unzip jq

# ROSA CLI
curl -LO https://mirror.openshift.com/pub/openshift-v4/clients/rosa/latest/rosa-linux.tar.gz
tar xzf rosa-linux.tar.gz
sudo mv rosa /usr/local/bin/rosa
sudo chmod +x /usr/local/bin/rosa
rm rosa-linux.tar.gz

# AWS CLI
curl 'https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip' -o 'awscliv2.zip'
unzip -q awscliv2.zip
sudo ./aws/install
rm -rf aws awscliv2.zip

# OpenShift CLI
curl -LO https://mirror.openshift.com/pub/openshift-v4/clients/ocp/stable/openshift-client-linux.tar.gz
tar xzf openshift-client-linux.tar.gz
sudo mv oc kubectl /usr/local/bin/
sudo chmod +x /usr/local/bin/oc /usr/local/bin/kubectl
rm openshift-client-linux.tar.gz

echo "✅ Installation complete!"
echo ""
echo "Next steps:"
echo "1. Configure AWS: aws configure"
echo "2. Login to ROSA: rosa login --token='YOUR_TOKEN'"
echo "3. Verify: rosa whoami && aws sts get-caller-identity"
```

### Required Credentials

#### AWS IAM Credentials

**Create IAM User**:
1. Log in to AWS Console: https://console.aws.amazon.com
2. Go to IAM → Users → Create user
3. Attach policy: `AdministratorAccess` (or ROSA-specific policy)
4. Create access key
5. Save Access Key ID and Secret Access Key

**Configure**:
```bash
# Method 1: Interactive
aws configure
# Enter: Access Key ID, Secret Key, Region (us-west-2), Output format (json)

# Method 2: Environment variables
export AWS_ACCESS_KEY_ID='YOUR_ACCESS_KEY'
export AWS_SECRET_ACCESS_KEY='YOUR_SECRET_KEY'
export AWS_DEFAULT_REGION='us-west-2'

# Verify
aws sts get-caller-identity
```

#### Red Hat OpenShift Token

**Get Token**:
1. Visit: https://console.redhat.com/openshift/token/rosa
2. Log in with Red Hat account
3. Click "Load token"
4. Copy the **Offline token** (long string starting with `eyJ...`)

**Configure**:
```bash
rosa login --token='YOUR_OFFLINE_TOKEN'

# Verify
rosa whoami
```

### Required Account Entitlements

#### ROSA Entitlement

**Check if you have it**:
```bash
rosa whoami
# If successful → You have entitlement ✅
# If "not entitled" → Need to request access ❌
```

**How to get**:
- **Free Trial**: Contact Red Hat sales (https://www.redhat.com/en/technologies/cloud-computing/openshift/aws)
- **Paid Subscription**: Contact Red Hat sales
- **AWS Marketplace**: Subscribe via AWS Marketplace (pay-as-you-go)
- **Red Hat Employees**: Check internal access procedures

### AWS Service Quotas

#### Check Current Quotas

```bash
# Check EC2 vCPU quota (most critical)
aws service-quotas get-service-quota \
  --service-code ec2 \
  --quota-code L-1216C47A \
  --region us-west-2 \
  --query 'Quota.Value'

# Run ROSA quota check
rosa verify quota --region us-west-2
```

#### Required Quotas

| Quota | Minimum for 24 Workers | Default | Action |
|-------|----------------------|---------|--------|
| EC2 vCPUs (Standard) | 96 vCPUs (24 × m6i.xlarge) | 64 | **Increase needed** |
| VPCs per Region | 1 | 5 | Usually OK |
| NAT Gateways per AZ | 1 per AZ (3 total) | 5 | Usually OK |
| Elastic IPs | 3+ | 5 | Usually OK |

#### Request Quota Increase

```bash
# Request 400 vCPUs (supports ~100 m6i.xlarge instances)
aws service-quotas request-service-quota-increase \
  --service-code ec2 \
  --quota-code L-1216C47A \
  --desired-value 400 \
  --region us-west-2
```

Or via AWS Console: https://console.aws.amazon.com/servicequotas/

**Processing time**: Usually 15-30 minutes, can take up to 24 hours

### Pre-Flight Verification

Run all verification commands:

```bash
#!/bin/bash

echo "=== CLI Tools ==="
rosa version || echo "❌ ROSA CLI missing"
aws --version || echo "❌ AWS CLI missing"
oc version --client || echo "❌ OpenShift CLI missing"
jq --version || echo "❌ jq missing"

echo ""
echo "=== AWS Credentials ==="
aws sts get-caller-identity || echo "❌ AWS not configured"

echo ""
echo "=== ROSA Authentication ==="
rosa whoami || echo "❌ ROSA not authenticated"

echo ""
echo "=== AWS Quotas ==="
rosa verify quota --region us-west-2
rosa verify permissions

echo ""
echo "✅ Pre-flight check complete"
```

## Usage with agentic-perf

```bash
# Copy skill to your private skills directory
cp sample-private-skills/rosa.json ~/.agentic-perf/private-skills/

# Deploy with natural language
agentic-perf submit "Deploy ROSA cluster with 24 workers for performance testing"

# Monitor progress
agentic-perf watch
# Or open: http://localhost:8090/
```

### Example Requests

```bash
# Minimal deployment (3 workers, testing)
agentic-perf submit "Deploy ROSA cluster with 3 workers in us-west-2 named test-cluster"

# Specific configuration
agentic-perf submit "Deploy ROSA cluster with these settings:
  - Name: perf-test
  - Region: us-east-1
  - Workers: 48
  - Instance type: m6i.2xlarge"

# Without bastion (use local machine)
agentic-perf submit "Deploy ROSA cluster without bastion instance"
```

## Deployment Timeline

**Total Duration**: ~35-45 minutes

| Stage | Duration | Description |
|-------|----------|-------------|
| Network Infrastructure | 5-10 min | VPC, subnets, route tables |
| Bastion Instance | 5 min | Optional EC2 for cluster access |
| ROSA Prerequisites | 2-5 min | OIDC config, IAM roles |
| ROSA Deployment | 30-40 min | Cluster creation and node startup |
| Bastion Configuration | 5 min | kubeconfig transfer, tool setup |
| kube-burner Setup | 2-3 min | Install and configure |
| Validation | 2 min | Health checks |

## Configuration Options

Default values (customizable via natural language):

| Parameter | Default | Description |
|-----------|---------|-------------|
| `region` | us-west-2 | AWS region |
| `worker_replicas` | 24 | Number of worker nodes |
| `worker_instance_type` | m6i.xlarge | EC2 instance type |
| `cluster_version` | 4.22-latest | OpenShift version |
| `install_bastion` | true | Create bastion instance |

## Testing kube-burner

After deployment, SSH to bastion:

```bash
# Access bastion
ssh ec2-user@<BASTION_PUBLIC_IP>
# Or: aws ssm start-session --region us-west-2 --target <INSTANCE_ID>

# Run performance tests
~/run-kube-burner.sh cluster-density --iterations=100
~/run-kube-burner.sh node-density --pods-per-node=245
~/run-kube-burner.sh networkpolicy --iterations=50
```

## Cleanup

```bash
# Automated cleanup
agentic-perf submit "Clean up cluster <cluster-name>"

# Manual cleanup
rosa delete cluster --cluster=<cluster-name> --yes
```

## Cost Awareness

**Estimated costs (us-west-2)**:
- 24-node cluster (m6i.xlarge): ~$6-7/hour
- ROSA control plane: ~$0.03/hour
- **Total for 1-hour test**: ~$7

**Cost-saving tips**:
- Delete clusters immediately after testing
- Use 3-node minimum for initial validation
- Set cluster expiration times

## Troubleshooting

### Deployment Issues

#### Cluster Creation Fails with Quota Error

**Error**: `Quota exceeded for resource type: EC2/vCPUs`

**Solution**:
```bash
# Check current quota
aws service-quotas get-service-quota \
  --service-code ec2 --quota-code L-1216C47A \
  --region us-west-2

# Request increase
aws service-quotas request-service-quota-increase \
  --service-code ec2 --quota-code L-1216C47A \
  --desired-value 400 --region us-west-2

# Wait 15-30 minutes for approval
```

#### "Not entitled to use ROSA"

**Error**: `Account is not entitled to use ROSA`

**Cause**: Your Red Hat account doesn't have ROSA subscription/entitlement

**Solutions**:
1. **Request Free Trial**: https://www.redhat.com/en/technologies/cloud-computing/openshift/aws
2. **Contact Red Hat Sales**: sales@redhat.com
3. **AWS Marketplace**: Subscribe via marketplace (pay-as-you-go)
4. **Red Hat Employees**: Check internal access procedures

#### OIDC Configuration Fails

**Error**: `Failed to create OIDC configuration`

**Solutions**:
```bash
# List existing OIDC configs
rosa list oidc-config

# If stuck, delete and recreate
rosa delete oidc-config --oidc-config-id <ID> --mode auto

# Create new one
rosa create oidc-config --mode auto --yes
```

#### IAM Role Creation Fails

**Error**: `Access denied when creating IAM roles`

**Cause**: AWS user lacks IAM permissions

**Solution**:
1. Check IAM user permissions in AWS Console
2. Ensure user has `IAMFullAccess` or `AdministratorAccess`
3. Verify with:
```bash
aws iam get-user
aws iam list-attached-user-policies --user-name YOUR_USERNAME
```

### Cluster Access Issues

#### Cannot Access Cluster from Bastion

**Error**: `Unable to connect to the server`

**Solutions**:
```bash
# On bastion, check kubeconfig
cat ~/.kube/config

# Verify API URL is correct
rosa describe cluster --cluster=<cluster-name> | grep API

# Test API connectivity
curl -k <API_URL>

# Re-transfer kubeconfig if needed
# (From local machine)
oc config view --minify --flatten > /tmp/kubeconfig
base64 -w0 /tmp/kubeconfig
# Copy output to bastion ~/.kube/config (base64 decode)
```

#### SSH to Bastion Fails

**Error**: `Connection refused` or `Permission denied`

**Solutions**:
```bash
# Check instance is running
aws ec2 describe-instances --region us-west-2 \
  --filters "Name=tag:Name,Values=*bastion*"

# Check security group allows SSH from your IP
MY_IP=$(curl -s https://checkip.amazonaws.com)
echo "Your IP: $MY_IP"

# Use SSM instead of SSH (no key needed)
aws ssm start-session --region us-west-2 --target <INSTANCE_ID>
```

### kube-burner Issues

#### "kube-burner: command not found"

**Error**: Binary not found on bastion

**Solution**:
```bash
# On bastion, download kube-burner manually
mkdir -p ~/kube-burner-ocp/bin
curl -L https://github.com/kube-burner/kube-burner-ocp/releases/latest/download/kube-burner-ocp-linux-amd64 \
  -o ~/kube-burner-ocp/bin/kube-burner-ocp
chmod +x ~/kube-burner-ocp/bin/kube-burner-ocp

# Verify
~/kube-burner-ocp/bin/kube-burner-ocp version
```

#### kube-burner Fails with Permission Error

**Error**: `operation not permitted` or `capability not set`

**Solution**:
```bash
# Grant network capabilities
sudo setcap cap_net_raw,cap_net_admin=ep ~/kube-burner-ocp/bin/kube-burner-ocp

# Verify
getcap ~/kube-burner-ocp/bin/kube-burner-ocp
```

#### Cannot Connect to Cluster from kube-burner

**Error**: `Unable to connect to cluster`

**Solution**:
```bash
# Verify KUBECONFIG is set
echo $KUBECONFIG

# Test cluster access
oc whoami
oc get nodes

# If fails, check kubeconfig
export KUBECONFIG=~/.kube/config
oc whoami
```

### Agent/Ticket Issues

#### Agent Stuck in Stage

**Symptoms**: Deployment not progressing, agent repeating same action

**Debug**:
```bash
# Check agent transcript
agentic-perf transcript <ticket-id>

# Check recent logs
tail -100 ~/.agentic-perf/logs/orchestrator.log

# Cancel and restart if needed
agentic-perf cancel <ticket-id>
```

#### Ticket Fails with Validation Error

**Error**: `Validation failed: <reason>`

**Common Causes**:
1. **Invalid region**: Use `rosa list regions` to see available regions
2. **Invalid instance type**: Check AWS region supports the instance type
3. **Worker count not multiple of AZs**: Use multiples of 3 for even distribution

**Solution**: Review ticket parameters and resubmit

### AWS Infrastructure Issues

#### VPC Creation Fails

**Error**: `VPC limit exceeded` or `CIDR block overlaps`

**Solutions**:
```bash
# List existing VPCs
aws ec2 describe-vpcs --region us-west-2

# Check quota
aws service-quotas get-service-quota \
  --service-code vpc --quota-code L-F678F1CE --region us-west-2

# Delete unused VPCs
aws ec2 delete-vpc --region us-west-2 --vpc-id <vpc-id>
```

#### NAT Gateway Creation Fails

**Error**: `Elastic IP address limit exceeded`

**Solution**:
```bash
# List Elastic IPs
aws ec2 describe-addresses --region us-west-2

# Release unused IPs
aws ec2 release-address --region us-west-2 --allocation-id <eipalloc-id>

# Request quota increase
aws service-quotas request-service-quota-increase \
  --service-code ec2 --quota-code L-0263D0A3 \
  --desired-value 10 --region us-west-2
```

### Cost-Related Issues

#### Unexpected High Costs

**Cause**: Cluster left running, large instance types

**Prevention**:
```bash
# Set cluster expiration
ocm edit cluster --expiration 24h <cluster-id>

# Monitor cluster age
rosa list clusters

# Delete when done testing
rosa delete cluster --cluster=<cluster-name> --yes
```

#### Cluster Won't Delete

**Error**: `Cluster deletion failed` or stuck in "uninstalling"

**Solutions**:
```bash
# Force deletion
rosa delete cluster --cluster=<cluster-name> --yes --watch

# If still stuck after 30 minutes, check AWS Console:
# - EC2 instances manually terminated?
# - VPC dependencies removed?
# - Contact Red Hat support if persists
```

### Credential Issues

#### "Unable to locate credentials"

**Error**: AWS CLI cannot find credentials

**Solution**:
```bash
# Set credentials
aws configure

# Or environment variables
export AWS_ACCESS_KEY_ID='YOUR_KEY'
export AWS_SECRET_ACCESS_KEY='YOUR_SECRET'
export AWS_DEFAULT_REGION='us-west-2'

# Verify
aws sts get-caller-identity
```

#### ROSA Token Expired

**Error**: `Token is invalid or expired`

**Solution**:
```bash
# Get new token from: https://console.redhat.com/openshift/token/rosa
rosa login --token='NEW_TOKEN'

# Verify
rosa whoami
```

### Performance Test Issues

#### kube-burner Test Fails Immediately

**Error**: Test exits with error before running

**Debug**:
```bash
# Run with debug logging
~/run-kube-burner.sh cluster-density --iterations=1 --log-level=debug

# Check cluster resources
oc get nodes
oc get clusteroperators

# Ensure cluster is fully ready
rosa describe cluster --cluster=<cluster-name>
```

#### Test Runs But No Pods Created

**Cause**: Insufficient permissions or quota

**Solutions**:
```bash
# Check user permissions
oc whoami
oc auth can-i create pods

# Check cluster capacity
oc describe nodes | grep -A5 "Allocated resources"

# Check for pod errors
oc get events --all-namespaces --sort-by='.lastTimestamp'
```

### Getting Help

Since this skill is **UNTESTED**, please report issues with:

1. **Detailed error messages**: Full output from failed stage
2. **Environment details**:
   - AWS region
   - Worker count and instance type
   - ROSA version
3. **Reproduction steps**: Exact agentic-perf command used
4. **Logs**:
   - `agentic-perf transcript <ticket-id>`
   - Relevant AWS/ROSA CLI output

#### Useful Debug Commands

```bash
# Cluster state
rosa describe cluster --cluster=<cluster-name>
rosa list machinepools --cluster=<cluster-name>
oc get clusterversion
oc get clusteroperators
oc get nodes -o wide

# AWS resources
aws ec2 describe-vpcs --region us-west-2
aws ec2 describe-subnets --region us-west-2
aws ec2 describe-instances --region us-west-2

# Agent/ticket state
agentic-perf list
agentic-perf transcript <ticket-id>
cat ~/.agentic-perf/tickets/<ticket-id>.json
```

#### Emergency Cleanup

If deployment fails and leaves resources:

```bash
# Delete ROSA cluster
rosa delete cluster --cluster=<cluster-name> --yes

# Terminate EC2 instances
aws ec2 describe-instances --region us-west-2 \
  --filters "Name=tag:Name,Values=*<cluster-name>*"
aws ec2 terminate-instances --region us-west-2 --instance-ids <id>

# Delete VPC (after cluster is gone)
# NOTE: ROSA cleanup usually handles this
aws ec2 describe-vpcs --region us-west-2
# If VPC still exists after cluster deletion:
# Manually delete NAT gateways, Internet Gateways, subnets, then VPC
```

## Known Limitations

- ⚠️ **UNTESTED**: This skill has not been validated in production
- Requires ROSA entitlement (may need Red Hat sales contact)
- AWS quota increases often required for typical deployments
- Deployment can fail if quotas insufficient (check with `rosa verify quota`)

## Contributing

This is a community contribution. Testing reports, bug fixes, and improvements are welcome!

**Please report issues with**:
- Your test results (success/failure)
- Error messages
- AWS region tested
- Cluster size attempted

## References

- [ROSA Documentation](https://docs.openshift.com/rosa/)
- [kube-burner Documentation](https://kube-burner.github.io/kube-burner/)
- [AWS Service Quotas](https://docs.aws.amazon.com/servicequotas/)
- [Red Hat Support](https://access.redhat.com/support/)
- [agentic-perf Issues](https://github.com/atheurer/agentic-perf/issues)
