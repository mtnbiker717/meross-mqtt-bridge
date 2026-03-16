# Security

## Deployment Recommendations

### Use a reverse proxy with TLS

This project does not include TLS termination. The GUI runs on HTTP port 8080.

**Do not expose port 8080 to the internet.** If you need external access, place the GUI behind a reverse proxy with a valid TLS certificate:

- [Caddy](https://caddyserver.com) — automatic HTTPS, minimal config
- [Nginx Proxy Manager](https://nginxproxymanager.com) — GUI-based
- [Traefik](https://traefik.io) — Docker-native, label-based config

For homelab use on a trusted local network, HTTP is acceptable.

### Change the default credentials

The default login is `admin` / `admin`. You are required to change the password on first login. You should also change the username via **Account Settings** in the GUI.

### Network isolation

The bridge container has no exposed ports — only the GUI container exposes 8080. Consider placing the GUI on an isolated VLAN or behind a VPN for additional protection.

---

## Known Limitations

### Credentials stored in plaintext

Meross and MQTT passwords are stored in `config/config.yaml` in plaintext. This file is excluded from git (`.gitignore`) and should be protected with appropriate filesystem permissions.

Future improvement: encryption at rest with a machine-derived key.

### HTTP only

No TLS is provided out of the box. Credentials entered in the Settings page are transmitted over HTTP. On a trusted local network this is low risk; over the internet it is not acceptable without a TLS proxy in front.

### In-memory rate limiting

Login rate limiting resets when the container restarts. This is acceptable for homelab use but would not be sufficient for internet-exposed deployments.

### Session cookie missing Secure flag

The session cookie does not set the `Secure` flag because TLS is not guaranteed. When deployed behind a TLS proxy, configure the proxy to add `Secure` to cookies, or set `GUI_SECRET_KEY` as an environment variable and configure the proxy to handle cookie security.

### CSRF protection scope

CSRF tokens are currently applied to the password change form only. Other POST endpoints (settings save, door config save) rely on the session cookie for authentication. Full CSRF coverage is planned before public release.

### Tailwind CDN requires unsafe-inline CSP

The GUI uses the Tailwind CDN play script which generates styles at runtime via
JavaScript. This requires `unsafe-inline` in the Content Security Policy's
`script-src` directive, which weakens XSS protection. For a hardened deployment,
replace the Tailwind CDN with a compiled CSS build step. This is tracked as a
future improvement.

---

## Pre-Public Release Checklist

The following items are tracked for completion before this repository is made public:

- [x] Full CSRF token coverage on form endpoints
- [x] X-Requested-With header check on all JSON POST endpoints
- [x] Dependency pinning
- [x] Docker containers run as non-root user (gosu entrypoint pattern)
- [x] Body size limits on JSON POST endpoints (64KB)
- [x] Schema validation on door config writes
- [x] Security headers (X-Frame-Options, X-Content-Type-Options, CSP, Referrer-Policy)
- [x] SRI hashes on DaisyUI and Font Awesome CDN resources
- [ ] pip-audit clean run (run before making repo public)
- [ ] Tailwind CDN replaced with build step for full CSP compliance (future improvement)

---

## Reporting Vulnerabilities

This is a personal homelab project. If you find a security issue, please open a GitHub issue with the label `security`. For sensitive disclosures, contact via the GitHub profile.
