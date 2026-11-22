#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

#
# Produce a clean, single-commit export of this repository for publishing.
#
# The export is the tracked tree at HEAD (git archive): uncommitted changes and git-ignored files
# stay behind, and the history is replaced by one fresh commit on main. The script does not add a
# remote or push.
#
# Usage:
#   scripts/export_public.sh [DEST_DIR]
#
# DEST_DIR defaults to a sibling of this repository named after the public repository, and must not
# exist yet. Nothing is written there unless every check below passes.
#
# Leak check. The export is refused, with every hit printed as file:line:match, when it matches
# either of two lists:
#
#   1. Built-in shapes (below). They name no person, account, host or tool, so they are published
#      with the script. Checked in text files (the account-ID shape matches a run of exactly 12
#      digits even when letters or underscores abut it, but not a 13- or 16-digit run; the others as
#      whole words):
#        - 12-digit AWS account IDs, except the AWS documentation placeholders 111122223333,
#          444455556666, 777788889999, 123456789012 and twelve repeated digits;
#        - amazon.com email addresses, except the public contact addresses;
#        - amazon.com host names, except aws.amazon.com sites;
#        - VPC resource IDs (vpc-, subnet-, sg-, vpce- and similar), except this sample's own
#          placeholders;
#        - ticket IDs (a capital letter and 9 or 10 digits) and short review-item codes (a capital
#          B, H or S and one or two digits; S3 is the service, so it is not one).
#   2. Private patterns: your account IDs, aliases, colleagues' names, internal host and tool names,
#      and the names of your own deployments. They are read from the file named by
#      EXPORT_DENYLIST_FILE, by default .export-denylist in the repository root, which .gitignore
#      excludes, so they never sit in a tracked file. The file must be UTF-8 (it is refused
#      otherwise). One extended regular expression (grep -E) per line, matched anywhere in a line
#      and ignoring case; a leading byte-order mark, carriage returns (CRLF or CR-only endings) and
#      trailing whitespace are stripped, and blank lines and lines starting with # are skipped.
#      They are checked in every file, binary files included (an image can carry metadata), and in
#      file names. The script refuses to run without them, and refuses a denylist that yields no
#      usable pattern rather than running a leak check that would match nothing.
#
# The commit's author and committer are checked against both lists as well. If your git identity
# is a work address, set GIT_AUTHOR_NAME, GIT_AUTHOR_EMAIL, GIT_COMMITTER_NAME and
# GIT_COMMITTER_EMAIL to the identity you publish under.
#
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo_name="sample-tenant-isolated-agent-harness-on-aws"
dest="${1:-$(dirname "$repo_root")/$repo_name}"
case "$dest" in /*) ;; *) dest="$PWD/$dest" ;; esac   # the script changes directory below
denylist="${EXPORT_DENYLIST_FILE:-$repo_root/.export-denylist}"

shapes_any_case=(
  -e '[a-z0-9._%+-]+@amazon\.com'                                          # email addresses
  -e '([a-z0-9-]+\.)+amazon\.com'                                          # host names
)
shapes=(
  -e '(vpc|subnet|sg|vpce|eni|rtb|igw|nat|acl)-[0-9a-f]{8}([0-9a-f]{9})?'  # VPC resource IDs
  -e '[A-Z][0-9]{9,10}'                                                    # ticket IDs
  -e '([BH][0-9]{1,2}|S[0-9]{2}|S[124-9])[a-z]?'                           # review-item codes
)
# Account IDs are matched on their own (below), without -w, so an ID glued to letters or underscores
# is still caught.
# Public values that the shapes also match, compared with the whole match, ignoring case.
allowed='111122223333|444455556666|777788889999|123456789012'
allowed+='|0{12}|1{12}|2{12}|3{12}|4{12}|5{12}|6{12}|7{12}|8{12}|9{12}'
allowed+='|opensource-codeofconduct@amazon\.com|aws-security@amazon\.com|([a-z0-9-]+\.)*aws\.amazon\.com'
allowed+='|vpce-0a1b2c3d4e5f60001|vpce-0ffffffffffff9999'

# Print every match in the given files or directories as path:line:match. A grep error (status 2,
# for example a malformed private pattern) stops the script, so a broken check never passes.
find_leaks() {
  {
    grep -rnoIwiE "${shapes_any_case[@]}" "$@" || [ $? -eq 1 ]
    grep -rnoIwE "${shapes[@]}" "$@" || [ $? -eq 1 ]
    # Account IDs: a run of exactly 12 digits, with letters or underscores allowed on either side,
    # but not part of a longer digit run. grep -w rejects word-character neighbours too, so it would
    # miss an ID glued to text (an acct_<id>_prod token). Instead match maximal runs of 12 or more
    # digits and keep only the length-12 ones: a 13- or 16-digit run yields a longer match that the
    # ":[0-9]{12}$" filter drops, so joined IDs are caught while longer runs are not.
    { grep -rnoIE '[0-9]{12,}' "$@" || [ $? -eq 1 ]; } | { grep -E ':[0-9]{12}$' || [ $? -eq 1 ]; }
  } | { grep -viE ":($allowed)\$" || [ $? -eq 1 ]; }
  grep -rnoaiE -f "$work/private" "$@" || [ $? -eq 1 ]
}

if [ -e "$dest" ]; then
  echo "error: $dest already exists; remove it or pass a different DEST_DIR" >&2
  exit 1
fi
if [ ! -f "$denylist" ]; then
  echo "error: no private denylist at $denylist; create it (see the top of this script) or set EXPORT_DENYLIST_FILE" >&2
  exit 1
fi

work="$(mktemp -d "${TMPDIR:-/tmp}/export_public.XXXXXX")"
trap 'rm -rf "$work"' EXIT
# A denylist that is not valid UTF-8 is refused: a stray Latin-1 or Windows-1252 byte would
# otherwise abort the byte tools below mid-stream (BSD tr reports "illegal byte sequence" and exits
# 1), and under pipefail that 1 is indistinguishable from grep's "nothing matched", so every pattern
# after the byte would be dropped and the leak check would pass with those patterns never applied.
if ! iconv -f UTF-8 -t UTF-8 "$denylist" >/dev/null 2>&1; then
  echo "error: $denylist is not valid UTF-8; re-save it as UTF-8 (one pattern per line)" >&2
  exit 1
fi
# Normalize byte-wise (LC_ALL=C, so a valid multi-byte pattern such as a name with an accent cannot
# abort BSD tr): drop a leading UTF-8 byte-order mark, turn every carriage return into a newline so
# both CRLF and CR-only files split into lines (a bare CR left on a pattern matches nothing while
# the check still reports success), strip trailing whitespace, then drop blank and comment lines.
LC_ALL=C sed $'1s/^\xef\xbb\xbf//' "$denylist" \
  | LC_ALL=C tr '\r' '\n' \
  | LC_ALL=C sed 's/[[:space:]]*$//' \
  | LC_ALL=C grep -vE '^[[:space:]]*(#|$)' > "$work/private" || [ $? -eq 1 ]
if [ ! -s "$work/private" ]; then
  echo "error: $denylist has no usable patterns (every line was blank, a comment, whitespace only, or a byte-order mark); refusing to run a leak check that would match nothing" >&2
  exit 1
fi

short="$(git -C "$repo_root" rev-parse --short HEAD)"
echo "Exporting tracked tree at $short to $dest"
tree="$work/tree"
mkdir "$tree"
# git archive emits only tracked files at HEAD, so git-ignored files (the private denylist, local
# build output) are left behind and uncommitted changes are not included.
git -C "$repo_root" archive --format=tar HEAD | tar -x -C "$tree"

cd "$tree"
find_leaks . > "$work/hits"
find . | { grep -iE -f "$work/private" || [ $? -eq 1 ]; } | sed 's/^/file name: /' >> "$work/hits"
if [ -s "$work/hits" ]; then
  echo "error: the export contains internal references; nothing was written to $dest:" >&2
  cat "$work/hits" >&2
  exit 1
fi
echo "Leak check passed: $(find . -type f | wc -l | tr -d ' ') files, no internal references."

git init -q -b main
printf '%s\n' "$(git var GIT_AUTHOR_IDENT)" "$(git var GIT_COMMITTER_IDENT)" > "$work/commit-identity"
(cd "$work" && find_leaks commit-identity) > "$work/hits"
if [ -s "$work/hits" ]; then
  echo "error: the commit identity contains internal references; nothing was written to $dest:" >&2
  cat "$work/hits" >&2
  echo "Set GIT_AUTHOR_NAME, GIT_AUTHOR_EMAIL, GIT_COMMITTER_NAME and GIT_COMMITTER_EMAIL to the identity you publish under." >&2
  exit 1
fi
git add -A
git commit -q -m "Sample: tenant-isolated agent harness on AWS"

cd "$repo_root"
mkdir -p "$(dirname "$dest")"
mv "$tree" "$dest"

echo
echo "Done. $dest now has a single commit on main:"
git -C "$dest" --no-pager log --format='%h %an <%ae> %s'
echo
echo "Next steps (outside this script):"
echo "  1. Create the public repository '$repo_name'."
echo "  2. In $dest, add it as 'origin' and push main."
