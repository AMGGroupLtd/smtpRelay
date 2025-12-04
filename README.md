# SMTP OAuth Relay (for Microsoft 365/Entra)

Simple Dockerized SMTP relay that accepts plain SMTP from legacy clients and sends mail via Microsoft Graph using OAuth2.

This wraps the excellent `smtp-oauth-relay` project:
https://github.com/justiniven/smtp-oauth-relay/

Why this exists
- Microsoft has been restricting basic SMTP authentication and pushing OAuth for sending mail. Many legacy devices, scripts, and tools can’t do OAuth.
- This container lets those clients speak plain SMTP to a local relay while the relay handles OAuth2 with Microsoft 365 on their behalf.

Important security note
- Do NOT expose your SMTP port to the public Internet. Keep it on an internal network or behind a firewall.

What you get
- A single container that listens on a configurable SMTP port (default: 2525) and relays mail through Microsoft Graph using your Entra ID app registration.

Quick start
1) Prerequisites
   - Docker or Docker Desktop
   - A Microsoft Entra ID (Azure AD) app registration with permission to send mail (see below)

2) Configure environment
   - Copy the example env file and edit it:
     ```bash
     cp .env.example .env
     # open .env and fill in values
     ```
   - Required values are documented in `.env.example` and summarized below.

3) Run with Docker (simplest)
   - Start the container using your `.env` file and map the SMTP port you want to use:
     ```bash
     docker run -d \
       --name smtp-oauth-relay \
       --restart unless-stopped \
       --env-file ./.env \
       -p 2525:2525 \
       ghcr.io/justiniven/smtp-oauth-relay:latest
     ```
   - Adjust `-p 2525:2525` if you changed `SMTP_LISTEN_PORT` in your `.env`.

4) Test sending
   - From a host that can reach the container, point your legacy client or use a simple tool, e.g. `swaks`:
     ```bash
     swaks --server <relay-host> --port 2525 --from you@yourdomain.com --to someone@yourdomain.com --data "Subject: Test\n\nHello from the relay!"
     ```

Using Docker Compose
- This repo includes:
  - `docker-compose.yml` (basic stub)
  - `docker-compose.j2.yml` (Jinja template)

Option A: Compose with docker run-equivalent settings (recommended for simplicity)
- Create your own `docker-compose.override.yml` alongside `docker-compose.yml` with an environment and port mapping:
  ```yaml
  services:
    smtp-oauth-relay:
      image: ghcr.io/justiniven/smtp-oauth-relay:latest
      container_name: smtp-oauth-relay
      restart: unless-stopped
      env_file:
        - .env
      ports:
        - "2525:2525"
  ```
  Then run:
  ```bash
  docker compose up -d
  ```

Option B: Render the Jinja template with dockerComposeJinja (no override needed)
- Use dockerComposeJinja to compile `docker-compose.j2.yml` into a standard `docker-compose.yml` using variables from your `.env` file directly. See: https://github.com/AMGGroupLtd/dockerComposeJinja
  ```bash
  # from the project directory where .env and docker-compose.j2.yml
  # This compile the Jinja template into a standard docker-compose.yml using the .env file and then passes all the parameters to 'docker compose' or 'docker-compose'
  dcj up -d
  ```
  Notes:
  - Edit `.env` to change ports, names, or networks and re-run the compile command above; then `docker compose up -d` to apply changes.
  - This method lets you drive all variables from `.env` without creating a `docker-compose.override.yml`.

Option C: Render the Jinja template with jinja2-cli (advanced)
- If you prefer templating for container name/network/ports using a CLI tool, install `jinja2-cli` and render `docker-compose.j2.yml` using variables from `.env`, then bring it up:
  ```bash
  # example using jinja2-cli (Python package)
  pip install jinja2-cli
  jinja2 docker-compose.j2.yml .env > docker-compose.generated.yml
  docker compose -f docker-compose.generated.yml up -d
  ```

Environment variables
These correspond to `.env.example`:
- Microsoft Entra / Azure AD OAuth2
  - `OAUTH_CLIENT_ID` – App registration (client) ID
  - `OAUTH_CLIENT_SECRET` – Client secret value
  - `OAUTH_TENANT_ID` – Directory (tenant) ID

- Microsoft Graph
  - `GRAPH_API_SCOPE` – usually `https://graph.microsoft.com/.default`
  - `GRAPH_API_ENDPOINT` – usually `https://graph.microsoft.com/v1.0`

- SMTP relay behavior
  - `SMTP_LISTEN_PORT` – Port inside the container (default `2525`). Map this to a host port when running.
  - `SMTP_HOSTNAME` – Hostname the relay identifies as.
  - `SMTP_MAX_MESSAGE_SIZE` – Max message size in bytes (default 10 MB).

- Port exposure
  - `DOCKER_PORT` – If set when templating with `docker-compose.j2.yml`, exposes the port. Leave empty to avoid publishing a public port.

- Logging
  - `LOG_LEVEL` – e.g. `info`, `debug`.

- TLS settings (client-to-relay)
  - `SMTP_DISABLE_TLS`, `SMTP_REQUIRE_TLS`, `SMTP_TLS_SOURCE` – Control STARTTLS behavior for clients connecting to the relay. Many legacy clients require TLS to be disabled; use with caution on trusted networks only.

- Docker settings (templating helpers)
  - `SMTP_DOCKER_NAME`, `SMTP_DOCKER_NETWORK` – Used by the Jinja template to set container name and attach to a Docker network.

Microsoft 365/Entra setup (high level)
1) In Entra ID, create an App Registration.
2) Add a client secret and record its value.
3) Grant Microsoft Graph permission to send mail. One of:
   - Application permission: `Mail.Send` (requires admin consent), or
   - Delegated permission appropriate for your use case.
4) Grant admin consent for the tenant.
5) Use the App’s Tenant ID, Client ID, and Client Secret in your `.env`.

Best practices
- Keep the relay on a private network; allow only trusted clients to connect.
- Rotate the client secret periodically; store `.env` securely.
- Monitor logs (`LOG_LEVEL=info` or `debug` when troubleshooting).

Troubleshooting
- Authentication errors: Re-check `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET`, `OAUTH_TENANT_ID`, and that Graph permissions are granted with admin consent.
- Connection issues: Ensure your firewall allows the mapped port (default 2525) from your internal clients. Confirm you didn’t expose it publicly.
- Message too large: Increase `SMTP_MAX_MESSAGE_SIZE` or reduce attachment size.

License and attribution
- This setup uses the `ghcr.io/justiniven/smtp-oauth-relay` image. See that upstream repository for licensing and implementation details.

CHANGE LOG
- 2025-12-02: Initial release
- 2025-12-04: Added docker network settings to Jinja template