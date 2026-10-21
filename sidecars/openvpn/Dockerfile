# Sidecar «openvpn»: a client-only OpenVPN container. The runner shares its network namespace
# (`network_mode: service:vpn`), so the runner reaches corporate subnets the server routes, while
# everything else keeps going out directly. No secrets in the image: the profile and the login
# arrive as project files (see deploy/governetics/builders.compose.yml).
# alpine:3.22, digest taken 2026-10-21 (linux/amd64 manifest list)
FROM alpine:3.22@sha256:5291449c3df73caf6ed85e649dec1b9e818b39a5d8c871e97afc13e9cd5e8fa8
RUN apk add --no-cache openvpn openssl iproute2
COPY sidecars/openvpn/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
