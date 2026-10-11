# Security policy

## Reporting a vulnerability

Please don't open a public issue for security problems. Report them privately
through GitHub: **Security → Report a vulnerability** on this repository.
The maintainer aims to reply within a week.

Useful details: what an attacker can do, the steps to reproduce, and the commit
you tested.

## In scope

- Leaking tokens, API keys or other members' data through Discord replies or logs
- Bypassing admin checks, opt-in or guest restrictions
- Prompt injection that makes the bot take an action the sender isn't allowed to
- Getting the bot to run up LLM costs past the per-member rate limit
- Anything in the Docker image or GitHub workflows that exposes secrets

## Supported versions

Only the latest `master` is supported. Self-hosters should update to it.
