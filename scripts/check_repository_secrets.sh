#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

history=false
if [ "${1:-}" = "--history" ]; then
  history=true
elif [ "$#" -gt 0 ]; then
  echo "Usage: $0 [--history]" >&2
  exit 2
fi

allowed_sensitive_paths='^(\.env\.example|deploy/uat/\.env\.example|deploy/uat_single_node/\.env\.example|test/\.env\.test|test/\.env\.test\.example)$'
sensitive_path_pattern='(^|/)(\.env($|\.)|[^/]*\.(pem|key|p12|pfx)$|id_rsa$|id_ed25519$|backups?/|[^/]*\.sql(\.gz)?$)'
credential_pattern='-----BEGIN (RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----|(^|[^A-Za-z0-9])(sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|AKIA[0-9A-Z]{16})([^A-Za-z0-9]|$)'

tracked_sensitive="$({ git ls-files | grep -E "$sensitive_path_pattern" || true; } | grep -Ev "$allowed_sensitive_paths" || true)"
if [ -n "$tracked_sensitive" ]; then
  echo "Refusing release: sensitive-looking paths are tracked:" >&2
  printf '%s\n' "$tracked_sensitive" >&2
  exit 1
fi

credential_paths="$(git grep -I -l -E -- "$credential_pattern" -- . ':!*.example' ':!test/.env.test' 2>/dev/null || true)"
if [ -n "$credential_paths" ]; then
  echo "Refusing release: credential-like content exists in tracked files:" >&2
  printf '%s\n' "$credential_paths" >&2
  exit 1
fi

if $history; then
  history_paths="$({
    for revision in $(git rev-list --all); do
      git grep -I -l -E -- "$credential_pattern" "$revision" -- . \
        ':!*.example' ':!test/.env.test' 2>/dev/null || true
    done
  } | sed -E 's/^[0-9a-f]{40}://' | sort -u)"
  if [ -n "$history_paths" ]; then
    echo "Refusing release: credential-like content exists in Git history:" >&2
    printf '%s\n' "$history_paths" >&2
    echo "Revoke and rotate the credential before rewriting history." >&2
    exit 1
  fi

  historical_sensitive="$({
    git log --all --name-only --pretty=format: --diff-filter=ACMR
  } | grep -E "$sensitive_path_pattern" | grep -Ev "$allowed_sensitive_paths" | sort -u || true)"
  if [ -n "$historical_sensitive" ]; then
    echo "Refusing release: sensitive-looking paths exist in Git history:" >&2
    printf '%s\n' "$historical_sensitive" >&2
    exit 1
  fi
fi

echo "Repository secret check passed$($history && printf ' (including Git history)')."
