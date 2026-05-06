# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

GroupQuest-App is a social challenge platform where users create and join time-boxed challenges, track progress via check-ins, and earn points/badges. Built with Python, Streamlit (frontend), and TiDB (backend). Deployed via Streamlit Community Cloud (streamlit.io).

## Tech Stack & Constraints

- **Frontend**: Streamlit
- **Database**: TiDB (MySQL-compatible distributed SQL, TiDB Cloud Serverless)
- **Deployment**: GitHub → Streamlit Community Cloud
- **DB credentials**: stored in `.streamlit/secrets.toml` locally; configured as Streamlit Cloud Secrets in production — never committed

## Running the App

```bash
# Install dependencies
pip install -r requirements.txt

# Create .streamlit/secrets.toml with TiDB connection details (see below)
streamlit run app.py
```

### TiDB connection secrets (`/.streamlit/secrets.toml`)

```toml
[tidb]
host     = "<your-tidb-host>"
port     = 4000
user     = "<your-user>"
password = "<your-password>"
database = "groupquest"
ssl_ca   = ""          # path to CA cert if required by TiDB Cloud
```

Access in code via `st.secrets["tidb"]`.

## Project Structure (expected)

```
app.py              # Streamlit entry point
database.py         # TiDB connection, schema init, all DB helpers
requirements.txt    # Python dependencies (streamlit, PyMySQL, etc.)
pages/              # Optional multi-page Streamlit modules
```

## Architecture

The app follows a simple two-layer structure:

- **`database.py`** owns all TiDB logic: connection management (via `PyMySQL`), schema creation, and query functions. Schema is initialized on first run via `CREATE TABLE IF NOT EXISTS`. No ORM — raw SQL with parameterized queries. Connections are opened per request using `st.cache_resource` for the connection pool.
- **`app.py`** (and optional `pages/` modules) owns all Streamlit UI. It calls functions from `database.py` directly — no intermediate service layer.

Streamlit's session state (`st.session_state`) is used for login state and ephemeral UI state between reruns.

## Key Domain Concepts

- **Challenge**: has title, rules, duration, visibility (public/group), created by a user; can be duplicated, prioritized, grouped, and filtered
- **Check-in**: a progress entry (text/photo) submitted by a participant for a challenge
- **Points / Badges / Level**: awarded based on check-in consistency and milestones
- **Leaderboard / Social Feed**: derived views over check-ins and points
- **Group**: a named collection of users who share challenges; users can create or join groups
- **Clipboard**: a personal and community pinboard for pinning active challenges and tasks
- **Admin**: privileged role for user management, moderation, blacklists, and usage statistics

## User Story Areas (GitHub Milestones)

| Area | Assigned |
|------|----------|
| Authentifizierung & Account | — |
| Challenge-Verwaltung | — |
| Fortschritt & Check-ins | Nico |
| Community & Sharing | Nico |
| Gamification | Jonas |
| Personalisierung & UX | Jonas |
| Benachrichtigungen | Jonas |
| Administration | — |
| Technisch / Sicherheit | — |

## Development Workflow

This project uses SCRUM: work is tracked via GitHub Issues (user stories) and GitHub Projects (kanban). Features are tracked as GitHub Milestones.

Streamlit reruns the entire script on every interaction — design stateful flows using `st.session_state` and avoid expensive DB calls outside of conditional blocks.
