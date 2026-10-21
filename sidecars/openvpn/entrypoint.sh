#!/bin/sh
# Starts an OpenVPN client from a profile and a login delivered as files.
#   OVPN_CONFIG        profile (.ovpn); default /etc/g9s/files/vpn.ovpn
#   OVPN_USER_FILE     first line = user name;  default /etc/g9s/files/vpn_user
#   OVPN_PASSWORD_FILE first line = password;   default /etc/g9s/files/vpn_password
#   OVPN_LEGACY=1      allow legacy ciphers / TLS 1.0 for old servers (default 1)
#   OVPN_EXTRA         extra openvpn arguments
# The profile is adapted for OpenVPN 2.6 / OpenSSL 3 in a copy; the delivered file is not touched.
set -eu
umask 077
CONFIG="${OVPN_CONFIG:-/etc/g9s/files/vpn.ovpn}"
USER_FILE="${OVPN_USER_FILE:-/etc/g9s/files/vpn_user}"
PASS_FILE="${OVPN_PASSWORD_FILE:-/etc/g9s/files/vpn_password}"
for f in "$CONFIG" "$USER_FILE" "$PASS_FILE"; do
  [ -r "$f" ] || { echo "openvpn sidecar: $f is missing or unreadable"; exit 2; }
done
{ head -n1 "$USER_FILE"; head -n1 "$PASS_FILE"; } | tr -d '\r' > /tmp/auth.txt
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
