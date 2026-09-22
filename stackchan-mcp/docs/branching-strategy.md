# Branching Strategy

This repository uses a lightweight trunk-based workflow. `master` is the
single integration branch and should always be releasable for host tooling and
buildable for firmware.

## Branch Roles

| Branch | Purpose | Lifetime |
| --- | --- | --- |
| `master` | Protected integration branch for released documentation, host tools, and firmware | Permanent |
| `feat/<topic>` | New user-visible behavior | Short-lived |
| `fix/<topic>` | Bug fixes | Short-lived |
| `ci/<topic>` | CI, automation, and repository policy changes | Short-lived |
| `docs/<topic>` | Documentation-only changes | Short-lived |
| `chore/<topic>` | Maintenance that does not change behavior | Short-lived |
| `deps/<topic>` | Manual dependency updates outside Dependabot | Short-lived |
| `experiment/<topic>` | Risky hardware, voice, or protocol exploration | Temporary, draft PR preferred |

Do not add a permanent `develop` branch. The project is small enough that a
second integration branch would mostly duplicate CI and make hardware behavior
harder to reason about.

## Merge Policy

All normal changes enter `master` through pull requests.

- Keep branches focused on one topic and rebase from `master` before review
  when practical.
- Use Conventional Commits for every commit.
- Require the repository CI, security audit, dependency review, and CodeQL
  checks before merging.
- Prefer squash merge for external or noisy contribution branches.
- Prefer regular merge only when preserving a carefully structured series helps
  review or future archaeology.
- Do not force-push `master` except for an owner-approved emergency history
  rewrite, such as removing secrets from public history.

## Releases

Use tags from `master` for release points instead of release branches until the
project needs to patch multiple supported firmware or host versions at once.

Recommended tag format:

```text
vYYYY.MM.DD
```

If firmware compatibility becomes more formal, switch to SemVer tags and add
release notes that call out firmware HTTP API changes, MCP tool behavior
changes, and required environment variable changes.

## Experiments

Hardware and voice experiments can use `experiment/<topic>` while they are
being validated. Keep these branches public only when their history follows the
same secret and local-network hygiene rules as `master`.

Before merging an experiment:

- Split unrelated work into reviewable PRs.
- Rewrite local-only debug commits out of the feature branch.
- Replace live LAN addresses, tunnel hostnames, and local tokens with examples.
- Run the same checks required for normal PRs.

Delete merged experiment branches. Archive abandoned experiments as docs only
when the debugging record is useful.

## Hotfixes

For urgent fixes:

1. Branch from the current `master` using `fix/<topic>`.
2. Make the smallest change that fixes the issue.
3. Run the narrow relevant checks plus `make audit-security`.
4. Open a PR and wait for required checks.
5. Tag from `master` after merge if users need a stable reference.

Only bypass the PR path if the repository owner determines that the risk of
waiting is higher than the risk of direct push. Direct pushes should still use
Conventional Commits and be followed by CI verification.
