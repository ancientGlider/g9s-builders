#!/bin/sh
# Starts an OpenVPN client from a profile and a login delivered as files.
#   OVPN_CONFIG        profile (.ovpn); default /etc/g9s/files/vpn.ovpn
#   OVPN_AUTH_FILE     two lines: user name, password (OpenVPN auth-user-pass format); default /etc/g9s/files/vpn_auth
#   OVPN_USER_FILE     first line = user name;  default /etc/g9s/files/vpn_user      (used when there is no auth file)
#   OVPN_PASSWORD_FILE first line = password;   default /etc/g9s/files/vpn_password  (used when there is no auth file)
#   OVPN_LEGACY=1      allow legacy ciphers / TLS 1.0 for old servers (default 1)
#   OVPN_EXTRA         extra openvpn arguments
# The profile is adapted for OpenVPN 2.6 / OpenSSL 3 in a copy; the delivered file is not touched.
set -eu
umask 077
CONFIG="${OVPN_CONFIG:-/etc/g9s/files/vpn.ovpn}"
AUTH_FILE="${OVPN_AUTH_FILE:-/etc/g9s/files/vpn_auth}"
USER_FILE="${OVPN_USER_FILE:-/etc/g9s/files/vpn_user}"
PASS_FILE="${OVPN_PASSWORD_FILE:-/etc/g9s/files/vpn_password}"
[ -r "$CONFIG" ] || { echo "openvpn sidecar: $CONFIG is missing or unreadable"; exit 2; }
if [ -r "$AUTH_FILE" ]; then
  # first two lines, CR stripped, each ending with a newline even if the upload had none
  { sed -n '1p' "$AUTH_FILE"; sed -n '2p' "$AUTH_FILE"; } | tr -d '\r' > /tmp/auth.txt
elif [ -r "$USER_FILE" ] && [ -r "$PASS_FILE" ]; then
  { sed -n '1p' "$USER_FILE"; sed -n '1p' "$PASS_FILE"; } | tr -d '\r' > /tmp/auth.txt
else
  echo "openvpn sidecar: no login — provide $AUTH_FILE (two lines) or $USER_FILE + $PASS_FILE"; exit 2
fi
[ "$(grep -c . /tmp/auth.txt)" = 2 ] || { echo "openvpn sidecar: the login must be two non-empty lines (user, password)"; exit 2; }
sed -e 's/^ns-cert-type server/remote-cert-tls server/' \
    -e 's#^auth-user-pass.*#auth-user-pass /tmp/auth.txt#' \
    "$CONFIG" > /tmp/client.conf
grep -q '^auth-user-pass' /tmp/client.conf || echo 'auth-user-pass /tmp/auth.txt' >> /tmp/client.conf

[ -c /dev/net/tun ] || { mkdir -p /dev/net; mknod /dev/net/tun c 10 200; }

LEGACY=""
if [ "${OVPN_LEGACY:-1}" = 1 ]; then
  LEGACY="--providers legacy default --data-ciphers BF-CBC:AES-256-GCM:AES-128-GCM --data-ciphers-fallback BF-CBC \
          --allow-compression yes --tls-cipher DEFAULT:@SECLEVEL=0 --tls-version-min 1.0"
fi
# shellcheck disable=SC2086
exec openvpn --config /tmp/client.conf --auth-nocache --verb 3 $LEGACY ${OVPN_EXTRA:-}
