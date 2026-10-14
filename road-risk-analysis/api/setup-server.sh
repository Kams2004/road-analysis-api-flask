#!/bin/bash
# setup-server.sh — run once on the server to configure native PostgreSQL
# Usage: sudo bash setup-server.sh
set -e

ENV_DIR="/etc/road-analysis"
ENV_FILE="$ENV_DIR/db.env"

# ── 1. Ask for a password (or reuse existing) ─────────────────────────────────
if [ -f "$ENV_FILE" ]; then
    source "$ENV_FILE"
    echo "[*] Using existing credentials from $ENV_FILE"
    echo "    DB_USER=$DB_USER  DB_NAME=$DB_NAME  DB_PASSWORD=$DB_PASSWORD"
else
    mkdir -p "$ENV_DIR"
    chmod 700 "$ENV_DIR"

    read -p "Choose a DB username [default: road_api]: " DB_USER
    DB_USER=${DB_USER:-road_api}

    read -p "Choose a DB name [default: road_analysis]: " DB_NAME
    DB_NAME=${DB_NAME:-road_analysis}

    while true; do
        read -s -p "Choose a DB password (you will use this to connect externally): " DB_PASSWORD
        echo
        read -s -p "Confirm password: " DB_PASSWORD2
        echo
        [ "$DB_PASSWORD" = "$DB_PASSWORD2" ] && break
        echo "Passwords do not match, try again."
    done

    cat > "$ENV_FILE" <<EOF
DB_USER=$DB_USER
DB_NAME=$DB_NAME
DB_PASSWORD=$DB_PASSWORD
EOF
    chmod 600 "$ENV_FILE"
    echo "[+] Credentials saved to $ENV_FILE"
fi

# ── 2. Ensure PostgreSQL is running ──────────────────────────────────────────
systemctl enable postgresql
systemctl start postgresql

# ── 3. Create DB user and database ───────────────────────────────────────────
sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE USER $DB_USER WITH PASSWORD '$DB_PASSWORD';"

sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE DATABASE $DB_NAME OWNER $DB_USER;"

# Always sync the password (ensures it matches what's in the file)
sudo -u postgres psql -c "ALTER USER $DB_USER WITH PASSWORD '$DB_PASSWORD';"
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE $DB_NAME TO $DB_USER;"

# ── 4. Allow local TCP connections in pg_hba.conf ────────────────────────────
PG_HBA=$(sudo -u postgres psql -t -c "SHOW hba_file;" | tr -d ' ')
if ! grep -q "$DB_USER" "$PG_HBA"; then
    echo "host    $DB_NAME   $DB_USER    127.0.0.1/32    md5" >> "$PG_HBA"
    # Allow external connections too (from your machine / DBeaver / TablePlus)
    echo "host    $DB_NAME   $DB_USER    0.0.0.0/0       md5" >> "$PG_HBA"
    systemctl reload postgresql
    echo "[+] pg_hba.conf updated"
fi

# ── 5. Allow PostgreSQL to listen on all interfaces ──────────────────────────
PG_CONF=$(sudo -u postgres psql -t -c "SHOW config_file;" | tr -d ' ')
if grep -q "^#listen_addresses" "$PG_CONF" || grep -q "^listen_addresses = 'localhost'" "$PG_CONF"; then
    sed -i "s/^#*listen_addresses.*/listen_addresses = '*'/" "$PG_CONF"
    systemctl reload postgresql
    echo "[+] PostgreSQL now listens on all interfaces"
fi

# ── 6. Open firewall port for PostgreSQL ─────────────────────────────────────
ufw allow 5432/tcp && ufw reload || true

# ── 7. Write DATABASE_URL into .env ──────────────────────────────────────────
APP_ENV="$(dirname "$0")/.env"
if grep -q "^DATABASE_URL=" "$APP_ENV"; then
    sed -i "s|^DATABASE_URL=.*|DATABASE_URL=postgresql+asyncpg://$DB_USER:$DB_PASSWORD@127.0.0.1:5432/$DB_NAME|" "$APP_ENV"
else
    echo "DATABASE_URL=postgresql+asyncpg://$DB_USER:$DB_PASSWORD@127.0.0.1:5432/$DB_NAME" >> "$APP_ENV"
fi

echo ""
echo "=== Setup complete ==="
echo ""
echo "  Host:     $(hostname -I | awk '{print $1}')"
echo "  Port:     5432"
echo "  Database: $DB_NAME"
echo "  User:     $DB_USER"
echo "  Password: $DB_PASSWORD"
echo ""
echo "Use the above to connect from DBeaver / TablePlus / pgAdmin."
echo "Credentials also saved in: $ENV_FILE"
