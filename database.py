import base64
import json
import os
import secrets
import pymysql
import pymysql.cursors
import bcrypt
import streamlit as st
from datetime import datetime, timedelta


def _build_ssl(cfg) -> dict | None:
    ca = cfg.get("ssl_ca", "")
    if ca and os.path.exists(ca):
        return {"ca": ca}
    # TiDB Cloud requires TLS — enable it without pinning a specific CA file
    # so the OS trust store is used (works on macOS and Linux)
    return {}


@st.cache_resource
def _open_connection():
    cfg = st.secrets["tidb"]
    return pymysql.connect(
        host=cfg["host"],
        port=int(cfg["port"]),
        user=cfg["user"],
        password=cfg["password"],
        database=cfg["database"],
        ssl=_build_ssl(cfg),
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
    db_name = cfg["database"]

    bootstrap = pymysql.connect(
        host=cfg["host"],
        port=int(cfg["port"]),
        user=cfg["user"],
        password=cfg["password"],
        ssl=_build_ssl(cfg),
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
                "ADD COLUMN preferences TEXT",
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
            cur.execute("""
                CREATE TABLE IF NOT EXISTS challenges (
                    id           INT AUTO_INCREMENT PRIMARY KEY,
                    user_id      INT NOT NULL,
                    title        VARCHAR(200) NOT NULL,
                    description  TEXT,
                    rules        TEXT,
                    priority     ENUM('low', 'medium', 'high') DEFAULT 'medium',
                    group_name   VARCHAR(100),
                    status       ENUM('active', 'completed', 'paused') DEFAULT 'active',
                    deadline     DATETIME,
                    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMP NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                )
            """)
            # Migrate deadline column from DATE to DATETIME if needed
            try:
                cur.execute("ALTER TABLE challenges MODIFY COLUMN deadline DATETIME")
            except pymysql.err.OperationalError:
                pass
            cur.execute("""
                CREATE TABLE IF NOT EXISTS checkins (
                    id           INT AUTO_INCREMENT PRIMARY KEY,
                    challenge_id INT NOT NULL,
                    user_id      INT NOT NULL,
                    text         TEXT,
                    photo        LONGTEXT,
                    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (challenge_id) REFERENCES challenges(id) ON DELETE CASCADE,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_sessions (
                    id         INT AUTO_INCREMENT PRIMARY KEY,
                    token      VARCHAR(64) UNIQUE NOT NULL,
                    user_id    INT NOT NULL,
                    expires_at TIMESTAMP NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                )
            """)
            _init_gamification_tables(cur)
            _init_notification_tables(cur)
            _init_community_tables(cur)
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


# --- Challenge helpers ---

def create_challenge(user_id: int, title: str, description: str, rules: str,
                     priority: str, group_name: str, deadline,
                     visibility: str = 'private') -> None:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO challenges
               (user_id, title, description, rules, priority, group_name, deadline, visibility)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (user_id, title, description or None, rules or None,
             priority, group_name or None, deadline or None, visibility),
        )


def get_challenges(user_id: int, status=None, priority=None, group_name=None, search=None):
    conn = get_connection()
    query = """
        SELECT c.*,
               (SELECT COUNT(*) FROM checkins WHERE challenge_id = c.id) AS checkin_count
        FROM challenges c
        WHERE c.user_id = %s
    """
    params = [user_id]
    if status:
        query += " AND c.status = %s"
        params.append(status)
    if priority:
        query += " AND c.priority = %s"
        params.append(priority)
    if group_name:
        query += " AND c.group_name = %s"
        params.append(group_name)
    if search:
        query += " AND (c.title LIKE %s OR c.description LIKE %s)"
        params.extend([f"%{search}%", f"%{search}%"])
    query += " ORDER BY FIELD(c.priority,'high','medium','low'), c.created_at DESC"
    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()


def get_challenge(challenge_id: int):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM challenges WHERE id = %s", (challenge_id,))
        return cur.fetchone()


def update_challenge(challenge_id: int, user_id: int, title: str, description: str,
                     rules: str, priority: str, group_name: str, deadline, status: str,
                     visibility: str = 'private') -> None:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE challenges
               SET title=%s, description=%s, rules=%s, priority=%s,
                   group_name=%s, deadline=%s, status=%s, visibility=%s,
                   completed_at=IF(status != 'completed' AND %s = 'completed', NOW(), completed_at)
               WHERE id=%s AND user_id=%s""",
            (title, description or None, rules or None, priority,
             group_name or None, deadline or None, status, visibility,
             status, challenge_id, user_id),
        )


def delete_challenge(challenge_id: int, user_id: int) -> None:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM challenges WHERE id=%s AND user_id=%s", (challenge_id, user_id))


def complete_challenge(challenge_id: int, user_id: int) -> None:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE challenges SET status='completed', completed_at=NOW() WHERE id=%s AND user_id=%s",
            (challenge_id, user_id),
        )


def duplicate_challenge(challenge_id: int, user_id: int) -> None:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT title, description, rules, priority, group_name, deadline FROM challenges WHERE id=%s AND user_id=%s",
            (challenge_id, user_id),
        )
        ch = cur.fetchone()
    if not ch:
        return
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO challenges
               (user_id, title, description, rules, priority, group_name, deadline, visibility)
               VALUES (%s, %s, %s, %s, %s, %s, %s, 'private')""",
            (user_id, f"{ch['title']} (Kopie)", ch['description'], ch['rules'],
             ch['priority'], ch['group_name'], ch['deadline']),
        )


