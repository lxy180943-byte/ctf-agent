# CTFd Competition Profiles

CTFd support is intended only for authorized CTF competitions, local platforms, and benchmark instances. Listing, pulling, and solving are enabled by profile configuration; real submission remains blocked unless all safety gates pass.

## Config Template

Keep real tokens out of git. Use `configs/ctfd.example.yaml` as a shape reference and store real values in an ignored local config such as `configs/ctfd.local.yaml`, or in environment variables.

```yaml
platform:
  ctfd:
    default_profile: quals
    profiles:
      quals:
        name: quals
        url: https://ctf.example
        token:
        team: my-team
        flag_format: flag\{[^}]+\}
        submit_enabled: false
        retries: 3
        timeout: 30
```

Environment token override:

```bash
export CTF_AGENT_CTFD_QUALS_TOKEN='...'
```

## Workflow

```bash
ctf-agent --config configs/ctfd.local.yaml ctfd list --profile quals
ctf-agent --config configs/ctfd.local.yaml ctfd pull 7 --profile quals
ctf-agent --config configs/ctfd.local.yaml ctfd solve 7 --profile quals --mode specialist
ctf-agent --config configs/ctfd.local.yaml ctfd submit ~/ctf-workspace/runs/7 --profile quals
```

The last command is a dry-run unless `--submit` is present.

## Real Submit Gates

Real submission requires all three:

- `submit_enabled: true` in the selected profile.
- CLI flag `--submit`.
- Exact confirmation string `--confirm 'SUBMIT <profile> <challenge_id>'`.

Example:

```bash
ctf-agent --config configs/ctfd.local.yaml ctfd submit ~/ctf-workspace/runs/7 --profile quals --submit --confirm 'SUBMIT quals 7'
```

Downloaded attachments are stored in the run work directory. Artifact metadata records original URL, resolved URL, SHA-256, file size, and profile name.
