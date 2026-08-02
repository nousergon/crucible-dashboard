# Dashboard Box — Rebuild Runbook

**Instance:** `i-09b539c844515d549` · t3.small · us-east-1f · AL2023
**Root volume:** `vol-0b11efe28ad2f2073` (32 GB gp3, `DeleteOnTermination=false`)
**Governing policy:** [`nous-ergon-ops/policies/shared-application-host-policy.md`](https://github.com/nousergon/nous-ergon-ops/blob/main/policies/shared-application-host-policy.md) — **T0-1 (recoverability)**
**RTO target:** 4 hours · **RPO:** 24 hours (nightly snapshot)

**Last executed:** *never — see "Rehearsal" below. Until this runbook has been run end-to-end, its RTO is an estimate, not a measurement.*

---

## What backs this box up

| Layer | Mechanism | Retention |
|---|---|---|
| Whole-box | DLM policy `policy-0047cfbfb7070a3b7`, nightly 08:00 UTC, targets tag `Backup=daily` on the **instance** (multi-volume snapshot set) | 14 days |
| Accidental terminate | `DeleteOnTermination=false` on the root volume — the volume survives instance termination | — |
| Code | 17 git checkouts under `/home/ec2-user/`, all pushed to GitHub | authoritative upstream |
| Secrets | SSM SecureString under `/alpha-engine/`, `/symposion/`, `/metron/`, etc. | authoritative upstream |
| Data artifacts | S3 (`alpha-engine-research`), Neon (Metron), Mnemon vault sync | authoritative upstream |

**The snapshot is the only copy of:** the two Cloudflare Origin private keys (`/etc/ssl/certs/*-origin.pem` + keys), `~/.netrc` (GitHub deploy PAT), `flow_doctor.db`, the built venvs and `node_modules` trees, and any local config not yet in a repo.

---

## Path A — instance is gone, volume survives (most likely)

Because `DeleteOnTermination=false`, a terminated instance leaves `vol-0b11efe28ad2f2073` intact and `available`.

1. Launch a new t3.small in **us-east-1f**, AL2023, **no** additional root volume beyond the default.
2. Stop the new instance. Detach and delete its fresh root volume.
3. Attach `vol-0b11efe28ad2f2073` as `/dev/xvda`. Start the instance.
4. Jump to **Common post-restore steps**.

*This is the fastest path — the filesystem is byte-identical, nothing is lost.*

---

## Path B — volume is lost or corrupt (restore from snapshot)

1. Find the most recent snapshot:
   ```
   aws ec2 describe-snapshots --owner-ids self \
     --filters Name=tag:Purpose,Values=shared-application-host-policy-T0-1 \
     --query 'sort_by(Snapshots,&StartTime)[-1].[SnapshotId,StartTime,State]' --output text
   ```
2. Create a volume from it in **us-east-1f** (the AZ matters — a volume cannot attach across AZs):
   ```
   aws ec2 create-volume --snapshot-id <snap-id> --availability-zone us-east-1f \
     --volume-type gp3 --tag-specifications \
     'ResourceType=volume,Tags=[{Key=Name,Value=alpha-engine-dashboard-root},{Key=Backup,Value=daily}]'
   ```
3. Launch a new t3.small in us-east-1f, stop it, detach+delete its root volume, attach the restored volume as `/dev/xvda`, start.
4. Jump to **Common post-restore steps**.

**Data loss window:** everything written since the last nightly snapshot (up to 24h). For this box that is: `flow_doctor.db` rows, uncommitted local edits, and any log history. No product data — every product's authoritative store is off-box.

---

## Common post-restore steps

These are the parts a snapshot does **not** carry. Work through all of them.

### 1. Instance identity and tags
```
aws ec2 create-tags --resources <new-instance-id> \
  --tags Key=Name,Value=alpha-engine-dashboard Key=Backup,Value=daily
```
`Backup=daily` is what the DLM policy targets — **without it the new box is unbacked**, silently.

### 2. IAM instance profile
```
aws ec2 associate-iam-instance-profile --instance-id <new-instance-id> \
  --iam-instance-profile Name=alpha-engine-dashboard-profile
```
Nothing on the box works without this — every service resolves secrets from SSM and reads S3 via the role.

### 3. Security group
Attach the dashboard's own SG (**not** `alpha-engine-executor-sg` — see policy T0-5). Ingress is Cloudflare ranges on 443 only.

### 4. Public IP — reassociate the Elastic IP

**An Elastic IP already exists** (`eipalloc-05492e8b3853eecf0` = `54.144.111.193`), so **no DNS changes are needed**. Reassociate it to the new instance:
```
aws ec2 associate-address --instance-id <new-instance-id> \
  --allocation-id eipalloc-05492e8b3853eecf0
```
This is the single biggest RTO saving available and it is already in place. Every Cloudflare A record points at `54.144.111.193`; keep it that way. **Do not release this EIP** — doing so would add a 12-record DNS update to every future rebuild.

### 5. Verify services came up
```
systemctl --failed
/usr/local/bin/box_health.sh
systemctl list-timers --all
```
All 14 application units active, all timers with a populated `NEXT`. A timer showing `NEXT = -` is dead — see policy T0-4.

### 6. Verify ingress end-to-end
```
for h in console live memory auth telos-dash metron-dash vires-app; do
  curl -s -o /dev/null -w "$h %{http_code}\n" https://$h.nousergon.ai/
done
curl -s -o /dev/null -w "signal %{http_code}\n" https://signal.thecyphering.com/
```
Then confirm the origin is **not** directly reachable (policy T0-2):
```
curl -s -o /dev/null -w "direct-origin %{http_code}\n" \
  --resolve dashboard.nousergon.ai:443:<new-ip> https://dashboard.nousergon.ai/ -k
```
Expected: **403 or connection refused**. A 200 here means Cloudflare Access is bypassed.

### 7. Re-verify the GitHub PAT
`~/.netrc` is restored from the snapshot, but the PAT may have expired in the interim.
`cd ~/alpha-engine-dashboard && git fetch` — if it fails, mint a new fine-grained PAT (`Contents: read`) and rewrite `~/.netrc` (chmod 600).

### 8. Re-run the drift check
```
/home/ec2-user/alpha-engine-data/.venv/bin/python \
  /home/ec2-user/alpha-engine-data/infrastructure/systemd/check-systemd-unit-drift.py --report
```
Confirms installed units still match the repo.

---

## Rehearsal

**This runbook is a hypothesis until it has been executed.** Rehearse Path B against a throwaway instance — restore the latest snapshot to a second t3.small in us-east-1f, work through the post-restore steps, measure the wall-clock, then terminate it and delete the volume. Record the result here:

| Date rehearsed | Path | Measured RTO | Notes |
|---|---|---|---|
| *(not yet)* | | | |

Re-rehearse whenever the box's service set changes materially, or annually — whichever comes first.

---

## Known gaps in this runbook

- **No infrastructure-as-code for the instance itself.** Launch parameters, SG rules, and IAM associations are documented here in prose rather than declared. Acceptable at current scale (policy §3 — "what genuinely does not matter at our scale"), but it is the reason the RTO is 4 hours rather than minutes.
- **The 4-hour RTO is unmeasured.** It is an estimate built from the step list, not a stopwatch reading. The rehearsal above is what converts it into a number worth relying on.
