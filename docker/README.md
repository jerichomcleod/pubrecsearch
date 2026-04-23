# PostgreSQL via Docker

This directory contains the Docker Compose configuration for the PubRecSearch PostgreSQL instance. The database runs locally in a container with a named volume for persistent storage. The schema from `db/schema.sql` is applied automatically on first startup.

## Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Mac/Windows) or Docker Engine + Compose plugin (Linux)
- Verify your install: `docker compose version`

---

## Starting the database

Run from this directory (`docker/`):

```bash
docker compose up -d
```

Or from the project root:

```bash
docker compose -f docker/docker-compose.yml up -d
```

Docker will:
1. Pull `postgres:16` if not already cached
2. Create a named volume (`pubrecsearch_postgres_data`) for persistent storage
3. Start the container and apply `db/schema.sql` automatically on first launch
4. Expose PostgreSQL on `localhost:5432`

Check that the container is healthy:

```bash
docker compose -f docker/docker-compose.yml ps
```

The `Status` column should show `healthy` within ~10 seconds.

---

## Connecting

### From the application

Set this in your `.env` file (already the default):

```
DATABASE_URL=postgresql://pubrecsearch:changeme@localhost:5432/pubrecsearch
```

### Via psql (interactive shell)

```bash
# Using the running container
docker exec -it pubrecsearch-postgres psql -U pubrecsearch -d pubrecsearch

# Using a local psql client
psql postgresql://pubrecsearch:changeme@localhost:5432/pubrecsearch
```

### Via a GUI client (TablePlus, DBeaver, DataGrip, etc.)

| Field    | Value          |
|----------|----------------|
| Host     | `localhost`    |
| Port     | `5432`         |
| Database | `pubrecsearch` |
| User     | `pubrecsearch` |
| Password | `changeme`     |

---

## Stopping the database

```bash
# Stop the container (data is preserved in the volume)
docker compose -f docker/docker-compose.yml stop

# Stop and remove the container (data still preserved in the volume)
docker compose -f docker/docker-compose.yml down
```

---

## Resetting the database (wipe all data)

This destroys all scraped data and lets the schema be re-applied from scratch:

```bash
docker compose -f docker/docker-compose.yml down -v
docker compose -f docker/docker-compose.yml up -d
```

The `-v` flag removes the named volume along with the container.

---

## Re-applying the schema manually

If you update `db/schema.sql` after the container already exists, the init script won't re-run automatically (it only fires on a fresh volume). Apply changes manually:

```bash
docker exec -i pubrecsearch-postgres \
  psql -U pubrecsearch -d pubrecsearch \
  < ../db/schema.sql
```

Or use the CLI command from the project root:

```bash
pubrecsearch init-db
```

---

## Viewing logs

```bash
docker compose -f docker/docker-compose.yml logs -f
```

---

## Changing credentials

Edit the `environment` block in `docker-compose.yml` **before** first launch, then update your `.env` to match. If the container already exists, you must wipe the volume (see above) for the new credentials to take effect, since PostgreSQL stores them in the data directory.
