#!/bin/bash
# Prints the FAIRPLAY_PINS values (base64 SHA-256 of each certificate's DER) for every
# certificate the server sends. Pin the intermediate/root CA lines (they survive normal
# certificate renewals), ideally two of them.   bash pin.sh exam.example.edu
set -euo pipefail
HOST="${1:?usage: pin.sh host[:port]}"; [[ "$HOST" == *:* ]] || HOST="$HOST:443"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
openssl s_client -connect "$HOST" -servername "${HOST%%:*}" -showcerts </dev/null 2>/dev/null \
  | awk -v d="$TMP" '/BEGIN CERT/{n++} n{print > (d "/c" n ".pem")} /END CERT/{}'
for f in "$TMP"/c*.pem; do
  subj="$(openssl x509 -in "$f" -noout -subject)"
  pin="$(openssl x509 -in "$f" -outform DER | openssl dgst -sha256 -binary | base64)"
  echo "$pin   $subj"
done
