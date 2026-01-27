#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CERT_DIR="${CERT_DIR:-$ROOT_DIR/certs}"

mkdir -p "$CERT_DIR"

CA_KEY="$CERT_DIR/ca.key"
CA_CERT="$CERT_DIR/ca.pem"
SERVER_KEY="$CERT_DIR/atc-drone-key.pem"
SERVER_CSR="$CERT_DIR/atc-drone.csr"
SERVER_CERT="$CERT_DIR/atc-drone.pem"
OPENSSL_CONFIG="$CERT_DIR/openssl-atc-drone.cnf"

if ! command -v openssl >/dev/null 2>&1; then
  echo "openssl is required" >&2
  exit 1
fi

echo "[tls] cert dir: $CERT_DIR"

if [[ ! -f "$CA_KEY" ]]; then
  echo "[tls] generating CA key: $CA_KEY"
  openssl genrsa -out "$CA_KEY" 4096 >/dev/null 2>&1
fi

if [[ ! -f "$CA_CERT" ]]; then
  echo "[tls] generating CA cert: $CA_CERT"
  openssl req -x509 -new -nodes -key "$CA_KEY" -sha256 -days 3650 \
    -subj "/CN=ATC Demo CA" \
    -out "$CA_CERT" >/dev/null 2>&1
fi

if [[ ! -f "$SERVER_KEY" ]]; then
  echo "[tls] generating server key: $SERVER_KEY"
  openssl genrsa -out "$SERVER_KEY" 2048 >/dev/null 2>&1
fi

cat >"$OPENSSL_CONFIG" <<'EOF'
[ req ]
default_bits       = 2048
prompt             = no
default_md         = sha256
distinguished_name = dn
req_extensions     = req_ext

[ dn ]
CN = atc-drone

[ req_ext ]
subjectAltName = @alt_names

[ alt_names ]
DNS.1 = atc-drone
DNS.2 = localhost
IP.1 = 127.0.0.1
EOF

echo "[tls] generating CSR: $SERVER_CSR"
openssl req -new -key "$SERVER_KEY" -out "$SERVER_CSR" -config "$OPENSSL_CONFIG" >/dev/null 2>&1

echo "[tls] signing server cert: $SERVER_CERT"
openssl x509 -req -in "$SERVER_CSR" -CA "$CA_CERT" -CAkey "$CA_KEY" -CAcreateserial \
  -out "$SERVER_CERT" -days 3650 -sha256 -extensions req_ext -extfile "$OPENSSL_CONFIG" >/dev/null 2>&1

rm -f "$SERVER_CSR" "$CERT_DIR/ca.srl"

echo
echo "[tls] generated:"
echo "  - $CA_CERT"
echo "  - $SERVER_CERT"
echo "  - $SERVER_KEY"
echo
echo "[tls] suggested .env values:"
echo "  ATC_SERVER_URL=https://atc-drone:3000"
echo "  ATC_SERVER_CA_CERT_PATH=/certs/ca.pem"
echo "  ATC_TLS_CERT_PATH=/certs/atc-drone.pem"
echo "  ATC_TLS_KEY_PATH=/certs/atc-drone-key.pem"
echo "  ATC_ENV=production"

