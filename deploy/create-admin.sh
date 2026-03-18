#!/bin/bash
# Create the initial platform admin user in Supabase.
# Run from the deploy/ directory after ./dev-up.sh
#
# Usage: ./create-admin.sh <email> [password]
# Example: ./create-admin.sh admin@example.com mypassword123

set -e
cd "$(dirname "$0")"

EMAIL="${1:?Usage: ./create-admin.sh <email> [password]}"
PASSWORD="${2:-changeme123}"

# Load env
source .env.supabase 2>/dev/null || true

GOTRUE_PORT="${GOTRUE_PORT:-54324}"
POSTGRES_PORT="${POSTGRES_PORT:-54322}"
DB="${POSTGRES_DB:-corridorkey}"

echo "Creating admin user: $EMAIL"

# Step 1: Create user via GoTrue signup (auto-confirmed via MAILER_AUTOCONFIRM=true)
RESPONSE=$(curl -s -X POST "http://localhost:${GOTRUE_PORT}/signup" \
  -H "Content-Type: application/json" \
  -d "{\"email\":\"${EMAIL}\",\"password\":\"${PASSWORD}\"}")

if echo "$RESPONSE" | grep -q "access_token"; then
  USER_ID=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['user']['id'])")
  echo "User created: $USER_ID"
else
  echo "Signup failed: $RESPONSE"
  echo ""
  echo "If the user already exists, this will set their tier to platform_admin anyway."
fi

# Step 2: Set platform_admin tier via SQL
docker exec -e PGPASSWORD="$POSTGRES_PASSWORD" deploy-supabase-db-1 \
  psql -U supabase_admin -d "$DB" -c \
  "UPDATE auth.users SET raw_app_meta_data = raw_app_meta_data || '{\"tier\": \"platform_admin\"}'::jsonb WHERE email = '${EMAIL}';" 2>&1

echo ""
echo "Admin user ready:"
echo "  Email:    $EMAIL"
echo "  Password: $PASSWORD"
echo "  Tier:     platform_admin"
echo ""
echo "Change your password after first login!"
