#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

failures=0

while IFS= read -r source; do
  [ -f "$source" ] || continue
  source_dir="$(dirname "$source")"

  while IFS= read -r target; do
    target="${target#<}"
    target="${target%>}"
    target="${target%%#*}"

    case "$target" in
      ''|http://*|https://*|mailto:*|tel:*) continue ;;
    esac

    if [ ! -e "$source_dir/$target" ]; then
      printf 'Broken documentation link: %s -> %s\n' "$source" "$target" >&2
      failures=$((failures + 1))
    fi
  done < <(perl -ne 'while (/\[[^][]*\]\(([^ )]+)(?:\s+"[^"]*")?\)/g) { print "$1\n" }' "$source")
done < <(git ls-files '*.md')

if [ "$failures" -ne 0 ]; then
  printf 'Documentation check failed with %s broken link(s).\n' "$failures" >&2
  exit 1
fi

echo 'Documentation link check passed.'
