import base64
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pymysql
import streamlit as st

from database import (
    init_db, create_user, verify_login, get_user,
    create_session_token, validate_session_token, delete_session_token,
    update_user_profile, update_password,
    update_avatar, delete_user,
    create_reset_token, validate_reset_token, use_reset_token,
    create_challenge, get_challenges, get_challenge, update_challenge,
    delete_challenge, complete_challenge, duplicate_challenge, get_challenge_groups,
    create_checkin, get_checkins, get_recent_checkins,
    get_daily_checkins, get_checkins_per_challenge, get_checkin_totals,
    award_points, get_total_points, get_level_info, get_user_badges,
    check_and_award_badges, get_current_streak, get_points_history,
    BADGE_DEFINITIONS, ACTION_LABELS,
    get_preferences, save_preferences,
    get_active_notifications, dismiss_notification,
    # Community
    create_group, get_groups_for_user, get_all_groups, delete_group,
    join_group, leave_group, is_group_member, get_group_members,
    get_public_challenges, join_challenge_as_participant,
    leave_challenge_participation, is_challenge_participant,
    get_community_feed, toggle_like, get_comments, add_comment, delete_comment,
    get_leaderboard,
    pin_challenge, unpin_challenge, get_pinned_challenges,
    get_community_pinned_challenges, is_pinned,
    get_user_participated_challenge_ids, get_user_group_ids,
    get_user_pinned_challenge_ids,
    create_report,
)

st.set_page_config(page_title="GroupQuest", page_icon="🏆", layout="centered")

init_db()

# --- Personalization constants (must precede session state init) ---
CLIPBOARD_SECTIONS = {
    "metrics": "Kennzahlen",
    "daily_chart": "Aktivität – letzte 30 Tage",
    "per_challenge_chart": "Check-ins pro Herausforderung",
    "recent_feed": "Neueste Check-ins",
}
DEFAULT_PREFS: dict = {
    "theme": "system",
    "accent_color": "#FF4B4B",
    "clipboard_sections": ["metrics", "daily_chart", "per_challenge_chart", "recent_feed"],
    "notify_days_before": [3, 7],
}

# --- Session state defaults ---
if "logged_in" not in st.session_state:
    st.session_state.logged_in = False
if "username" not in st.session_state:
    st.session_state.username = None
if "user_id" not in st.session_state:
    st.session_state.user_id = None
if "current_view" not in st.session_state:
    st.session_state.current_view = "dashboard"
if "challenge_form" not in st.session_state:
    st.session_state.challenge_form = None
if "challenge_edit_id" not in st.session_state:
    st.session_state.challenge_edit_id = None
if "delete_confirm_id" not in st.session_state:
    st.session_state.delete_confirm_id = None
if "preferences" not in st.session_state:
    st.session_state.preferences = DEFAULT_PREFS.copy()

# --- Challenge display constants ---
PRIORITY_LABELS = {"high": "🔴 Hoch", "medium": "🟡 Mittel", "low": "🟢 Niedrig"}
PRIORITY_VALUES = {"🔴 Hoch": "high", "🟡 Mittel": "medium", "🟢 Niedrig": "low"}
STATUS_LABELS = {"active": "▶️ Aktiv", "completed": "✅ Abgeschlossen", "paused": "⏸️ Pausiert"}
STATUS_VALUES = {"▶️ Aktiv": "active", "✅ Abgeschlossen": "completed", "⏸️ Pausiert": "paused"}


# --- E-Mail helper ---
def _send_reset_email(to_address: str, username: str, reset_link: str) -> None:
    """Send password-reset e-mail via SMTP (configured in secrets.toml [email])."""
    cfg = st.secrets["email"]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "GroupQuest – Passwort zurücksetzen"
    msg["From"] = cfg["from_address"]
    msg["To"] = to_address

    text = (
        f"Hallo {username},\n\n"
        f"du hast eine Passwortzurücksetzung für dein GroupQuest-Konto angefordert.\n\n"
        f"Klicke auf folgenden Link, um ein neues Passwort zu setzen (gültig für 1 Stunde):\n"
        f"{reset_link}\n\n"
        f"Falls du diese Anfrage nicht gestellt hast, kannst du diese E-Mail ignorieren.\n\n"
        f"Dein GroupQuest-Team"
    )
    html = (
        f"<p>Hallo <strong>{username}</strong>,</p>"
        f"<p>du hast eine Passwortzurücksetzung für dein GroupQuest-Konto angefordert.</p>"
        f"<p><a href='{reset_link}'>Passwort jetzt zurücksetzen</a> (gültig für 1 Stunde)</p>"
        f"<p>Falls du diese Anfrage nicht gestellt hast, kannst du diese E-Mail ignorieren.</p>"
        f"<p>Dein GroupQuest-Team</p>"
    )
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(cfg["smtp_host"], int(cfg["smtp_port"])) as server:
        server.ehlo()
        server.starttls()
        server.login(cfg["smtp_user"], cfg["smtp_password"])
        server.sendmail(cfg["from_address"], to_address, msg.as_string())


# --- In-app deadline notifications ---

def _notif_label(days_rem: int, title: str, deadline) -> str:
    if days_rem == 0:
        return f"⚠️ Deadline **heute**: {title}"
    if days_rem == 1:
        return f"⏰ Deadline **morgen**: {title}"
    import datetime as _dt
    if isinstance(deadline, _dt.datetime):
        date_str = deadline.strftime("%d.%m.%Y %H:%M")
    elif hasattr(deadline, "strftime"):
        date_str = deadline.strftime("%d.%m.%Y")
    else:
        date_str = str(deadline)
    return f"📅 {title} – Deadline in **{days_rem} Tagen** ({date_str})"


def _load_notifications_cache() -> list:
    """
    Fetch active notifications once per render and cache in session_state.
    Calling this multiple times in the same Streamlit rerun returns the cached list.
    The cache is invalidated by setting st.session_state._notif_cache = None
    (done automatically on dismiss via st.rerun()).
    """
    if st.session_state.get("_notif_cache") is None:
        prefs = st.session_state.get("preferences", DEFAULT_PREFS)
        notify_days = prefs.get("notify_days_before", [3, 7])
        if not notify_days:
            st.session_state._notif_cache = []
        else:
            st.session_state._notif_cache = get_active_notifications(
                st.session_state.user_id, notify_days
            )
    return st.session_state._notif_cache


def show_deadline_alerts() -> None:
    """Render dismissable in-app deadline banners on the dashboard."""
    notifications = _load_notifications_cache()
    for n in notifications:
        days_rem = n["days_remaining"]
        col_msg, col_btn = st.columns([5, 1])
        with col_msg:
            if days_rem == 0:
                st.error(_notif_label(days_rem, n["title"], n["deadline"]))
            else:
                st.warning(_notif_label(days_rem, n["title"], n["deadline"]))
        with col_btn:
            st.write("")
            if st.button("✕", key=f"dismiss_{n['id']}_{n['threshold']}", help="Mitteilung schließen"):
                dismiss_notification(st.session_state.user_id, n["id"], n["threshold"])
                st.session_state._notif_cache = None
                st.rerun()


