## Cloud Provider IP Handling

When choosing IPs for the run-file, the ticket may have two IP fields:
`ssh_hardware_ips` (for SSH access — may be public/NAT'd) and
`assigned_hardware_ips` (for benchmark traffic — typically private/direct).
Always use `assigned_hardware_ips` for run-file host entries and benchmark
parameters like `remotehost`. These are the IPs where benchmark traffic
flows — they need direct connectivity without firewalls blocking benchmark
ports. Public/cloud IPs often have security groups or firewalls that only
allow SSH (port 22), which will cause benchmark connection failures.
If only one IP field is populated, use it but be aware that if it contains
public IPs, benchmark traffic may be blocked. Use IPs, never hostnames.
