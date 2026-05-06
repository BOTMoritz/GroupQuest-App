import pymysql
import pymysql.cursors
import bcrypt
import streamlit as st


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

    # Connect without selecting a database so we can create it first
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
                    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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


def verify_login(username: str, password: str):
    """Return user dict {id, username, email} on success, None on failure."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, username, email, password_hash FROM users WHERE username = %s",
            (username,),
        )
        user = cur.fetchone()
    if user and bcrypt.checkpw(password.encode(), user["password_hash"].encode()):
        return {"id": user["id"], "username": user["username"], "email": user["email"]}
    return None