def count_active_notifications() -> int:
    """Return the number of current unread deadline notifications (uses render cache)."""
    return len(_load_notifications_cache())


# --- CSS injection for theming, responsiveness and accessibility ---

def inject_css() -> None:
    """Inject user-preference CSS: accent color, theme override, responsive layout, a11y."""
    prefs = st.session_state.get("preferences", DEFAULT_PREFS)
    accent = prefs.get("accent_color", "#FF4B4B")
    theme = prefs.get("theme", "system")

    if theme == "dark":
        theme_css = f"""
        [data-testid="stAppViewContainer"] {{background-color:#0E1117;}}
        [data-testid="stSidebar"] {{background-color:#262730;}}
        [data-testid="stHeader"] {{background-color:#0E1117;}}
        .stMarkdown p, .stMarkdown li, .stMarkdown h1, .stMarkdown h2, .stMarkdown h3,
        label, .stCaption, .stText {{color:#FAFAFA !important;}}
        [data-baseweb="input"] input, [data-baseweb="textarea"] textarea,
        [data-baseweb="select"] div {{background-color:#31333F; color:#FAFAFA;}}
        """
    elif theme == "light":
        theme_css = f"""
        [data-testid="stAppViewContainer"] {{background-color:#FFFFFF;}}
        [data-testid="stSidebar"] {{background-color:#F0F2F6;}}
        [data-testid="stHeader"] {{background-color:#FFFFFF;}}
        .stMarkdown p, .stMarkdown li, label {{color:#31333F !important;}}
        """
    else:
        theme_css = ""

    css = f"""
    <style>
    /* === Akzentfarbe === */
    .stButton > button[kind="primary"] {{
        background-color: {accent} !important;
        border-color: {accent} !important;
    }}
    .stButton > button[kind="primary"]:hover {{
        opacity: 0.85;
    }}
    a {{ color: {accent} !important; }}
    [data-testid="stProgressBar"] > div > div > div > div {{
        background-color: {accent} !important;
    }}

    /* === Barrierefreiheit: Fokus-Indikator === */
    :focus-visible {{
        outline: 3px solid {accent} !important;
        outline-offset: 2px !important;
        border-radius: 4px;
    }}

    /* === Responsivität: Spalten auf Mobilgeräten stapeln === */
    @media (max-width: 640px) {{
        [data-testid="column"] {{
            width: 100% !important;
            flex: 1 1 100% !important;
            min-width: 100% !important;
        }}
        /* Größere Touch-Ziele für Buttons */
        .stButton > button {{
            min-height: 48px !important;
            font-size: 16px !important;
        }}
        /* Breite Eingabefelder */
        [data-baseweb="input"], [data-baseweb="textarea"] {{
            width: 100% !important;
        }}
    }}

    /* === Theme === */
    {theme_css}
    </style>
    """
    st.markdown(css, unsafe_allow_html=True)


# --- Settings page ---

def show_settings_page() -> None:
    st.title("Einstellungen")
    if st.button("← Zurück"):
        st.session_state.current_view = "dashboard"
        st.rerun()

    prefs = st.session_state.get("preferences", DEFAULT_PREFS.copy())

    st.subheader("Erscheinungsbild")

    theme_keys = ["system", "light", "dark"]
    theme_labels = ["System (automatisch)", "Hell", "Dunkel"]
    current_theme = prefs.get("theme", "system")
    theme_idx = theme_keys.index(current_theme) if current_theme in theme_keys else 0
    selected_theme = st.selectbox(
        "Farbschema",
        theme_labels,
        index=theme_idx,
        help="'System' übernimmt die Einstellung deines Betriebssystems.",
    )
    new_theme = theme_keys[theme_labels.index(selected_theme)]

    new_accent = st.color_picker(
        "Akzentfarbe",
        value=prefs.get("accent_color", "#FF4B4B"),
        help="Wird für Buttons, Links und Fokusrahmen verwendet.",
    )

    st.divider()
    st.subheader("Clipboard anpassen")
    st.write("Wähle, welche Bereiche im Clipboard angezeigt werden:")
    current_sections = prefs.get("clipboard_sections", list(CLIPBOARD_SECTIONS.keys()))
    new_sections = []
    for key, label in CLIPBOARD_SECTIONS.items():
        if st.checkbox(label, value=key in current_sections, key=f"pref_section_{key}"):
            new_sections.append(key)

    st.divider()
    st.subheader("Mitteilungen")
    st.caption(
        "Mitteilungen werden beim Laden der Seite geprüft und erscheinen im Dashboard und in der Glocke."
    )
    NOTIFY_OPTIONS = [1, 2, 3, 5, 7, 14]
    current_days = prefs.get("notify_days_before", [3, 7])
    st.write("Erinnere mich … Tage vor Fälligkeit:")
    new_notify_days = []
    cols = st.columns(len(NOTIFY_OPTIONS))
    for i, d in enumerate(NOTIFY_OPTIONS):
        label = f"{d} Tag" if d == 1 else f"{d} Tage"
        with cols[i]:
            if st.checkbox(label, value=d in current_days, key=f"pref_days_{d}"):
                new_notify_days.append(d)

    st.divider()
    if st.button("Einstellungen speichern", use_container_width=True, type="primary"):
        if not new_sections:
            st.warning("Bitte mindestens einen Clipboard-Bereich aktivieren.")
        else:
            new_prefs = {
                "theme": new_theme,
                "accent_color": new_accent,
                "clipboard_sections": new_sections,
                "notify_days_before": new_notify_days,
            }
            save_preferences(st.session_state.user_id, new_prefs)
            st.session_state.preferences = new_prefs
            st.success("Einstellungen gespeichert.")
            st.rerun()


# --- Password reset page (token from URL) ---
def show_reset_password_page(token: str):
    st.title("GroupQuest 🏆")
    st.subheader("Neues Passwort setzen")

    user_id = validate_reset_token(token)
    if not user_id:
        st.error("Dieser Link ist ungültig oder abgelaufen. Bitte fordere einen neuen Reset-Link an.")
        if st.button("Zurück zum Login"):
            st.query_params.clear()
            st.rerun()
        return

    with st.form("set_new_password_form"):
        new_pw = st.text_input("Neues Passwort", type="password")
        new_pw2 = st.text_input("Neues Passwort bestätigen", type="password")
        submitted = st.form_submit_button("Passwort speichern", use_container_width=True)

    if submitted:
        if not new_pw or not new_pw2:
            st.error("Bitte beide Felder ausfüllen.")
        elif new_pw != new_pw2:
            st.error("Passwörter stimmen nicht überein.")
        elif len(new_pw) < 8:
            st.error("Passwort muss mindestens 8 Zeichen lang sein.")
        else:
            ok = use_reset_token(token, new_pw)
            if ok:
                user = get_user(user_id)
                st.session_state.logged_in = True
                st.session_state.username = user["username"]
                st.session_state.user_id = user["id"]
                _token = create_session_token(user["id"])
                st.session_state["_session_token"] = _token
                st.query_params.clear()
                st.rerun()
            else:
                st.error("Der Link ist leider abgelaufen. Bitte fordere einen neuen an.")


