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

# Load env (supports both unified .env and legacy .env.supabase)
source .env 2>/dev/null || true
source .env.supabase 2>/dev/null || true

GOTRUE_PORT="${GOTRUE_PORT:-54324}"
POSTGRES_PORT="${POSTGRES_PORT:-54322}"
DB="${POSTGRES_DB:-corridorkey}"

echo "Creating admin user: $EMAIL"

# Step 1: Create user via GoTrue admin API (works even with DISABLE_SIGNUP=true)
if [ -n "$SERVICE_ROLE_KEY" ]; then
  echo "Using GoTrue admin API (service role key)..."
  RESPONSE=$(curl -s -X POST "http://localhost:${GOTRUE_PORT}/admin/users" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${SERVICE_ROLE_KEY}" \
    -d "{\"email\":\"${EMAIL}\",\"password\":\"${PASSWORD}\",\"email_confirm\":true,\"app_metadata\":{\"tier\":\"platform_admin\"}}")

  if echo "$RESPONSE" | grep -q '"id"'; then
    USER_ID=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
    echo "User created with admin API: $USER_ID"
    echo ""
    echo "Admin user ready:"
    echo "  Email:    $EMAIL"
    echo "  Password: $PASSWORD"
    echo "  Tier:     platform_admin"
    echo ""
    echo "Change your password after first login!"
    exit 0
  elif echo "$RESPONSE" | grep -q "email_exists"; then
    echo "User already exists — updating tier via admin API..."
    # Get user ID by listing users
    USER_ID=$(curl -s "http://localhost:${GOTRUE_PORT}/admin/users" \
      -H "Authorization: Bearer ${SERVICE_ROLE_KEY}" \
      | python3 -c "
import sys, json
data = json.load(sys.stdin)
users = data.get('users', data) if isinstance(data, dict) else data
for u in users:
    if u.get('email') == '${EMAIL}':
        print(u['id'])
        break
" 2>/dev/null)
    if [ -n "$USER_ID" ]; then
      curl -s -X PUT "http://localhost:${GOTRUE_PORT}/admin/users/${USER_ID}" \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer ${SERVICE_ROLE_KEY}" \
        -d "{\"app_metadata\":{\"tier\":\"platform_admin\"}}" > /dev/null
      echo "Updated user $USER_ID to platform_admin"
      echo ""
      echo "Admin user ready:"
      echo "  Email:    $EMAIL"
      echo "  Tier:     platform_admin"
      echo ""
      exit 0
    fi
    echo "Could not find user ID — falling back to SQL..."
  else
    echo "Admin API failed: $RESPONSE"
    echo "Falling back to signup + SQL..."
  fi
fi

# Fallback: direct signup + SQL tier update
RESPONSE=$(curl -s -X POST "http://localhost:${GOTRUE_PORT}/signup" \
  -H "Content-Type: application/json" \
  -d "{\"email\":\"${EMAIL}\",\"password\":\"${PASSWORD}\"}")

if echo "$RESPONSE" | grep -q "access_token"; then
  USER_ID=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['user']['id'])")
  echo "User created via signup: $USER_ID"
else
  echo "Signup response: $RESPONSE"
  echo "If the user already exists, setting tier via SQL..."
fi

# Set platform_admin tier via SQL
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
