# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

GroupQuest-App is a social challenge platform where users create and join time-boxed challenges, track progress via check-ins, and earn points/badges. Built with Python, Streamlit (frontend), and SQLite (backend). Deployed via Streamlit Community Cloud (streamlit.io).

## Tech Stack & Constraints

- **Frontend**: Streamlit
- **Database**: SQLite (no external services — all data stays local in a `.db` file)
- **Deployment**: GitHub → Streamlit Community Cloud
- **No external APIs or storage services**

## Running the App

```bash
# Install dependencies
pip install -r requirements.txt

# Run locally
streamlit run app.py
```

## Project Structure (expected)

```
app.py              # Streamlit entry point
database.py         # SQLite connection, schema init, all DB helpers
requirements.txt    # Python dependencies (streamlit, etc.)
```

## Architecture

The app follows a simple two-layer structure:

- **`database.py`** owns all SQLite logic: schema creation, connection management, and query functions. Schema is initialized on first run via `CREATE TABLE IF NOT EXISTS`. No ORM — raw `sqlite3` with parameterized queries.
- **`app.py`** (and optional page modules) owns all Streamlit UI. It calls functions from `database.py` directly — no intermediate service layer.

Streamlit's session state (`st.session_state`) is used for login state and ephemeral UI state between reruns.

## Key Domain Concepts

- **Challenge**: has title, rules, duration, visibility (public/group), created by a user
- **Check-in**: a progress entry (text/photo) submitted by a participant for a challenge
- **Points / Badges / Level**: awarded based on check-in consistency and milestones
- **Leaderboard / Social Feed**: derived views over check-ins and points

## Development Workflow

This project uses SCRUM: work is tracked via GitHub Issues (user stories) and GitHub Projects (kanban). Features are tracked as GitHub Milestones.

Streamlit reruns the entire script on every interaction — design stateful flows using `st.session_state` and avoid expensive DB calls outside of conditional blocks.