# --- Auth page ---
def show_auth_page():
    st.title("GroupQuest 🏆")

    tab_login, tab_register = st.tabs(["Einloggen", "Account erstellen"])

    with tab_login:
        with st.form("login_form"):
            username = st.text_input("Benutzername oder E-Mail")
            password = st.text_input("Passwort", type="password")
            submitted = st.form_submit_button("Einloggen", use_container_width=True)

        if submitted:
            if not username or not password:
                st.error("Bitte alle Felder ausfüllen.")
            else:
                user = verify_login(username, password)
                if user:
                    st.session_state.logged_in = True
                    st.session_state.username = user["username"]
                    st.session_state.user_id = user["id"]
                    _token = create_session_token(user["id"])
                    st.session_state["_session_token"] = _token
                    st.rerun()
                else:
                    st.error("Ungültiger Benutzername oder falsches Passwort.")

        with st.expander("Passwort vergessen?"):
            st.write("Gib deine E-Mail-Adresse ein. Du erhältst einen Link zum Zurücksetzen.")
            with st.form("forgot_password_form"):
                reset_email = st.text_input("E-Mail-Adresse")
                submitted_reset = st.form_submit_button("Reset-Link senden", use_container_width=True)

            if submitted_reset:
                if not reset_email:
                    st.error("Bitte E-Mail-Adresse eingeben.")
                else:
                    result = create_reset_token(reset_email)
                    # Always show success to avoid user enumeration
                    if result:
                        token, uname = result
                        app_url = st.secrets.get("email", {}).get("app_url", "")
                        reset_link = f"{app_url}?reset_token={token}"
                        is_local = "localhost" in app_url or "127.0.0.1" in app_url
                        if is_local:
                            st.info(f"**Lokaler Testmodus** – kein E-Mail-Versand.\n\n**Reset-Link:**\n{reset_link}")
                            st.stop()
                        try:
                            _send_reset_email(reset_email, uname, reset_link)
                        except Exception as e:
                            st.error(f"E-Mail konnte nicht gesendet werden: {e}")
                            st.stop()
                    st.success("Falls ein Konto mit dieser E-Mail existiert, wurde ein Reset-Link gesendet.")

    with tab_register:
        with st.form("register_form"):
            new_username = st.text_input("Benutzername")
            new_email = st.text_input("E-Mail")
            new_password = st.text_input("Passwort", type="password")
            confirm_password = st.text_input("Passwort bestätigen", type="password")
            submitted = st.form_submit_button("Account erstellen", use_container_width=True)

        if submitted:
            if not all([new_username, new_email, new_password, confirm_password]):
                st.error("Bitte alle Felder ausfüllen.")
            elif new_password != confirm_password:
                st.error("Passwörter stimmen nicht überein.")
            elif len(new_password) < 8:
                st.error("Passwort muss mindestens 8 Zeichen lang sein.")
            else:
                try:
                    create_user(new_username, new_email, new_password)
                    user = verify_login(new_username, new_password)
                    st.session_state.logged_in = True
                    st.session_state.username = user["username"]
                    st.session_state.user_id = user["id"]
                    _token = create_session_token(user["id"])
                    st.session_state["_session_token"] = _token
                    st.rerun()
                except pymysql.IntegrityError:
                    st.error("Benutzername oder E-Mail ist bereits vergeben.")


# --- Profile page ---
def show_profile_page():
    st.title("Mein Profil")
    if st.button("Zurück zum Dashboard"):
        st.session_state.current_view = "dashboard"
        st.rerun()

    user = get_user(st.session_state.user_id)
    if not user:
        st.error("Profil konnte nicht geladen werden.")
        return

    col_avatar, col_info = st.columns([1, 3])
    with col_avatar:
        if user.get("avatar"):
            img_bytes = base64.b64decode(user["avatar"])
            st.image(img_bytes, width=100)
        else:
            st.markdown("### 👤")
    with col_info:
        st.markdown(f"**{user.get('display_name') or user['username']}**")
        st.caption(f"@{user['username']} · {user['email']}")
        if user.get("bio"):
            st.write(user["bio"])

    st.divider()

    tab_profile, tab_avatar, tab_password, tab_delete = st.tabs([
        "Profil bearbeiten", "Profilbild", "Passwort ändern", "Account löschen"
    ])

    # --- Tab: Profil bearbeiten ---
    with tab_profile:
        with st.form("edit_profile_form"):
            new_username = st.text_input("Benutzername", value=user["username"])
            new_email = st.text_input("E-Mail", value=user["email"])
            new_display = st.text_input("Anzeigename", value=user.get("display_name") or "")
            new_bio = st.text_area("Bio (max. 500 Zeichen)", value=user.get("bio") or "", max_chars=500)
            submitted = st.form_submit_button("Speichern", use_container_width=True)

        if submitted:
            if not new_username or not new_email:
                st.error("Benutzername und E-Mail dürfen nicht leer sein.")
            else:
                try:
                    update_user_profile(st.session_state.user_id, new_username, new_email, new_display, new_bio)
                    st.session_state.username = new_username
                    st.success("Profil erfolgreich gespeichert.")
                    st.rerun()
                except pymysql.IntegrityError:
                    st.error("Benutzername oder E-Mail ist bereits vergeben.")

    # --- Tab: Profilbild ---
    with tab_avatar:
        st.write("Lade ein Bild hoch (JPG oder PNG, max. 2 MB).")
        uploaded = st.file_uploader("Profilbild auswählen", type=["jpg", "jpeg", "png"])
        if uploaded:
            if uploaded.size > 2 * 1024 * 1024:
                st.error("Das Bild darf maximal 2 MB groß sein.")
            else:
                st.image(uploaded, width=150, caption="Vorschau")
                if st.button("Profilbild speichern"):
                    update_avatar(st.session_state.user_id, uploaded.read())
                    st.success("Profilbild gespeichert.")
                    st.rerun()

        if user.get("avatar"):
            if st.button("Profilbild entfernen"):
                update_avatar(st.session_state.user_id, b"")
                st.success("Profilbild entfernt.")
                st.rerun()

    # --- Tab: Passwort ändern ---
    with tab_password:
        with st.form("change_pw_form"):
            current_pw = st.text_input("Aktuelles Passwort", type="password")
            new_pw = st.text_input("Neues Passwort", type="password")
            new_pw2 = st.text_input("Neues Passwort bestätigen", type="password")
            submitted = st.form_submit_button("Passwort ändern", use_container_width=True)

        if submitted:
            if not all([current_pw, new_pw, new_pw2]):
                st.error("Bitte alle Felder ausfüllen.")
            elif new_pw != new_pw2:
                st.error("Neue Passwörter stimmen nicht überein.")
            elif len(new_pw) < 8:
                st.error("Passwort muss mindestens 8 Zeichen lang sein.")
            else:
                ok = update_password(st.session_state.user_id, current_pw, new_pw)
                if ok:
                    st.success("Passwort erfolgreich geändert.")
                else:
                    st.error("Das aktuelle Passwort ist falsch.")

    # --- Tab: Account löschen ---
    with tab_delete:
        st.warning("Das Löschen deines Accounts ist dauerhaft und kann nicht rückgängig gemacht werden.")
        with st.form("delete_account_form"):
            confirm_pw = st.text_input("Passwort zur Bestätigung", type="password")
            confirm_check = st.checkbox("Ich möchte meinen Account dauerhaft löschen.")
            submitted = st.form_submit_button("Account löschen", use_container_width=True, type="primary")

        if submitted:
            if not confirm_pw:
                st.error("Bitte Passwort eingeben.")
            elif not confirm_check:
                st.error("Bitte die Checkbox bestätigen.")
            else:
                user_check = verify_login(user["username"], confirm_pw)
                if not user_check:
                    st.error("Falsches Passwort.")
                else:
                    delete_user(st.session_state.user_id)
                    st.session_state.logged_in = False
                    st.session_state.username = None
                    st.session_state.user_id = None
                    st.session_state.current_view = "dashboard"
                    st.success("Account wurde gelöscht.")
                    st.rerun()


