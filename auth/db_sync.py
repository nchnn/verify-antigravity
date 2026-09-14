# File: auth/db_sync.py
# Purpose: Sync verified Antigravity accounts directly with 9router SQLite database

import os
import json
import sqlite3
import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any
from pathlib import Path

from config.settings import get_settings

logger = logging.getLogger("AntigravityAuditor")


class NineRouterDbSync:
    """Manages synchronization of verified OAuth tokens with 9router's SQLite database."""

    def __init__(self, db_path: Optional[Path] = None):
        self.settings = get_settings()
        self.db_path = Path(db_path or self.settings.nine_router_db_path)

    def is_available(self) -> bool:
        """Returns True if the 9router database file exists and is accessible."""
        return self.db_path.exists() and self.db_path.is_file()

    def sync_account(
        self,
        email: str,
        access_token: str,
        refresh_token: str,
        project_id: str = "aicode-consumers",
        expires_in: int = 3599,
        additional_data: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Inserts or updates an Antigravity account in 9router's providerConnections table.
        Clears previous 401/403/429 errors and unlocks models immediately.
        """
        if not self.is_available():
            logger.warning(f"9router database not found at {self.db_path}. Skipping SQLite sync.")
            return False

        if not refresh_token:
            logger.warning(f"No refresh_token provided for {email}. Skipping 9router SQLite sync.")
            return False

        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(seconds=expires_in)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        now_iso = now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

        try:
            conn = sqlite3.connect(str(self.db_path))
            cursor = conn.cursor()

            # Ensure table exists
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='providerConnections'"
            )
            if not cursor.fetchone():
                logger.warning("Table 'providerConnections' does not exist in 9router database.")
                conn.close()
                return False

            # Check if record already exists
            cursor.execute(
                "SELECT id, data FROM providerConnections WHERE provider = 'antigravity' AND email = ?",
                (email,),
            )
            existing = cursor.fetchone()

            if existing:
                row_id, existing_data_str = existing
                try:
                    data = json.loads(existing_data_str) if existing_data_str else {}
                except Exception:
                    data = {}

                # Update OAuth fields
                data["accessToken"] = access_token
                data["refreshToken"] = refresh_token
                data["projectId"] = project_id or data.get("projectId") or "aicode-consumers"
                data["expiresAt"] = expires_at
                data["expiresIn"] = expires_in
                data["lastRefreshAt"] = now_iso
                data["errorCode"] = None
                data["lastError"] = None
                data["lastErrorAt"] = None
                data["rateLimitedUntil"] = None
                data["testStatus"] = "active"
                data["status"] = "active"
                data["backoffLevel"] = 0

                # Clear model lockouts
                for key in list(data.keys()):
                    if key.startswith("modelLock_"):
                        del data[key]

                if additional_data:
                    data.update(additional_data)

                cursor.execute(
                    "UPDATE providerConnections SET data = ?, updatedAt = ? WHERE id = ?",
                    (json.dumps(data), now_iso, row_id),
                )
                logger.info(f"Updated existing 9router providerConnection for {email}")
            else:
                new_id = f"ag-{uuid.uuid4().hex[:12]}"
                data = {
                    "accessToken": access_token,
                    "refreshToken": refresh_token,
                    "projectId": project_id or "aicode-consumers",
                    "expiresAt": expires_at,
                    "expiresIn": expires_in,
                    "lastRefreshAt": now_iso,
                    "errorCode": None,
                    "lastError": None,
                    "lastErrorAt": None,
                    "rateLimitedUntil": None,
                    "testStatus": "active",
                    "status": "active",
                    "backoffLevel": 0,
                }
                if additional_data:
                    data.update(additional_data)

                # Check table columns dynamically
                cursor.execute("PRAGMA table_info(providerConnections)")
                cols = {c[1] for c in cursor.fetchall()}

                insert_cols = ["id", "provider", "email", "data", "createdAt", "updatedAt"]
                values = [new_id, "antigravity", email, json.dumps(data), now_iso, now_iso]

                if "authType" in cols:
                    insert_cols.append("authType")
                    values.append("oauth")
                if "name" in cols:
                    insert_cols.append("name")
                    values.append(email)
                if "isActive" in cols:
                    insert_cols.append("isActive")
                    values.append(1)
                if "priority" in cols:
                    insert_cols.append("priority")
                    values.append(50)
                if "status" in cols:
                    insert_cols.append("status")
                    values.append("active")

                placeholders = ", ".join(["?"] * len(values))
                sql = f"INSERT INTO providerConnections ({', '.join(insert_cols)}) VALUES ({placeholders})"
                cursor.execute(sql, values)
                logger.info(f"Created new 9router providerConnection ({new_id}) for {email}")

            conn.commit()
            conn.close()
            return True

        except Exception as e:
            logger.error(f"Failed to sync {email} to 9router SQLite: {e}")
            return False
