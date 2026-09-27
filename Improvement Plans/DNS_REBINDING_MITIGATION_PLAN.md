# DNS rebinding mitigation plan

## Current gap

`app/security.py` resolves and validates a target hostname before a request.
The HTTP and browser clients then resolve the hostname independently when
they connect. A DNS answer can change between those operations, so the
validated address is not guaranteed to be the address the client reaches.
Redirects and browser subresources are checked too, but each check has the
same time-of-check/time-of-use gap.

Relevant paths:

- `app/security.py::check_target_url_async`
- `app/solver/engine.py::HybridSolverEngine.process_request`
- `app/solver/fast_tls.py` (curl_cffi request and manual redirect flow)
- `app/solver/browser/navigation.py::install_media_blocking`

## Why not patch only the validator

Changing DNS validation alone cannot pin the connection. A curl-only pin
would leave Camoufox and its subresources exposed. A browser-only route
check has the same gap because Firefox resolves the hostname after the
route callback has approved it.

The installed curl_cffi version supports libcurl resolve entries at session
setup, but Solverr reuses sessions and follows redirects across hosts. The
pin must be scoped to each request and refreshed for each redirect without
leaking one request's DNS mapping into another pooled session. This needs a
public, regression-tested integration rather than mutation of curl_cffi
internals.

## Preferred implementation

Use one local, policy-enforcing outbound forward proxy for both HTTP stacks.
It should bind only to loopback, resolve each requested destination once,
reject the destination if any returned address is disallowed under the
existing `ALLOW_PRIVATE_NETWORKS` / `ALLOWED_HOSTS` /
`DENIED_HOSTS` policy, then connect directly to one of the validated
addresses. It must never pass the hostname to a second resolver when opening
the upstream socket.

For HTTPS, handle CONNECT as a byte tunnel; leave TLS end-to-end between the
client and the origin so origin SNI and certificate validation stay intact.
For plain HTTP, support absolute-form proxy requests and apply the same
resolve, validate, and connect sequence. Do not log proxy credentials or
request bodies.

Route default Fast TLS and Camoufox egress through this proxy. Preserve
caller-configured external proxies only as an explicit trusted-proxy mode:
Solverr cannot enforce the final target resolution performed by a remote
proxy, so that limitation must be clear in configuration and documentation.

## Required tests before enabling by default

1. Unit-test resolution and policy decisions for public, loopback, private,
   link-local, reserved, IPv4, and IPv6 addresses, including mixed public
   and blocked DNS answers.
2. Use a controlled DNS server that changes a hostname from public to
   loopback between lookups. Verify the proxy connects only to the address
   it validated and never reaches the loopback listener.
3. Cover HTTP, HTTPS CONNECT, redirects, browser subresources, concurrent
   requests to the same hostname, DNS rotation, resolver failures, and
   request cancellation.
4. Verify that `ALLOWED_HOSTS`, `DENIED_HOSTS`, and
   `ALLOW_PRIVATE_NETWORKS` retain their documented precedence.
5. Run the integration suite against a built container as root and as
   `PUID=1000` / `PGID=10`, with multiple browser workers. Confirm
   FlareSolverr compatibility, user-supplied proxies, health checks, and
   shutdown behavior.

Do not ship a partial curl-only or browser-only pin as a complete SSRF fix.