# --- Challenges ---

def show_challenges_page():
    if st.session_state.challenge_form in ("create", "edit"):
        _show_challenge_form()
        return
    if st.session_state.challenge_form == "checkin":
        _show_checkin_form()
        return

    col_title, col_btn = st.columns([3, 1])
    with col_title:
        st.title("Meine Herausforderungen")
    with col_btn:
        st.write("")
        if st.button("+ Neue anlegen", use_container_width=True):
            st.session_state.challenge_form = "create"
            st.session_state.challenge_edit_id = None
            st.session_state.delete_confirm_id = None
            st.rerun()

    search = st.text_input("Suchen...", placeholder="Titel oder Beschreibung", key="ch_search")

    col_f1, col_f2, col_f3 = st.columns(3)
    with col_f1:
        filter_status = st.selectbox(
            "Status", ["Alle", "▶️ Aktiv", "✅ Abgeschlossen", "⏸️ Pausiert"], key="ch_filter_status"
        )
    with col_f2:
        filter_priority = st.selectbox(
            "Priorität", ["Alle", "🔴 Hoch", "🟡 Mittel", "🟢 Niedrig"], key="ch_filter_priority"
        )
    with col_f3:
        groups = get_challenge_groups(st.session_state.user_id)
        filter_group = st.selectbox("Gruppe", ["Alle"] + groups, key="ch_filter_group")

    status_param = STATUS_VALUES.get(filter_status)
    priority_param = PRIORITY_VALUES.get(filter_priority)
    group_param = filter_group if filter_group != "Alle" else None
    search_param = search.strip() if search.strip() else None

    challenges = get_challenges(
        st.session_state.user_id,
        status=status_param,
        priority=priority_param,
        group_name=group_param,
        search=search_param,
    )

    if not challenges:
        st.info("Keine Herausforderungen gefunden.")
        return

    st.caption(f"{len(challenges)} Herausforderung(en)")

    for ch in challenges:
        _render_challenge_card(ch)


def _render_challenge_card(ch):
    priority_label = PRIORITY_LABELS.get(ch["priority"], ch["priority"])
    status_label = STATUS_LABELS.get(ch["status"], ch["status"])

    checkin_count = ch.get("checkin_count", 0)
    parts = [f"{priority_label} **{ch['title']}** — {status_label}"]
    if ch.get("group_name"):
        parts.append(f"📁 {ch['group_name']}")
    if ch.get("deadline"):
        import datetime as _dt
        dl = ch["deadline"]
        if isinstance(dl, _dt.datetime):
            parts.append(f"📅 {dl.strftime('%d.%m.%Y %H:%M')}")
        else:
            parts.append(f"📅 {dl}")
    if checkin_count:
        parts.append(f"📝 {checkin_count}")
    header = " · ".join(parts)

    with st.expander(header):
        if ch.get("description"):
            st.write(ch["description"])
        if ch.get("rules"):
            st.markdown(f"**Regeln:** {ch['rules']}")

        created = ch["created_at"].strftime("%d.%m.%Y") if ch.get("created_at") else "—"
        st.caption(f"Erstellt: {created}")
        if ch.get("completed_at"):
            st.caption(f"Abgeschlossen: {ch['completed_at'].strftime('%d.%m.%Y')}")

        if st.button("➕ Check-in hinzufügen", key=f"ci_{ch['id']}", use_container_width=True):
            st.session_state.challenge_form = "checkin"
            st.session_state.challenge_edit_id = ch["id"]
            st.rerun()

        is_confirming_delete = st.session_state.delete_confirm_id == ch["id"]
        if is_confirming_delete:
            st.warning("Herausforderung wirklich löschen?")
            col1, col2 = st.columns(2)
            with col1:
                if st.button("Ja, löschen", key=f"del_yes_{ch['id']}", use_container_width=True, type="primary"):
                    delete_challenge(ch["id"], st.session_state.user_id)
                    st.session_state.delete_confirm_id = None
                    st.rerun()
            with col2:
                if st.button("Abbrechen", key=f"del_no_{ch['id']}", use_container_width=True):
                    st.session_state.delete_confirm_id = None
                    st.rerun()
        else:
            num_cols = 4 if ch["status"] != "completed" else 3
            cols = st.columns(num_cols)
            col_idx = 0

            with cols[col_idx]:
                if st.button("✏️ Bearbeiten", key=f"edit_{ch['id']}", use_container_width=True):
                    st.session_state.challenge_form = "edit"
                    st.session_state.challenge_edit_id = ch["id"]
                    st.rerun()
            col_idx += 1

            if ch["status"] != "completed":
                with cols[col_idx]:
                    if st.button("✅ Abschließen", key=f"complete_{ch['id']}", use_container_width=True):
                        complete_challenge(ch["id"], st.session_state.user_id)
                        award_points(st.session_state.user_id, "challenge_complete", 50, ch["id"])
                        new_badges = check_and_award_badges(st.session_state.user_id)
                        if new_badges:
                            for bk in new_badges:
                                emoji, name, _ = BADGE_DEFINITIONS[bk]
                                st.success(f"Neues Badge verdient: {emoji} **{name}**!")
                        st.rerun()
                col_idx += 1

            with cols[col_idx]:
                if st.button("📋 Duplizieren", key=f"dup_{ch['id']}", use_container_width=True):
                    duplicate_challenge(ch["id"], st.session_state.user_id)
                    st.rerun()
            col_idx += 1

            with cols[col_idx]:
                if st.button("🗑️ Löschen", key=f"del_{ch['id']}", use_container_width=True):
                    st.session_state.delete_confirm_id = ch["id"]
                    st.rerun()


