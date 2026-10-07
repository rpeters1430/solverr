# Docker releases and rollback

Pull requests run application checks and validate an amd64 candidate container.
They never log in to GHCR or publish images. Main-branch pushes and version tags
run the same checks before promotion. A manual run on another branch validates
only; it does not replace `latest`.

A release builds once to a unique staging tag, then uses the candidate digest for
runtime checks and vulnerability scanning. Only a passing candidate is promoted
to release tags. The Actions summary records the exact digest and commit tag.
Fixable HIGH/CRITICAL findings block promotion; unfixed findings are excluded.
A passing image check does not prove an external service or a host GPU works.

## Updating a NAS installation

Back up persistent config/data before an application update. Pull the image and
recreate the service using your existing Compose project:

```sh
docker compose pull
docker compose up -d
docker compose ps
```

To hold or restore a tested image, replace the image in your Compose file with
the full digest reference recorded in the successful Actions summary:

```yaml
image: ghcr.io/rpeters1430/solverr@sha256:REPLACE_WITH_VALIDATED_DIGEST
```

Then pull and recreate the service. `latest` tracks new validated main builds;
a digest remains fixed until you edit it. Image rollback does not reverse data
or database migrations, so keep a matching pre-upgrade config/data backup.
Version tags `vX.Y.Z` publish version and major/minor image tags without moving
`latest`. Commit tags remain available for identifying earlier builds.

## Maintaining the release pipeline

Actions and base images are pinned by digest; Renovate manages their updates.
Application dependencies use the committed lockfiles (Node) or exact direct
requirements (Python). OS packages and Python transitive dependencies still
resolve during a fresh build; an image digest is the exact deployed artifact.
The workflows disable Docker build-record artifacts to avoid accumulating them.
Staging image versions remain in GHCR; they are not release tags.

Camoufox's Python package determines its paired browser. The Docker build resets
explicit selection with `camoufox set --release`, fetches the paired browser,
and verifies its executable. Update Camoufox through a PR; do not add a separate
hardcoded browser version. CI also starts the browser under UGREEN UID/GID
1000:10. Lint/security tool versions live in `requirements-ci.txt`.
