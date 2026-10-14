# GCP VM access and lifecycle

Source: Slack setup instructions pasted by the user on October 14, 2026.
This document preserves those instructions and adds transfer and shutdown guidance.
Read with [plan.md](plan.md) for approvals and [validation.md](validation.md) for
the experiment procedure. Recording these commands does not confirm that the VM
exists or authorize provisioning, paid calls, or lifting the execution hold.

**Standing instruction: stop the VM when it is no longer being used.** After an
authorized work session completes or fails, preserve results, logs, and the budget
ledger; check for other active work; stop the VM and verify `TERMINATED`.
Closing SSH or detaching tmux leaves the VM running and accruing compute charges.
If other work still needs the machine, resolve that before stopping it.

## Project details supplied in Slack

| Setting | Value |
| --- | --- |
| GCP project | `dixit-c11-vm` |
| Sign-in account | `jc3683@cornell.edu` |
| Region / zone | `us-central1` / `us-central1-a` |
| Example VM name | `agent-swarm-1` |
| Initial allocation | $1,000; this is not evidence of an enforced spending cutoff |
| Example machine | `n2-standard-64`, sized for the 100-agent estimate |
| OS | Ubuntu 24.04 LTS, amd64 |
| Disk | 300 GB persistent SSD |
| Runtime service account | `vm-runtime-sa@dixit-c11-vm.iam.gserviceaccount.com` |
| Network | Private VM; IAP SSH; outbound internet through Cloud NAT per Slack |

Slack permits a smaller machine for the 20-agent pilot. The example machine does
not change the approved experiment population. Infrastructure allocation is separate
from model spending limits recorded in the plan.

## Authenticate and inspect from the local computer

Sign in with the Cornell account when prompted:

```bash
gcloud auth login
gcloud config set project dixit-c11-vm
gcloud config set compute/region us-central1
gcloud config set compute/zone us-central1-a
```

Check the actual instance before deciding whether to start or create one:

```bash
gcloud compute instances describe agent-swarm-1 \
  --project=dixit-c11-vm \
  --zone=us-central1-a \
  --format='value(status)'
```

When an existing stopped VM is authorized for use:

```bash
gcloud compute instances start agent-swarm-1 \
  --project=dixit-c11-vm \
  --zone=us-central1-a
```

## Create only when authorized and needed

The supplied 100-agent example, with an explicit project flag added:

```bash
gcloud compute instances create agent-swarm-1 \
  --project=dixit-c11-vm \
  --zone=us-central1-a \
  --machine-type=n2-standard-64 \
  --image-family=ubuntu-2404-lts-amd64 \
  --image-project=ubuntu-os-cloud \
  --boot-disk-type=pd-ssd \
  --boot-disk-size=300GB \
  --service-account=vm-runtime-sa@dixit-c11-vm.iam.gserviceaccount.com \
  --scopes=logging-write,monitoring-write \
  --tags=iap-ssh \
  --no-address
```

Keep `--no-address` as the default. Slack says Cloud NAT allows the host to reach
OpenRouter, GitHub, Docker Hub, and package repositories. If a public IP is
explicitly needed, Slack permits omitting that flag; SSH still uses IAP.

## Connect and keep the run alive

From the local computer:

```bash
gcloud compute ssh agent-swarm-1 \
  --project=dixit-c11-vm \
  --zone=us-central1-a \
  --tunnel-through-iap
```

Local Codex can operate the remote machine through SSH commands. Installing Codex
on the VM is optional. The experiment controller and containers execute on the VM;
model inference requests go to the configured provider. SSH does not automatically
transfer the local repository, dependencies, API keys, or Codex conversation.

For a long run, use a persistent remote session such as tmux (install if needed):

```bash
tmux new -s gdm-pilot
# Later, reconnect to the existing session:
tmux attach -t gdm-pilot
```

Detach with Ctrl-B then D. Detaching preserves the session; it does not stop the
experiment or the VM. Use the validation runbook for actual experiment commands.

## Transfer code and configure the VM

Follow validation runbook step 0.2 for the reviewed transfer procedure and exclusions.
Options include rsync for incremental copying, or SCP of a sanitized staging directory:

```bash
gcloud compute scp --recurse SANITIZED_DIRECTORY agent-swarm-1:~/ \
  --project=dixit-c11-vm \
  --zone=us-central1-a \
  --tunnel-through-iap
```

`SANITIZED_DIRECTORY` is a placeholder, not the raw local checkout. Exclude real
`.env` files, credentials, local environments, caches, and private run data. A blank
`.env.example` can be included. For an initial deployment, omit local runs and state;
if prior spending exists, deliberately preserve and migrate the authoritative budget
ledger instead of resetting or overwriting it.

Git is another option after committing the intended code and obtaining authorization
to publish to a remote repository: clone it on the VM, then pull later commits.
Git does not synchronize uncommitted files, ignored files, or credentials. At the time
of recording these instructions, this repository had no commits or verified remote.
Recheck before choosing this route. Saving these instructions does not authorize publication.

Install dependencies and build Linux images on the VM according to the validation
runbook. A local macOS virtual environment or Docker image is not a substitute for
the VM build and validation.

Slack's newer-kernel commands run **on the VM**:

```bash
sudo apt-get update
sudo apt-get install --install-recommends -y linux-generic-hwe-24.04
sudo reboot
```

Reconnect afterward and verify the actual kernel and required isolation features
using the runbook. The host has outbound internet; experimental agent containers
must still have Docker networking disabled as planned.

The user supplies provider credentials privately in the VM host's ignored `.env`,
with restrictive permissions such as `chmod 600 .env`. Agents must not read, print,
copy, commit, or mount real credential values. Only the host provider bridge receives
provider keys; experimental containers never receive them or the host `.env`.
GCP sign-in does not supply an OpenRouter API key. An optional Codex installation
on the VM would also need its own authentication.

## End each work session

Preserve outputs, logs, and `state/budget.sqlite`, including unresolved billing
reservations. Check that no unrelated active job needs the VM. Then, from the local
computer, stop it and verify its state:

```bash
gcloud compute instances stop agent-swarm-1 \
  --project=dixit-c11-vm \
  --zone=us-central1-a

gcloud compute instances describe agent-swarm-1 \
  --project=dixit-c11-vm \
  --zone=us-central1-a \
  --format='value(status)'
```

Confirm `TERMINATED`; if stopping fails, report that it may still be running.
The user's standing shutdown instruction covers stopping after completed authorized
work; another confirmation is unnecessary when no other work needs the machine.
Stopping preserves the persistent disk. Disk and applicable networking charges can
continue; do not delete disks, results, or the budget ledger without authorization.
Report SSH, permission, or capacity problems to the user so they can contact the
project administrator.

Command references: [IAP SSH](https://docs.cloud.google.com/iap/docs/using-tcp-forwarding),
[gcloud SCP](https://docs.cloud.google.com/sdk/gcloud/reference/compute/scp), and
[stop/start behavior](https://docs.cloud.google.com/compute/docs/instances/stop-start-instance).