def _show_challenge_form():
    is_edit = st.session_state.challenge_form == "edit"
    ch = get_challenge(st.session_state.challenge_edit_id) if is_edit else None

    st.title("Herausforderung bearbeiten" if is_edit else "Neue Herausforderung anlegen")
    if st.button("← Zurück zur Liste"):
        st.session_state.challenge_form = None
        st.rerun()

    groups = get_challenge_groups(st.session_state.user_id)

    with st.form("ch_form"):
        title = st.text_input("Titel *", value=ch["title"] if ch else "")
        description = st.text_area("Beschreibung", value=ch.get("description") or "" if ch else "")
        rules = st.text_area("Regeln", value=ch.get("rules") or "" if ch else "")

        col1, col2 = st.columns(2)
        with col1:
            priority_options = ["🔴 Hoch", "🟡 Mittel", "🟢 Niedrig"]
            cur_priority = PRIORITY_LABELS.get(ch["priority"], "🟡 Mittel") if ch else "🟡 Mittel"
            priority_idx = priority_options.index(cur_priority) if cur_priority in priority_options else 1
            priority_sel = st.selectbox("Priorität", priority_options, index=priority_idx)

        with col2:
            if is_edit:
                status_options = ["▶️ Aktiv", "⏸️ Pausiert", "✅ Abgeschlossen"]
                cur_status = STATUS_LABELS.get(ch["status"], "▶️ Aktiv") if ch else "▶️ Aktiv"
                status_idx = status_options.index(cur_status) if cur_status in status_options else 0
                status_sel = st.selectbox("Status", status_options, index=status_idx)
            else:
                status_sel = "▶️ Aktiv"
                st.empty()

        group_input = st.text_input(
            "Gruppe",
            value=ch.get("group_name") or "" if ch else "",
            placeholder="z.B. Fitness, Lernen (leer lassen = keine Gruppe)",
        )
        if groups:
            st.caption("Bestehende Gruppen: " + ", ".join(groups))

        has_deadline = st.checkbox(
            "Zieldatum festlegen",
            value=bool(ch.get("deadline") if ch else False),
        )
        deadline_val = ch.get("deadline") if ch else None
        import datetime as _dt
        deadline_date_val = deadline_val.date() if isinstance(deadline_val, _dt.datetime) else deadline_val
        deadline_time_val = deadline_val.time() if isinstance(deadline_val, _dt.datetime) else _dt.time(23, 59)
        dcol1, dcol2 = st.columns(2)
        with dcol1:
            deadline_date = st.date_input(
                "Zieldatum",
                value=deadline_date_val,
                format="DD.MM.YYYY",
            )
        with dcol2:
            deadline_time = st.time_input(
                "Uhrzeit",
                value=deadline_time_val,
            )

        cur_visibility = ch.get("visibility", "private") if ch else "private"
        visibility_options = ["🔒 Privat", "🌍 Öffentlich"]
        visibility_idx = 1 if cur_visibility == "public" else 0
        visibility_sel = st.selectbox(
            "Sichtbarkeit",
            visibility_options,
            index=visibility_idx,
            help="Öffentliche Herausforderungen erscheinen im Community-Feed und können von anderen betreten werden.",
        )

        submitted = st.form_submit_button("Speichern", use_container_width=True)

    if submitted:
        if not title.strip():
            st.error("Titel ist erforderlich.")
        else:
            priority_val = PRIORITY_VALUES.get(priority_sel, "medium")
            status_val = STATUS_VALUES.get(status_sel, "active")
            final_deadline = _dt.datetime.combine(deadline_date, deadline_time) if has_deadline else None
            visibility_val = "public" if visibility_sel == "🌍 Öffentlich" else "private"

            if is_edit:
                update_challenge(
                    ch["id"], st.session_state.user_id,
                    title.strip(), description.strip(), rules.strip(),
                    priority_val, group_input.strip() or None, final_deadline, status_val,
                    visibility_val,
                )
            else:
                create_challenge(
                    st.session_state.user_id,
                    title.strip(), description.strip(), rules.strip(),
                    priority_val, group_input.strip() or None, final_deadline,
                    visibility_val,
                )

            st.session_state.challenge_form = None
            st.rerun()


# --- Check-in form ---

def _show_checkin_form():
    ch = get_challenge(st.session_state.challenge_edit_id)
    if not ch:
        st.session_state.challenge_form = None
        st.rerun()

    st.title(f"Check-in: {ch['title']}")
    if st.button("← Zurück zur Liste"):
        st.session_state.challenge_form = None
        st.rerun()

    with st.form("checkin_form"):
        text = st.text_area("Was hast du erreicht?", placeholder="Beschreibe deinen Fortschritt...")
        photo = st.file_uploader("Foto hinzufügen (optional, max. 5 MB)", type=["jpg", "jpeg", "png"])
        submitted = st.form_submit_button("Check-in speichern", use_container_width=True)

    if submitted:
        if not text.strip() and not photo:
            st.error("Bitte mindestens Text oder ein Foto hinzufügen.")
        elif photo and photo.size > 5 * 1024 * 1024:
            st.error("Foto darf maximal 5 MB groß sein.")
        else:
            photo_bytes = photo.read() if photo else None
            create_checkin(ch["id"], st.session_state.user_id, text.strip(), photo_bytes)

            # Award points
            user_id = st.session_state.user_id
            if photo_bytes:
                award_points(user_id, "checkin_photo", 15, ch["id"])
            else:
                award_points(user_id, "checkin", 10, ch["id"])

            # Check streak bonus
            streak = get_current_streak(user_id)
            if streak > 0 and streak % 7 == 0:
                award_points(user_id, "streak_7", 20)

            # Check and award new badges
            new_badges = check_and_award_badges(user_id)
            if new_badges:
                for bk in new_badges:
                    emoji, name, _ = BADGE_DEFINITIONS[bk]
                    st.success(f"Neues Badge verdient: {emoji} **{name}**!")

            st.session_state.challenge_form = None
            st.rerun()

    checkins = get_checkins(ch["id"])
    if checkins:
        st.divider()
        st.subheader(f"Bisherige Check-ins ({len(checkins)})")
        for ci in checkins:
            st.caption(ci["created_at"].strftime("%d.%m.%Y %H:%M"))
            if ci.get("text"):
                st.write(ci["text"])
            if ci.get("photo"):
                st.image(base64.b64decode(ci["photo"]), width=400, caption="Check-in Foto")
            st.divider()


