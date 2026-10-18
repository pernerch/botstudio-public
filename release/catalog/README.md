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

## License and source

The catalog automation code and workflow are GPL-3.0-only; see [LICENSE](LICENSE).
Their complete installed source is available in this repository at
`automation/catalog_automation.py` and `.github/workflows/catalog-submissions.yml`.
They originate from BotStudio's `release/catalog` templates, maintained by
pernerch with assistance from Copilot.

**The repository's automation license does not relicense game packages.**
Each package retains its own manifest license, full terms, third-party notices
and attribution. No permission to redistribute is inferred from catalog presence.
