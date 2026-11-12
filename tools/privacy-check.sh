#!/bin/zsh
# Privacy check for a public repo: run before every push (tools/git-hooks/pre-push) and on any
# folder about to be published.
#   tools/privacy-check.sh --tree [REF]        the committed tree of REF (default HEAD)
#   tools/privacy-check.sh DIR [--no-git] [--tests]
#
# 1. gitleaks and trufflehog find nothing;
# 2. no line matches the private denylist: one case-insensitive extended regex per line in
#    PENDANT_PRIVACY_DENYLIST (default ~/.config/pendant/privacy-denylist.txt), kept out of
#    the repo because it lists the real identifiers. LICENSE (the author's copyright) is exempt.
#    Without a denylist the check fails: a public push needs one;
# 3. every email address and 16-hex device-style ID is listed for a look (not a failure);
# --no-git: DIR must not be a git checkout (a fresh export); --tests: the Python suite passes
# (run on a copy, so DIR is never written to).
# It can't see issue or PR text, attachments made in the browser, or names the denylist lacks.
set -u
DENY="${PENDANT_PRIVACY_DENYLIST:-$HOME/.config/pendant/privacy-denylist.txt}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PENDANT_PY:-$ROOT/.venv/bin/python}"
NOGIT=0; TESTS=0; TMPT=""
if [[ "${1:-}" == "--tree" ]]; then
  REF="${2:-HEAD}"; TMPT=$(mktemp -d); DIR="$TMPT"
  git -C "$ROOT" archive "$REF" | tar -x -C "$DIR" || { print "FAIL: cannot read $REF"; exit 1; }
else
  DIR="${1:?usage: privacy-check.sh --tree [REF] | DIR [--no-git] [--tests]}"; shift
  for a in "$@"; do [[ "$a" == --no-git ]] && NOGIT=1; [[ "$a" == --tests ]] && TESTS=1; done
fi
fail=0
step() { print -- "== $1"; }
if (( NOGIT )); then
  step "no git"; [[ -e "$DIR/.git" ]] && { print "FAIL: $DIR/.git exists"; fail=1; } || print ok
fi
step "gitleaks"
if ! command -v gitleaks >/dev/null; then print "FAIL: gitleaks not installed"; fail=1
elif gitleaks dir --no-banner "$DIR" >/dev/null 2>&1; then print ok
else print "FAIL: gitleaks found something (gitleaks dir -v <dir> to see it)"; fail=1; fi
step "trufflehog"
if ! command -v trufflehog >/dev/null; then print "FAIL: trufflehog not installed"; fail=1
else
  out=$(trufflehog filesystem "$DIR" --no-update --results=verified,unverified,unknown --json 2>/dev/null); rc=$?
  n=$(print -r -- "$out" | grep -c '"SourceMetadata"')
  if (( rc != 0 )); then print "FAIL: trufflehog exited $rc"; fail=1
  elif [[ "$n" == 0 ]]; then print ok; else print "FAIL: trufflehog: $n findings"; fail=1; fi
fi
step "denylist"
if [[ ! -r "$DENY" ]]; then print "FAIL: no denylist at $DENY"; fail=1
else
  pats=("${(@f)$(grep -v '^#' "$DENY" | grep -v '^[[:space:]]*$')}")
  if (( ${#pats} == 0 )) || [[ -z "${pats[1]}" ]]; then print "FAIL: the denylist has no patterns"; fail=1
  else
    hits=""; bad=""
    for pat in "${pats[@]}"; do
      r=$(grep -rInE -i --exclude-dir=.git --exclude=LICENSE -- "$pat" "$DIR" 2>&1); rc=$?
      if (( rc == 2 )); then bad+="$pat"$'\n'; elif (( rc == 0 )); then hits+="$r"$'\n'; fi
    done
    if [[ -n "$bad" ]]; then print "FAIL: invalid denylist pattern(s):"; print -r -- "$bad"; fail=1; fi
    if [[ -n "$hits" ]]; then print "FAIL:"; print -r -- "${hits//$DIR\//}" | sort -u | head -40; fail=1
    elif [[ -z "$bad" ]]; then print ok; fi
  fi
fi
step "emails and device-style IDs (review)"
grep -rhoIE --exclude-dir=.git '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}' "$DIR" | sort | uniq -c | sort -rn | head -30
grep -rhoIE --exclude-dir=.git '\b[0-9A-F]{16}\b' "$DIR" | sort | uniq -c | sort -rn | head -10
if (( TESTS )); then
  step "python tests"
  T=$(mktemp -d); cp -a "$DIR"/. "$T"/; rm -rf "$T/.git"
  t=$(cd "$T" && PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$T" "$PY" -m pytest -q -p no:cacheprovider 2>&1 | tail -1); rm -rf "$T"
  print -- "$t"; [[ "$t" == *" passed"* && "$t" != *failed* ]] || fail=1
fi
[[ -n "$TMPT" ]] && rm -rf "$TMPT"
[[ $fail == 0 ]] && print "PRIVACY CHECK PASSED" || print "PRIVACY CHECK FAILED"
exit $fail