# --- Clipboard ---

def show_clipboard_page():
    import pandas as pd
    from datetime import date, timedelta

    st.title("Clipboard")

    prefs = st.session_state.get("preferences", DEFAULT_PREFS)
    sections = prefs.get("clipboard_sections", list(CLIPBOARD_SECTIONS.keys()))
    user_id = st.session_state.user_id

    if "metrics" in sections:
        totals = get_checkin_totals(user_id)
        col1, col2, col3 = st.columns(3)
        col1.metric("Check-ins gesamt", totals["total_checkins"])
        col2.metric("Aktive Herausforderungen", totals["active_challenges"])
        col3.metric("Abgeschlossen", totals["completed_challenges"])
        st.divider()

    if "daily_chart" in sections:
        st.subheader("Aktivität – letzte 30 Tage")
        daily = get_daily_checkins(user_id, 30)
        today = date.today()
        date_range = {(today - timedelta(days=i)).isoformat(): 0 for i in range(29, -1, -1)}
        for row in daily:
            d = str(row["date"])
            if d in date_range:
                date_range[d] = int(row["count"])
        df_daily = pd.DataFrame({"Datum": list(date_range.keys()), "Check-ins": list(date_range.values())})
        df_daily = df_daily.set_index("Datum")
        st.line_chart(df_daily)
        st.divider()

    if "per_challenge_chart" in sections:
        st.subheader("Check-ins pro Herausforderung")
        per_ch = get_checkins_per_challenge(user_id)
        if per_ch and any(int(r["count"]) > 0 for r in per_ch):
            df_ch = pd.DataFrame(per_ch).set_index("title").rename(columns={"count": "Check-ins"})
            st.bar_chart(df_ch)
        else:
            st.info("Noch keine Check-ins vorhanden.")
        st.divider()

    if "recent_feed" in sections:
        st.subheader("Neueste Check-ins")
        recent = get_recent_checkins(user_id, limit=10)
        if not recent:
            st.info("Noch keine Check-ins vorhanden.")
        for ci in recent:
            with st.container():
                st.markdown(f"**{ci['challenge_title']}** · {ci['created_at'].strftime('%d.%m.%Y %H:%M')}")
                if ci.get("text"):
                    st.write(ci["text"])
                if ci.get("photo"):
                    st.image(
                        base64.b64decode(ci["photo"]),
                        width=300,
                        caption=f"Foto zum Check-in: {ci['challenge_title']}",
                    )
                st.divider()

    if not sections:
        st.info("Alle Bereiche sind ausgeblendet. Passe das Clipboard unter Einstellungen an.")


# --- Gamification page ---

def show_gamification_page():
    st.title("Gamification")
    user_id = st.session_state.user_id

    points = get_total_points(user_id)
    level = get_level_info(points)
    streak = get_current_streak(user_id)
    badges = get_user_badges(user_id)

    # Level card
    col_lvl, col_pts, col_streak = st.columns(3)
    with col_lvl:
        st.metric("Level", f"{level['emoji']} {level['level']} – {level['name']}")
    with col_pts:
        st.metric("Punkte gesamt", points)
    with col_streak:
        st.metric("Aktuelle Streak", f"{streak} Tag(e)")

    # Progress bar to next level
    if level["next_min"] is not None:
        st.caption(f"Fortschritt zu Level {level['level'] + 1}: {points} / {level['next_min']} Punkte")
        st.progress(level["progress_pct"] / 100)
    else:
        st.success("Du hast das höchste Level erreicht!")

    st.divider()

    # Points guide
    with st.expander("Wie verdiene ich Punkte?"):
        st.markdown("""
| Aktion | Punkte |
|---|---|
| Check-in (Text) | +10 |
| Check-in mit Foto | +15 |
| Challenge abschliessen | +50 |
| 7-Tage-Streak | +20 Bonus |
        """)

    st.divider()

    # Badges
    st.subheader(f"Badges ({len(badges)} / {len(BADGE_DEFINITIONS)} verdient)")
    earned_keys = {b["badge_key"] for b in badges}

    badge_cols = st.columns(4)
    for i, (badge_key, (emoji, name, desc)) in enumerate(BADGE_DEFINITIONS.items()):
        with badge_cols[i % 4]:
            if badge_key in earned_keys:
                st.markdown(f"### {emoji}")
                st.markdown(f"**{name}**")
                st.caption(desc)
            else:
                st.markdown("### 🔒")
                st.markdown(f"~~{name}~~")
                st.caption(desc)

    st.divider()

    # Points history
    st.subheader("Punktehistorie")
    history = get_points_history(user_id, limit=15)
    if not history:
        st.info("Noch keine Punkte verdient.")
    else:
        for entry in history:
            label = ACTION_LABELS.get(entry["action_type"], entry["action_type"])
            ts = entry["created_at"].strftime("%d.%m.%Y %H:%M")
            st.markdown(f"**+{entry['points']} Punkte** — {label} · {ts}")


# --- Community ---

def _render_feed_item(ci, user_id):
    """Render a single community feed check-in with like/comment interaction."""
    display = ci.get("display_name") or ci["username"]
    ts = ci["created_at"].strftime("%d.%m.%Y %H:%M")
    st.markdown(f"**{display}** (@{ci['username']}) · {ci['challenge_title']} · {ts}")
    if ci.get("text"):
        st.write(ci["text"])
    if ci.get("photo"):
        st.image(base64.b64decode(ci["photo"]), width=350)

    col_like, col_comment, col_report = st.columns([1, 1, 1])
    with col_like:
        liked = bool(ci.get("liked_by_me"))
        like_label = f"❤️ {ci['like_count']}" if liked else f"🤍 {ci['like_count']}"
        if st.button(like_label, key=f"like_{ci['id']}_{user_id}"):
            toggle_like(ci["id"], user_id)
            st.rerun()

    with col_comment:
        if st.button(f"💬 {ci['comment_count']}", key=f"cmtbtn_{ci['id']}"):
            key = f"show_comments_{ci['id']}"
            st.session_state[key] = not st.session_state.get(key, False)
            st.rerun()

    with col_report:
        if st.button("🚩 Melden", key=f"report_{ci['id']}"):
            st.session_state[f"report_open_{ci['id']}"] = True

    # Report form
    if st.session_state.get(f"report_open_{ci['id']}"):
        with st.form(key=f"report_form_{ci['id']}"):
            reason = st.text_area("Grund der Meldung")
            submitted = st.form_submit_button("Meldung absenden")
            cancel = st.form_submit_button("Abbrechen")
        if submitted:
            if not reason.strip():
                st.error("Bitte einen Grund angeben.")
            else:
                create_report(user_id, "checkin", ci["id"], reason.strip())
                st.session_state[f"report_open_{ci['id']}"] = False
                st.success("Gemeldet. Danke für dein Feedback.")
                st.rerun()
        if cancel:
            st.session_state[f"report_open_{ci['id']}"] = False
            st.rerun()

    # Comments section
    if st.session_state.get(f"show_comments_{ci['id']}"):
        comments = get_comments(ci["id"])
        for c in comments:
            author = c.get("display_name") or c["username"]
            st.markdown(f"> **{author}**: {c['text']}  \n> *{c['created_at'].strftime('%d.%m.%Y %H:%M')}*")
            # Compare user_id, not username (username can change)
            if c["user_id"] == user_id:
                if st.button("Löschen", key=f"delcmt_{c['id']}"):
                    delete_comment(c["id"], user_id)
                    st.rerun()
        with st.form(key=f"comment_form_{ci['id']}"):
            new_comment = st.text_input("Kommentar schreiben...")
            if st.form_submit_button("Senden") and new_comment.strip():
                add_comment(ci["id"], user_id, new_comment.strip())
                st.rerun()

    st.divider()