def get_challenge_groups(user_id: int):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT group_name FROM challenges WHERE user_id=%s AND group_name IS NOT NULL ORDER BY group_name",
            (user_id,),
        )
        return [row['group_name'] for row in cur.fetchall()]


# --- Check-in helpers ---

def create_checkin(challenge_id: int, user_id: int, text: str, photo_bytes: bytes) -> None:
    photo_b64 = base64.b64encode(photo_bytes).decode() if photo_bytes else None
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO checkins (challenge_id, user_id, text, photo) VALUES (%s, %s, %s, %s)",
            (challenge_id, user_id, text or None, photo_b64),
        )


def get_checkins(challenge_id: int, limit: int = None):
    conn = get_connection()
    query = "SELECT * FROM checkins WHERE challenge_id = %s ORDER BY created_at DESC"
    if limit:
        query += f" LIMIT {int(limit)}"
    with conn.cursor() as cur:
        cur.execute(query, (challenge_id,))
        return cur.fetchall()


def get_recent_checkins(user_id: int, limit: int = 10):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT ci.*, c.title AS challenge_title
               FROM checkins ci
               JOIN challenges c ON ci.challenge_id = c.id
               WHERE c.user_id = %s
               ORDER BY ci.created_at DESC
               LIMIT %s""",
            (user_id, limit),
        )
        return cur.fetchall()


def get_daily_checkins(user_id: int, days: int = 30):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT DATE(ci.created_at) AS date, COUNT(*) AS count
               FROM checkins ci
               JOIN challenges c ON ci.challenge_id = c.id
               WHERE c.user_id = %s AND ci.created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
               GROUP BY DATE(ci.created_at)
               ORDER BY date""",
            (user_id, days),
        )
        return cur.fetchall()


def get_checkins_per_challenge(user_id: int):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT c.title, COUNT(ci.id) AS count
               FROM challenges c
               LEFT JOIN checkins ci ON c.id = ci.challenge_id
               WHERE c.user_id = %s
               GROUP BY c.id, c.title
               ORDER BY count DESC
               LIMIT 10""",
            (user_id,),
        )
        return cur.fetchall()


def get_checkin_totals(user_id: int):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT
               (SELECT COUNT(*) FROM checkins ci
                JOIN challenges c ON ci.challenge_id = c.id
                WHERE c.user_id = %s) AS total_checkins,
               (SELECT COUNT(*) FROM challenges WHERE user_id = %s AND status = 'active') AS active_challenges,
               (SELECT COUNT(*) FROM challenges WHERE user_id = %s AND status = 'completed') AS completed_challenges""",
            (user_id, user_id, user_id),
        )
        return cur.fetchone()


