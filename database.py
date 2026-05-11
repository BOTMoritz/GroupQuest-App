import base64
import secrets
import pymysql
import pymysql.cursors
import bcrypt
import streamlit as st
from datetime import datetime, timedelta


@st.cache_resource
def _open_connection():
    cfg = st.secrets["tidb"]
    ssl = {"ca": cfg["ssl_ca"]} if cfg.get("ssl_ca") else None
    return pymysql.connect(
        host=cfg["host"],
        port=int(cfg["port"]),
        user=cfg["user"],
        password=cfg["password"],
        database=cfg["database"],
        ssl=ssl,
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def get_connection():
    conn = _open_connection()
    try:
        conn.ping(reconnect=True)
    except Exception:
        _open_connection.clear()
        conn = _open_connection()
    return conn


def init_db():
    cfg = st.secrets["tidb"]
    ssl = {"ca": cfg["ssl_ca"]} if cfg.get("ssl_ca") else None
    db_name = cfg["database"]

    bootstrap = pymysql.connect(
        host=cfg["host"],
        port=int(cfg["port"]),
        user=cfg["user"],
        password=cfg["password"],
        ssl=ssl,
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with bootstrap.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{db_name}`")
            cur.execute(f"USE `{db_name}`")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id            INT AUTO_INCREMENT PRIMARY KEY,
                    username      VARCHAR(50)  UNIQUE NOT NULL,
                    email         VARCHAR(100) UNIQUE NOT NULL,
                    password_hash VARCHAR(255) NOT NULL,
                    display_name  VARCHAR(100),
                    bio           VARCHAR(500),
                    avatar        LONGTEXT,
                    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Migrate existing tables that may not have the new columns
            for col_def in [
                "ADD COLUMN display_name VARCHAR(100)",
                "ADD COLUMN bio VARCHAR(500)",
                "ADD COLUMN avatar LONGTEXT",
            ]:
                try:
                    cur.execute(f"ALTER TABLE users {col_def}")
                except pymysql.err.OperationalError:
                    pass  # column already exists

            cur.execute("""
                CREATE TABLE IF NOT EXISTS password_reset_tokens (
                    id         INT AUTO_INCREMENT PRIMARY KEY,
                    user_id    INT NOT NULL,
                    token      VARCHAR(64) UNIQUE NOT NULL,
                    expires_at TIMESTAMP NOT NULL,
                    used       TINYINT(1) DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                )
            """)
    finally:
        bootstrap.close()


# --- User helpers ---

def create_user(username: str, email: str, password: str) -> None:
    """Insert a new user. Raises pymysql.IntegrityError on duplicate username/email."""
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (username, email, password_hash) VALUES (%s, %s, %s)",
            (username, email, pw_hash),
        )


def verify_login(username_or_email: str, password: str):
    """Return user dict {id, username, email} on success, None on failure. Accepts username or email."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, username, email, password_hash FROM users WHERE username = %s OR email = %s",
            (username_or_email, username_or_email),
        )
        user = cur.fetchone()
    if user and bcrypt.checkpw(password.encode(), user["password_hash"].encode()):
        return {"id": user["id"], "username": user["username"], "email": user["email"]}
    return None


def get_user(user_id: int):
    """Return full user profile dict."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, username, email, display_name, bio, avatar FROM users WHERE id = %s",
            (user_id,),
        )
        return cur.fetchone()


def update_user_profile(user_id: int, username: str, email: str, display_name: str, bio: str) -> None:
    """Update profile fields. Raises pymysql.IntegrityError on duplicate username/email."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET username=%s, email=%s, display_name=%s, bio=%s WHERE id=%s",
            (username, email, display_name or None, bio or None, user_id),
        )
    conn.commit()


def update_password(user_id: int, current_password: str, new_password: str) -> bool:
    """Verify current password then update. Returns True on success."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT password_hash FROM users WHERE id = %s", (user_id,))
        user = cur.fetchone()
    if not user or not bcrypt.checkpw(current_password.encode(), user["password_hash"].encode()):
        return False
    new_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    with conn.cursor() as cur:
        cur.execute("UPDATE users SET password_hash=%s WHERE id=%s", (new_hash, user_id))
    conn.commit()
    return True


def update_avatar(user_id: int, image_bytes: bytes) -> None:
    """Store avatar as base64-encoded string. Pass empty bytes to remove."""
    b64 = base64.b64encode(image_bytes).decode() if image_bytes else None
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("UPDATE users SET avatar=%s WHERE id=%s", (b64, user_id))
    conn.commit()


def delete_user(user_id: int) -> None:
    """Permanently delete a user account."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM users WHERE id=%s", (user_id,))


# --- Password reset helpers ---

def create_reset_token(email: str):
    """
    Look up user by email, create a one-time reset token valid for 1 hour.
    Returns (token, username) on success, None if email not found.
    """
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT id, username FROM users WHERE email = %s", (email,))
        user = cur.fetchone()
    if not user:
        return None

    token = secrets.token_urlsafe(32)
    expires_at = datetime.utcnow() + timedelta(hours=1)
    with conn.cursor() as cur:
        # Invalidate any existing unused tokens for this user
        cur.execute(
            "UPDATE password_reset_tokens SET used=1 WHERE user_id=%s AND used=0",
            (user["id"],),
        )
        cur.execute(
            "INSERT INTO password_reset_tokens (user_id, token, expires_at) VALUES (%s, %s, %s)",
            (user["id"], token, expires_at),
        )
    return token, user["username"]


def validate_reset_token(token: str):
    """
    Check if token is valid (exists, not used, not expired).
    Returns user_id on success, None otherwise.
    """
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT user_id, expires_at, used
            FROM password_reset_tokens
            WHERE token = %s
            """,
            (token,),
        )
        row = cur.fetchone()
    if not row:
        return None
    if row["used"]:
        return None
    if row["expires_at"] < datetime.utcnow():
        return None
    return row["user_id"]


def use_reset_token(token: str, new_password: str) -> bool:
    """
    Set new password and mark token as used.
    Returns True on success, False if token is invalid/expired.
    """
    user_id = validate_reset_token(token)
    if not user_id:
        return False
    new_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("UPDATE users SET password_hash=%s WHERE id=%s", (new_hash, user_id))
        cur.execute("UPDATE password_reset_tokens SET used=1 WHERE token=%s", (token,))
    conn.commit()
    return True
