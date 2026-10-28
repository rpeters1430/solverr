# Forward Proxy and CA Setup Implementation Plan

## Goal

Add an opt-in HTTP/HTTPS forward proxy to Solverr for Docker, NAS, and trusted LAN clients, starting with Prowlarr indexers and yt-dlp forum-page requests.

## Architecture

Run a separate asyncio TCP listener alongside FastAPI. For ordinary proxy requests, hand the reconstructed request to Solverr's existing tiered solver. For HTTPS, accept CONNECT, terminate the client TLS session with a host certificate signed by Solverr's persistent local CA, and then use the same request path. Serve only the public CA certificate from the HTTP API and document how to install it in client containers.

## First-release boundaries

- Disabled by default; the listener starts only when `MITM_ENABLED=true`.
- HTTP/1.1 only. WebSockets, HTTP/2, and chunked uploads are deferred. Download-shaped GET/HEAD requests use a streaming path so binary bodies and Range responses remain byte-preserving; HTML responses fall back to Solverr's solver.
- Reject unsupported/binary request bodies instead of silently corrupting them.
- Persist the CA under the configured data directory; never expose or download the private key.
- Proxy authentication is required when enabled. Docker clients on the same network use the service name; publishing the listener to the host is an explicit deployment choice.
- yt-dlp can use its normal HTTP proxy option and system-certificate mode (`--compat-options no-certifi`) through the provided CA-verifying entrypoint wrapper.

## Planned files

- `app/forward_proxy.py`: request parsing, optional listener lifecycle, CONNECT/TLS handling, authentication, and solver dispatch.
- `app/proxy_certificates.py`: persistent CA creation and per-host leaf certificate creation.
- `app/config.py`: opt-in listener and certificate settings.
- `app/main.py`: lifespan ownership and public CA download route.
- `requirements.txt`: direct cryptography dependency for certificate management.
- `docker-compose.yml`, `Dockerfile`, and `README.md`: LAN/Docker configuration and Prowlarr/yt-dlp setup.

## Implementation order

1. Add certificate authority management and settings, with safe file permissions and existing-CA validation.
2. Add the HTTP/1.1 listener, CONNECT handling, TLS termination, authentication, and solver request dispatch.
3. Start and stop the listener through FastAPI lifespan and expose the public CA certificate only while enabled.
4. Add Docker Compose environment/port examples and client setup instructions, including LinuxServer Prowlarr init and yt-dlp trust configuration.
5. Add a yt-dlp container entrypoint wrapper to install the verified CA, configure the proxy and system-certificate option, and pass user arguments through.
6. Review the diff for protocol boundaries and document that tests and live client integration were not run in this pass.

## Known limitations to address before production use

- Solverr's current response model stores bodies as text and applies `MAX_RESPONSE_BODY_MB`; the proxy bypasses that model for download-shaped GET/HEAD responses. The streaming path does not yet apply Solverr challenge solving to a binary response and is not a general purpose tunnel for arbitrary protocols.
- The original SSRF validator still has the DNS time-of-check/time-of-use gap tracked as CR-011. Do not treat the forward proxy as closing that gap.
- Clients that pin certificates or use their own trust store need separate setup. Do not recommend disabling certificate validation.