# --- User preferences helpers ---

def get_preferences(user_id: int) -> dict:
    """Return user preferences dict, or empty dict if none stored."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT preferences FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
    if row and row.get("preferences"):
        try:
            return json.loads(row["preferences"])
        except Exception:
            return {}
    return {}


def save_preferences(user_id: int, prefs: dict) -> None:
    """Persist user preferences as JSON."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET preferences = %s WHERE id = %s",
            (json.dumps(prefs), user_id),
        )


# --- Deadline notification helpers ---

def _init_notification_tables(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS notification_dismissals (
            id             INT AUTO_INCREMENT PRIMARY KEY,
            user_id        INT NOT NULL,
            challenge_id   INT NOT NULL,
            days_threshold INT NOT NULL,
            dismissed_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY unique_dismissal (user_id, challenge_id, days_threshold),
            FOREIGN KEY (user_id)      REFERENCES users(id)      ON DELETE CASCADE,
            FOREIGN KEY (challenge_id) REFERENCES challenges(id) ON DELETE CASCADE
        )
    """)


def get_active_notifications(user_id: int, notify_days: list) -> list:
    """
    Return due, non-dismissed deadline notifications.

    For each active challenge whose deadline falls within the user's configured
    thresholds, compute the smallest covering threshold and exclude any already
    dismissed by the user.  Returns list of dicts with keys:
        id, title, deadline, days_remaining, threshold
    """
    if not notify_days:
        return []
    thresholds = sorted(notify_days)
    max_days = max(thresholds)

    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT c.id, c.title, c.deadline,
                      DATEDIFF(DATE(c.deadline), CURDATE()) AS days_remaining
               FROM challenges c
               WHERE c.user_id = %s
                 AND c.status = 'active'
                 AND c.deadline IS NOT NULL
                 AND DATE(c.deadline) BETWEEN CURDATE() AND DATE_ADD(CURDATE(), INTERVAL %s DAY)
               ORDER BY c.deadline""",
            (user_id, max_days),
        )
        rows = cur.fetchall()

        # Fetch all dismissals for this user in one query
        cur.execute(
            "SELECT challenge_id, days_threshold FROM notification_dismissals WHERE user_id = %s",
            (user_id,),
        )
        dismissed = {(r["challenge_id"], r["days_threshold"]) for r in cur.fetchall()}

    result = []
    for row in rows:
        days_rem = int(row["days_remaining"])
        threshold = next((t for t in thresholds if days_rem <= t), None)
        if threshold is None:
            continue
        if (row["id"], threshold) in dismissed:
            continue
        result.append({**row, "days_remaining": days_rem, "threshold": threshold})
    return result


def dismiss_notification(user_id: int, challenge_id: int, days_threshold: int) -> None:
    """Mark a deadline notification as dismissed (idempotent)."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO notification_dismissals (user_id, challenge_id, days_threshold)
               VALUES (%s, %s, %s)
               ON DUPLICATE KEY UPDATE dismissed_at = NOW()""",
            (user_id, challenge_id, days_threshold),
        )


# --- Gamification constants ---

LEVEL_THRESHOLDS = [
    (5, 1500, "Legende",   "🏆"),
    (4, 700,  "Meister",   "🎖️"),
    (3, 300,  "Kämpfer",   "⚔️"),
    (2, 100,  "Entdecker", "🔍"),
    (1, 0,    "Anfänger",  "🌱"),
]

