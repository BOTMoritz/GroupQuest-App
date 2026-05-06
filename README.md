# GroupQuest-App

A social challenge platform that motivates people to stay consistent on small goals together. Users create time-boxed challenges, track progress via check-ins, and earn points, levels, and badges. Leaderboards and a social feed add friendly competition.

## Tech Stack

- **Frontend**: [Streamlit](https://streamlit.io)
- **Database**: [TiDB Cloud Serverless](https://tidbcloud.com) (MySQL-compatible distributed SQL)
- **Deployment**: Streamlit Community Cloud

## Getting Started

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure TiDB credentials

Create `.streamlit/secrets.toml` (never commit this file):

```toml
[tidb]
host     = "<your-tidb-host>"
port     = 4000
user     = "<your-user>"
password = "<your-password>"
database = "groupquest"
ssl_ca   = ""   # path to CA cert if required by TiDB Cloud
```

On Streamlit Community Cloud, add the same values under **App Settings → Secrets**.

### 3. Run locally

```bash
streamlit run app.py
```

## Project Management

Issues and user stories are tracked via [GitHub Issues](../../issues) and [GitHub Projects](../../projects). Features are organized as GitHub Milestones by area:

- Authentifizierung & Account
- Challenge-Verwaltung
- Fortschritt & Check-ins
- Community & Sharing
- Gamification
- Personalisierung & UX
- Benachrichtigungen
- Administration
- Technisch / Sicherheit