def show_community_page():
    st.title("Community")
    user_id = st.session_state.user_id

    tab_feed, tab_challenges, tab_leaderboard, tab_groups, tab_clipboard = st.tabs([
        "Feed", "Herausforderungen", "Leaderboard", "Gruppen", "Community-Clipboard"
    ])

    # --- Tab: Feed ---
    with tab_feed:
        st.subheader("Öffentlicher Feed")
        feed = get_community_feed(user_id, limit=30)
        if not feed:
            st.info("Noch keine öffentlichen Check-ins. Teile deine Herausforderungen, um hier zu erscheinen!")
        for ci in feed:
            _render_feed_item(ci, user_id)

    # --- Tab: Public Challenges ---
    with tab_challenges:
        st.subheader("Öffentliche Herausforderungen")
        search = st.text_input("Suchen...", key="community_ch_search")
        public_chs = get_public_challenges(search.strip() or None)
        # Batch-load participation and pin state — avoids N+1 queries
        participated_ids = get_user_participated_challenge_ids(user_id)
        pinned_comm_ids = get_user_pinned_challenge_ids(user_id, "community")
        if not public_chs:
            st.info("Keine öffentlichen Herausforderungen gefunden.")
        for ch in public_chs:
            is_owner = ch["user_id"] == user_id
            is_participant = ch["id"] in participated_ids
            pinned = ch["id"] in pinned_comm_ids
            header = (
                f"**{ch['title']}** · von @{ch['creator_name']} "
                f"· {ch['participant_count']} Teilnehmer · {ch['checkin_count']} Check-ins"
            )
            with st.expander(header):
                if ch.get("description"):
                    st.write(ch["description"])
                if ch.get("rules"):
                    st.markdown(f"**Regeln:** {ch['rules']}")

                col_join, col_pin = st.columns(2)
                with col_join:
                    if is_owner:
                        st.caption("Das ist deine Challenge.")
                    elif is_participant:
                        if st.button("Austreten", key=f"leave_ch_{ch['id']}", use_container_width=True):
                            leave_challenge_participation(ch["id"], user_id)
                            st.rerun()
                    else:
                        if st.button("Beitreten", key=f"join_ch_{ch['id']}", use_container_width=True, type="primary"):
                            join_challenge_as_participant(ch["id"], user_id)
                            st.rerun()
                with col_pin:
                    pin_label = "📌 Entpinnen" if pinned else "📌 Anpinnen"
                    if st.button(pin_label, key=f"pin_comm_{ch['id']}", use_container_width=True):
                        if pinned:
                            unpin_challenge(user_id, ch["id"], "community")
                        else:
                            pin_challenge(user_id, ch["id"], "community")
                        st.rerun()

    # --- Tab: Leaderboard ---
    with tab_leaderboard:
        st.subheader("Punkteranking")
        board = get_leaderboard(limit=20)
        if not board:
            st.info("Noch keine Daten.")
        for rank, row in enumerate(board, start=1):
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, f"{rank}.")
            display = row.get("display_name") or row["username"]
            is_me = row["id"] == user_id
            line = f"{medal} **{display}** (@{row['username']}) — {row['total_points']} Punkte"
            if is_me:
                st.markdown(f"➡ {line} *(du)*")
            else:
                st.markdown(line)

    # --- Tab: Groups ---
    with tab_groups:
        sub_my, sub_all, sub_create = st.tabs(["Meine Gruppen", "Alle Gruppen", "Gruppe erstellen"])

        with sub_my:
            my_groups = get_groups_for_user(user_id)
            if not my_groups:
                st.info("Du bist noch in keiner Gruppe.")
            for g in my_groups:
                with st.expander(f"**{g['name']}** · {g['member_count']} Mitglieder · Rolle: {g['role']}"):
                    if g.get("description"):
                        st.write(g["description"])
                    members = get_group_members(g["id"])
                    st.markdown("**Mitglieder:**")
                    for m in members:
                        name = m.get("display_name") or m["username"]
                        st.caption(f"{'👑' if m['role'] == 'admin' else '👤'} {name} (@{m['username']})")
                    if g["role"] == "admin":
                        if st.button("Gruppe löschen", key=f"del_g_{g['id']}", use_container_width=True, type="primary"):
                            st.session_state[f"del_group_confirm_{g['id']}"] = True
                        if st.session_state.get(f"del_group_confirm_{g['id']}"):
                            st.warning("Gruppe wirklich löschen? Alle Mitgliedschaften werden entfernt.")
                            col_y, col_n = st.columns(2)
                            with col_y:
                                if st.button("Ja, löschen", key=f"del_g_yes_{g['id']}", use_container_width=True):
                                    delete_group(g["id"], user_id)
                                    st.session_state.pop(f"del_group_confirm_{g['id']}", None)
                                    st.rerun()
                            with col_n:
                                if st.button("Abbrechen", key=f"del_g_no_{g['id']}", use_container_width=True):
                                    st.session_state.pop(f"del_group_confirm_{g['id']}", None)
                                    st.rerun()
                    else:
                        if st.button("Gruppe verlassen", key=f"leave_g_{g['id']}", use_container_width=True):
                            leave_group(g["id"], user_id)
                            st.rerun()

        with sub_all:
            g_search = st.text_input("Gruppe suchen...", key="group_search")
            all_groups_filtered = get_all_groups(g_search.strip() or None)
            # Batch-load membership — avoids N+1 queries
            my_group_ids = get_user_group_ids(user_id)
            if not all_groups_filtered:
                st.info("Keine Gruppen gefunden.")
            for g in all_groups_filtered:
                member = g["id"] in my_group_ids
                with st.expander(f"**{g['name']}** · {g['member_count']} Mitglieder · von @{g['creator_name']}"):
                    if g.get("description"):
                        st.write(g["description"])
                    if member:
                        st.success("Du bist Mitglied.")
                        if st.button("Verlassen", key=f"leave_all_g_{g['id']}", use_container_width=True):
                            leave_group(g["id"], user_id)
                            st.rerun()
                    else:
                        if st.button("Beitreten", key=f"join_g_{g['id']}", use_container_width=True, type="primary"):
                            join_group(g["id"], user_id)
                            st.rerun()

        with sub_create:
            with st.form("create_group_form"):
                g_name = st.text_input("Gruppenname *")
                g_desc = st.text_area("Beschreibung (optional)")
                if st.form_submit_button("Gruppe erstellen", use_container_width=True, type="primary"):
                    if not g_name.strip():
                        st.error("Name ist erforderlich.")
                    else:
                        create_group(g_name.strip(), g_desc.strip(), user_id)
                        st.success(f"Gruppe '{g_name.strip()}' erstellt!")
                        st.rerun()

    # --- Tab: Community-Clipboard ---
    with tab_clipboard:
        st.subheader("Angepinnte Community-Challenges")
        comm_pinned = get_community_pinned_challenges()
        pinned_comm_ids_clip = get_user_pinned_challenge_ids(user_id, "community")
        if not comm_pinned:
            st.info("Noch keine Challenges angepinnt. Pinne öffentliche Challenges im Tab 'Herausforderungen'.")
        for ch in comm_pinned:
            col_info, col_action = st.columns([4, 1])
            with col_info:
                st.markdown(f"**{ch['title']}** · @{ch['creator_name']} · {ch['pin_count']}x angepinnt")
                if ch.get("description"):
                    st.caption(ch["description"])
            with col_action:
                pinned = ch["id"] in pinned_comm_ids_clip
                if pinned:
                    if st.button("Entpinnen", key=f"unpin_tab_{ch['id']}", use_container_width=True):
                        unpin_challenge(user_id, ch["id"], "community")
                        st.rerun()
                else:
                    if st.button("Anpinnen", key=f"pin_tab_{ch['id']}", use_container_width=True):
                        pin_challenge(user_id, ch["id"], "community")
                        st.rerun()
            st.divider()