BADGE_DEFINITIONS = {
    "first_checkin":   ("🎯", "Erster Schritt",      "Deinen ersten Check-in erstellt"),
    "photo_checkin":   ("📸", "Fotograf",             "Einen Check-in mit Foto erstellt"),
    "checkins_10":     ("📝", "Produktiv",            "10 Check-ins erstellt"),
    "checkins_50":     ("⚡", "Ausdauer",             "50 Check-ins erstellt"),
    "streak_7":        ("🔥", "Wochenkämpfer",        "7 Tage in Folge eingecheckt"),
    "first_complete":  ("✅", "Abschlussmacher",      "Eine Challenge abgeschlossen"),
    "challenges_3":    ("🏅", "Seriöser Challenger",  "3 Challenges abgeschlossen"),
    "multi_challenge": ("🌟", "Allrounder",           "In 3 verschiedenen Challenges eingecheckt"),
}

ACTION_LABELS = {
    "checkin":            "Check-in erstellt",
    "checkin_photo":      "Check-in mit Foto",
    "challenge_complete": "Challenge abgeschlossen",
    "streak_7":           "7-Tage-Streak",
}


def _init_community_tables(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS community_groups (
            id          INT AUTO_INCREMENT PRIMARY KEY,
            name        VARCHAR(100) NOT NULL,
            description TEXT,
            created_by  INT NOT NULL,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS group_members (
            id        INT AUTO_INCREMENT PRIMARY KEY,
            group_id  INT NOT NULL,
            user_id   INT NOT NULL,
            role      ENUM('admin', 'member') DEFAULT 'member',
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY unique_group_member (group_id, user_id),
            FOREIGN KEY (group_id) REFERENCES community_groups(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS challenge_participants (
            id           INT AUTO_INCREMENT PRIMARY KEY,
            challenge_id INT NOT NULL,
            user_id      INT NOT NULL,
            joined_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY unique_participant (challenge_id, user_id),
            FOREIGN KEY (challenge_id) REFERENCES challenges(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS checkin_likes (
            id         INT AUTO_INCREMENT PRIMARY KEY,
            checkin_id INT NOT NULL,
            user_id    INT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY unique_like (checkin_id, user_id),
            FOREIGN KEY (checkin_id) REFERENCES checkins(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS checkin_comments (
            id         INT AUTO_INCREMENT PRIMARY KEY,
            checkin_id INT NOT NULL,
            user_id    INT NOT NULL,
            text       TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (checkin_id) REFERENCES checkins(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pinned_challenges (
            id           INT AUTO_INCREMENT PRIMARY KEY,
            user_id      INT NOT NULL,
            challenge_id INT NOT NULL,
            scope        ENUM('personal', 'community') DEFAULT 'personal',
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY unique_pin (user_id, challenge_id, scope),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (challenge_id) REFERENCES challenges(id) ON DELETE CASCADE
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id           INT AUTO_INCREMENT PRIMARY KEY,
            reporter_id  INT NOT NULL,
            content_type VARCHAR(20) NOT NULL,
            content_id   INT NOT NULL,
            reason       TEXT,
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (reporter_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    # Migrate challenges: add visibility column
    try:
        cur.execute(
            "ALTER TABLE challenges ADD COLUMN visibility ENUM('private','public') DEFAULT 'private'"
        )
    except pymysql.err.OperationalError:
        pass


def _init_gamification_tables(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_points (
            id           INT AUTO_INCREMENT PRIMARY KEY,
            user_id      INT NOT NULL,
            action_type  VARCHAR(50) NOT NULL,
            points       INT NOT NULL,
            reference_id INT,
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_badges (
            id         INT AUTO_INCREMENT PRIMARY KEY,
            user_id    INT NOT NULL,
            badge_key  VARCHAR(50) NOT NULL,
            earned_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY unique_user_badge (user_id, badge_key),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)


def award_points(user_id: int, action_type: str, points: int, reference_id: int = None) -> None:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO user_points (user_id, action_type, points, reference_id) VALUES (%s, %s, %s, %s)",
            (user_id, action_type, points, reference_id),
        )


def get_total_points(user_id: int) -> int:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT COALESCE(SUM(points), 0) AS total FROM user_points WHERE user_id = %s", (user_id,))
        return int(cur.fetchone()["total"])


def get_level_info(points: int) -> dict:
    """Return dict with level, name, emoji, current_min, next_min, progress_pct."""
    for level, threshold, name, emoji in LEVEL_THRESHOLDS:
        if points >= threshold:
            idx = LEVEL_THRESHOLDS.index((level, threshold, name, emoji))
            if idx == 0:
                return {"level": level, "name": name, "emoji": emoji,
                        "current_min": threshold, "next_min": None, "progress_pct": 100}
            next_threshold = LEVEL_THRESHOLDS[idx - 1][1]
            span = next_threshold - threshold
            progress = min(100, int((points - threshold) / span * 100))
            return {"level": level, "name": name, "emoji": emoji,
                    "current_min": threshold, "next_min": next_threshold, "progress_pct": progress}
    return {"level": 1, "name": "Anfänger", "emoji": "🌱",
            "current_min": 0, "next_min": 100, "progress_pct": 0}


def get_user_badges(user_id: int) -> list:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT badge_key, earned_at FROM user_badges WHERE user_id = %s ORDER BY earned_at",
            (user_id,),
        )
        return cur.fetchall()


def award_badge(user_id: int, badge_key: str) -> bool:
    """Award badge if not already earned. Returns True if newly awarded."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO user_badges (user_id, badge_key) VALUES (%s, %s)",
                (user_id, badge_key),
            )
        return True
    except pymysql.err.IntegrityError:
        return False


def check_and_award_badges(user_id: int) -> list:
    """Check all badge conditions and award new ones. Returns list of newly awarded badge_keys."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT badge_key FROM user_badges WHERE user_id = %s", (user_id,))
        earned = {row["badge_key"] for row in cur.fetchall()}

        cur.execute(
            """SELECT
               COUNT(*) AS total_checkins,
               SUM(CASE WHEN ci.photo IS NOT NULL THEN 1 ELSE 0 END) AS photo_checkins
               FROM checkins ci
               JOIN challenges c ON ci.challenge_id = c.id
               WHERE c.user_id = %s""",
            (user_id,),
        )
        stats = cur.fetchone()
        total_checkins = int(stats["total_checkins"] or 0)
        photo_checkins = int(stats["photo_checkins"] or 0)

        cur.execute(
            "SELECT COUNT(*) AS cnt FROM challenges WHERE user_id = %s AND status = 'completed'",
            (user_id,),
        )
        completed_challenges = int(cur.fetchone()["cnt"])

        cur.execute(
            """SELECT COUNT(DISTINCT ci.challenge_id) AS cnt
               FROM checkins ci
               JOIN challenges c ON ci.challenge_id = c.id
               WHERE c.user_id = %s""",
            (user_id,),
        )
        challenges_with_checkins = int(cur.fetchone()["cnt"])

    streak = get_current_streak(user_id)

    conditions = {
        "first_checkin":   total_checkins >= 1,
        "photo_checkin":   photo_checkins >= 1,
        "checkins_10":     total_checkins >= 10,
        "checkins_50":     total_checkins >= 50,
        "streak_7":        streak >= 7,
        "first_complete":  completed_challenges >= 1,
        "challenges_3":    completed_challenges >= 3,
        "multi_challenge": challenges_with_checkins >= 3,
    }

    newly_awarded = []
    for badge_key, condition in conditions.items():
        if condition and badge_key not in earned:
            if award_badge(user_id, badge_key):
                newly_awarded.append(badge_key)
    return newly_awarded


def get_current_streak(user_id: int) -> int:
    """Return number of consecutive days up to today with at least one check-in."""
    from datetime import date, timedelta
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT DATE(ci.created_at) AS day
               FROM checkins ci
               JOIN challenges c ON ci.challenge_id = c.id
               WHERE c.user_id = %s
               ORDER BY day DESC
               LIMIT 60""",
            (user_id,),
        )
        days = [row["day"] for row in cur.fetchall()]

    if not days:
        return 0

    today = date.today()
    # Streak starts only if user checked in today or yesterday
    if days[0] < today - timedelta(days=1):
        return 0

    streak = 0
    expected = days[0]
    for day in days:
        if day == expected:
            streak += 1
            expected -= timedelta(days=1)
        else:
            break
    return streak


def get_points_history(user_id: int, limit: int = 20) -> list:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT action_type, points, created_at
               FROM user_points WHERE user_id = %s
               ORDER BY created_at DESC LIMIT %s""",
            (user_id, limit),
        )
        return cur.fetchall()


# --- Community: Groups ---

def create_group(name: str, description: str, created_by: int) -> int:
    """Create a new group, add creator as admin. Returns group id."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO community_groups (name, description, created_by) VALUES (%s, %s, %s)",
            (name, description or None, created_by),
        )
        group_id = cur.lastrowid
        cur.execute(
            "INSERT INTO group_members (group_id, user_id, role) VALUES (%s, %s, 'admin')",
            (group_id, created_by),
        )
    return group_id


def get_groups_for_user(user_id: int) -> list:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT g.*, gm.role,
               (SELECT COUNT(*) FROM group_members WHERE group_id = g.id) AS member_count
               FROM community_groups g
               JOIN group_members gm ON g.id = gm.group_id
               WHERE gm.user_id = %s
               ORDER BY g.name""",
            (user_id,),
        )
        return cur.fetchall()


def get_all_groups(search=None) -> list:
    conn = get_connection()
    query = """
        SELECT g.*,
               (SELECT COUNT(*) FROM group_members WHERE group_id = g.id) AS member_count,
               u.username AS creator_name
        FROM community_groups g
        JOIN users u ON g.created_by = u.id
    """
    params = []
    if search:
        query += " WHERE g.name LIKE %s OR g.description LIKE %s"
        params.extend([f"%{search}%", f"%{search}%"])
    query += " ORDER BY member_count DESC, g.name"
    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()


def join_group(group_id: int, user_id: int) -> bool:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO group_members (group_id, user_id, role) VALUES (%s, %s, 'member')",
                (group_id, user_id),
            )
        return True
    except pymysql.err.IntegrityError:
        return False


def leave_group(group_id: int, user_id: int) -> None:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM group_members WHERE group_id=%s AND user_id=%s",
            (group_id, user_id),
        )


def is_group_member(group_id: int, user_id: int) -> bool:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM group_members WHERE group_id=%s AND user_id=%s",
            (group_id, user_id),
        )
        return cur.fetchone() is not None


def get_group_members(group_id: int) -> list:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT u.id, u.username, u.display_name, gm.role, gm.joined_at
               FROM group_members gm
               JOIN users u ON gm.user_id = u.id
               WHERE gm.group_id = %s
               ORDER BY gm.role DESC, u.username""",
            (group_id,),
        )
        return cur.fetchall()


def delete_group(group_id: int, user_id: int) -> bool:
    """Delete a group. Only the admin may do this. Returns True on success."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT role FROM group_members WHERE group_id=%s AND user_id=%s",
            (group_id, user_id),
        )
        row = cur.fetchone()
        if not row or row["role"] != "admin":
            return False
        cur.execute("DELETE FROM community_groups WHERE id=%s", (group_id,))
    return True


# --- Community: Public Challenges ---

def get_public_challenges(search=None, limit: int = 50) -> list:
    conn = get_connection()
    query = """
        SELECT c.*, u.username AS creator_name,
               COUNT(DISTINCT ci.id) AS checkin_count,
               COUNT(DISTINCT cp.id) AS participant_count
        FROM challenges c
        JOIN users u ON c.user_id = u.id
        LEFT JOIN checkins ci ON ci.challenge_id = c.id
        LEFT JOIN challenge_participants cp ON cp.challenge_id = c.id
        WHERE c.visibility = 'public' AND c.status = 'active'
    """
    params = []
    if search:
        query += " AND (c.title LIKE %s OR c.description LIKE %s)"
        params.extend([f"%{search}%", f"%{search}%"])
    query += " GROUP BY c.id, u.id, u.username ORDER BY participant_count DESC, c.created_at DESC LIMIT %s"
    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()


def join_challenge_as_participant(challenge_id: int, user_id: int) -> bool:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO challenge_participants (challenge_id, user_id) VALUES (%s, %s)",
                (challenge_id, user_id),
            )
        return True
    except pymysql.err.IntegrityError:
        return False


def leave_challenge_participation(challenge_id: int, user_id: int) -> None:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM challenge_participants WHERE challenge_id=%s AND user_id=%s",
            (challenge_id, user_id),
        )


def is_challenge_participant(challenge_id: int, user_id: int) -> bool:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM challenge_participants WHERE challenge_id=%s AND user_id=%s",
            (challenge_id, user_id),
        )
        return cur.fetchone() is not None


# --- Community: Social Feed ---

def get_community_feed(user_id: int, limit: int = 30) -> list:
    """Recent check-ins from public challenges, with like/comment counts."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT ci.id, ci.user_id AS checkin_user_id, ci.text, ci.photo, ci.created_at,
                      u.username, u.display_name,
                      c.title AS challenge_title, c.id AS challenge_id,
                      COUNT(DISTINCT cl.id) AS like_count,
                      COUNT(DISTINCT cc.id) AS comment_count,
                      MAX(CASE WHEN my_like.user_id IS NOT NULL THEN 1 ELSE 0 END) AS liked_by_me
               FROM checkins ci
               JOIN challenges c ON ci.challenge_id = c.id
               JOIN users u ON ci.user_id = u.id
               LEFT JOIN checkin_likes cl ON cl.checkin_id = ci.id
               LEFT JOIN checkin_comments cc ON cc.checkin_id = ci.id
               LEFT JOIN checkin_likes my_like ON my_like.checkin_id = ci.id AND my_like.user_id = %s
               WHERE c.visibility = 'public'
               GROUP BY ci.id, ci.user_id, ci.text, ci.photo, ci.created_at,
                        u.username, u.display_name, c.id, c.title
               ORDER BY ci.created_at DESC
               LIMIT %s""",
            (user_id, limit),
        )
        return cur.fetchall()


# --- Community: Likes & Comments ---

def toggle_like(checkin_id: int, user_id: int) -> bool:
    """Toggle like on a check-in. Returns True if now liked, False if unliked."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO checkin_likes (checkin_id, user_id) VALUES (%s, %s)",
                (checkin_id, user_id),
            )
        return True
    except pymysql.err.IntegrityError:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM checkin_likes WHERE checkin_id=%s AND user_id=%s",
                (checkin_id, user_id),
            )
        return False


def get_comments(checkin_id: int) -> list:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT cc.id, cc.user_id, cc.text, cc.created_at, u.username, u.display_name
               FROM checkin_comments cc
               JOIN users u ON cc.user_id = u.id
               WHERE cc.checkin_id = %s
               ORDER BY cc.created_at ASC""",
            (checkin_id,),
        )
        return cur.fetchall()


