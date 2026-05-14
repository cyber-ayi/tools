# cc-session threat model

Specifically the **bastion deployment** (VPS-as-RC-entry, mbp as
data plane). For the all-on-mbp deployment, the trust boundary is
just "user owns the laptop" and this doc is mostly N/A.

A sibling threat-model lives at
[`agent-manifest/docs/threat-model.md`](https://github.com/Jarvie8176/agent-manifest)
covering the agent stack (memory poisoning, LLM router, etc.). That
doc cross-references this one; this one focuses on cc-session's
specific surface.

## Trust boundaries

| Component | Trust level | Holds |
|---|---|---|
| **mbp** | Full trust (user's workstation) | `~/cc/.env` secret VALUES; full project files; ssh-manifest authoritative copy; primary data plane |
| **VPS** (e.g. Servarica MTL) | Near-zero trust | SSH private key (`~/.ssh/cc_bridge_ed25519`) into mbp's `me` account, IP-restricted via `from=`; Anthropic OAuth token; cc-session checkout; LiteLLM proxy config |
| **Tailscale tailnet** | Trusted transport | All inter-node traffic |
| **Anthropic API** | Trusted by contract (subscription / API agreement) | Only outbound HTTPS from VPS for RC + paid-API calls |
| **Public internet** | Untrusted | Only outbound to Anthropic + GitHub + CF DNS |

## Accepted blast radius if VPS is compromised

These are explicitly **accepted** because mitigations exist with
documented response times. Tracker: [Jarvie8176/tools#24](https://github.com/Jarvie8176/tools/issues/24).

- ✅ **VPS Anthropic OAuth gets used to run inference on attacker prompts.** Detection: Anthropic console usage anomaly. Recovery: `claude auth login` from a clean machine to revoke + re-issue. <30 min.
- ✅ **Attacker SSHes from VPS into mbp as user `me`.** Mitigation: `from=` clause in mbp's `authorized_keys` restricts the key to a single Tailscale IP — useless if exfiltrated to a non-tailnet host. Even within mbp, every `~/cc/.env` value is rotatable. Recovery: rotation playbook. <30 min.
- ✅ **Attacker reads memory entries pushed from VPS.** Mitigation: memory entries reference token NAMES, never VALUES (gitleaks pre-commit blocks the canonical secret shapes; values that slip past gitleaks are still operationally tied to specific services that can be rotated).
- ✅ **Attacker garbles cc-session state files** (`$TMPDIR/cc-session/*.url`). Effect: misleading URL in monitoring scripts. Recovery: `cc-session --kill` cleans state, restart cc-session.

## NOT accepted — would require redesign

- ❌ **Attacker reads `~/cc/.env` directly.** Mitigation: file is `chmod 600 me:me` on mbp; the SSH bridge runs as `me` user but only via cc-bridge key — not as a separate restricted user. So the `from=` clause is the load-bearing control. **If the bastion model goes to a setup where `from=` isn't enforceable** (e.g., VPS roams its Tailscale IP), the threat model degrades and we'd need a separate restricted SSH user (`cc-bridge`) on mbp with no `~/cc/.env` access.
- ❌ **Attacker exfiltrates ssh-manifest's authoritative copy** (lives on mbp at `~/ssh-manifest/`). Mitigation: same as above — file permissions + the fact that mbp itself isn't reachable from VPS without the bridge key.
- ❌ **Attacker compromises Anthropic / Tailscale / GitHub upstream.** Out of scope; assume those are operationally protected by their providers.

## Defense in depth

1. **Network**: Tailscale ACL restricts mbp's sshd to accept connections only from known tailnet IPs (mbp itself, pc, the VPS). Public internet sshd not exposed.
2. **SSH bridge**: VPS's key on mbp's `authorized_keys` carries:
   - `from="100.126.89.3"` (or whatever the VPS's Tailscale IP is)
   - `no-port-forwarding` — prevents using SSH for tunneling
   - `no-X11-forwarding` — prevents X tunneling
3. **Filesystem perms on mbp**: `~/cc/.env` is `chmod 600 me:me`. Other secret-bearing files (`~/.claude/auth.json`, `~/ssh-manifest/`) are similarly restricted.
4. **Memory pipeline**: agents in agent-manifest stack write to `_pending/` only; promotion to authoritative namespaces requires human or curator review.
5. **Secret scanning**: gitleaks pre-commit + CI on agent-manifest. cc-session itself doesn't house secrets, so no gitleaks needed there.
6. **Audit logs**:
   - mbp `sshd` auth log → ssh-manifest's `audit-keys.sh` cron flags drift
   - cc-session's `--status` for monitoring scripts
   - VPS `journalctl --user -u cc-session` for bastion service health

## Specific risks + mitigations

### R1: VPS Anthropic API key (LiteLLM stack) leaks

LiteLLM container has the API key in env. Container compromise extracts it.

**Mitigation**: API key is rotatable in Anthropic console. Detection via usage anomaly. The cc-session bastion itself doesn't expose the API key — that's a LiteLLM stack concern; cross-reference [`agent-manifest/docs/threat-model.md`](https://github.com/Jarvie8176/agent-manifest).

### R2: VPS subscription OAuth token leakage

VPS holds `~/.claude/auth.json` (subscription OAuth). If extracted, attacker could use it to drive RC sessions or load the Anthropic UI as you.

**Mitigation**: Per Anthropic ToS, subscription OAuth in non-official tools is a ban-worthy violation (post-OpenClaw enforcement) — attacker would be flagged by Anthropic's anti-abuse. Additional: rotate via `claude auth login` if compromise suspected.

### R3: cc-session state file poisoning

Attacker writes a malicious URL into `$TMPDIR/cc-session/<NAME>.url`. Monitoring tools or browser bookmarks pull that URL and the user opens it.

**Mitigation**: file is per-user (`$TMPDIR` is `/var/folders/...` on macOS, owned by user). Attacker reaching that file already has user-level access to mbp, in which case the state file is the least of your concerns.

### R4: Resume-key polling fails open

cc-session's post-launch subshell polls for "Resume from summary". If pane content is attacker-controlled (somehow), they could trigger a false-positive resume key.

**Mitigation**: pane content originates from `claude --teleport`'s stdout — Anthropic-controlled. For the attacker to inject it, they'd need to compromise the claude binary itself. Out of scope.

### R5: Stale state file points at dead env URL

User's phone bookmark or monitoring loop opens a URL that's no longer live (env_xxx server died but state file persisted).

**Mitigation**: cc-session 0.4 fixes via `--kill` cleaning state files; `--status` distinguishes alive/dead by checking tmux session existence. Not a security risk per se, but operational hygiene.

## Incident response

Full playbook lives in [Jarvie8176/tools#24](https://github.com/Jarvie8176/tools/issues/24). Short version on suspected VPS compromise:

1. **Cut VPS network reach to mbp**: `tailscale down` on VPS, OR remove the `from=` IP allowance line in mbp's `authorized_keys`.
2. **Revoke VPS Anthropic OAuth**: `claude auth logout` on VPS (if still reachable) + revoke session via claude.ai → settings.
3. **Rotate every `~/cc/.env` token**: CF, Portainer, HASS, etc. — see [#24](https://github.com/Jarvie8176/tools/issues/24) for per-service rotation steps.
4. **Audit downstream**: Portainer audit log, CF activity log, HASS history for unauthorized writes during compromise window.
5. **Reprovision VPS container fresh**: redistribute new SSH bridge key via `setup-bridge-key.sh`.
6. **Update ssh-manifest + Notion + 1Password DR**: per ssh-manifest's existing rotation flow.

## What's deliberately NOT in this model

- Physical access to mbp (out of scope; user owns the device)
- Anthropic-side compromise of subscription / API (regulatory protection assumed)
- GitHub account compromise (covered by GitHub 2FA + cyber-ayi PAT rotation, ssh-manifest tracks this)
- Tailscale outage (out of scope; WireGuard + MagicDNS infrastructure)
- Software supply-chain attack on Claude Code / tmux / OS packages (mitigated by version pinning where possible; not zero risk)