# --- Notifications page ---

def show_notifications_page() -> None:
    st.title("Mitteilungen")
    if st.button("← Zurück"):
        st.session_state.current_view = "dashboard"
        st.rerun()

    prefs = st.session_state.get("preferences", DEFAULT_PREFS)
    notify_days = prefs.get("notify_days_before", [3, 7])

    if not notify_days:
        st.info("Mitteilungen sind deaktiviert. Aktiviere sie unter Einstellungen → Mitteilungen.")
        return

    notifications = get_active_notifications(st.session_state.user_id, notify_days)

    if not notifications:
        st.success("Keine offenen Mitteilungen – alles im grünen Bereich!")
        return

    st.caption(f"{len(notifications)} offene Mitteilung(en)")

    for n in notifications:
        days_rem = n["days_remaining"]
        col_msg, col_btn = st.columns([5, 1])
        with col_msg:
            if days_rem == 0:
                st.error(_notif_label(days_rem, n["title"], n["deadline"]))
            else:
                st.warning(_notif_label(days_rem, n["title"], n["deadline"]))
        with col_btn:
            st.write("")
            if st.button("✕", key=f"notif_page_dismiss_{n['id']}_{n['threshold']}", help="Als gelesen markieren"):
                dismiss_notification(st.session_state.user_id, n["id"], n["threshold"])
                st.rerun()

    st.divider()
    if st.button("Alle als gelesen markieren", use_container_width=True):
        for n in notifications:
            dismiss_notification(st.session_state.user_id, n["id"], n["threshold"])
        st.rerun()


# --- Main app ---
def show_main_page():
    # Load preferences from DB exactly once per session
    if not st.session_state.get("_prefs_loaded"):
        stored = get_preferences(st.session_state.user_id)
        if stored:
            st.session_state.preferences = {**DEFAULT_PREFS, **stored}
        st.session_state._prefs_loaded = True

    inject_css()

    with st.sidebar:
        user = get_user(st.session_state.user_id)
        if user and user.get("avatar"):
            img_bytes = base64.b64decode(user["avatar"])
            st.image(img_bytes, width=60, caption="Profilbild")
        else:
            st.markdown("### 👤")
        st.write(f"Eingeloggt als **{st.session_state.username}**")

        # Level & points mini-display
        _pts = get_total_points(st.session_state.user_id)
        _lvl = get_level_info(_pts)
        st.caption(f"{_lvl['emoji']} Level {_lvl['level']} · {_pts} Punkte")

        # Notification bell with live count
        _n = count_active_notifications()
        _bell = f"🔔 Mitteilungen ({_n})" if _n else "🔔 Mitteilungen"

        st.divider()
        if st.button("🏠 Dashboard", use_container_width=True):
            st.session_state.current_view = "dashboard"
            st.session_state.challenge_form = None
            st.rerun()
        if st.button("🎯 Herausforderungen", use_container_width=True):
            st.session_state.current_view = "challenges"
            st.session_state.challenge_form = None
            st.rerun()
        if st.button("📋 Clipboard", use_container_width=True):
            st.session_state.current_view = "clipboard"
            st.rerun()
        if st.button("🏆 Gamification", use_container_width=True):
            st.session_state.current_view = "gamification"
            st.rerun()
        if st.button("🌍 Community", use_container_width=True):
            st.session_state.current_view = "community"
            st.rerun()
        if st.button(_bell, use_container_width=True):
            st.session_state.current_view = "notifications"
            st.rerun()
        st.divider()
        if st.button("👤 Mein Profil", use_container_width=True):
            st.session_state.current_view = "profile"
            st.rerun()
        if st.button("⚙️ Einstellungen", use_container_width=True):
            st.session_state.current_view = "settings"
            st.rerun()
        if st.button("Ausloggen", use_container_width=True):
            _t = st.session_state.pop("_session_token", None)
            if _t:
                delete_session_token(_t)
            st.session_state.logged_in = False
            st.session_state.username = None
            st.session_state.user_id = None
            st.session_state.current_view = "dashboard"
            st.session_state.preferences = DEFAULT_PREFS.copy()
            st.session_state._prefs_loaded = False
            st.rerun()

    if st.session_state.current_view == "profile":
        show_profile_page()
    elif st.session_state.current_view == "challenges":
        show_challenges_page()
    elif st.session_state.current_view == "clipboard":
        show_clipboard_page()
    elif st.session_state.current_view == "gamification":
        show_gamification_page()
    elif st.session_state.current_view == "settings":
        show_settings_page()
    elif st.session_state.current_view == "community":
        show_community_page()
    elif st.session_state.current_view == "notifications":
        show_notifications_page()
    else:
        st.title(f"Willkommen, {st.session_state.username}! 👋")
        show_deadline_alerts()
        st.info("Dashboard folgt in den nächsten Sprints.")


# --- Router ---
reset_token = st.query_params.get("reset_token")
if reset_token:
    show_reset_password_page(reset_token)
elif st.session_state.logged_in:
    show_main_page()
else:
    show_auth_page()
