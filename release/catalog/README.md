# BotStudio game package catalog

One public catalog for attributed game packages, grouped by Game, numeric GitHub
publisher ID, package and immutable version. Contribution forks are technical
workspaces, not separate catalogs to browse.

## Contributions

Use BotStudio's Games > Packages archive inspection and explicit GitHub pull
request submission. Normal sign-in and optional public contribution authorization
must belong to the same account. Submission publishes the complete archive and
metadata publicly; never include secrets or content you cannot redistribute.

A package PR must add exactly its archive and submission descriptor. Trusted
automation independently checks the PR author's numeric ID against the declared
publisher, exact head/files, version paths, archive bounds, SHA-256 and internal
checksums, and declared publication rights. It never executes submitted content.
Passing formal checks is not legal approval or a guarantee that a package works.
Original authors and parent/fork references remain declared claims.

Valid submissions are merged automatically and published as releases, without
manual Game-specific approval. Other contributors' variants keep separate
identities; they do not silently take over original packages.

## Automation and recovery

The workflow executes only `automation/catalog_automation.py` from the trusted
default branch. Never switch privileged checkout to PR code or trust validation
artifacts/dependencies from contributors. Keep default-branch write access limited
to administrators and the validated publication job.

Publication verifies the merged bytes, creates a unique tag and draft release,
uploads the checked ZIP, and requires GitHub's SHA-256 digest before publishing.
Failures can leave a merged PR, tag or draft. Inspect before retrying. Manual
workflow dispatch accepts a PR number for recovery before a tag/release exists;
existing tags/releases are not overwritten automatically. GitHub quotas apply.

## Central Game registry

`game-registry.json` on the current default branch is the authority for package
Game IDs. Both validation and publication fetch it freshly through the Contents
API, not from a PR, checkout, client declaration or earlier validation result.
The pure ZIP validator remains independent of registry/network access.

Registry schema:

```json
{"schema_version":1,"games":[{"id":"whiteout_survival","name":"Whiteout Survival","android_packages":["com.gof.global"]}]}
```

Administrators seed this file before enabling the workflows; preserve existing
canonical IDs, especially `whiteout_survival`. The example is not an automatic
seed or migration. Missing/malformed registries fail closed. New Games receive
server-generated UUID4 IDs, never name/package-derived IDs.

An authenticated user registers by opening (or reopening) an issue titled
`[BotStudio Game] <name>` whose **entire body** is JSON:

```json
{"schema_version":1,"name":"Example Game","android_package_name":"com.example.game"}
```

Names have 1-120 Unicode characters, no control/format/surrogate characters,
and a nonempty NFKC-normalized, whitespace-collapsed display name. Matching uses
NFKC, casefold and whitespace collapse. Android packages are exact, case-sensitive
ASCII identifiers (two or more dot-separated segments, each starting with a
letter and followed by letters/digits/underscores), at most 255 characters.
Bodies are at most 4096 UTF-8 bytes; unknown/duplicate fields, caller-selected IDs,
noninteger schema versions and malformed requests are rejected.

An exact Android package selects an existing Game regardless of display wording.
A normalized-name match with an unknown package is rejected: explicitly select
the existing Game for a changed local package, or use a distinct edition name.
Registration never silently appends aliases or merges regional editions.
Administrators can edit centrally reviewed aliases later. A package match retains
the canonical name; it does not rename the Game. When name and package select
different Games, registration fails rather than merging identities.

The trusted issue workflow serializes repeated events for the same issue,
loads the issue through the API and re-reads the registry on each
attempt. Updates create a unique server-generated
`botstudio-game-registration/<issue>-<UUID>` branch from the default branch,
use Contents PUT with the current file SHA **on that branch**, and create an
automatic registry-only PR. The workflow freshly verifies its Actions bot author,
repository, generated branch, exact commit head and single registry path, then
merges via the ordinary exact-head merge API. It does not write directly to the
protected default branch or bypass/modify protections. A changed default-branch
registry aborts the stale proposal before merging. 405/409/422 collisions retry
from freshly read state at most three total attempts. Failed/superseded PRs get
diagnostic comments and are closed to prevent later accidental stale merges;
failed issue comments include their PR numbers. Branches remain for inspection.
It confirms the committed default-branch registry before success.
No issue text becomes a shell
command, import, checkout ref, path, commit message or executable content.
Different issues do not share an Actions concurrency group: GitHub retains only
one pending job per group and would otherwise drop queued registrations.
SHA checks, merge conflicts and fresh identity checks prevent racing requests
from blindly overwriting the registry. Retrying reuses an already registered ID.

Success comments contain raw machine-readable JSON
`{"schema_version":1,"game_id":"<canonical ID>","name":"<canonical name>"}` and the
issue is closed. Invalid requests or registration failures receive an explicit
JSON `error` comment and remain open; inspect the workflow diagnostics, correct
the request and reopen to retry. A partial failure may already have committed
the Game; retries reuse it. Clients need not read comments: poll the fresh
default-branch registry by normalized name/package after creating the issue.
These matches are open community declarations, not proof of Game ownership.

Registry bounds are 512 KiB, 10000 Games and 100 Android aliases per Game.
Duplicate case-insensitive IDs, normalized names or exact packages fail closed.

### Installation mapping (separate catalog repository)

| Local template | Catalog destination |
| --- | --- |
| `release/catalog/catalog_automation.py` | `automation/catalog_automation.py` |
| `release/catalog/catalog-submissions.yml` | `.github/workflows/catalog-submissions.yml` |
| `release/catalog/catalog-games.yml` | `.github/workflows/catalog-games.yml` |
| `release/catalog/README.md` | `README.md` |
| Administrator-provided seed | `game-registry.json` |

Keep both workflows on the trusted default branch, with pinned checkout and
`persist-credentials: false`. The issue job needs `contents: write`,
`issues: write` and `pull-requests: write`; package jobs retain their existing
permissions. Repository Actions settings must allow GitHub Actions to create
pull requests. PR-only branch protection with zero required approvals and no
required checks permits automatic ordinary merges; stronger protections remain
enforced and fail visibly rather than being bypassed.
The package workflow's trusted script skips package validation/publication only
after freshly verifying a same-repository `github-actions[bot]` PR (numeric
41898282), generated registry branch prefix and exactly one modified
`game-registry.json` file. Other bots, forks, branch names or extra files do not
qualify; arbitrary submissions continue through normal package validation.
No workflow deployment, remote write or registry seeding is performed by these
local templates. `tests/test_catalog_registration_backend.py` is an offline test, not a
deployment dependency.

## License and source

The catalog automation code and workflow are GPL-3.0-only; see [LICENSE](LICENSE).
Their complete installed source is available in this repository at
`automation/catalog_automation.py`, `.github/workflows/catalog-submissions.yml`
and `.github/workflows/catalog-games.yml`.
They originate from BotStudio's `release/catalog` templates, maintained by
pernerch with assistance from Copilot.

**The repository's automation license does not relicense game packages.**
Each package retains its own manifest license, full terms, third-party notices
and attribution. No permission to redistribute is inferred from catalog presence.