def add_comment(checkin_id: int, user_id: int, text: str) -> None:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO checkin_comments (checkin_id, user_id, text) VALUES (%s, %s, %s)",
            (checkin_id, user_id, text),
        )


def delete_comment(comment_id: int, user_id: int) -> None:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM checkin_comments WHERE id=%s AND user_id=%s",
            (comment_id, user_id),
        )


# --- Community: Leaderboard ---

def get_leaderboard(limit: int = 20) -> list:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT u.id, u.username, u.display_name,
                      COALESCE(SUM(up.points), 0) AS total_points
               FROM users u
               LEFT JOIN user_points up ON u.id = up.user_id
               GROUP BY u.id
               ORDER BY total_points DESC
               LIMIT %s""",
            (limit,),
        )
        return cur.fetchall()


# --- Community: Pinned Challenges ---

def pin_challenge(user_id: int, challenge_id: int, scope: str = 'personal') -> bool:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO pinned_challenges (user_id, challenge_id, scope) VALUES (%s, %s, %s)",
                (user_id, challenge_id, scope),
            )
        return True
    except pymysql.err.IntegrityError:
        return False


def unpin_challenge(user_id: int, challenge_id: int, scope: str = 'personal') -> None:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM pinned_challenges WHERE user_id=%s AND challenge_id=%s AND scope=%s",
            (user_id, challenge_id, scope),
        )


def get_pinned_challenges(user_id: int, scope: str = 'personal') -> list:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT c.*, u.username AS creator_name, pc.created_at AS pinned_at
               FROM pinned_challenges pc
               JOIN challenges c ON pc.challenge_id = c.id
               JOIN users u ON c.user_id = u.id
               WHERE pc.user_id = %s AND pc.scope = %s
               ORDER BY pc.created_at DESC""",
            (user_id, scope),
        )
        return cur.fetchall()


