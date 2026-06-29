## Bootstrap Root SSH Access (Cloud Instances)

If the ticket's ssh_user is NOT root (e.g., ec2-user, ubuntu, cloud-user),
you MUST establish root SSH access before doing anything else. Crucible and
most benchmark harnesses require root. There are TWO requirements:

**Part A: Enable root login on every host (controller + all targets).**
Use execute_command on EACH host directly (do NOT SSH hop through the
controller — execute_command connects to each host independently):
```
sudo sed -i 's/.*PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config
sudo mkdir -p /root/.ssh
sudo cp ~/.ssh/authorized_keys /root/.ssh/authorized_keys
sudo chmod 700 /root/.ssh
sudo chmod 600 /root/.ssh/authorized_keys
sudo systemctl restart sshd
```

**Part B: Set up controller-to-endpoint passwordless root SSH.**
Crucible SSHes FROM the controller TO each endpoint as root to deploy
containers and collect data. The controller's root user needs a key
pair, and its public key must be in each endpoint's authorized_keys.
Run on the controller (as root, after Part A):
```
test -f /root/.ssh/id_rsa || ssh-keygen -t rsa -b 4096 -f /root/.ssh/id_rsa -N ""
```
Then for EACH endpoint, copy the controller's public key. Run on the
controller:
```
ssh-copy-id -o StrictHostKeyChecking=no root@<endpoint-private-ip>
```
Or if ssh-copy-id is not available:
```
cat /root/.ssh/id_rsa.pub | ssh -o StrictHostKeyChecking=no root@<endpoint-private-ip> "cat >> /root/.ssh/authorized_keys"
```

**Part C: Verify.** From the controller, confirm passwordless root
SSH to each endpoint works:
```
ssh -o StrictHostKeyChecking=no root@<endpoint-private-ip> hostname
```

Once root access is established, use root for ALL subsequent operations.
When you submit your result, include ssh_user: "root" so downstream
agents use root.
Do NOT install harnesses or run commands as a non-root cloud user —
crucible requires root for container management and cgroup access.
