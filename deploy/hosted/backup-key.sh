#!/bin/sh
# Make the key pair for the box's nightly secrets backup. Run once, on your Mac.
#
#   deploy/hosted/backup-key.sh
#
# The private key goes straight into your login Keychain as "quarter-hosted-backup" and is
# never printed or written to disk. The public key goes to deploy/hosted/backup-recipient.txt,
# which is safe to commit. To read a backup later:
#
#   security find-generic-password -s quarter-hosted-backup -w | age -d -i - <file>.tar.age | tar -tzv
set -eu
command -v age-keygen >/dev/null || { echo "Install age first: brew install age" >&2; exit 1; }
if security find-generic-password -s quarter-hosted-backup >/dev/null 2>&1; then
  echo "A key named quarter-hosted-backup is already in your Keychain; not replacing it." >&2
  exit 1
fi
here=$(cd "$(dirname "$0")" && pwd)
key=$(age-keygen 2>/dev/null | grep '^AGE-SECRET-KEY-')
security add-generic-password -s quarter-hosted-backup -a quarter -w "$key"
printf '%s\n' "$key" | age-keygen -y > "$here/backup-recipient.txt"
key=
echo "Private key saved in your Keychain as quarter-hosted-backup."
echo "Public key: $(cat "$here/backup-recipient.txt") (in deploy/hosted/backup-recipient.txt)"