def get_community_pinned_challenges() -> list:
    """Challenges most pinned to the community board."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT c.*, u.username AS creator_name, COUNT(pc.id) AS pin_count
               FROM pinned_challenges pc
               JOIN challenges c ON pc.challenge_id = c.id
               JOIN users u ON c.user_id = u.id
               WHERE pc.scope = 'community' AND c.visibility = 'public'
               GROUP BY c.id, u.id, u.username
               ORDER BY pin_count DESC
               LIMIT 20"""
        )
        return cur.fetchall()


def get_user_participated_challenge_ids(user_id: int) -> set:
    """Return set of challenge IDs the user participates in."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT challenge_id FROM challenge_participants WHERE user_id = %s",
            (user_id,),
        )
        return {row["challenge_id"] for row in cur.fetchall()}


def get_user_group_ids(user_id: int) -> set:
    """Return set of group IDs the user is a member of."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT group_id FROM group_members WHERE user_id = %s",
            (user_id,),
        )
        return {row["group_id"] for row in cur.fetchall()}


def get_user_pinned_challenge_ids(user_id: int, scope: str = 'personal') -> set:
    """Return set of challenge IDs pinned by user for a given scope."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT challenge_id FROM pinned_challenges WHERE user_id = %s AND scope = %s",
            (user_id, scope),
        )
        return {row["challenge_id"] for row in cur.fetchall()}


def is_pinned(user_id: int, challenge_id: int, scope: str = 'personal') -> bool:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pinned_challenges WHERE user_id=%s AND challenge_id=%s AND scope=%s",
            (user_id, challenge_id, scope),
        )
        return cur.fetchone() is not None


# --- Community: Reports ---

def create_report(reporter_id: int, content_type: str, content_id: int, reason: str) -> None:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO reports (reporter_id, content_type, content_id, reason) VALUES (%s, %s, %s, %s)",
            (reporter_id, content_type, content_id, reason or None),
        )


# --- Session tokens (persistent login) ---

def create_session_token(user_id: int) -> str:
    """Create a 30-day session token for the given user and return it."""
    token = secrets.token_hex(32)
    expires_at = datetime.utcnow() + timedelta(days=30)
    conn = get_connection()
    with conn.cursor() as cur:
        # Clean up expired tokens for this user
        cur.execute("DELETE FROM user_sessions WHERE user_id=%s AND expires_at < NOW()", (user_id,))
        cur.execute(
            "INSERT INTO user_sessions (token, user_id, expires_at) VALUES (%s, %s, %s)",
            (token, user_id, expires_at),
        )
    return token


def validate_session_token(token: str):
    """Return user dict {id, username, email} if token is valid and not expired, else None."""
    if not token:
        return None
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT u.id, u.username, u.email
               FROM user_sessions s
               JOIN users u ON s.user_id = u.id
               WHERE s.token = %s AND s.expires_at > NOW()""",
            (token,),
        )
        return cur.fetchone()


def delete_session_token(token: str) -> None:
    """Remove a session token (logout)."""
    if not token:
        return
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM user_sessions WHERE token=%s", (token,))
