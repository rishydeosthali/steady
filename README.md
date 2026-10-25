# Steady

A proactive financial agent for older adults, by phone and text. Steady watches
for upcoming bills, plans payment timing around income, and executes a payment
after explicit confirmation using Capital One's Nessie sandbox.

**The LLM handles judgment and language; code handles money.**

See [the project brief](docs/PROJECT_CONTEXT.md) for features, architecture, and
the HackTX demo, and [AGENTS.md](AGENTS.md) for collaboration and conflict handling.

## Development

This repository currently contains project context and collaboration guidance.
The application stack, run commands, and tests will be documented when chosen.

Andrew works on `andrew`. Team changes should go through pull requests to `main`
once the team establishes that branch with shared history.

For each new clone, enable remembered merge conflict resolutions:

```sh
git config --local rerere.enabled true
git config --local rerere.autoupdate false
git config --local pull.ff only
```

When integrating an existing `main` into a clean working branch:

```sh
git fetch origin
git merge origin/main
```

Resolve conflicts using both contributors' intent and run the relevant checks.
Git remembers resolutions; agents still need to review and validate them.
